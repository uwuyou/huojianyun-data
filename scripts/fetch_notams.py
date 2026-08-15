#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Actions 定时任务：抓取多数据源 → 解析 → 合并去重 → 生成预测/历史数据 → 提交回仓库
数据源（并行抓取，按时间窗合并去重，每条标注来源）：
  1. DAIP (https://www.daip.jcs.mil/daip/mobile/query)  POST，verify_ssl=False —— NOTAM 原始报文 + 落区坐标
  2. Launch Library 2 (https://ll.thespacedevs.com/2.2.0/launch)  GET —— 官方排期 + 火箭/任务名
     - /upcoming 未来发射（含窗口、火箭型号、任务名）
     - /previous 历史发射（补全历史归档）
  3. FAA NOTAM Search (https://notams.aim.faa.gov/notamSearch/search)  POST —— 美国域 NOTAM（海射/跨境任务）

输出：
  data/prediction.json
  data/history.json
  data/status.json
并提交推送到仓库（使用 GITHUB_TOKEN）。
"""

import os
import sys
import json
import subprocess
from datetime import datetime, timezone, timedelta

import requests
from urllib3.exceptions import InsecureRequestWarning
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

# 确保 scripts 目录在导入路径中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from astro import iso_bj
from notam_parser import (
    DAIP_LOCATIONS, FREEFORM_TERMS,
    parse_daip_response, analyze,
    build_prediction_from_daip, build_prediction_from_ll,
    build_history, build_history_from_ll,
)

# ---------- 路径 ----------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(SCRIPT_DIR)
DATA_DIR = os.path.join(REPO_DIR, 'data')

UTC = timezone.utc

# ---------- 数据源 URL ----------
DAIP_INDEX_URL = 'https://www.daip.jcs.mil/daip/mobile/index'
DAIP_QUERY_URL = 'https://www.daip.jcs.mil/daip/mobile/query'
LL_URL = 'https://ll.thespacedevs.com/2.2.0/launch/upcoming?limit=100'
FAA_INDEX_URL = 'https://notams.aim.faa.gov/notamSearch/nsapp.html'
FAA_URL = 'https://notams.aim.faa.gov/notamSearch/search'

CHROME_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
             '(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36')

# DAIP 请求头
DAIP_HEADERS = {
    'Accept': 'application/json',
    'Content-Type': 'application/json',
    'X-Requested-With': 'XMLHttpRequest',
    'Referer': DAIP_INDEX_URL,
    'User-Agent': CHROME_UA,
}

# DAIP 查询 payload
DAIP_PAYLOAD = {
    'locs': ' '.join(DAIP_LOCATIONS), 'poa': '', 'pod': '', 'alternates': '', 'route': '',
    'radius': '10', 'runwayLength': '', 'runwayWidth': '', 'airportType': '',
    'type': 'LOCATION', 'notamId': '', 'acode': '', 'artcc': '', 'tfrsOnly': '',
    'orgLoc': '', 'lat1': '', 'lat2': '', 'lng1': '', 'lng2': '',
    'latdir': '', 'longdir': '', 'includeRegulatoryNotices': '',
    'briefing': '', 'scheduleDate': '', 'sendTime': '', 'active': '',
    'sunday': '', 'monday': '', 'tuesday': '', 'wednesday': '',
    'thursday': '', 'friday': '', 'saturday': '', 'sort': 'Criticality',
}

FAA_HEADERS = {
    'Accept': 'application/json, text/plain, */*',
    'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
    'Origin': 'https://notams.aim.faa.gov',
    'Referer': FAA_INDEX_URL,
    'User-Agent': CHROME_UA,
}

LL_HEADERS = {
    'Accept': 'application/json',
    'User-Agent': 'huojianyun/1.0 (rocket-cloud observer)',
}


# ---------- 数据源抓取 ----------
def fetch_daip():
    """抓取 DAIP 数据并解析为记录列表。失败抛异常。"""
    s = requests.Session()
    s.headers.update(DAIP_HEADERS)
    # 先建立 session（可能失败，忽略）
    try:
        s.get(DAIP_INDEX_URL, verify=False, timeout=15)
    except Exception:
        pass
    r = s.post(DAIP_QUERY_URL, json=DAIP_PAYLOAD, verify=False, timeout=15)
    r.raise_for_status()
    data = r.json()
    records = parse_daip_response(data)
    if not records:
        raise RuntimeError('DAIP 返回无有效记录')
    return records


def fetch_ll():
    """抓取 Launch Library 2 API，过滤 country_code == 'CHN'。"""
    r = requests.get(LL_URL, headers=LL_HEADERS, timeout=20)
    r.raise_for_status()
    data = r.json()
    launches = data.get('results', []) or []
    return [l for l in launches
            if isinstance(l, dict) and _ll_country_code(l) == 'CHN']


def _ll_country_code(launch):
    pad = launch.get('pad') or {}
    loc = pad.get('location')
    if isinstance(loc, dict):
        return loc.get('country_code', '') or ''
    return ''


LL_PREV_URL = 'https://ll.thespacedevs.com/2.2.0/launch/previous?limit=40'


def fetch_ll_previous():
    """抓取 Launch Library 2 历史发射（previous），过滤 country_code == 'CHN'。"""
    r = requests.get(LL_PREV_URL, headers=LL_HEADERS, timeout=20)
    r.raise_for_status()
    data = r.json()
    launches = data.get('results', []) or []
    return [l for l in launches
            if isinstance(l, dict) and _ll_country_code(l) == 'CHN']


def fetch_faa():
    """抓取 FAA NOTAM Search 并解析为记录列表。失败抛异常。"""
    s = requests.Session()
    try:
        s.get(FAA_INDEX_URL, headers=FAA_HEADERS, verify=False, timeout=15)
    except Exception:
        pass

    # 关键词 + 4 个核心 FIR（覆盖所有中国发射场）
    queries = []
    for t in FREEFORM_TERMS:
        queries.append({'searchType': '4', 'freeFormText': t})
    for loc in ['ZLHW', 'ZHWH', 'ZGZU', 'ZJSA']:
        queries.append({'searchType': '0', 'designatorsForLocation': loc})

    raw_msgs = []
    for q in queries:
        page = 0
        while page < 4:
            payload = {'offset': str(page * 30), 'notamsOnly': 'false'}
            payload.update(q)
            try:
                r = s.post(FAA_URL, data=payload, headers=FAA_HEADERS,
                           verify=False, timeout=15)
                if r.status_code != 200:
                    break
                items = r.json().get('notamList', []) or []
            except Exception:
                break
            if not items:
                break
            for it in items:
                msg = (it.get('icaoMessage') or it.get('notamText')
                       or it.get('traditionalMessageFrom4thWord') or '')
                if msg:
                    raw_msgs.append(msg)
            if len(items) < 30:
                break
            page += 1
    records = analyze(raw_msgs)
    if not records:
        raise RuntimeError('FAA 返回无有效记录')
    return records


# ---------- 文件写入 ----------
def write_json(path, obj):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def read_last_success():
    try:
        with open(os.path.join(DATA_DIR, 'status.json'), 'r', encoding='utf-8') as f:
            prev = json.load(f)
        return prev.get('last_success')
    except Exception:
        return None


def git_push():
    """提交 data/ 目录变更并推送到 origin（使用 GITHUB_TOKEN 鉴权）。"""
    token = os.environ.get('GITHUB_TOKEN', '')
    gh_repo = os.environ.get('GITHUB_REPOSITORY', '')
    try:
        if token and gh_repo:
            url = 'https://x-access-token:%s@github.com/%s.git' % (token, gh_repo)
            subprocess.run(['git', 'remote', 'set-url', 'origin', url],
                           cwd=REPO_DIR, check=False, capture_output=True)
        subprocess.run(['git', 'config', 'user.name', 'github-actions[bot]'],
                       cwd=REPO_DIR, check=False, capture_output=True)
        subprocess.run(['git', 'config', 'user.email',
                        'github-actions[bot]@users.noreply.github.com'],
                       cwd=REPO_DIR, check=False, capture_output=True)
        subprocess.run(['git', 'add', 'data/'],
                       cwd=REPO_DIR, check=False, capture_output=True)
        diff = subprocess.run(['git', 'diff', '--staged', '--quiet'],
                              cwd=REPO_DIR, capture_output=True)
        if diff.returncode != 0:
            msg = 'Auto-sync NOTAMs %s UTC' % datetime.now(UTC).strftime('%Y-%m-%d %H:%M')
            subprocess.run(['git', 'commit', '-m', msg],
                           cwd=REPO_DIR, check=False, capture_output=True)
            subprocess.run(['git', 'push'],
                           cwd=REPO_DIR, check=False, capture_output=True)
    except Exception:
        pass


def finalize(prediction, history, records, source, now, error):
    """写入三个 JSON 文件、更新状态、提交推送。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    if prediction is None:
        prediction = {
            'fetched_at': iso_bj(now), 'source': source or '',
            'next_launch': None, 'upcoming': [],
        }
    if history is None:
        history = {'fetched_at': iso_bj(now), 'records': []}

    upcoming_count = (1 if prediction.get('next_launch') else 0) + len(prediction.get('upcoming') or [])
    status = {
        'ok': error is None,
        'error': error,
        'last_attempt': iso_bj(now),
        'last_success': iso_bj(now) if error is None else read_last_success(),
        'source': source,
        'record_count': len(records),
        'upcoming_count': upcoming_count,
        'history_count': len(history.get('records') or []),
    }

    write_json(os.path.join(DATA_DIR, 'prediction.json'), prediction)
    write_json(os.path.join(DATA_DIR, 'history.json'), history)
    write_json(os.path.join(DATA_DIR, 'status.json'), status)
    git_push()
    return status


# ---------- 多源合并去重 ----------
def _parse_bj_iso(s):
    """解析北京时间 ISO 字符串（含 +08:00）为 aware datetime，失败返回 None。"""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except Exception:
        return None


def _windows_close(a, b, minutes=180):
    """判断两次发射时间窗是否重叠或起始接近（视为同一次发射）。"""
    a_s = _parse_bj_iso(a.get('window_start'))
    a_e = _parse_bj_iso(a.get('window_end'))
    b_s = _parse_bj_iso(b.get('window_start'))
    b_e = _parse_bj_iso(b.get('window_end'))
    # 起始接近
    if a_s and b_s and abs((a_s - b_s).total_seconds()) <= minutes * 60:
        return True
    # 窗口重叠
    if a_s and a_e and b_s and b_e:
        if a_s <= b_e and b_s <= a_e:
            return True
    return False


def _absorb_launch(target, src):
    """将 src 的信息补充进 target（同一次发射的不同来源）。"""
    # 落区坐标：target 缺失则取 src（NOTAM 才有坐标）
    if not (target.get('debris_lat') and target.get('debris_lng')):
        if src.get('debris_lat') and src.get('debris_lng'):
            target['debris_lat'] = src['debris_lat']
            target['debris_lng'] = src['debris_lng']
            target['debris_zone'] = src.get('debris_zone', target.get('debris_zone'))
            target['path'] = src.get('path', target.get('path'))
            target['direction'] = src.get('direction', target.get('direction'))
            target['debris_polygons'] = src.get('debris_polygons', target.get('debris_polygons'))
    # 任务类型：target 为泛化值时采用 src
    if src.get('mission_type') and (not target.get('mission_type')
                                    or target.get('mission_type') in ('待定', '卫星发射')):
        target['mission_type'] = src['mission_type']
    # 时间窗取更早开始
    t_s = _parse_bj_iso(target.get('window_start'))
    s_s = _parse_bj_iso(src.get('window_start'))
    if t_s and s_s and s_s < t_s:
        target['window_start'] = src['window_start']
        target['launchTime'] = src.get('launchTime', target.get('launchTime'))
        target['label'] = src.get('label', target.get('label'))
    # 来源合并
    s1 = target.get('source', '')
    s2 = src.get('source', '')
    if s2 and s2 not in s1:
        target['source'] = (s1 + ' + ' + s2) if s1 else s2


def merge_launch_items(preds, sources):
    """
    合并多个预测 dict 的所有发射项，按（同发射场 + 时间窗接近）去重。
    preds: list[dict]，与 sources 一一对应（短来源名）。
    返回排序后的发射项列表。
    """
    items = []
    for pred, src in zip(preds, sources):
        if not pred:
            continue
        pool = []
        if pred.get('next_launch'):
            pool.append(pred['next_launch'])
        pool.extend(pred.get('upcoming') or [])
        for it in pool:
            if not it:
                continue
            it = dict(it)
            it['source'] = src
            items.append(it)

    items.sort(key=lambda x: (x.get('window_start') or '', x.get('site') or ''))
    merged = []
    for it in items:
        target = None
        for m in merged:
            if m.get('site') == it.get('site') and _windows_close(m, it):
                target = m
                break
        if target is None:
            merged.append(it)
        else:
            _absorb_launch(target, it)
    merged.sort(key=lambda x: x.get('window_start') or '')
    # 去掉内部字段，避免污染前端
    for it in merged:
        it.pop('_src', None)
    return merged


def merge_history_items(histories):
    """
    合并历史归档：NOTAM 历史（含坐标）与 LL2 历史（含火箭名）互补。
    按（同发射场 + 同日）去重，优先保留含坐标的 NOTAM 记录。
    """
    seen = {}
    records = []
    for h in histories:
        for r in (h.get('records') or []) if h else []:
            if not r:
                continue
            lt = r.get('launchTime') or ''
            key = (r.get('site') or '', lt[:10])  # site + 日期(YYYY-MM-DD)
            has_coord = len((r.get('path') or [[], []])[-1]) == 2 and \
                r.get('path', [[], []])[-1] != r.get('path', [[], []])[0]
            if key in seen:
                # 已有：优先保留含坐标的
                if has_coord and not seen[key][1]:
                    idx = seen[key][0]
                    records[idx] = r
                    seen[key] = (idx, True)
                continue
            seen[key] = (len(records), has_coord)
            records.append(r)
    records.sort(key=lambda t: t.get('launchTime') or '')
    return records


# ---------- 主流程 ----------
def main():
    now = datetime.now(UTC)
    print('[*] 当前 UTC: %s' % now.isoformat())
    errors = []
    preds = []
    pred_sources = []
    histories = []
    all_records = []
    sources_used = []

    # 1) DAIP（NOTAM 原始报文 + 落区坐标）
    try:
        print('[*] 尝试 DAIP ...')
        daip_records = fetch_daip()
        preds.append(build_prediction_from_daip(daip_records))
        pred_sources.append('DAIP NOTAM')
        histories.append(build_history(daip_records))
        all_records.extend(daip_records)
        sources_used.append('DAIP NOTAM')
        print('[+] DAIP %d 条记录' % len(daip_records))
    except Exception as e:
        print('[!] DAIP 失败: %s' % e)
        errors.append('DAIP: %s' % e)

    # 2) Launch Library 2（官方排期 + 火箭/任务名，未来 + 历史）
    try:
        print('[*] 尝试 Launch Library 2 ...')
        launches = fetch_ll()
        preds.append(build_prediction_from_ll(launches))
        pred_sources.append('Launch Library 2')
        prev = fetch_ll_previous()
        histories.append(build_history_from_ll(prev))
        sources_used.append('Launch Library 2')
        print('[+] LL upcoming=%d previous=%d' % (len(launches), len(prev)))
    except Exception as e:
        print('[!] LL 失败: %s' % e)
        errors.append('LL: %s' % e)

    # 3) FAA NOTAM（美国域 NOTAM，海射/跨境任务）
    try:
        print('[*] 尝试 FAA NOTAM ...')
        faa_records = fetch_faa()
        preds.append(build_prediction_from_daip(faa_records))
        pred_sources.append('FAA NOTAM')
        histories.append(build_history(faa_records))
        all_records.extend(faa_records)
        sources_used.append('FAA NOTAM')
        print('[+] FAA %d 条记录' % len(faa_records))
    except Exception as e:
        print('[!] FAA 失败: %s' % e)
        errors.append('FAA: %s' % e)

    # 合并去重
    merged_items = merge_launch_items(preds, pred_sources)
    merged_history = merge_history_items(histories)
    source_label = ' + '.join(sources_used) or '无'

    if not merged_items and not merged_history:
        # 全部失败或无有效数据
        error = '；'.join(errors) or '所有数据源均不可达'
        return finalize(None, None, all_records, source_label, now, error)

    prediction = {
        'fetched_at': iso_bj(now),
        'source': source_label,
        'next_launch': merged_items[0] if merged_items else None,
        'upcoming': merged_items[1:] if len(merged_items) > 1 else [],
    }
    history = {'fetched_at': iso_bj(now), 'records': merged_history}
    return finalize(prediction, history, all_records, source_label, now, None)


if __name__ == '__main__':
    result = main()
    print('[*] 完成: %s' % json.dumps(result, ensure_ascii=False))
    sys.exit(0 if result.get('ok') else 1)
