#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NOTAM 抓取 + 解析 + 生成前端数据格式
移植自 netlify/functions/lib/notam.js（其本身移植自 notam_crawler.py / notam_archive.py）。
解析火箭发射临时危险区/落区通告，推断发射场、时间窗、落区坐标与拍摄位置。
"""

import re
import math
from datetime import datetime, timezone, timedelta

from astro import solar_altitude_deg, is_twilight, twilight_desc, fmt_bj, iso_bj

# ---------- 常量 ----------
CHINA_FIRS = ['ZLHW', 'ZHWH', 'ZGZU', 'ZJSA', 'ZBPE', 'ZPKM', 'ZSHA', 'ZYSH', 'ZWUQ']
DAIP_LOCATIONS = ['ZBPE', 'ZGZU', 'ZHWH', 'ZJSA', 'ZLHW', 'ZPKM', 'ZSHA', 'ZYSH', 'ZWUQ',
                  'RPHI', 'YMMM']

FREEFORM_TERMS = ['AEROSPACE', 'DNG ZONE', 'ROCKET']

SITE_COLORS = {
    '文昌航天发射场': '#C0392B',
    '酒泉卫星发射中心': '#D68910',
    '太原卫星发射中心': '#1E8449',
    '西昌卫星发射中心': '#1F618D',
}

# 发射场 + 预设观测点（spots）
LAUNCH_SITES = [
    {
        'name': '文昌航天发射场', 'lat': 19.6143, 'lng': 110.9512,
        'fir': ['ZGZU', 'ZJSA'],
        'mission': '海上发射 · 低倾角轨道',
        'spots': [
            {'name': '淇水湾沙滩', 'type': '免费', 'detail': '距发射塔约3km · 视野开阔', 'lat': 19.633, 'lng': 110.967},
            {'name': '富荣公寓楼顶', 'type': '白名单', 'detail': '约3.5km · 正对发射塔架', 'lat': 19.6345, 'lng': 110.9685},
            {'name': '铜鼓岭', 'type': '远距', 'detail': '约8km · 尾迹云+海景组合', 'lat': 19.668, 'lng': 110.983},
        ],
    },
    {
        'name': '酒泉卫星发射中心', 'lat': 40.967, 'lng': 100.267, 'fir': ['ZLHW'],
        'mission': '载人航天/商业发射',
        'spots': [
            {'name': '45号观礼点', 'type': '官方', 'detail': '约2km · 正对发射工位', 'lat': 40.955, 'lng': 100.285},
            {'name': '96号观礼点', 'type': '官方', 'detail': '约2km · 地势略高', 'lat': 40.95, 'lng': 100.29},
            {'name': '巴彦宝格德狼心山', 'type': '外围', 'detail': '远眺发射场全景', 'lat': 40.92, 'lng': 100.24},
        ],
    },
    {
        'name': '太原卫星发射中心', 'lat': 38.849, 'lng': 111.608, 'fir': ['ZLHW', 'ZHWH'],
        'mission': '太阳同步轨道卫星',
        'spots': [
            {'name': '岢岚县团城村', 'type': '体验点', 'detail': '毗邻发射中心 · 尾迹远眺', 'lat': 38.79, 'lng': 111.65},
            {'name': '岢岚周边高点', 'type': '野外', 'detail': '尾迹云+山脊组合', 'lat': 38.81, 'lng': 111.58},
        ],
    },
    {
        'name': '西昌卫星发射中心', 'lat': 28.2463, 'lng': 102.027, 'fir': ['ZPKM', 'ZHWH'],
        'mission': '地球同步轨道卫星',
        'spots': [
            {'name': '青杠坝山坡观景台', 'type': '观景台', 'detail': '距发射场约3km · 远眺发射场', 'lat': 28.235, 'lng': 102.045},
            {'name': '奔月广场', 'type': '展示区', 'detail': '航天主题展示区', 'lat': 28.25, 'lng': 102.05},
            {'name': '南山坡机位', 'type': '机位', 'detail': '历史最佳机位 · 俯拍发射场', 'lat': 28.238, 'lng': 102.02},
        ],
    },
]

# Launch Library 2 站点名映射
SITE_KEYWORDS = [
    {'kw': 'Wenchang', 'name': '文昌航天发射场'},
    {'kw': 'Taiyuan', 'name': '太原卫星发射中心'},
    {'kw': 'Jiuquan', 'name': '酒泉卫星发射中心'},
    {'kw': 'Xichang', 'name': '西昌卫星发射中心'},
]

BJ_TZ = timezone(timedelta(hours=8))
UTC = timezone.utc

# 匹配 NOTAM 坐标令牌：前缀 N395600E1192100 或后缀 395600N1192100E（允许 lat/lng 间空白）
_COORD_TOKEN_RE = re.compile(
    r'[NS]\d{6}[EW]\d{7}|\d{6}[NS]\s*\d{7}[EW]',
    re.IGNORECASE,
)


def _site_by_name(name):
    for s in LAUNCH_SITES:
        if s['name'] == name:
            return s
    return None


def _map_site_name(loc_name):
    if not loc_name:
        return None
    for s in SITE_KEYWORDS:
        if s['kw'] in loc_name:
            return s['name']
    return None


def _bj_full(dt):
    """完整北京时间字符串：YYYY年MM月DD日 HH:MM"""
    if dt is None:
        return ''
    if not isinstance(dt, datetime):
        return str(dt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    bj = dt.astimezone(BJ_TZ)
    return bj.strftime('%Y年%m月%d日 %H:%M')


def _iso_utc_z(dt):
    """UTC ISO 字符串以 Z 结尾（带毫秒，等价于 JS Date.toISOString()）。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    return dt.strftime('%Y-%m-%dT%H:%M:%S.000Z')


def _iso_utc_z_no_ms(dt):
    """UTC ISO 字符串以 Z 结尾（不带毫秒，截断到秒）。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ')


# ---------- 时间解析 ----------
def parse_yymmddhhmm(s):
    """解析 YYMMDDHHMM（10 位）为 UTC datetime，无法解析返回 None。"""
    if not s:
        return None
    s = s.strip()
    if not re.match(r'^\d{10}$', s):
        return None
    try:
        return datetime.strptime(s, '%y%m%d%H%M').replace(tzinfo=UTC)
    except ValueError:
        return None


def extract_time_from_msg(raw):
    """从 NOTAM 报文提取时间区间（B)/C) 字段），返回 (start, end)。"""
    if not raw:
        return (None, None)
    t = raw.upper()
    sm = re.search(r'\bB\)\s*(\d{10})', t)
    em = re.search(r'\bC\)\s*(\d{10}|PERM)\b', t)
    if sm and em:
        start = parse_yymmddhhmm(sm.group(1))
        end = None if em.group(1) == 'PERM' else parse_yymmddhhmm(em.group(1))
        if start:
            return (start, end)
    return (None, None)


def extract_fir_from_msg(raw, fallback='UNKNOWN'):
    """从 NOTAM 报文提取 FIR（A) 字段），无法提取返回 fallback。"""
    if not raw:
        return fallback
    m = re.search(r'\bA\)\s*([A-Z]{4})\b', raw)
    return m.group(1) if m else fallback


def _extract_code(raw):
    """从报文提取 NOTAM 编号（如 A1234/25）。"""
    if not raw:
        return ''
    m = re.search(r'([A-Z]\d{4}/\d{2})', raw)
    return m.group(1) if m else ''


# ---------- 坐标解析 ----------
def standardize_coord(tok):
    """
    标准化坐标令牌为 (lat, lng)。
    支持 N395600E1192100（前缀）与 395600N1192100E（后缀）两种格式。
    lat 为 4~6 位（DDMM 或 DDMMSS），lng 为 5~7 位（DDDMM 或 DDDMMSS）。
    """
    if not tok:
        return None
    tok = tok.strip()
    m = re.match(r'^([NS])(\d{4,6})([EW])(\d{5,7})$', tok)
    if not m:
        m2 = re.match(r'^(\d{4,6})([NS])\s*(\d{5,7})([EW])$', tok)
        if m2:
            # 后缀格式 -> 转为前缀格式
            tok = m2.group(2) + m2.group(1) + m2.group(4) + m2.group(3)
            m = re.match(r'^([NS])(\d{4,6})([EW])(\d{5,7})$', tok)
    if not m:
        return None
    ns, lat_s, ew, lon_s = m.group(1), m.group(2), m.group(3), m.group(4)
    if len(lat_s) == 6:
        lat = int(lat_s[0:2]) + int(lat_s[2:4]) / 60.0 + int(lat_s[4:6]) / 3600.0
    else:
        lat = int(lat_s[0:2]) + int(lat_s[2:4]) / 60.0
    if len(lon_s) == 7:
        lng = int(lon_s[0:3]) + int(lon_s[3:5]) / 60.0 + int(lon_s[5:7]) / 3600.0
    else:
        lng = int(lon_s[0:3]) + int(lon_s[3:5]) / 60.0
    if ns == 'S':
        lat = -lat
    if ew == 'W':
        lng = -lng
    return (round(lat, 5), round(lng, 5))


def extract_coord_groups(text, min_points=3):
    """
    从文本中提取坐标多边形分组。
    连续的坐标令牌（其间仅含分隔符 空白/-/逗号 等）归为一组。
    返回 list[list[str]]，仅保留顶点数 >= min_points 的分组。
    """
    if not text:
        return []
    matches = list(_COORD_TOKEN_RE.finditer(text))
    if not matches:
        return []
    groups = []
    current = [matches[0].group(0)]
    for prev, cur in zip(matches, matches[1:]):
        gap = text[prev.end():cur.start()]
        if re.fullmatch(r'[\s\-–,\.;/]+', gap):
            current.append(cur.group(0))
        else:
            if len(current) >= min_points:
                groups.append(current)
            current = [cur.group(0)]
    if len(current) >= min_points:
        groups.append(current)
    return groups


def _all_coord_tokens(text):
    """返回文本中所有坐标令牌字符串（前缀或后缀格式）。"""
    if not text:
        return []
    return _COORD_TOKEN_RE.findall(text)


def _build_polygon(raw, coord_str=''):
    """从报文或显式坐标串解析落区多边形顶点列表 [(lat, lng), ...]。"""
    coord_polygon = []
    if coord_str:
        toks = [t for t in re.split(r'[\s\-–,;]+', coord_str) if t]
    else:
        groups = extract_coord_groups(raw, min_points=3)
        toks = groups[0] if groups else _all_coord_tokens(raw)
    for tok in toks:
        c = standardize_coord(tok)
        if c:
            coord_polygon.append(c)
    return coord_polygon


def _build_record_from_raw(raw, code='', fir=''):
    """从原始报文构建一条记录字典。"""
    raw = raw or ''
    if not code:
        code = _extract_code(raw)
    if not fir:
        fir = extract_fir_from_msg(raw, 'UNKNOWN')
    start, end = extract_time_from_msg(raw)
    coord_polygon = _build_polygon(raw)
    coord = None
    if coord_polygon:
        lat = sum(p[0] for p in coord_polygon) / len(coord_polygon)
        lng = sum(p[1] for p in coord_polygon) / len(coord_polygon)
        coord = (round(lat, 4), round(lng, 4))
    site = assign_launch_site(fir, coord)
    return {
        'code': code,
        'fir': fir,
        'coord': coord,
        'raw': raw,
        'start': start,
        'end': end,
        'site': site,
        'coord_polygon': coord_polygon,
    }


# ---------- 相关性判定 ----------
def is_relevant_area_notam(msg):
    """DAIP 数据相关性判定（移植自 notam.js isRelevantAreaNotam）。"""
    t = (msg or '').upper()
    return (
        ('A TEMPORARY' in t and '-' in t)
        or 'AEROSPACE' in t
        or 'AER0SPACE' in t
        or ('CHINA' in t and 'DNG ZONE' in t and 'AERIAL' in t)
    )


def is_rocket_notam(text):
    """是否为火箭发射相关 NOTAM（移植自 notam.js isRocketNotam）。"""
    t = (text or '').upper()
    if 'FIREWORK' in t or 'FIREWORKS' in t:
        return False
    kws = ['DANGER AREA', 'TEMPORARY DANGER', 'PROHIBITED AREA',
           'AEROSPACE FLT ACT', 'AEROSPACE ACTIVITIES', 'SPECIAL OPS',
           'UNBURNED DEBRIS', 'FALL AREA', 'ROCKET', 'LAUNCH',
           'QRDCA', 'QWMLW', 'QRPCA']
    return any(k in t for k in kws)


def assign_launch_site(fir, coord):
    """根据 FIR 与落区坐标推断发射场（移植自 notam.js assignLaunchSite）。"""
    if coord:
        lat, lng = coord
        if 106 < lng < 127 and lat < 20 and fir in ('ZGZU', 'ZJSA', 'RPHI'):
            return '文昌航天发射场'
        if 100 < lng < 112 and 31 < lat < 42 and fir == 'ZLHW':
            return '酒泉卫星发射中心'
        if 108 < lng < 114 and 32 < lat < 39 and fir in ('ZHWH', 'ZLHW', 'ZXXX'):
            return '太原卫星发射中心'
    for site in LAUNCH_SITES:
        if fir in site['fir']:
            return site['name']
    return '未知'


# ---------- DAIP 响应解析 ----------
def parse_daip_response(data):
    """
    解析 DAIP 响应为记录列表。
    兼容两种格式：
      1) DAIP mobile query 响应：{"group":[{"notams":[{"list":[{"message":...}]}]}]}
      2) joey0609 镜像 JSON：{"CODE":[], "COORDINATES":[], "FIR":[], "RAWMESSAGE":[]}
    每条记录包含：code, fir, coord, raw, start, end, site, coord_polygon
    """
    records = []
    if not data:
        return records

    # joey0609 字典格式（带 RAWMESSAGE 数组）
    if isinstance(data, dict) and isinstance(data.get('RAWMESSAGE'), list):
        codes = data.get('CODE', []) or []
        coords = data.get('COORDINATES', []) or []
        firs = data.get('FIR', []) or []
        raws = data.get('RAWMESSAGE', []) or []
        for i, raw in enumerate(raws):
            if not raw or not is_relevant_area_notam(raw):
                continue
            code = codes[i] if i < len(codes) else ''
            fir = firs[i] if i < len(firs) else ''
            coord_str = coords[i] if i < len(coords) else ''
            rec = _build_record_from_raw(raw, code=code, fir=fir)
            if coord_str:
                rec['coord_polygon'] = _build_polygon('', coord_str)
                if rec['coord_polygon']:
                    lat = sum(p[0] for p in rec['coord_polygon']) / len(rec['coord_polygon'])
                    lng = sum(p[1] for p in rec['coord_polygon']) / len(rec['coord_polygon'])
                    rec['coord'] = (round(lat, 4), round(lng, 4))
                    rec['site'] = assign_launch_site(rec['fir'], rec['coord'])
            if rec['coord_polygon']:
                records.append(rec)
        return records

    # DAIP mobile query 格式：收集消息项
    items = []
    if isinstance(data, dict):
        for g in (data.get('group') or []):
            for n in (g.get('notams') or []):
                for it in (n.get('list') or []):
                    items.append(it)
        if not items:
            for n in (data.get('notams') or []):
                if isinstance(n, dict):
                    for it in (n.get('list') or []):
                        items.append(it)
                else:
                    items.append(n)
        if not items and isinstance(data.get('list'), list):
            items = data.get('list')
    elif isinstance(data, list):
        items = data

    for it in items:
        if isinstance(it, str):
            raw = it
        elif isinstance(it, dict):
            raw = (it.get('message') or it.get('raw') or it.get('text')
                   or it.get('traditionalMessage') or it.get('icaoMessage')
                   or it.get('notamText') or it.get('traditionalMessageFrom4thWord') or '')
        else:
            continue
        if not raw or not is_relevant_area_notam(raw):
            continue
        records.append(_build_record_from_raw(raw))
    return records


def analyze(raw_msgs):
    """
    从 FAA 原始报文列表解析火箭发射记录（移植自 notam.js analyze）。
    返回记录列表，结构与 parse_daip_response 一致。
    """
    rocket = []
    seen = set()
    for raw in raw_msgs:
        if not raw or not is_rocket_notam(raw):
            continue
        code = _extract_code(raw)
        if code and code in seen:
            continue
        if code:
            seen.add(code)
        fir = extract_fir_from_msg(raw, '')
        if not fir:
            m3 = re.search(r'Q\)\s*([A-Z]{4})/', raw)
            if m3:
                fir = m3.group(1)
        rocket.append(_build_record_from_raw(raw, code=code, fir=fir))
    return rocket


# ---------- 方位角 / 方向 ----------
def bearing_deg(lat1, lng1, lat2, lng2):
    """计算从 (lat1,lng1) 到 (lat2,lng2) 的初始方位角（度，0=正北，顺时针）。"""
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dl = math.radians(lng2 - lng1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def bearing_to_dir(b):
    """方位角转中文八方向。"""
    dirs = ['北', '东北', '东', '东南', '南', '西南', '西', '西北']
    return dirs[int(round(b / 45.0)) % 8]


def coord_label(lat, lng):
    """坐标可读标签：12.34°N, 56.78°E"""
    ns = 'N' if lat >= 0 else 'S'
    ew = 'E' if lng >= 0 else 'W'
    return '%.2f°%s, %.2f°%s' % (abs(lat), ns, abs(lng), ew)


# ---------- 预测数据生成（DAIP，含真实落区坐标 + 航迹 path）----------
def build_prediction_from_daip(records):
    """
    从 DAIP 记录构建预测数据。
    1) 过滤结束时间在未来且 site 非『未知』的记录
    2) 按开始时间排序
    3) 合并同发射场且窗口起始相差 < 90 分钟的记录
    4) 构建预测项（含真实落区坐标、path、太阳高度角、晨昏信息、落区多边形）
    5) 返回 {fetched_at, source, next_launch, upcoming}
    """
    now = datetime.now(UTC)
    valid = [r for r in records
             if r.get('end') and r['end'] >= now and r.get('site') and r['site'] != '未知']
    valid.sort(key=lambda r: r['start'] or datetime.max.replace(tzinfo=UTC))

    # 合并同发射场且窗口起始相差 < 90 分钟
    merged = []
    for r in valid:
        last = merged[-1] if merged else None
        if (last and last['site'] == r['site']
                and last.get('start') and r.get('start')
                and abs((r['start'] - last['start']).total_seconds()) < 90 * 60):
            if r['start'] < last['start']:
                last['start'] = r['start']
            if r.get('end') and (not last.get('end') or r['end'] > last['end']):
                last['end'] = r['end']
            if r.get('coord_polygon'):
                last.setdefault('polygons', []).append(r['coord_polygon'])
            last.setdefault('codes', [last.get('code', '')]).append(r.get('code', ''))
            continue
        new_rec = dict(r)
        new_rec['polygons'] = [r['coord_polygon']] if r.get('coord_polygon') else []
        new_rec['codes'] = [r.get('code', '')]
        merged.append(new_rec)

    items = []
    for rec in merged:
        site = _site_by_name(rec['site'])
        lat = site['lat'] if site else 0
        lng = site['lng'] if site else 0
        debris_lat = rec['coord'][0] if rec.get('coord') else lat
        debris_lng = rec['coord'][1] if rec.get('coord') else lng

        start = rec.get('start') or now
        end = rec.get('end') or start
        mid = start + (end - start) / 2
        sun_alt = solar_altitude_deg(lat, lng, mid)
        twilight_fav = is_twilight(sun_alt)

        dir_str = bearing_to_dir(bearing_deg(lat, lng, debris_lat, debris_lng))
        sea = rec['site'] == '文昌航天发射场'
        direction = '%s · 出海方向' % dir_str if sea else dir_str

        if rec.get('coord'):
            debris_zone = ('南海 · 落区 (%s)' % coord_label(debris_lat, debris_lng) if sea
                           else '落区 (%s)' % coord_label(debris_lat, debris_lng))
        else:
            debris_zone = '落区待定'

        path = [[lat, lng], [debris_lat, debris_lng]]
        bj_str = _bj_full(start)
        twilight_desc_str = twilight_desc(sun_alt)
        code_str = ' + '.join(rec.get('codes') or [rec.get('code', '')])
        est_t0 = start - timedelta(minutes=18)

        if twilight_fav:
            note = '接近晨昏时段，高空尾迹可能被阳光照亮，适合尾迹云/火箭云拍摄'
        elif sun_alt >= 0:
            note = '白天发射，高空尾迹不被阳光照亮，通常无法形成可见火箭云'
        else:
            note = '深夜发射，高空进入地影，无法拍摄'

        items.append({
            'code': code_str,
            'site': rec['site'],
            'site_lat': lat,
            'site_lng': lng,
            'mission_type': site['mission'] if site else '卫星发射',
            'window_start': iso_bj(start),
            'window_end': iso_bj(end),
            'est_t0': iso_bj(est_t0),
            'debris_zone': debris_zone,
            'debris_lat': debris_lat,
            'debris_lng': debris_lng,
            'direction': direction,
            'twilight_favorable': twilight_fav,
            'note': note,
            'spots': site['spots'] if site else [],
            'path': path,
            'label': '%s · %s' % (bj_str, code_str),
            'launchTime': _iso_utc_z(start),
            'sunAlt': round(sun_alt * 100) / 100.0,
            'twilight': twilight_fav,
            'twilightDesc': twilight_desc_str,
            'status': 'go',
            'debris_polygons': rec.get('polygons') or [],
        })

    return {
        'fetched_at': iso_bj(datetime.now(UTC)),
        'source': 'DAIP NOTAM（美国国防信息系统）',
        'next_launch': items[0] if items else None,
        'upcoming': items[1:] if len(items) > 1 else [],
    }


# ---------- Launch Library 2 预测数据生成 ----------
def _pad_location(launch):
    pad = launch.get('pad') or {}
    loc = pad.get('location')
    if isinstance(loc, dict):
        return loc
    return {}


def _parse_iso(s):
    """解析 ISO 8601 字符串为 aware datetime，失败返回 None。"""
    if not s:
        return None
    try:
        v = s
        if v.endswith('Z'):
            v = v[:-1] + '+00:00'
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except Exception:
        return None


def build_prediction_from_ll(launches):
    """
    从 Launch Library 2 发射列表构建预测数据。
    LL API 无落区坐标，path 默认指向发射场自身。
    返回 {fetched_at, source, next_launch, upcoming} 或 None（无数据）。
    """
    items = []
    for l in launches:
        loc = _pad_location(l)
        site_name = _map_site_name(loc.get('name', ''))
        if not site_name:
            continue
        site_info = _site_by_name(site_name)
        if not site_info:
            continue

        net = _parse_iso(l.get('net')) or datetime.now(UTC)
        start = _parse_iso(l.get('window_start')) or net
        end = _parse_iso(l.get('window_end')) or net
        sun_alt = solar_altitude_deg(site_info['lat'], site_info['lng'], net)
        twilight_fav = is_twilight(sun_alt)

        rocket_cfg = ((l.get('rocket') or {}).get('configuration')) or {}
        rocket_name = (rocket_cfg.get('full_name')
                       or (l.get('name', '').split('|')[0].strip() if l.get('name') else '')
                       or '火箭')
        mission = l.get('mission') or {}
        mission_name = mission.get('name') or '载荷待定'

        debris_lat = site_info['lat']
        debris_lng = site_info['lng']
        path = [[site_info['lat'], site_info['lng']], [debris_lat, debris_lng]]
        bj_str = _bj_full(net)
        twilight_desc_str = twilight_desc(sun_alt)
        est_t0 = net - timedelta(minutes=18)

        if twilight_fav:
            note = '接近晨昏时段，高空尾迹可能被阳光照亮，适合尾迹云/火箭云拍摄'
        elif sun_alt >= 0:
            note = '白天发射，高空尾迹不被阳光照亮，通常无法形成可见火箭云'
        else:
            note = '深夜发射，高空进入地影，无法拍摄'

        items.append({
            'code': '%s · %s' % (rocket_name, mission_name),
            'site': site_name,
            'site_lat': site_info['lat'],
            'site_lng': site_info['lng'],
            'mission_type': mission.get('type') or '待定',
            'window_start': iso_bj(start),
            'window_end': iso_bj(end),
            'est_t0': iso_bj(est_t0),
            'debris_zone': '落区详情待官方公布',
            'debris_lat': debris_lat,
            'debris_lng': debris_lng,
            'direction': '待定',
            'twilight_favorable': twilight_fav,
            'note': note,
            'spots': site_info['spots'],
            'path': path,
            'label': '%s · %s' % (bj_str, rocket_name),
            'launchTime': _iso_utc_z(net),
            'sunAlt': round(sun_alt * 100) / 100.0,
            'twilight': twilight_fav,
            'twilightDesc': twilight_desc_str,
            'status': 'go',
        })

    items.sort(key=lambda x: x['window_start'] or '')
    if not items:
        return None
    return {
        'fetched_at': iso_bj(datetime.now(UTC)),
        'source': 'Launch Library 2 API（The Space Devs）',
        'next_launch': items[0],
        'upcoming': items[1:] if len(items) > 1 else [],
    }


# ---------- 历史任务生成 ----------
def build_history(records):
    """
    从记录构建历史任务数据。
    仅保留已结束（end < now）且 site 非『未知』的记录。
    返回 {fetched_at, records}。
    """
    now = datetime.now(UTC)
    tasks = []
    for r in records:
        if not r.get('start') or not r.get('end'):
            continue
        if r['end'] >= now:
            continue
        if r.get('site') == '未知':
            continue
        site = _site_by_name(r['site'])
        if not site:
            continue
        lat, lng = site['lat'], site['lng']
        mid = r['start'] + (r['end'] - r['start']) / 2
        sun_alt = solar_altitude_deg(lat, lng, mid)
        twilight = is_twilight(sun_alt)
        end_point = list(r['coord']) if r.get('coord') else [lat, lng]
        bj = fmt_bj(mid)
        tasks.append({
            'code': r.get('code', ''),
            'site': r['site'],
            'color': SITE_COLORS.get(r['site'], '#9A938A'),
            'path': [[lat, lng], end_point],
            'label': '%s · %s发射 · 落区 %.2f°N %.2f°E' % (
                bj, '傍晚' if twilight else '白天', end_point[0], end_point[1]),
            'launchTime': _iso_utc_z_no_ms(mid),
            'sunAlt': round(sun_alt * 100) / 100.0,
            'twilight': twilight,
            'twilightDesc': twilight_desc(sun_alt),
        })
    tasks.sort(key=lambda t: t['launchTime'] or '')
    return {
        'fetched_at': iso_bj(datetime.now(UTC)),
        'records': tasks,
    }
