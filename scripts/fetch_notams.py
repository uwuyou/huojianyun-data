#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Actions 定时任务：抓取 NOTAM → 解析 → 生成预测/历史数据 → 提交回仓库
数据源（按优先级）：
  1. DAIP (https://www.daip.jcs.mil/daip/mobile/query)  POST，verify_ssl=False
  2. Launch Library 2 API (https://ll.thespacedevs.com/2.2.0/launch)  GET
  3. FAA NOTAM Search (https://notams.aim.faa.gov/notamSearch/search)  POST

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
from datetime import datetime, timezone

import requests
from urllib3.exceptions import InsecureRequestWarning
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

# 确保 scripts 目录在导入路径中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from astro import iso_bj
from notam_parser import (
    DAIP_LOCATIONS, FREEFORM_TERMS,
    parse_daip_response, analyze,
    build_prediction_from_daip, build_prediction_from_ll, build_history,
)

# ---------- 路径 ----------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(SCRIPT_DIR)
DATA_DIR = os.path.join(REPO_DIR, 'data')

UTC = timezone.utc

# ---------- 数据源 URL ----------
DAIP_INDEX_URL = 'https://www.daip.jcs.mil/daip/mobile/index'
DAIP_QUERY_URL = 'https://www.daip.jcs.mil/daip/mobile/query'
LL_URL = 'https://ll.thespacedevs.com/2.2.0/launch?limit=60&country_code=CHN'
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


# ---------- 主流程 ----------
def main():
    now = datetime.now(UTC)
    print('[*] 当前 UTC: %s' % now.isoformat())
    errors = []

    # 1) 主数据源：DAIP（NOTAM 原始报文 + 落区坐标）
    try:
        print('[*] 尝试 DAIP ...')
        records = fetch_daip()
        prediction = build_prediction_from_daip(records)
        history = build_history(records)
        print('[+] DAIP 返回 %d 条记录, valid=%d' % (
            len(records), 1 if prediction.get('next_launch') else 0))
        if prediction.get('next_launch'):
            return finalize(prediction, history, records, 'DAIP NOTAM', now, None)
        # 有记录但无未来发射，继续尝试 LL
        _daip_pred, _daip_hist, _daip_recs = prediction, history, records
    except Exception as e:
        print('[!] DAIP 失败: %s' % e)
        errors.append('DAIP: %s' % e)
        _daip_pred = _daip_hist = _daip_recs = None

    # 2) 备选：Launch Library 2 API
    try:
        print('[*] 尝试 Launch Library 2 API ...')
        launches = fetch_ll()
        prediction = build_prediction_from_ll(launches)
        history = {'fetched_at': iso_bj(now), 'records': []}
        print('[+] LL 返回 %d 条中国发射' % len(launches))
        if prediction and prediction.get('next_launch'):
            return finalize(prediction, history, [], 'Launch Library 2 API', now, None)
    except Exception as e:
        print('[!] LL 失败: %s' % e)
        errors.append('LL: %s' % e)
        prediction = None

    # 3) 最后备选：FAA NOTAM
    try:
        print('[*] 尝试 FAA NOTAM Search ...')
        records = fetch_faa()
        prediction = build_prediction_from_daip(records)
        history = build_history(records)
        print('[+] FAA 返回 %d 条记录' % len(records))
        if prediction.get('next_launch'):
            return finalize(prediction, history, records, 'FAA NOTAM Search', now, None)
    except Exception as e:
        print('[!] FAA 失败: %s' % e)
        errors.append('FAA: %s' % e)
        prediction = None
        records = []

    # 所有数据源均无未来发射或失败
    if prediction is not None:
        # 有数据但无未来发射任务
        return finalize(prediction, history, records, 'DAIP NOTAM', now, None)

    # 全部失败
    if _daip_pred is not None:
        return finalize(_daip_pred, _daip_hist, _daip_recs, 'DAIP NOTAM', now, None)

    error = '；'.join(errors) or '所有数据源均不可达'
    return finalize(None, None, [], '', now, error)


if __name__ == '__main__':
    result = main()
    print('[*] 完成: %s' % json.dumps(result, ensure_ascii=False))
    sys.exit(0 if result.get('ok') else 1)
