# -*- coding: utf-8 -*-
"""10min K 线的「多单 / 空单」分布（2026-10-20 owner）。

**需求**：K 线弹窗切到 10 分周期后，十字光标点某根 K 线 → 显示那根 K 线里
主动买（多单）与主动卖（空单）的分布。

## 数据可行性（2026-10-20 实测，别重复考古）
  · **腾讯分笔明细**（本模块用的就是它）
    `https://stock.gtimg.cn/data/index.php?appn=detail&action=data&c=sh600900&p=N`
    字段 `序号/时间/价/涨跌/量(手)/额(元)/方向(B|S|M)`，**每页 70 笔**，顺序翻页到空页为止。
    一条 keep-alive 连接抓全天实测 **68 页 / 5.56s**（600900，4749 笔）——
    ⚠️ 裸 `urlopen` 会慢 5 倍（每次重连），必须走 `http_pool`。
  · **只有当天**：`&d=` / `&date=` / `&t=` 三个日期参数实测**全被忽略**（仍回当天）；
    `action=download` 回「暂无数据」；`web.ifzq.gtimg.cn/appstock/app/dayDetail/get` 回
    "Can't load controller"；新浪历史分笔 `market.finance.sina.com.cn/downxls.php` 已下线
    （回「服务已下线」）。东财分钟资金流 `push2.eastmoney.com/.../fflow/kline/get?klt=1`
    本机**间歇性**可用（连打几次即 `RemoteDisconnected`），不能当依赖。
  ⇒ **历史 10min 桶拿不到多空分布**，除非当天就存下来。故本模块把当天结果落盘到
    `t_io/cache/order_flow/{code}_{date}.json`，**从今天起自然累积历史**（缓存键含日期）。

## 口径
  · 桶标签 = 桶**右端**时刻，与 K 线弹窗 10min 周期**同一口径**
    （`position_structure.resample_5min_to_10min`：m5 首根 09:35、两根配一对 ⇒
    标签 `09:40` 的桶覆盖 (09:30, 09:40]，下午首个桶 `13:10` 覆盖 (13:00, 13:10]）。
  · 方向：`B`=主动买（多单）、`S`=主动卖（空单）、`M`=中性（含 14:57~15:00 收盘竞价）。
    **09:25 集合竞价的成交方向也是无意义的**（腾讯标成 S），统一按中性计，不许算作空单。
  · 单笔金额分档沿用东财口径：超大单 ≥100 万 / 大单 20~100 万 / 中单 4~20 万 / 小单 <4 万。
  · ⚠️ 腾讯的"分笔"是**约 3 秒聚合的一笔**（实测 600900 全天 4749 笔 ≈ 18.5 笔/分钟），
    不是交易所逐笔 ⇒ 分档是**近似**（同一 3 秒内的多笔小单会被算成一笔大单）。
    但**量是准的**：24 根桶的分笔量合计与 K 线 10min 的 volume 逐根相等（实测 0.00% 差异）。
  · 盘后固定价格交易（15:01 起）**不属于任何 K 线桶**，直接丢弃。
"""
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

_BASE = Path(__file__).resolve().parents[1]
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

from core.market_data.http_pool import pooled_get   # noqa: E402

_CACHE_DIR = _BASE / "t_io" / "cache" / "order_flow"

_TICK_HOST = "stock.gtimg.cn"
_TICK_PATH = "/data/index.php?appn=detail&action=data&c={sym}&p={p}"
_TICK_HEADERS = {"User-Agent": "Mozilla/5.0",
                 "Referer": "https://stockapp.finance.qq.com/"}
_RE_DATA = re.compile(r"=\[(.*)\]")

_PAGE_SIZE = 70          # 腾讯分笔每页固定 70 笔（无参数可改，实测 num/size 均被忽略）
_MAX_PAGES = 400         # 全天 ~70 页；给足余量，正常不会触发
_PAGE_BATCH = 6          # 并行翻页的批宽度（6 条 keep-alive 连接，与前端在飞上限同量级）

# 单笔成交额分档（元），从大到小匹配
TIERS = ((1_000_000, "超大单"), (200_000, "大单"), (40_000, "中单"), (0, "小单"))
TIER_NAMES = tuple(n for _, n in TIERS)

_OPEN_MIN = 9 * 60 + 30      # 09:30
_LAST_MIN = 15 * 60          # 15:00（桶标签上限；15:01 起是盘后定价）


class OrderFlowError(Exception):
    pass


# ---------------------------------------------------------------- 时间分桶
def _bucket_label(hhmmss):
    """`HH:MM:SS` → 10min 桶标签（桶右端时刻）；不属于任何桶返回 None。"""
    try:
        hh, mm, ss = hhmmss.split(":")
        mins = int(hh) * 60 + int(mm)
        sec = int(ss)
    except (ValueError, AttributeError):
        return None
    if mins > _LAST_MIN:
        return None                       # 15:01 起是盘后定价，不属于任何 K 线
    if mins == _LAST_MIN:
        return "15:00"                    # 15:00:xx（收盘竞价成交）都归最后一根
    if sec > 0 or mins % 10 != 0:
        mins = (mins // 10 + 1) * 10      # 向上取到下一个 10 分钟边界
    if mins <= _OPEN_MIN:
        mins = _OPEN_MIN + 10             # 09:25 竞价 → 第一根 09:40
    if 11 * 60 + 30 < mins <= 13 * 60:
        mins = 13 * 60 + 10               # 午休 → 下午第一根 13:10
    return f"{mins // 60:02d}:{mins % 60:02d}"


def _tier_of(amount):
    for thr, name in TIERS:
        if amount >= thr:
            return name
    return "小单"


def _parse_tick(line):
    """一行分笔 → {idx,time,vol,amt,flag,label}；解析不了返回 None。"""
    f = str(line).split("/")
    if len(f) < 7:
        return None
    try:
        idx = int(f[0])
        tm = f[1]
        vol = float(f[4] or 0)            # 手
        amt = float(f[5] or 0)            # 元
    except (ValueError, IndexError):
        return None
    flag = f[6].strip().upper()
    if flag not in ("B", "S", "M"):
        return None
    label = _bucket_label(tm)
    if label is None:
        return None
    # 09:25 集合竞价：腾讯给的方向无意义，按中性计
    if tm < "09:30:00":
        flag = "M"
    return {"idx": idx, "time": tm, "vol": vol, "amt": amt,
            "flag": flag, "label": label}


# ---------------------------------------------------------------- 取数
def _index_like(code):
    """带市场前缀的指数码（sh000001 / sz399001 / bj899050…）。

    裸 6 位码 `000001` 是**平安银行**不是上证指数 ⇒ 只在带前缀时才判指数。
    """
    c = str(code).strip().lower()
    if c[:2] not in ("sh", "sz", "bj") or not c[2:].isdigit():
        return False
    return c[2:6] in ("0000", "3990", "8990", "8800")


def _tx_symbol(code):
    c = str(code).split("_")[0].strip()
    lo = c.lower()
    if lo[:2] in ("sh", "sz", "bj") and lo[2:].isdigit():
        return lo
    from core.market_data.codec import to_tx
    return to_tx(c)


def _fetch_one(sym, p, timeout):
    """取第 p 页 → 分笔行列表；网络/解析失败回 None（与"空页"区分开：空页 = 到底了）。"""
    try:
        raw = pooled_get(_TICK_HOST, _TICK_PATH.format(sym=sym, p=p), timeout, _TICK_HEADERS)
    except Exception:
        return None
    m = _RE_DATA.search(raw)
    if not m:
        return None
    parts = [x for x in m.group(1).split('"') if "/" in x]
    if not parts:
        return []
    return [t for t in parts[0].split("|") if t]


def _fetch_pages(sym, start_page=0, timeout=6.0):
    """从 `start_page` 翻页到空页 → (原始行列表, 实际有数据的页数)。

    **并行翻页**（每批 `_PAGE_BATCH` 页，批内保序）：全天 ~68 页顺抓实测 5.6s，
    是首次点击的主要等待；批内并行只加并发连接数、不加请求数。
    遇到**空页**或**不足 70 笔的短页**即判到底（后续批内结果丢弃）。
    失败即停并保留已拿到的，不抛 —— 与"失败即当作没数据"的调用语义一致。
    """
    lines, pages = [], 0
    p = max(0, int(start_page))
    ex = ThreadPoolExecutor(max_workers=_PAGE_BATCH)
    try:
        while p < _MAX_PAGES:
            idxs = list(range(p, min(p + _PAGE_BATCH, _MAX_PAGES)))
            for i, page in zip(idxs, ex.map(lambda q: _fetch_one(sym, q, timeout), idxs)):
                if page is None or not page:
                    return lines, pages
                lines.extend(page)
                pages += 1
                if len(page) < _PAGE_SIZE:      # 最后一页（不足 70 笔）
                    return lines, pages
            p += _PAGE_BATCH
    finally:
        ex.shutdown(wait=False)
    return lines, pages


# ---------------------------------------------------------------- 聚合
def _new_bucket(label):
    return {"time": label, "n": 0,
            "buy_vol": 0.0, "sell_vol": 0.0, "neutral_vol": 0.0,
            "buy_amt": 0.0, "sell_amt": 0.0,
            "tiers": {name: {"buy": 0.0, "sell": 0.0} for name in TIER_NAMES}}


def _add(bucket, tk):
    bucket["n"] += 1
    amt, vol = tk["amt"], tk["vol"]
    if tk["flag"] == "B":
        bucket["buy_vol"] += vol
        bucket["buy_amt"] += amt
        bucket["tiers"][_tier_of(amt)]["buy"] += amt
    elif tk["flag"] == "S":
        bucket["sell_vol"] += vol
        bucket["sell_amt"] += amt
        bucket["tiers"][_tier_of(amt)]["sell"] += amt
    else:
        bucket["neutral_vol"] += vol


def _round_bucket(b):
    """落盘/下发的瘦身：金额留整元、量留整手（分笔本身就是这个精度）。"""
    out = {"time": b["time"], "n": int(b["n"]),
           "buy_vol": round(b["buy_vol"]), "sell_vol": round(b["sell_vol"]),
           "neutral_vol": round(b["neutral_vol"]),
           "buy_amt": round(b["buy_amt"]), "sell_amt": round(b["sell_amt"]),
           "tiers": {k: {"buy": round(v["buy"]), "sell": round(v["sell"])}
                     for k, v in b["tiers"].items()}}
    tot = out["buy_vol"] + out["sell_vol"]
    out["buy_ratio"] = round(out["buy_vol"] / tot, 4) if tot else None
    out["net_amt"] = out["buy_amt"] - out["sell_amt"]
    return out


# ---------------------------------------------------------------- 缓存
def _cache_fp(code, date_str):
    return _CACHE_DIR / f"{code}_{date_str}.json"


def _read_cache(code, date_str):
    fp = _cache_fp(code, date_str)
    try:
        if not fp.exists():
            return None
        with open(fp, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_cache(code, date_str, rec):
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        fp = _cache_fp(code, date_str)
        tmp = fp.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False)
        os.replace(tmp, fp)
    except Exception:
        pass


def _view(rec):
    """缓存记录 → 前端契约。`bars` 按时间升序的列表（ECharts 侧不需要查表）。"""
    bars = rec.get("bars") or {}
    keys = sorted(bars)
    return {"ok": bool(keys), "code": rec.get("code"), "date": rec.get("date"),
            "saved_at": rec.get("saved_at"), "n_ticks": rec.get("n_ticks"),
            "pages": rec.get("pages"),
            "bars": [bars[k] for k in keys],
            "note": rec.get("note") or ""}


def load(code, force=False, date_str=None, timeout=6.0):
    """取某只票**当天**的 10min 多空分布。**永不抛**：失败回 `{"ok": False, ...}`。

    增量：缓存里有 `n_ticks` ⇒ 只从 `n_ticks // 70` 那页往后翻（通常 0~3 页），
    重算最后一根桶、补齐新桶；不重抓全天那 68 页。
    """
    code = str(code).split("_")[0].strip()
    today = date_str or datetime.now().strftime("%Y-%m-%d")
    rec = None if force else _read_cache(code, today)
    if rec and rec.get("date") == today:
        # 收盘后落过盘 ⇒ 当天数据已完整，直接回缓存（不再出网）
        saved_hm = str(rec.get("saved_at") or "")[11:16]
        now_hm = datetime.now().strftime("%H:%M")
        if saved_hm and saved_hm >= "15:01" and now_hm >= "15:01":
            return _view(rec)

    sym = None
    try:
        sym = _tx_symbol(code)
    except Exception:
        pass
    if not sym:
        # 代码认不出来就别出网；但**有缓存就照回**（历史档的价值不因今天认不出而消失）
        if rec:
            return _view(rec)
        return {"ok": False, "code": code, "date": today, "bars": [],
                "note": "无法识别的代码"}

    start_page = max(0, int((rec or {}).get("n_ticks") or 0) // _PAGE_SIZE)
    lines, pages = _fetch_pages(sym, start_page, timeout)
    if not lines and not rec:
        return {"ok": False, "code": code, "date": today, "bars": [],
                "note": ("指数没有分笔明细（腾讯逐笔只对个股/ETF 提供）" if _index_like(code)
                         else "分笔接口无数据（非交易日 / 停牌 / 网络）")}

    if rec:
        bars = dict(rec.get("bars") or {})
        tail_lines = list(rec.get("tail") or [])
        old_last = rec.get("last") or (max(bars) if bars else None)
    else:
        bars, tail_lines, old_last = {}, [], None

    # 尾部原始分笔与新抓到的合并（按 idx 去重：增量起点那页会与已存的尾巴重叠）
    tick_map = {}
    for ln in tail_lines + lines:
        tk = _parse_tick(ln)
        if tk:
            tick_map[tk["idx"]] = tk
    if not tick_map:
        return _view(rec) if rec else {"ok": False, "code": code, "date": today,
                                       "bars": [], "note": "分笔解析为空"}

    if old_last and old_last in bars:
        # 原来那根"最后一桶"可能又长出新成交 ⇒ 清空重算（其余桶已封闭，不动）
        bars.pop(old_last, None)

    for idx in sorted(tick_map):
        tk = tick_map[idx]
        b = bars.get(tk["label"])
        if b is None:
            b = bars[tk["label"]] = _new_bucket(tk["label"])
        _add(b, tk)
    last = max(bars) if bars else None

    # 只保留最后一根桶的原始分笔供下次增量重算（其余桶已封闭，存档即可）
    tail = [ln for ln in (tail_lines + lines)
            if (_parse_tick(ln) or {}).get("label") == last] if last else []

    # `n_ticks` 必须**单日内单调不减**：本轮若一页没抓到（网络抖动），max(tick_map) 只是
    # 尾巴那几笔的序号，直接写会把它缩回去 ⇒ 下次增量从很早的页重翻，白抓几十页。
    prev_n = int((rec or {}).get("n_ticks") or 0)
    n_ticks = max(prev_n, (max(tick_map) + 1) if tick_map else 0)
    rec_new = {"code": code, "date": today, "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "n_ticks": n_ticks,
               "pages": int((rec or {}).get("pages") or 0) + pages,
               "last": last,
               "tail": tail,
               "bars": {k: _round_bucket(v) for k, v in bars.items()}}
    _write_cache(code, today, rec_new)
    return _view(rec_new)


def available_dates(code):
    """已落盘的历史日期（升序）。历史只有**本模块上线后**的日子。"""
    code = str(code).split("_")[0].strip()
    try:
        return sorted(p.stem.split("_")[-1] for p in _CACHE_DIR.glob(f"{code}_*.json"))
    except Exception:
        return []
