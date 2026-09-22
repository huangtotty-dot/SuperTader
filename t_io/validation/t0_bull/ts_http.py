# -*- coding: utf-8 -*-
"""tushare HTTP 薄封装（2026-09-22）—— 不依赖 tushare 包，不走 MCP。

token 只从环境变量 `TUSHARE_TOKEN` 读，**不落盘、不进仓库、不打印**。

tushare HTTP 约定：POST https://api.tushare.pro
    {"api_name": ..., "token": ..., "params": {...}, "fields": ...}
响应 {"code":0, "msg":..., "data":{"fields":[...], "items":[[...]]}}
code != 0 抛 `TushareError`（含 msg，便于区分「无权限」「超频」「无数据」）。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

import pandas as pd

URL = 'https://api.tushare.pro'


class TushareError(RuntimeError):
    pass


def token() -> str:
    t = (os.environ.get('TUSHARE_TOKEN') or '').strip()
    if not t:
        raise TushareError('未设置 TUSHARE_TOKEN 环境变量（本模块刻意不硬编码 token）')
    return t


def api(api_name: str, params: dict, fields: str = '') -> pd.DataFrame:
    body = json.dumps({'api_name': api_name, 'token': token(),
                       'params': params, 'fields': fields}).encode('utf-8')
    req = urllib.request.Request(URL, data=body,
                                headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read().decode('utf-8'))
    except urllib.error.URLError as e:
        raise TushareError(f'网络错误: {e}') from e
    if d.get('code') != 0:
        raise TushareError(f"[{d.get('code')}] {d.get('msg')}")
    data = d.get('data') or {}
    cols, items = data.get('fields') or [], data.get('items') or []
    return pd.DataFrame(items, columns=cols)


def stk_mins_30(ts_code: str, start: str, end: str) -> pd.DataFrame:
    """30min K线。返回列 open/high/low/close/vol/amount + trade_time。"""
    return api('stk_mins',
               {'ts_code': ts_code, 'freq': '30min',
                'start_date': start, 'end_date': end},
               'ts_code,trade_time,open,high,low,close,vol,amount')


def retry(fn, *a, tries: int = 3, sleep: float = 1.5, **kw):
    """对超频/瞬时错误重试。"""
    last = None
    for i in range(tries):
        try:
            return fn(*a, **kw)
        except TushareError as e:
            last = e
            msg = str(e)
            if '超频' in msg or '每分钟' in msg or '频率' in msg or '网络' in msg:
                time.sleep(sleep * (i + 1))
                continue
            raise
    raise last
