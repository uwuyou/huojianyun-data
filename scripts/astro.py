#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
太阳位置计算 —— NOAA 低精度算法（精度约 ±0.3°，足够晨昏判定）
移植自 netlify/functions/lib/astro.js（其本身移植自 pysolar 用途）。
计算太阳高度角，判断晨昏阶段（火箭云形成条件）。
"""

import math
from datetime import datetime, timezone, timedelta

rad = math.pi / 180.0
day_ms = 1000 * 60 * 60 * 24
J1970 = 2440588
J2000 = 2451545
OBLIQUITY = rad * 23.4397  # 黄赤交角

# 北京时区
BJ_TZ = timezone(timedelta(hours=8))

# 晨昏判定阈值（度）
TWILIGHT_LOWER = -18.0
TWILIGHT_UPPER = 0.0


def _to_timestamp_ms(date):
    """将 datetime / 毫秒时间戳转换为 Unix 毫秒时间戳（UTC 基准，等价于 JS Date.valueOf()）。"""
    if isinstance(date, (int, float)):
        return float(date)
    if isinstance(date, datetime):
        if date.tzinfo is None:
            # 朴素 datetime 视为 UTC（NOTAM 时间均为 UTC）
            date = date.replace(tzinfo=timezone.utc)
        return date.timestamp() * 1000.0
    raise TypeError("Unsupported date type: %r" % type(date))


def to_julian(date):
    return _to_timestamp_ms(date) / day_ms - 0.5 + J1970


def to_days(date):
    return to_julian(date) - J2000


def solar_mean_anomaly(d):
    return rad * (357.5291 + 0.98560028 * d)


def ecliptic_longitude(M):
    C = rad * (1.9148 * math.sin(M) + 0.02 * math.sin(2 * M) + 0.0003 * math.sin(3 * M))
    P = rad * 102.9372
    return M + C + P + math.pi


def declination(l):
    return math.asin(math.sin(OBLIQUITY) * math.sin(l))


def right_ascension(l):
    return math.atan2(math.sin(l) * math.cos(OBLIQUITY), math.cos(l))


def sidereal_time(d, lw):
    return rad * (280.16 + 360.9856235 * d) - lw


def altitude(H, phi, dec):
    return math.asin(math.sin(phi) * math.sin(dec) + math.cos(phi) * math.cos(dec) * math.cos(H))


def solar_altitude_deg(lat, lng, date):
    """
    太阳高度角（度）。
    :param lat: 纬度（北纬为正）
    :param lng: 经度（东经为正）
    :param date: datetime 或毫秒时间戳
    :return: 太阳高度角，单位度
    """
    lw = rad * -lng          # 西经弧度
    phi = rad * lat          # 纬度弧度
    d = to_days(date)
    M = solar_mean_anomaly(d)
    L = ecliptic_longitude(M)
    dec = declination(L)
    ra = right_ascension(L)
    H = sidereal_time(d, lw) - ra
    alt = altitude(H, phi, dec)
    return alt / rad


def is_twilight(sun_alt):
    """是否处于晨昏阶段：太阳高度角在 [-18, 0) 度之间。"""
    return sun_alt is not None and sun_alt >= TWILIGHT_LOWER and sun_alt < TWILIGHT_UPPER


def twilight_desc(sun_alt):
    """晨昏阶段中文描述（与 astro.js 保持一致）。"""
    if sun_alt is None:
        return '太阳高度角未知'
    if is_twilight(sun_alt):
        return '晨昏阶段（太阳高度角 %.1f°）· 可拍摄火箭云' % sun_alt
    if sun_alt >= TWILIGHT_UPPER:
        return '白天（太阳高度角 %.1f°）· 无法拍摄火箭云' % sun_alt
    return '深夜（太阳高度角 %.1f°）· 无法拍摄火箭云' % sun_alt


def fmt_bj(date):
    """北京时间格式化：MM-DD HH:MM"""
    if date is None:
        return None
    if isinstance(date, datetime):
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        bj = date.astimezone(BJ_TZ)
        return bj.strftime('%m-%d %H:%M')
    return date


def iso_bj(date):
    """北京时间 ISO 字符串（含 +08:00），格式 YYYY-MM-DDTHH:MM:SS+08:00"""
    if date is None:
        return None
    if isinstance(date, datetime):
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        bj = date.astimezone(BJ_TZ)
        return bj.strftime('%Y-%m-%dT%H:%M:%S+08:00')
    return date
