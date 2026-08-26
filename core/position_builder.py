# -*- coding: utf-8 -*-
"""
position_builder.py — 建仓信号扫描（只建议、不自动执行）
用法:
  python position_builder.py                    扫描 watchlist_buy.json 中所有 monitoring 状态的股票
  python position_builder.py --code 000988      只扫一只
  python position_builder.py --date 2026-08-05  指定日期（默认今天）
  python position_builder.py --capital 500000   覆盖总资金量

数据源: t_io/minute_snapshots/{year}/{month}/{code}_{date}.json
       复用 indicators.py 的 5 分钟 MACD/BOLL/RSI 计算

建仓五条件（全部满足才建议建仓，≥70 分触发 signal）:
  1. MACD 多头: dif_5m > dea_5m 且 macd_hist_5m > 0（近 3 根 ≥2 根满足）
  2. BOLL 中轨支撑: bb_pct_5m 在 0.3~0.7 区间
  3. RSI 健康区间: rsi_5m 在 35~60
  4. 成交量缩量: 近 30 分钟均量 < 前 60 分钟均量 × 0.8
  5. 回踩支撑不破: 最新价距支撑位 ≤2% 且未破位
"""
import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# ── Windows 终端 UTF-8 编码修复 ──
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import numpy as np
import pandas as pd

# ── 路径 ──
# fix 2026-08-14: 原硬编码 E:\06_T 在非生产路径（如本仓库 checkout）下读不到 watchlist/holdings；
# 自解析到项目根（本模块位于 core/ 下，parents[1] = 项目根），生产机仍解析到 E:\06_T，行为不变。
BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from analysis.indicators import (  # noqa: F401
    resample_to_5min, add_5min_indicators, add_indicators,
    resample_to_15min, add_15min_indicators,
)


# ── 飞书推送（可选，Webhook 未配置时静默跳过）──
try:
    from config import send_feishu_payload, FEISHU_WEBHOOK, FEISHU_KEYWORD
    _FEISHU_AVAILABLE = bool(FEISHU_WEBHOOK)
except Exception:
    _FEISHU_AVAILABLE = False
    send_feishu_payload = None
    FEISHU_KEYWORD = "建仓信号"

WATCHLIST_FILE = BASE / "t_io" / "state" / "watchlist_buy.json"
SNAPSHOT_DIR = BASE / "t_io" / "minute_snapshots"
TRACE_DIR = BASE / "t_io" / "traces"
TRACE_DIR.mkdir(parents=True, exist_ok=True)


def _write_trace_line(entry: dict, date_str: str):
    """追加一行 JSONL 到当日建仓扫描日志。"""
    fp = TRACE_DIR / f"position_builder_{date_str}.jsonl"
    with open(fp, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# fix P1-4: 飞书推送去重状态文件（(code, date) 当日 signal 只推一次）
STATE_DIR = BASE / "t_io" / "state"
PUSH_DEDUP_FILE = STATE_DIR / "position_signal_pushed.json"

# W33 A1 破闸回退 (2026-08-14 用户拍板"保留结构，仅观察不推"):
#   w33_backtest.py 离线回测判建仓双通道破闸（突破通道全参数域负期望、
#   冰点放宽验证段过拟合），按"破闸即放弃回退"纪律，顶层 signal 一律降级为
#   approaching 观察，不再推飞书建仓卡片、不再出建仓建议。
#   channels 明细仍保留原始通道 verdict（含 signal），供观察积累与后续复审。
A1_BUILD_SIGNAL_GATED = True


def _load_push_dedup() -> dict:
    """读取推送去重状态 {date: [code, ...]}，失败时返回空。"""
    try:
        if PUSH_DEDUP_FILE.exists():
            return json.loads(PUSH_DEDUP_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _signal_already_pushed(code: str, date_str: str, channel: str = None) -> bool:
    """(code, date, channel) 当日是否已推送过 signal。W33 A1: 去重键带通道，防同 code 第二通道被吞。"""
    key = f"{code}:{channel or 'x'}"
    return key in _load_push_dedup().get(date_str, [])


def _mark_signal_pushed(code: str, date_str: str, channel: str = None):
    """记录 (code, date, channel) 已推送，仅保留最近 15 个日期。"""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        dedup = _load_push_dedup()
        dedup.setdefault(date_str, [])
        key = f"{code}:{channel or 'x'}"
        if key not in dedup[date_str]:
            dedup[date_str].append(key)
        dedup = {d: dedup[d] for d in sorted(dedup)[-15:]}
        PUSH_DEDUP_FILE.write_text(
            json.dumps(dedup, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ============================================================
# 数据加载
# ============================================================

def find_latest_snapshot(code: str, date_str: str = None) -> tuple:
    """查找某只股票最新的分钟快照文件。返回 (path, date_str)。"""
    if date_str:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        ym_dir = SNAPSHOT_DIR / str(dt.year) / f"{dt.month:02d}"
        candidates = [
            ym_dir / f"{code}_{date_str}.json",
            ym_dir / f"{code}_A_{date_str}.json",
            ym_dir / f"{code}_B_{date_str}.json",
            SNAPSHOT_DIR / f"{code}_{date_str}.json",
        ]
        for p in candidates:
            if p.exists():
                return p, date_str
        return None, date_str

    # 无指定日期：扫描目录找最新文件
    best_path, best_date = None, None
    for ym_dir in sorted(SNAPSHOT_DIR.glob("*/*"), reverse=True):
        if not ym_dir.is_dir():
            continue
        for fp in sorted(ym_dir.glob(f"{code}_*.json"), reverse=True):
            # 提取日期: {code}_{date}.json
            stem = fp.stem  # e.g. "000988_2026-08-05"
            parts = stem.split("_", 1)
            if len(parts) >= 2:
                d = parts[-1]
                if len(d) == 10 and d[4] == "-":
                    if not best_date or d > best_date:
                        best_path, best_date = fp, d
                    break  # 同目录下取第一个（最新日期）
    return best_path, best_date


def load_snapshot_df(code: str, date_str: str = None) -> tuple:
    """加载快照为 DataFrame + daily_context。返回 (df, daily_ctx_dict, snap_date)。
    快照缺失时尝试在线拉取腾讯分钟线。"""
    fp, snap_date = find_latest_snapshot(code, date_str)
    if fp is not None:
        return _parse_snapshot_file(fp, snap_date)

    # 快照缺失 → 在线拉取（仅当天或昨天，历史日腾讯接口无分钟数据）
    target = date_str or datetime.now().strftime("%Y-%m-%d")
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    if target not in (today, yesterday):
        return pd.DataFrame(), {}, None

    import urllib.request as _ur, os as _os
    for _k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
               "ALL_PROXY", "all_proxy"]:
        _os.environ.pop(_k, None)
    _os.environ["NO_PROXY"] = "*"
    symbol = ("sh" + code if code[0] in "56" else "sz" + code)
    try:
        url = f"https://ifzq.gtimg.cn/appstock/app/minute/query?code={symbol}"
        req = _ur.Request(url, headers={
            "User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"})
        raw = _ur.urlopen(req, timeout=8).read().decode("utf-8", errors="replace")
        data = json.loads(raw)
    except Exception:
        return pd.DataFrame(), {}, None

    symbol_data = data.get("data", {}).get(symbol) or {}
    minute_arr = symbol_data.get("data", {}).get("data") or []
    if not minute_arr:
        return pd.DataFrame(), {}, None

    # 日期来自返回数据（格式YYYYMMDD）
    raw_date = symbol_data.get("data", {}).get("date", "")
    use_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}" if len(raw_date) >= 8 else (date_str or datetime.now().strftime("%Y-%m-%d"))

    # fix P2-4: 校验响应数据日期与目标日期一致，不符视为无效数据（置 insufficient_data）
    if use_date != target:
        return pd.DataFrame(), {}, use_date

    rows = []
    for b in minute_arr:
        parts = str(b).split()
        if len(parts) < 2:
            continue
        t = use_date + " " + parts[0][:2] + ":" + parts[0][2:4]
        price = float(parts[1]) if len(parts) > 1 else 0
        vol = float(parts[2]) if len(parts) > 2 else 0
        amt = float(parts[3]) if len(parts) > 3 else 0
        rows.append({"time": t, "open": price, "high": price,
                     "low": price, "close": price, "volume": vol, "amount": amt})

    df = pd.DataFrame(rows)
    if not df.empty:
        df["time"] = pd.to_datetime(df["time"], errors="coerce")
        df = df.sort_values("time").reset_index(drop=True)
    # online fetch 无 daily_context
    return df, {}, use_date


def _parse_snapshot_file(fp: Path, snap_date: str) -> tuple:
    """解析本地分钟快照 JSON 为 DataFrame。"""
    with open(fp, "r", encoding="utf-8") as f:
        data = json.load(f)

    daily_ctx = data.get("daily_context", {}) if isinstance(data, dict) else {}

    if isinstance(data, list):
        snaps = data
    elif isinstance(data, dict):
        snaps = data.get("bars") or data.get("snapshots") or []
    else:
        snaps = []

    rows = []
    for s in snaps:
        t = s.get("time", "")
        if len(str(t)) <= 5:
            t = f"{snap_date} {t}"
        rows.append({
            "time": t,
            "open": float(s.get("open", 0) or 0),
            "high": float(s.get("high", 0) or 0),
            "low": float(s.get("low", 0) or 0),
            "close": float(s.get("close", 0) or 0),
            "volume": float(s.get("volume", 0) or 0),
            "amount": float(s.get("amount", 0) or 0),
        })

    df = pd.DataFrame(rows)
    if not df.empty:
        df["time"] = pd.to_datetime(df["time"], errors="coerce")
        df = df.sort_values("time").reset_index(drop=True)
    return df, daily_ctx, snap_date


# ============================================================
# 突破箱体检测（第一优先级条件）
# ============================================================

_DAILY_CACHE_DIR = BASE / "t_io" / "cache" / "daily_kline"


def fetch_daily_kline(code: str) -> pd.DataFrame:
    """拉腾讯日线（前复权，365天），带本地缓存（每日更新）。
    返回 {date, open, close, high, low, volume}。"""
    import urllib.request as _ur, os as _os
    from datetime import datetime as _dt
    code = str(code)
    # 本地缓存路径
    try:
        _DAILY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_fp = _DAILY_CACHE_DIR / f"{code}.json"
    except Exception:
        cache_fp = None

    # 本地有缓存且日期是今天 → 直接用
    if cache_fp and cache_fp.exists():
        try:
            cached = json.loads(cache_fp.read_text(encoding="utf-8"))
            if cached.get("date") == _dt.now().strftime("%Y-%m-%d") and cached.get("rows"):
                rows = cached["rows"]
                # fix P0-14: 缓存含当日未完成K线且距缓存时间超过15分钟 → 重新拉取（防盘中冻结）
                # fix P0-15: 盘前写入的缓存缺当日K线(_last_date<今天)，09:15后也重拉补今日K线
                _now = _dt.now()
                _today = _now.strftime("%Y-%m-%d")
                _last_date = str(rows[-1].get("date", "")) if rows else ""
                _saved_at = cached.get("saved_at")
                try:
                    _ts = _dt.strptime(_saved_at, "%Y-%m-%d %H:%M:%S") if _saved_at \
                        else _dt.fromtimestamp(cache_fp.stat().st_mtime)
                except Exception:
                    _ts = _dt.fromtimestamp(cache_fp.stat().st_mtime)
                _stale_intraday = ((_last_date < _today and _now.strftime("%H:%M") >= "09:15")
                                   or (_last_date == _today
                                       and (_now - _ts).total_seconds() > 15 * 60))
                if not _stale_intraday:
                    return pd.DataFrame(rows)
        except Exception:
            pass

    for _k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
               "ALL_PROXY", "all_proxy"]:
        _os.environ.pop(_k, None)
    _os.environ["NO_PROXY"] = "*"
    symbol = ("sh" + code if code[0] in "56" else "sz" + code)
    # 2026-08-25: 腾讯 WAF 间歇性 501 拦截不同主机（ifzq / web.ifzq 轮换），单主机失败会静默
    # 回退旧缓存 → K线图/行情显示盘中旧价而非真实收盘。多主机兜底。
    for _host in ("ifzq.gtimg.cn", "web.ifzq.gtimg.cn"):
        try:
            url = f"https://{_host}/appstock/app/fqkline/get?param={symbol},day,,,800,qfq"
            req = _ur.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                            "Referer": "https://finance.qq.com/"})
            raw = _ur.urlopen(req, timeout=8).read().decode("utf-8", errors="ignore")
            data = json.loads(raw)
            kline = data.get("data", {}).get(symbol, {}).get("day") or \
                    data.get("data", {}).get(symbol, {}).get("qfqday") or []
            rows = [{"date": i[0], "open": float(i[1]), "close": float(i[2]),
                     "high": float(i[3]), "low": float(i[4]), "volume": float(i[5])}
                    for i in kline if len(i) >= 6]
            if not rows:
                continue
            # 写缓存（每日）
            if cache_fp:
                try:
                    # fix P0-14: 记录 saved_at，供读取端判断盘中缓存是否超龄
                    cache_fp.write_text(json.dumps(
                        {"date": _dt.now().strftime("%Y-%m-%d"),
                         "saved_at": _dt.now().strftime("%Y-%m-%d %H:%M:%S"),
                         "rows": rows},
                        ensure_ascii=False), encoding="utf-8")
                except Exception:
                    pass
            return pd.DataFrame(rows)
        except Exception:
            continue
    # 全部主机失败 → 回退旧缓存
    if cache_fp and cache_fp.exists():
        try:
            cached = json.loads(cache_fp.read_text(encoding="utf-8"))
            if cached.get("rows"):
                return pd.DataFrame(cached["rows"])
        except Exception:
            pass
    return pd.DataFrame()


def _detect_boxes_simple(df: pd.DataFrame, n_keep: int = 3) -> list:
    """简化滑窗箱体检测（与 t_gui._detect_boxes 同思路）：近150日滑窗30天，
    分位数(88/12)边界 + 触及验证 + 重叠合并。返回 [{low, high, rel}]。"""
    import numpy as np
    if df.empty or len(df) < 30:
        return []
    recent = df.tail(150).reset_index(drop=True)
    closes = recent["close"].values
    highs = recent["high"].values
    lows = recent["low"].values
    n = len(recent)
    last_close = float(closes[-1])
    WIN = 30
    box_flags = np.zeros(n, dtype=bool)
    for start in range(0, n - WIN + 1, 3):
        seg = closes[start:start + WIN]
        slope = np.polyfit(np.arange(WIN), seg, 1)[0]
        rel_slope = abs(slope) / (seg.mean() or 1e-9)
        up = float(np.percentile(highs[start:start + WIN], 88))
        dn = float(np.percentile(lows[start:start + WIN], 12))
        up_touch = int(np.sum(highs[start:start + WIN] >= up * 0.992))
        dn_touch = int(np.sum(lows[start:start + WIN] <= dn * 1.008))
        w = (up - dn) / (seg.mean() or 1e-9) * 100
        if rel_slope < 0.005 and 3.0 <= w <= 22.0 and up_touch >= 2 and dn_touch >= 2:
            box_flags[start:start + WIN] = True

    boxes = {}
    i = 0
    while i < n:
        if not box_flags[i]:
            i += 1
            continue
        j = i
        while j < n and box_flags[j]:
            j += 1
        if j - i >= 20:
            up = float(np.percentile(highs[i:j], 88))
            dn = float(np.percentile(lows[i:j], 12))
            up_touch = int(np.sum(highs[i:j] >= up * 0.992))
            dn_touch = int(np.sum(lows[i:j] <= dn * 1.008))
            w = (up - dn) / (closes[i:j].mean() or 1e-9) * 100
            if 3.0 <= w <= 22.0 and up_touch >= 2 and dn_touch >= 2:
                boxes[(round(up, 3), round(dn, 3))] = {"low": round(dn, 3), "high": round(up, 3)}
        i = j

    # 合并重叠
    result = list(boxes.values())
    merged = []
    for b in result:
        hit = next((m for m in merged if
                    min(b["high"], m["high"]) - max(b["low"], m["low"]) > min(b["high"]-b["low"], m["high"]-m["low"]) * 0.5), None)
        if hit:
            hit["low"] = min(hit["low"], b["low"])
            hit["high"] = max(hit["high"], b["high"])
        else:
            merged.append(dict(b))

    # 关联现价
    for b in merged:
        if b["low"] <= last_close <= b["high"]:
            b["rel"] = 0
        elif last_close > b["high"]:
            b["rel"] = -1
        else:
            b["rel"] = -2
    # 当前箱体优先，然后按 high 排序（就近）
    merged.sort(key=lambda b: (0 if b["rel"] == 0 else 1, -b["high"]))
    return merged[:n_keep]


def _box_raw_pct(df, price) -> dict:
    """变体无关的箱体原始检测（离线重扫预计算用）：返回 {rel0:{high,pct_above}|None, rel1:{high,pct_above}|None}。"""
    if df is None or df.empty or len(df) < 30:
        return {"rel0": None, "rel1": None}
    cur = float(price) if price else float(df["close"].iloc[-1])
    boxes = _detect_boxes_simple(df)
    rel0 = None
    for b in boxes:
        if b.get("rel") == 0 and b.get("high") and cur > b["high"]:
            rel0 = {"high": b["high"], "pct_above": (cur - b["high"]) / b["high"] * 100}
            break
    rel1 = None
    prev = [b for b in boxes if b.get("rel") == -1]
    if prev:
        top = max(prev, key=lambda b: b["high"])
        if top.get("high") and cur > top["high"]:
            rel1 = {"high": top["high"], "pct_above": (cur - top["high"]) / top["high"] * 100}
    return {"rel0": rel0, "rel1": rel1}


def check_box_breakout(code: str, price: float = None, df=None,
                       min_pct: float = 0.3, max_pct: float = 8.0,
                       retest_max_pct: float = 2.0, _raw: dict = None) -> dict:
    """判定是否突破当前箱体上沿（只认 rel=0 当前箱体）。返回 {broken, box, price, pct_above}。
    W33 A4: df 可传入 as-of 日线切片（离线重扫用），缺省仍 fetch_daily_kline；
    min_pct/max_pct/retest_max_pct 供离线调参；_raw 为 _box_raw_pct 预计算结果（调参变体间复用）。"""
    if _raw is None:
        if df is None:
            df = fetch_daily_kline(code)
        if df.empty or len(df) < 30:
            return {"broken": False, "error": "无日线"}
        _raw = _box_raw_pct(df, price)
    cur = float(price) if price else 0.0
    if _raw.get("rel0"):
        pct_above = _raw["rel0"]["pct_above"]
        if min_pct <= pct_above <= max_pct:
            return {"broken": True, "box": {"high": _raw["rel0"]["high"]},
                    "price": round(cur, 3), "pct_above": round(pct_above, 2)}
        # fix P1-6: >max_pct 单独标注「强势突破」，不再静默 False
        return {"broken": False, "price": round(cur, 3),
                "near_box": {"high": _raw["rel0"]["high"]}, "pct_above": round(pct_above, 2),
                "reason": f"强势突破(>{pct_above:.1f}%)" if pct_above > max_pct else "未达突破阈值"}
    if _raw.get("rel1"):
        pct_above = _raw["rel1"]["pct_above"]
        if min_pct <= pct_above <= retest_max_pct:
            return {"broken": True, "box": {"high": _raw["rel1"]["high"]},
                    "price": round(cur, 3), "pct_above": round(pct_above, 2), "reason": "突破后回踩"}
    return {"broken": False, "price": round(cur, 3)}


def _ensure_daily_indicators(daily_ctx: dict, code: str) -> dict:
    """确保 daily_ctx 含日线 MACD/RSI/BOLL/量能字段。
    旧快照（改版前生成）缺这些字段时，用 fetch_daily_kline 现算补齐。
    返回补齐后的 daily_ctx（传入的 dict 被就地更新）。"""
    if not isinstance(daily_ctx, dict):
        daily_ctx = {}
    # W33 A1: 双通道需 daily_ma5（转向确认站上MA5）；golden/ma5 任一缺失都需 fetch 补齐
    if daily_ctx.get("daily_macd_golden") is not None and daily_ctx.get("daily_ma5") is not None:
        return daily_ctx
    try:
        df = fetch_daily_kline(code)
        if df.empty or len(df) < 30:
            return daily_ctx
        c = df["close"].astype(float)
        ema12 = c.ewm(span=12, adjust=False).mean()
        ema26 = c.ewm(span=26, adjust=False).mean()
        macd_dif = (ema12 - ema26).values
        macd_dea = pd.Series(macd_dif).ewm(span=9, adjust=False).mean().values
        macd_hist = (macd_dif - macd_dea) * 2
        d = c.diff()
        g = d.clip(lower=0).rolling(14, min_periods=1).mean()
        l = (-d.clip(upper=0)).rolling(14, min_periods=1).mean()
        rsi = (100 - 100 / (1 + (g / l.replace(0, float("nan"))))).fillna(50.0)
        boll_mid = c.rolling(20).mean()
        boll_std = c.rolling(20).std()
        boll_up = boll_mid + 2 * boll_std
        boll_dn = boll_mid - 2 * boll_std
        boll_pct = (c - boll_dn) / (boll_up - boll_dn).replace(0, float("nan"))
        vol = df["volume"].astype(float)
        vol_ma5 = vol.rolling(5).mean()
        cross_up = (pd.Series(macd_dif) > pd.Series(macd_dea)) & (pd.Series(macd_dif).shift(1) <= pd.Series(macd_dea).shift(1))
        daily_ctx["daily_macd_dif"] = float(macd_dif[-1])
        daily_ctx["daily_macd_dea"] = float(macd_dea[-1])
        daily_ctx["daily_macd_hist"] = float(macd_hist[-1])
        daily_ctx["daily_macd_golden"] = bool(cross_up.tail(5).any())
        daily_ctx["daily_rsi"] = float(rsi.iloc[-1])
        daily_ctx["daily_boll_pct"] = float(boll_pct.iloc[-1]) if pd.notna(boll_pct.iloc[-1]) else None
        daily_ctx["daily_vol_today"] = float(vol.iloc[-1])
        daily_ctx["daily_vol_ma5"] = float(vol_ma5.iloc[-1]) if pd.notna(vol_ma5.iloc[-1]) else None
        # W33 A1: MA5（转向确认站上MA5 用）
        _ma5 = c.rolling(5).mean().iloc[-1]
        daily_ctx["daily_ma5"] = float(_ma5) if pd.notna(_ma5) else None
        # 支撑位（daily_ctx 可能缺时补）
        if not daily_ctx.get("daily_support_level"):
            mav = c.rolling(20).mean().iloc[-1]
            daily_ctx.setdefault("daily_support_level", float(mav) if pd.notna(mav) else 0.0)
            daily_ctx.setdefault("daily_support_name", "MA20")
        if not daily_ctx.get("daily_price_ref"):
            daily_ctx["daily_price_ref"] = float(c.iloc[-1])
    except Exception:
        pass
    return daily_ctx


# ============================================================
# 五个建仓条件
# ============================================================

def check_macd_golden(daily_ctx: dict) -> tuple:
    """日线 MACD 金叉: 近 5 日出现 DIF 上穿 DEA（不要求当前多头）。"""
    golden = daily_ctx.get("daily_macd_golden")
    if golden is None:
        return False, "缺日线MACD数据", True
    passed = bool(golden)
    detail = f"近5日MACD金叉={'有' if passed else '无'}（需近5日出现金叉）"
    return passed, detail


def check_boll_lower(daily_ctx: dict, max_pct: float = 0.15) -> tuple:
    """日线 BOLL 接近/跌破下轨(情绪冰点): bb_pct ≤ max_pct。max_pct 供离线重扫调参。"""
    bb_pct = daily_ctx.get("daily_boll_pct")
    if bb_pct is None or (isinstance(bb_pct, float) and math.isnan(bb_pct)):
        return False, "缺日线BOLL数据", True
    passed = float(bb_pct) <= max_pct
    detail = f"日线bb_pct={float(bb_pct):.3f}（需≤{max_pct}，接近/跌破下轨）"
    return passed, detail


def check_rsi_oversold(daily_ctx: dict) -> tuple:
    """日线 RSI 超卖（W33 A1: 降为展示层，不计分）: RSI < 35。"""
    rsi_val = daily_ctx.get("daily_rsi")
    if rsi_val is None or (isinstance(rsi_val, float) and math.isnan(rsi_val)):
        return False, "缺日线RSI数据", True
    passed = float(rsi_val) < 35
    detail = f"日线rsi={float(rsi_val):.1f}（展示层，<35）"
    return passed, detail


def check_volume_shrink(daily_ctx: dict, ratio_max: float = 0.8) -> tuple:
    """日线缩量: 当日成交量 < 5 日均量 × ratio_max。ratio_max 供离线重扫调参。
    返回 (passed, detail, insufficient) — insufficient=True 表示数据不足、不参与评分。"""
    vol_today = daily_ctx.get("daily_vol_today")
    vol_ma5 = daily_ctx.get("daily_vol_ma5")
    if vol_today is None or vol_ma5 is None or vol_ma5 <= 0:
        return False, "缺日线量能数据", True
    ratio = float(vol_today) / float(vol_ma5)
    passed = ratio < ratio_max
    detail = f"日线量={float(vol_today):.0f} / 5日均量={float(vol_ma5):.0f} = {ratio:.2f}（需<{ratio_max}）"
    return passed, detail, False


# ============================================================
# W33 A1 双通道判定条件（2026-08-13）
# ============================================================

def check_turn_confirm(daily_ctx: dict) -> tuple:
    """W33 A1 冰点通道·转向确认（必要项，40分）: 近5日MACD金叉 或 收盘站上MA5（二选一即过）。"""
    golden = daily_ctx.get("daily_macd_golden")
    price_ref = daily_ctx.get("daily_price_ref")
    ma5 = daily_ctx.get("daily_ma5")
    if golden is None or price_ref is None or ma5 is None:
        return False, "缺日线MACD/MA5数据", True
    macd_ok = bool(golden)
    ma5_ok = float(price_ref) > float(ma5)
    passed = macd_ok or ma5_ok
    detail = (f"转向确认={'通过' if passed else '未过'}（金叉={macd_ok} 站上MA5={ma5_ok}，需其一）")
    return passed, detail


def check_volume_confirm(daily_ctx: dict, ratio_min: float = 1.5) -> tuple:
    """W33 A1 突破通道·放量确认（30分）: 当日量 > 5 日均量 × ratio_min（与冰点通道缩量方向相反）。"""
    vol_today = daily_ctx.get("daily_vol_today")
    vol_ma5 = daily_ctx.get("daily_vol_ma5")
    if vol_today is None or vol_ma5 is None or vol_ma5 <= 0:
        return False, "缺日线量能数据", True
    ratio = float(vol_today) / float(vol_ma5)
    passed = ratio > ratio_min
    detail = f"日线量={float(vol_today):.0f} / 5日均量={float(vol_ma5):.0f} = {ratio:.2f}（需>{ratio_min}放量）"
    return passed, detail


def check_trend_bull(daily_ctx: dict) -> tuple:
    """W33 A1 突破通道·趋势多头（30分）: 当前 DIF > DEA（当前多头态，非金叉事件）。"""
    dif = daily_ctx.get("daily_macd_dif")
    dea = daily_ctx.get("daily_macd_dea")
    if dif is None or dea is None:
        return False, "缺日线DIF/DEA数据", True
    passed = float(dif) > float(dea)
    detail = f"DIF={float(dif):.4f} / DEA={float(dea):.4f}（需DIF>DEA多头）"
    return passed, detail


# ============================================================
# 5 分钟冰点条件（盘中择时，日线冰点确认后日内再确认）
# ============================================================

def _m5_warm(df_5min, min_bars=20) -> bool:
    return df_5min is not None and not df_5min.empty and len(df_5min) >= min_bars


def check_m5_macd_golden(df_5min) -> tuple:
    """5分钟 MACD 金叉: 近5根 DIF 上穿 DEA。"""
    if not _m5_warm(df_5min) or "dif_5m" not in df_5min.columns:
        return False, "5分钟数据不足", True
    dif = df_5min["dif_5m"]
    dea = df_5min["dea_5m"]
    cross_up = (dif > dea) & (dif.shift(1) <= dea.shift(1))
    passed = bool(cross_up.tail(5).any())
    detail = f"5分钟MACD金叉={'有' if passed else '无'}（近5根）"
    return passed, detail


def check_m5_boll_lower(df_5min) -> tuple:
    """5分钟 BOLL 接近/跌破下轨: bb_pct_5m ≤ 0.15。"""
    if not _m5_warm(df_5min) or "bb_pct_5m" not in df_5min.columns:
        return False, "5分钟BOLL数据不足", True
    bb = float(df_5min["bb_pct_5m"].iloc[-1])
    passed = bb <= 0.15
    detail = f"5分钟bb_pct={bb:.3f}（需≤0.15，接近/跌破下轨）"
    return passed, detail


def check_m5_rsi_oversold(df_5min) -> tuple:
    """5分钟 RSI 超卖: rsi_5m < 30。"""
    if not _m5_warm(df_5min) or "rsi_5m" not in df_5min.columns:
        return False, "5分钟RSI数据不足", True
    rsi = df_5min["rsi_5m"].iloc[-1]
    if pd.isna(rsi):
        return False, "5分钟RSI=NaN", True
    passed = float(rsi) < 30
    detail = f"5分钟rsi={float(rsi):.1f}（需<30，超卖）"
    return passed, detail


def check_m5_volume_shrink(df_5min) -> tuple:
    """5分钟缩量: 近5根均量 < 前20根均量 × 0.8。"""
    if not _m5_warm(df_5min, min_bars=25) or "volume" not in df_5min.columns:
        return False, "5分钟量能数据不足", True
    vol = df_5min["volume"]
    recent = vol.tail(5).mean()
    prior = vol.iloc[-25:-5].mean()
    if prior <= 0:
        return False, "前段成交量为0", True
    ratio = recent / prior
    passed = ratio < 0.8
    detail = f"5分钟量比={ratio:.2f}（近5根/前20根，需<0.8）"
    return passed, detail, False


# ============================================================
# 日内右侧买点确认（W35 2026-08-25 落地）
#
# 依据 w35_intraday_confirm_experiment 两年验证（2024-08~2026-08）：
#   在时机门控 GO 的日子里，等日内右侧确认后成交 vs 收盘价成交——
#   fwd1 两年均大幅改善（+0.9%→+1.4%），maxdd5 两年均变浅（回撤保护），
#   日内平均让价为负（等确认不追贵）。fwd5 样本外打平（长期弹性有小代价），
#   故做成「闸门 + 例外出口」：GO 且确认→signal；GO 未确认→approaching(待日内确认)，不丢弃。
# 判据（与实验 find_confirm_entry 同口径，但取截止当前最新已收盘 15m bar，不回溯首个）：
#   15m close > ema_fast_15m(EMA8) 且 vol_ratio_15m > vol_min 且 close >= 当日累计VWAP。
# ============================================================

def check_intraday_confirm(df_1min, vol_min: float = 1.2) -> tuple:
    """当日盘中右侧买点确认。返回 (passed, detail, insufficient)。

    无未来函数：只用截止最新一根【已收盘】15m bar 的数据；未收盘的当前根不参与。
    df_1min 为当日 1 分钟线（intraday 快照）。数据不足时 insufficient=True（不视为未确认）。
    """
    if df_1min is None or df_1min.empty or len(df_1min) < 20:
        return False, "日内分钟数据不足", True
    d = df_1min.copy()
    d["time"] = pd.to_datetime(d["time"], errors="coerce")
    d = d.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)
    # 当日累计 VWAP（Σamount/Σvol；缺 amount 用 close*vol 代理）
    if "amount" in d.columns and d["amount"].fillna(0).sum() > 0:
        cum_amt = d["amount"].fillna(0).cumsum()
    else:
        cum_amt = (d["close"] * d["volume"].fillna(0)).cumsum()
    cum_vol = d["volume"].fillna(0).cumsum().replace(0, np.nan)
    d["vwap_cum"] = cum_amt / cum_vol

    df15 = add_15min_indicators(resample_to_15min(d))
    if df15 is None or df15.empty:
        return False, "15分钟数据不足", True
    df15 = df15.copy()
    df15["time"] = pd.to_datetime(df15["time"], errors="coerce")
    last_min_ts = d["time"].iloc[-1]
    # 取最新一根【已收盘】15m bar（收盘时刻 <= 当日最新分钟+1min）
    closed = df15[(df15["time"] + pd.Timedelta(minutes=15)) <= (last_min_ts + pd.Timedelta(minutes=1))]
    if closed.empty:
        return False, "尚无已收盘15分钟bar", True
    bar = closed.iloc[-1]
    c = bar.get("close")
    ema8 = bar.get("ema_fast_15m")
    volr = bar.get("vol_ratio_15m")
    if any(pd.isna(x) for x in (c, ema8, volr)):
        return False, "15分钟指标NaN", True
    close_ts = bar["time"] + pd.Timedelta(minutes=15)
    vw_rows = d[d["time"] <= close_ts]
    vwap = float(vw_rows["vwap_cum"].iloc[-1]) if (not vw_rows.empty and pd.notna(vw_rows["vwap_cum"].iloc[-1])) else None
    ema_ok = float(c) > float(ema8)
    vol_ok = float(volr) > vol_min
    vwap_ok = (vwap is None) or (float(c) >= vwap)
    passed = ema_ok and vol_ok and vwap_ok
    detail = (f"15分钟确认: 站上EMA8={ema_ok}(c={float(c):.3f}/ema8={float(ema8):.3f}) "
              f"放量={vol_ok}(量比{float(volr):.2f}>{vol_min}) 站上VWAP={vwap_ok}"
              f"{f'(vwap={vwap:.3f})' if vwap is not None else ''}")
    return passed, detail, False


# ============================================================
# 卡点量化（GUI 直观化 2026-08-25）：不只显示"未过"，还算出"差多少"
# ============================================================

def build_blockers(regime, feats, dd, dir_ok, trend_ok, dd_ok, golden_ok,
                   intraday_confirm=None, scan_type="manual") -> tuple:
    """汇总所有未过的【必要条件】及其量化差距，返回 (block_reason, blockers)。

    blockers: [{key, label, gap_txt, need, cur}]，按判定顺序（regime→trend→drawdown→日内确认）。
    block_reason: 第一个卡住的必要条件的一句话（含差多少）。全部通过 → (None, [])。
    金叉是加分项，不计入 blockers（不卡 signal）。
    """
    f = feats or {}
    price = f.get("price")
    ma20 = f.get("ma20")
    ma60 = f.get("ma60")
    blockers = []

    # 1) 市场方向（regime 必须 trend_up/trend_dn）
    if not dir_ok:
        blockers.append({
            "key": "t_regime", "label": "市场有方向",
            "gap_txt": f"当前震荡市(指数在MA60±缓冲带内)，signal 结构性不可达",
            "need": "指数站上MA60×1.005(多头) 或 跌破MA60×0.97(空头)", "cur": regime,
        })

    # 2) 多头结构（价 > MA20 且 > MA60）——仅多头趋势要求
    if regime == "trend_up" and not trend_ok:
        parts = []
        if price is not None and ma20 and price <= ma20:
            parts.append(f"距MA20差{(price/ma20-1)*100:+.2f}%")
        if price is not None and ma60 and price <= ma60:
            parts.append(f"距MA60差{(price/ma60-1)*100:+.2f}%")
        blockers.append({
            "key": "t_trend", "label": "多头结构",
            "gap_txt": ("，".join(parts) if parts else "价未站上MA20/MA60"),
            "need": "价同时站上MA20和MA60", "cur": f"价{price}",
        })

    # 3) 回撤到位
    if not dd_ok:
        if regime == "trend_dn":
            need_txt = "深回撤<-10%"
            gap = f"当前回撤{dd:+.1%}，距-10%还差{(dd-(-0.10))*100:+.1f}pp"
        else:
            need_txt = "浅回撤≥-3%"
            gap = f"当前回撤{dd:+.1%}，距-3%还差{((-0.03)-dd)*100:+.1f}pp"
        blockers.append({
            "key": "t_drawdown", "label": "回撤到位",
            "gap_txt": gap, "need": need_txt, "cur": f"{dd:+.1%}",
        })

    # 4) 日内右侧确认（仅 intraday 且前三项已过时才可能成为卡点）
    if scan_type == "intraday" and dir_ok and (regime != "trend_up" or trend_ok) and dd_ok \
            and intraday_confirm and not intraday_confirm.get("insufficient") \
            and not intraday_confirm.get("passed"):
        blockers.append({
            "key": "intraday_confirm", "label": "日内确认",
            "gap_txt": intraday_confirm.get("detail") or "15分钟右侧确认未过",
            "need": "15m站上EMA8+放量+站上VWAP", "cur": "未确认",
        })

    block_reason = None
    if blockers:
        b0 = blockers[0]
        block_reason = f"卡「{b0['label']}」：{b0['gap_txt']}"
    return block_reason, blockers


# ============================================================
# 综合评分 & 仓位计算
# ============================================================

def compute_score(conditions: dict) -> int:
    """每满足一个条件 +20 分，满分 100。
    fix P0-7: 数据不足的条件（如缩量预热期）返回 passed=False 并在 conditions 中标注
    insufficient=True，不再恒真送分；得分分母保持 5 条件口径不变，前端可据此区分。"""
    return sum(20 for passed, *_ in conditions.values() if passed)


HOLDINGS_FILE = STATE_DIR / "holdings.json"
_HOLDINGS_CACHE = {"mtime": None, "codes": set()}


def _load_holding_codes() -> set:
    """fix 仓位一刀切(A1-A6): 读取 holdings.json 的已持仓代码集合（剥离 _A/_B 账户后缀）。
    按文件 mtime 缓存，避免逐股重复读盘；读取失败时返回空集合（不排除任何股）。"""
    try:
        mtime = HOLDINGS_FILE.stat().st_mtime
        if _HOLDINGS_CACHE["mtime"] == mtime:
            return _HOLDINGS_CACHE["codes"]
        with open(HOLDINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        codes = set()
        for key, val in (data.items() if isinstance(data, dict) else []):
            if not isinstance(val, dict):
                continue
            if not (val.get("qty") or 0):
                continue  # fix 2026-08-20: 已清仓(qty=0)不算持仓
            base_code = str(key)
            if base_code.endswith(("_A", "_B")):
                base_code = base_code[:-2]
            codes.add(base_code)
        _HOLDINGS_CACHE["mtime"] = mtime
        _HOLDINGS_CACHE["codes"] = codes
        return codes
    except Exception:
        return set()


def compute_position(latest_price: float, total_capital: float,
                     max_per_stock_pct: float, code: str = None,
                     position_gap: dict = None) -> dict:
    """计算建议股数（按手取整，不足一手则为 0）。

    W33 A3: suggested_qty = min(欠配缺口股数, 总资金×个股目标比例÷价格÷3) 取整一手，
    与仓位管理器"欠配分3次加仓"节奏一致。position_gap 由 config.build_position_gap 产出
    （ratio_sum + rows）；无目标配置的候选股回落全局默认 0.30。
    """
    # 个股目标比例（STOCK_PARAMS stock_qty_base_pct 或全局 0.30，与仓位管理器同源）
    raw_pct = 0.30
    try:
        from config import STOCK_PARAMS
        raw_pct = float(STOCK_PARAMS.get(code, {}).get("stock_qty_base_pct", 0.30))
    except Exception:
        pass
    ratio_sum = float(position_gap.get("ratio_sum")) if position_gap and position_gap.get("ratio_sum") else 1.0
    target_pct = raw_pct / ratio_sum  # 归一化（未持仓候选回落全局默认，无 position_gap 时 ratio_sum=1 → target_pct=raw_pct）
    target_val = total_capital * target_pct
    if latest_price > 0:
        gap_qty = int(target_val / latest_price // 100) * 100     # 欠配缺口（未持仓 mkt_val=0 → 全量目标）
        batch_qty = int(target_val / latest_price / 3 // 100) * 100  # 分3次节奏的 1/3 批
        raw_qty = max(min(gap_qty, batch_qty), 0)
    else:
        raw_qty = 0
    required = raw_qty * latest_price
    return {
        "suggested_qty": raw_qty,
        "suggested_price": round(latest_price, 3),
        "capital_required": round(required, 2),
        "max_capital_per_stock": round(target_val, 2),
    }


# ============================================================
# W33 A1 双通道判定（scan_stock 与 A4 离线重扫共用，单一真源）
#
# ⚠️ 验收状态（2026-08-14）：W33 A4 离线闸门未过（t_io/validation/w33_offline_rescan_report.md）——
#   冰点/突破 3日胜率与假阳性均不达标，8 组参数变体全 FAIL。
#   按 W33 纪律「破闸即放弃，不强行上线」：本判据为【参考级·未验收】，信号不得作为已验证建仓策略依赖。
#   保持实现以便后续样本扩大/参数重构后重验；B2/J6/A3 为独立已验证项不受影响。
# ============================================================

# 双通道扁平条件键（固定顺序，前端圆点列/COND_LABELS 消费）
CHANNEL_COND_KEYS = [
    "c1_turn_confirm", "c1_boll_lower", "c1_volume_shrink", "c1_rsi_oversold",
    "c1_m5_iceberg", "c2_box_breakout", "c2_volume_confirm", "c2_trend_bull",
]
CHANNEL_COND_LABELS = {
    "c1_turn_confirm": "转向确认", "c1_boll_lower": "BOLL冰点", "c1_volume_shrink": "缩量止跌",
    "c1_rsi_oversold": "RSI超卖(展示)", "c1_m5_iceberg": "5分钟冰点",
    "c2_box_breakout": "突破箱体", "c2_volume_confirm": "放量确认", "c2_trend_bull": "趋势多头",
}
# 冰点评分键（转向40 + BOLL20 + 缩量20 = 80 signal / 60 approaching）
C1_SCORED_KEYS = ("c1_turn_confirm", "c1_boll_lower", "c1_volume_shrink")
# 突破评分键（箱体40 + 放量30 + 多头30 = signal≥70）
C2_SCORED_KEYS = ("c2_box_breakout", "c2_volume_confirm", "c2_trend_bull")
_ICE_WEIGHTS = {"c1_turn_confirm": 40, "c1_boll_lower": 20, "c1_volume_shrink": 20}
_BREAK_WEIGHTS = {"c2_box_breakout": 40, "c2_volume_confirm": 30, "c2_trend_bull": 30}


def eval_dual_channels(code: str, daily_ctx: dict, df_1min, scan_type: str, price,
                       box_df=None, opts: dict = None):
    """W33 A1+A2: 双通道建仓判定。返回 {
      channels: {iceberg:{name,verdict,score,approach_status,conditions}, breakout:{...}},
      channel, approach_status, conditions(8键扁平), composite_score }
    通道一 冰点反转（左侧）: 转向确认(必要40) + BOLL冰点(20) + 缩量(20)；RSI<35 仅展示不计分。
       signal = 转向+冰点2项全过(80)；approaching = 转向+冰点1项(60)。
    通道二 突破跟随（右侧）: 突破箱体(40) + 放量>1.5(30) + DIF>DEA(30)；signal≥70 / approaching 40~69。
    A2 口径: intraday 且 m5 冰点过 → 冰点 signal/即时可建；否则待日内确认；eod → 冰点恒 approaching/待次日盘中确认。
    box_df: 离线重扫传入 as-of 日线切片（防箱体 look-ahead），缺省用实时全量 kline。
    opts: 离线调参覆盖 {boll_ice_max, vol_shrink_ratio, vol_confirm_ratio, box_min_pct, box_max_pct,
           breakout_signal_mode:"any"|"all"}（缺省=生产默认）。
    """
    daily_ctx = daily_ctx or {}
    opts = opts or {}
    c1 = {}
    turn = check_turn_confirm(daily_ctx)
    boll = check_boll_lower(daily_ctx, opts.get("boll_ice_max", 0.15))
    shrink = check_volume_shrink(daily_ctx, opts.get("vol_shrink_ratio", 0.8))
    rsi = check_rsi_oversold(daily_ctx)
    c1["c1_turn_confirm"] = turn
    c1["c1_boll_lower"] = boll
    c1["c1_volume_shrink"] = shrink
    c1["c1_rsi_oversold"] = rsi
    turn_p = bool(turn[0]); boll_p = bool(boll[0]); shrink_p = bool(shrink[0])
    ice_hits = int(boll_p) + int(shrink_p)

    c2 = {}
    bx = check_box_breakout(code, price, df=box_df,
                            min_pct=opts.get("box_min_pct", 0.3),
                            max_pct=opts.get("box_max_pct", 8.0),
                            _raw=opts.get("_box_raw"))
    box_passed = bool(bx.get("broken"))
    box_detail = (f"突破箱体上沿 {bx.get('box', {}).get('high')}，超出 {bx.get('pct_above')}%"
                  if box_passed else (f"{bx.get('reason')}（{bx.get('pct_above')}%）" if bx.get('reason') else "未突破箱体"))
    volc = check_volume_confirm(daily_ctx, opts.get("vol_confirm_ratio", 1.5))
    trend = check_trend_bull(daily_ctx)
    c2["c2_box_breakout"] = (box_passed, box_detail)
    c2["c2_volume_confirm"] = volc
    c2["c2_trend_bull"] = trend

    # 5 分钟冰点（intraday 择时层；eod 统一"待次日盘中确认"）
    m5_iceberg = False
    m5_detail = "待次日盘中确认" if scan_type != "intraday" else "待日内确认"
    if scan_type == "intraday" and df_1min is not None and not df_1min.empty and len(df_1min) >= 30:
        df_5min = resample_to_5min(df_1min)
        df_5min = add_5min_indicators(df_5min)
        m5_conditions = {
            "m5_macd_golden": check_m5_macd_golden(df_5min),
            "m5_boll_lower": check_m5_boll_lower(df_5min),
            "m5_rsi_oversold": check_m5_rsi_oversold(df_5min),
            "m5_volume_shrink": check_m5_volume_shrink(df_5min),
        }
        m5_score = compute_score(m5_conditions)
        m5_iceberg = m5_score >= 70
        m5_detail = f"5分钟冰点{'=通过' if m5_iceberg else f'={m5_score}/80未过'}"
    c1["c1_m5_iceberg"] = (m5_iceberg, m5_detail)

    # 通道一 verdict（须转向确认才够格）
    c1_score = sum(_ICE_WEIGHTS[k] for k, p in ((k, c1[k][0]) for k in _ICE_WEIGHTS) if p)
    c1_verdict = "weak"; c1_status = None
    if turn_p and ice_hits == 2:
        if scan_type == "intraday" and m5_iceberg:
            c1_verdict = "signal"; c1_status = "immediate"
        elif scan_type == "intraday":
            c1_verdict = "approaching"; c1_status = "intraday_pending"
        else:
            c1_verdict = "approaching"; c1_status = "next_day_pending"
    elif turn_p and ice_hits == 1:
        c1_verdict = "approaching"
        c1_status = "intraday_pending" if scan_type == "intraday" else "next_day_pending"

    # 通道二 verdict（不受 scan_type 门控）
    c2_score = sum(_BREAK_WEIGHTS[k] for k, p in ((k, c2[k][0]) for k in _BREAK_WEIGHTS) if p)
    # opts breakout_signal_mode: "all"=箱体+放量+多头全过(100) 才 signal；缺省 "any"=箱体+其一(≥70)
    c2_sig_min = 100 if opts.get("breakout_signal_mode") == "all" else 70
    if c2_score >= c2_sig_min:
        c2_verdict = "signal"
    elif c2_score >= 40:
        c2_verdict = "approaching"
    else:
        c2_verdict = "weak"

    # 汇总
    conditions = {}
    for k in CHANNEL_COND_KEYS:
        if k in c1:
            v = c1[k]
        elif k in c2:
            v = c2[k]
        else:
            continue
        cond = {"passed": bool(v[0]), "detail": v[1]}
        if len(v) > 2 and v[2]:
            cond["insufficient"] = True
        conditions[k] = cond

    iceberg_verdicts = {"signal": 3, "approaching": 2, "weak": 1}.get(c1_verdict, 0)
    breakout_verdicts = {"signal": 3, "approaching": 2, "weak": 1}.get(c2_verdict, 0)
    if iceberg_verdicts >= breakout_verdicts and iceberg_verdicts > 1:
        channel = "iceberg" if iceberg_verdicts > breakout_verdicts else "both"
        verdict = "signal" if c1_verdict == "signal" else "approaching"
    elif breakout_verdicts > 1:
        channel = "breakout"
        verdict = "signal" if c2_verdict == "signal" else "approaching"
    else:
        channel = None; verdict = "weak"

    # W33 A1 破闸回退: 顶层 signal 降级为 approaching（仅观察不推），
    # 原始通道 verdict 保留在 channels.*.verdict 供观察积累与后续复审。
    gated_from = None
    if A1_BUILD_SIGNAL_GATED and verdict == "signal":
        gated_from = verdict
        verdict = "approaching"

    return {
        "channels": {
            "iceberg": {"name": "冰点反转", "verdict": c1_verdict, "score": c1_score,
                        "approach_status": c1_status,
                        "conditions": {k: bool(conditions[k]["passed"]) for k in conditions if k.startswith("c1_")}},
            "breakout": {"name": "突破跟随", "verdict": c2_verdict, "score": c2_score,
                         "conditions": {k: bool(conditions[k]["passed"]) for k in conditions if k.startswith("c2_")}},
        },
        "channel": channel,
        "verdict": verdict,
        "approach_status": c1_status,
        "conditions": conditions,
        "composite_score": max(c1_score, c2_score),
        "gated": A1_BUILD_SIGNAL_GATED and gated_from is not None,
        "gated_from": gated_from,
    }


# ============================================================
# 主扫描逻辑
# ============================================================

def scan_stock(code: str, stock_info: dict, date_str: str = None,
               total_capital: float = 300000, max_pct: float = 0.2,
               allow_stale: bool = False, scan_type: str = "manual",
               position_gap: dict = None) -> dict:
    """扫描单只股票，返回结果字典。
    allow_stale=True 时允许分钟快照陈旧（盘后重跑，日线判断不依赖分钟快照）。
    scan_type: 'intraday' 需日线+5分钟冰点两级；'eod'/'manual' 盘后只看日线冰点。"""
    result = {
        "code": code,
        "name": stock_info.get("name", code),
        "date": None,
        "latest_price": None,
        "conditions": {},
        "composite_score": 0,
        "score_ceiling": 100,     # P1(2026-08-25): 当前 regime 下 score 上限（range=70，signal 不可达）
        "signal_reachable": True, # P1(2026-08-25): 当前 regime 下 signal 是否结构性可达
        "verdict": "insufficient_data",
        "channel": None,          # W33 A1: 触发通道 iceberg/breakout/both/None
        "approach_status": None,  # W33 A2: immediate/intraday_pending/next_day_pending
        "channels": {},           # W33 A1: 双通道明细 {iceberg:{...}, breakout:{...}}
        "gated": False,           # W33 A1 破闸: 顶层 signal 被降级为 approaching 标记
        "gated_from": None,       # 被降级前的原始 verdict（signal）
        "intraday_confirm": None, # W35(2026-08-25): 日内右侧确认 {passed,detail,insufficient}（仅intraday go时）
        "block_reason": None,     # GUI直观化(2026-08-25): 一句话卡点（第一个未过必要条件+差多少）
        "blockers": [],           # GUI直观化(2026-08-25): 全部未过必要条件的差距清单
        "position": None,
        "note": None,
        "errors": [],
    }

    # 加载数据
    df_1min, daily_ctx, snap_date = load_snapshot_df(code, date_str)
    # fix P1-7/P2-4: 快照（或在线数据）日期≠目标日期 → 强制 insufficient_data，防陈旧快照出 signal
    # 盘后重跑(allow_stale=True)时允许陈旧：日线判断用 fetch_daily_kline 独立拉取，与分钟快照时间无关
    target_date = date_str or datetime.now().strftime("%Y-%m-%d")
    if not allow_stale and snap_date and snap_date != target_date:
        result["date"] = snap_date
        result["errors"].append(f"快照陈旧({snap_date})")
        return result
    # 日线判断需 daily_ctx；无分钟快照的候选股用日线独立构建（盘后重跑依赖此路径）
    if not daily_ctx:
        daily_ctx = {}
    _ensure_daily_indicators(daily_ctx, code)

    # 30/60分钟线背离检测（2026-08-19 新增）：tushare 多日数据；当日缓存；失败返回 {}
    # 2026-08-19 验证后改为 detail 版（含连续标记），divergence 保留简版兼容前端/JSONL
    result["divergence"] = {}
    result["divergence_detail"] = {}
    try:
        from analysis.divergence import detect_minute_divergence_detail as _det_div
        result["divergence_detail"] = _det_div(code)
        result["divergence"] = {k: v["type"] for k, v in result["divergence_detail"].items()}
    except Exception:
        pass

    result["date"] = snap_date
    # 展示/箱体突破用实时价；日线五条件用 daily_price_ref（日线收盘/参考价）
    live_price = round(float(df_1min["close"].iloc[-1]), 3) if not df_1min.empty else None
    result["latest_price"] = live_price or round(float(daily_ctx.get("daily_price_ref") or 0), 3)

    # W33 A1+A2: 双通道判定（冰点反转 + 突破跟随），5 分钟层为择时加分项非闸门
    dc = eval_dual_channels(code, daily_ctx, df_1min, scan_type, result["latest_price"])
    result["channels"] = dc["channels"]
    result["channel"] = dc["channel"]
    result["approach_status"] = dc["approach_status"]
    result["verdict"] = dc["verdict"]
    result["composite_score"] = dc["composite_score"]
    result["conditions"] = dc["conditions"]
    result["gated"] = dc.get("gated", False)
    result["gated_from"] = dc.get("gated_from")

    # 方案A (2026-08-15 用户拍板): 建仓信号 = 时机门控 GO（替代永不触发的冰点双通道）。
    # 时机判定：多头趋势→追强(多头结构+浅回撤≥-3%)；空头趋势→抄底(深回撤<-10%)；震荡→降频。
    # 旧双通道结果保留在 result["channels"] 供参考，verdict/conditions 由时机判定驱动。
    result["timing"] = {"regime": None, "go": None, "reason": "未启用"}
    try:
        from core.timing_gate import timing_verdict as _timing_verdict
        from config import ENTRY_TIMING_PARAMS as _ETP
        if _ETP.get("enabled", True):
            _tv = _timing_verdict(code, target_date)
            _f = _tv.get("features") or {}
            _regime = _tv.get("regime", "range")
            _dir_ok = _regime in ("trend_up", "trend_dn")
            _trend = bool(_f.get("trend_multihead"))
            _dd = float(_f.get("drawdown") or 0.0)
            if _regime == "trend_up":
                _dd_ok = _dd >= -0.03
            elif _regime == "trend_dn":
                _dd_ok = _dd < -0.10
            else:
                # B-4(2026-08-21): range 市观察态回撤单独算——用多头口径 dd>=-3%(非硬编码 False)
                _dd_ok = _dd >= -0.03
            _golden = bool(_f.get("macd_golden_5d"))
            result["conditions"] = {
                "t_regime": {"passed": _dir_ok, "detail": f"市场状态:{_regime}(需多头/空头非震荡)"},
                "t_trend": {"passed": _trend, "detail": f"多头结构(价>MA20&MA60)={'是' if _trend else '否'}"},
                "t_drawdown": {"passed": _dd_ok, "detail": f"回撤到位({_dd:+.1%}，{'多头≥-3%' if _regime=='trend_up' else '空头<-10%'})"},
                "t_golden": {"passed": _golden, "detail": f"MACD金叉近5日={'是' if _golden else '否'}(加分)"},
            }
            _score = (30 if _dir_ok else 0) + (30 if _trend else 0) + (30 if _dd_ok else 0) + (10 if _golden else 0)
            if _tv.get("go"):
                _v = "signal"
            elif _regime == "range" and _trend and _dd_ok:
                # B-4(2026-08-21): range 市观察态 watch_signal——多头结构+浅回撤，
                # 只进 trace/C18 清单喂样本，不推飞书不出 position 建议
                _v = "watch_signal"
            elif _dir_ok and (_trend or _dd_ok):
                _v = "approaching"
            else:
                _v = "weak"
            result["verdict"] = _v
            result["composite_score"] = _score
            # P1(2026-08-25): verdict 与 score 脱钩修复。
            #   signal 唯一来源是 go=true，而 go 要求 regime∈{trend_up,trend_dn}（timing_gate:go 定义）。
            #   range 市 t_regime 恒 False → signal 结构性不可达，但 score 仍可达 70(trend30+dd30+golden10)，
            #   在 0~100 隐含刻度下"70 分"看起来像"离 signal 一步之遥"，实为天花板。
            #   显式给出：signal_reachable(当前 regime 下 signal 是否可能出现) 与
            #   score_ceiling(当前 regime 下 score 的上限)，供 GUI/trace 诚实展示，不改 go/verdict 逻辑本身。
            _signal_reachable = _dir_ok  # 仅 trend_up/trend_dn 时 signal 可达
            _score_ceiling = 100 if _signal_reachable else 70  # range: t_regime 那 30 分锁死，上限 70
            result["signal_reachable"] = bool(_signal_reachable)
            result["score_ceiling"] = int(_score_ceiling)
            result["channel"] = None
            result["approach_status"] = "immediate" if _v == "signal" else None
            result["gated"] = False
            result["gated_from"] = None
            result["timing"] = {"regime": _regime, "go": _tv["go"], "reason": _tv["reason"]}

            # W35(2026-08-25) 日内右侧确认闸门 + 例外出口（两年验证：回撤保护稳健，fwd5 样本外打平）。
            #   仅 intraday 扫描且 go=true 时生效：确认通过→保持 signal(即时可建)；
            #   未确认→降级 approaching + intraday_pending(待日内确认，不丢弃，收盘前/次日可再触发)。
            #   eod/manual 拿不到当日完整分钟线，不套此闸门（维持原 signal，盘后不惩罚）。
            result["intraday_confirm"] = None
            if _ETP.get("intraday_confirm_gate", True) and _v == "signal" and scan_type == "intraday":
                _cf_pass, _cf_detail, _cf_insuf = check_intraday_confirm(
                    df_1min, vol_min=float(_ETP.get("intraday_confirm_vol_min", 1.2)))
                result["intraday_confirm"] = {
                    "passed": bool(_cf_pass), "detail": _cf_detail,
                    "insufficient": bool(_cf_insuf),
                }
                if _cf_insuf:
                    # 数据不足：不惩罚，维持 signal（与盘后同等对待），但标注待确认
                    result["approach_status"] = "intraday_pending"
                elif not _cf_pass:
                    # GO 但日内未确认 → 降级观察，不出建仓建议
                    result["verdict"] = "approaching"
                    result["approach_status"] = "intraday_pending"

            # GUI 直观化(2026-08-25)：算"卡在哪、差多少"。signal 时无卡点。
            if result["verdict"] == "signal":
                result["block_reason"] = None
                result["blockers"] = []
            else:
                _br, _bk = build_blockers(_regime, _f, _dd, _dir_ok, _trend, _dd_ok, _golden,
                                          intraday_confirm=result.get("intraday_confirm"),
                                          scan_type=scan_type)
                result["block_reason"] = _br
                result["blockers"] = _bk
    except Exception as _te:
        pass  # timing_gate 故障时保留 W33 双通道判定

    # 仓位计算
    # fix 仓位一刀切(A1-A6): 仅 verdict=signal 才出建仓建议；已持仓股不再给"建仓"建议（防重复建仓）
    # W33 A3: 股数对齐仓位管理器（欠配缺口与分3次节奏）；欠配>5% 持仓股标注"欠配补仓候选"
    if result["verdict"] != "signal":
        result["position"] = None
    elif code in _load_holding_codes():
        result["position"] = None
        _gap_row = None
        if position_gap:
            _gap_row = next((x for x in position_gap.get("rows", []) if x.get("code") == code), None)
        if _gap_row and _gap_row.get("under"):
            result["note"] = f"欠配补仓候选（欠配 {_gap_row.get('gap_pct', 0):.1f}%），已持仓不出建仓建议"
        else:
            result["note"] = "已持仓，不出建仓建议（如需加仓请走加仓观察）"
    else:
        result["position"] = compute_position(
            result["latest_price"], total_capital, max_pct,
            code=code, position_gap=position_gap
        )

    return result


# ============================================================
# 飞书推送
# ============================================================

# 方案A (2026-08-15 用户拍板): 建仓信号改用时机门控 GO（替代永不触发的冰点双通道）。
# GUI/圆点列/trace 消费的新条件标签（t_* 键），与 scan_stock 实际判定一致。
TIMING_COND_LABELS = {
    "t_regime": "市场有方向",
    "t_trend": "多头结构",
    "t_drawdown": "回撤到位",
    "t_golden": "MACD金叉(加分)",
}
# W33 A1: 旧双通道 8 键标签（channels 参考保留，不再驱动建仓 verdict）
COND_LABELS = dict(TIMING_COND_LABELS)
_CHANNEL_COND_LABELS = dict(CHANNEL_COND_LABELS)


def build_signal_card(result: dict) -> dict:
    """构建建仓信号飞书卡片。"""
    code = result["code"]
    name = result["name"]
    score = result["composite_score"]
    pos = result.get("position") or {}
    conditions = result.get("conditions", {})

    # 条件清单
    cond_lines = []
    for key, label in COND_LABELS.items():
        cond = conditions.get(key, {})
        passed = cond.get("passed", False)
        icon = "✅" if passed else "❌"
        cond_lines.append(f"{icon} {label}：{cond.get('detail', '')}")

    cond_text = "\n".join(cond_lines)

    # W33 A1: 通道标注 + 择时状态
    _channel = result.get("channel")
    _ch_name = ""
    if _channel == "both":
        _ch_name = "冰点反转 + 突破跟随"
    elif _channel == "iceberg":
        _ch_name = "冰点反转"
    elif _channel == "breakout":
        _ch_name = "突破跟随"
    _status_txt = {"immediate": "即时可建", "intraday_pending": "待日内确认",
                   "next_day_pending": "待次日盘中确认"}.get(result.get("approach_status"), "")

    lines = [
        f"**{name}（{code}）** 建仓信号触发",
        "",
        f"📅 日期：{result.get('date') or 'N/A'}",
        f"📊 综合得分：**{score}** ｜ 通道：**{_ch_name or '—'}**{f'｜{_status_txt}' if _status_txt else ''}",
        f"💵 最新价：{result.get('latest_price')}",
    ]
    # fix 仓位一刀切(A1-A6): 已持仓等无仓位建议的信号，卡片显式标注原因
    if result.get("note"):
        lines.append(f"📌 备注：{result['note']}")
    lines += [
        "",
        "**条件检查：**",
        cond_text,
        "",
        f"💰 建议买入：**{pos.get('suggested_qty', 0)} 股** @ {pos.get('suggested_price', '?')}",
        f"💳 所需资金：**{pos.get('capital_required', 0):,.0f}**（单只上限 {pos.get('max_capital_per_stock', 0):,.0f}）",
        "",
        "⚠️ 以上仅为系统建议，请人工确认后手动加入 holdings.json",
    ]

    markdown = "\n".join(lines)

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "template": "blue",
                "title": {"tag": "plain_text", "content": f"🏗️ 建仓信号 - {name}({code})"},
            },
            "elements": [{"tag": "markdown", "content": markdown}],
        },
    }


def push_signal_feishu(result: dict, dry_run: bool = False) -> bool:
    """推送建仓信号到飞书。返回是否成功。"""
    if not _FEISHU_AVAILABLE:
        return False
    if dry_run:
        print(f"  [DRY-RUN] 跳过飞书推送: {result['code']} {result['name']}")
        return False
    card = build_signal_card(result)
    return send_feishu_payload(
        card,
        success_log=f"建仓信号飞书推送成功: {result['code']} {result['name']}",
        error_prefix=f"建仓信号飞书推送({result['code']})",
    )


VERDICT_ICON = {"signal": "🔴", "approaching": "🟡", "weak": "⚪", "insufficient_data": "⬜"}


def build_summary_card(results: list, date_str: str = "") -> dict:
    """构建盘后建仓扫描汇总飞书卡片。"""
    if not results:
        return None

    lines = [f"**建仓扫描汇总**  {date_str}", ""]

    # 统计
    signal_count = sum(1 for r in results if r.get("verdict") == "signal")
    approaching_count = sum(1 for r in results if r.get("verdict") == "approaching")
    weak_count = sum(1 for r in results if r.get("verdict") == "weak")
    no_data_count = sum(1 for r in results if r.get("verdict") == "insufficient_data")

    lines.append(f"🔴 满足条件: **{signal_count}** 只  |  🟡 接近: **{approaching_count}** 只  |  ⚪ 偏弱: **{weak_count}** 只  |  ⬜ 无数据: **{no_data_count}** 只")
    # W33 A1: 双通道 signal 计数
    _sig_ice = sum(1 for r in results if (r.get("channels") or {}).get("iceberg", {}).get("verdict") == "signal")
    _sig_brk = sum(1 for r in results if (r.get("channels") or {}).get("breakout", {}).get("verdict") == "signal")
    if _sig_ice or _sig_brk:
        lines.append(f"　🧊冰点 {_sig_ice} 只 ｜ 🚀突破 {_sig_brk} 只")
    if signal_count > 0:
        # W33 A4 闸门未过（2026-08-14）→ 双通道为参考级，汇总卡明示
        lines.append("> ⚠️ 双通道判据未过离线闸门（W33 A4），以下信号仅供研究参考，不作验收/跟单依据")
    lines.append("")

    # 有数据的股票排序：signal > approaching > weak
    scored = [r for r in results if r.get("verdict") != "insufficient_data"]
    scored.sort(key=lambda r: -r.get("composite_score", 0))

    if scored:
        lines.append("**各股得分：**")
        lines.append("")
        for r in scored:
            icon = VERDICT_ICON.get(r.get("verdict", ""), "⚪")
            price = r.get("latest_price")
            price_str = f"{price:.2f}" if isinstance(price, (int, float)) else "N/A"
            pos = r.get("position") or {}
            qty = pos.get("suggested_qty", 0)
            _ch = r.get("channel")
            _ch_txt = {"iceberg": "🧊", "breakout": "🚀", "both": "🧊🚀"}.get(_ch, "—")

            # 条件通过情况（方案A: 时机 4 键圆点串）
            conds = r.get("conditions", {})
            cond_parts = []
            for key in COND_LABELS:
                c = conds.get(key, {})
                cond_parts.append("●" if c.get("passed") else "○")
            cond_str = "".join(cond_parts)

            lines.append(
                f"{icon} **{r['code']}** {r['name']}  "
                f"[{_ch_txt}] 得分 **{r['composite_score']}**  "
                f"价 {price_str}  "
                f"建议 {qty}股  "
                f"{cond_str}"
            )

    # 无数据股票简表
    no_data_list = [r for r in results if r.get("verdict") == "insufficient_data"]
    if no_data_list:
        lines.append("")
        lines.append(f"⬜ 无分钟数据（{len(no_data_list)} 只）：")
        nd_codes = ", ".join(r["code"] for r in no_data_list)
        lines.append(nd_codes)

    lines.append("")
    lines.append("●=通过  ○=未通过  (转向/BOLL/缩量/RSI/5分冰点/突破/放量/多头)")

    markdown = "\n".join(lines)

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "template": "turquoise",
                "title": {"tag": "plain_text", "content": f"📋 建仓扫描汇总 - {date_str}"},
            },
            "elements": [{"tag": "markdown", "content": markdown}],
        },
    }


def push_summary_feishu(results: list, date_str: str = "", dry_run: bool = False) -> bool:
    """推送盘后建仓汇总到飞书。"""
    if not _FEISHU_AVAILABLE or not results:
        return False
    if dry_run:
        return False
    card = build_summary_card(results, date_str)
    if card is None:
        return False
    return send_feishu_payload(
        card,
        success_log=f"建仓扫描汇总飞书推送成功: {len(results)} 只",
        error_prefix="建仓扫描汇总飞书推送",
    )


# ============================================================
# 破5/10日线检测与飞书推送（2026-08-14 新增）
# ============================================================

MA_BREAK_STATE_FILE = STATE_DIR / "ma_break_pushed.json"


def _load_ma_break_dedup() -> dict:
    """读取破线推送去重状态 {date: [code,...]}，失败时返回空。"""
    try:
        if MA_BREAK_STATE_FILE.exists():
            return json.loads(MA_BREAK_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _mark_ma_break_pushed(code: str, date_str: str) -> None:
    """记录 (code, date) 已推送破线提醒，仅保留最近 15 个日期。"""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        dedup = _load_ma_break_dedup()
        dedup.setdefault(date_str, [])
        if code not in dedup[date_str]:
            dedup[date_str].append(code)
        dedup = {d: dedup[d] for d in sorted(dedup)[-15:]}
        MA_BREAK_STATE_FILE.write_text(
            json.dumps(dedup, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ---------- 30/60分钟线背离推送（2026-08-19 新增）----------
DIVERGENCE_STATE_FILE = STATE_DIR / "divergence_pushed.json"


def _load_divergence_dedup() -> dict:
    try:
        if DIVERGENCE_STATE_FILE.exists():
            return json.loads(DIVERGENCE_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _mark_divergence_pushed(codes: list, date_str: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        dedup = _load_divergence_dedup()
        dedup.setdefault(date_str, [])
        for c in codes:
            if c not in dedup[date_str]:
                dedup[date_str].append(c)
        dedup = {d: dedup[d] for d in sorted(dedup)[-15:]}
        DIVERGENCE_STATE_FILE.write_text(
            json.dumps(dedup, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _push_divergence_feishu(stocks: list, date_str: str, dry_run: bool = False) -> bool:
    """推送 60分钟连续底背离参考提醒飞书卡片。stocks=[(code, name, [描述...])]。"""
    if not stocks or not _FEISHU_AVAILABLE:
        return False
    if dry_run:
        print(f"  [DRY-RUN] 跳过背离飞书推送: {len(stocks)} 只")
        return False
    lines = [f"**60分钟连续底背离参考**  {date_str}", ""]
    for code, name, parts in stocks:
        lines.append(f"🟢 **{name}（{code}）** {' ｜ '.join(parts)}")
    lines += ["", "📌 依据180天验证：60分钟连续底背离命中率60%（样本40），单次背离无效；此为非买卖建议，结合大盘时机判断。"]
    card = {
        "msg_type": "interactive",
        "card": {
            "header": {"template": "green",
                       "title": {"tag": "plain_text", "content": f"🟢 60分连续底背离 - {FEISHU_KEYWORD}"}},
            "elements": [{"tag": "markdown", "content": "\n".join(lines)}],
        },
    }
    return send_feishu_payload(
        card, success_log=f"60分钟连续底背离飞书推送: {len(stocks)} 只",
        error_prefix="60分钟连续底背离飞书推送")


def check_ma_break(code: str, stock_info: dict = None, date_str: str = None,
                   live_price: float = None) -> dict:
    """检测单只股票是否刚跌破5日线/10日线（刚跌破事件）。

    口径：昨收 >= 昨MA(N) 且 现价 < 今MA(N)，N∈{5,10}，逐线独立判定。
    MA 基于腾讯 qfq 日线收盘（fetch_daily_kline），盘中最后一行是当日 forming bar。
    实时价优先当日分钟快照，否则回退日线最后一根收盘。返回 dict；insufficient 非空表示无法判定。
    """
    code = str(code)
    result = {
        "code": code,
        "name": (stock_info or {}).get("name") or code,
        "price": None, "ma5": None, "ma10": None,
        "prev_ma5": None, "prev_ma10": None, "prev_close": None,
        "broke5": False, "broke10": False,
        "below5": False, "below10": False,
        "dev5_pct": None, "dev10_pct": None,
        "is_holding": bool((stock_info or {}).get("is_holding", False)),
        "insufficient": None,
    }
    df = fetch_daily_kline(code)
    if df.empty or len(df) < 11:
        result["insufficient"] = "日线不足11根"
        return result
    closes = df["close"].astype(float).values
    target_date = date_str or datetime.now().strftime("%Y-%m-%d")
    last_date = str(df["date"].iloc[-1])
    has_today = last_date == target_date

    # 实时价：优先当日分钟快照，否则日线最后一根收盘（仅当该根是当日 forming bar）
    price = live_price
    if price is None:
        df_1min, _, snap_date = load_snapshot_df(code, date_str)
        if not df_1min.empty and snap_date == target_date:
            price = float(df_1min["close"].iloc[-1])
    if price is None or price <= 0:
        if has_today:
            price = float(closes[-1])
        else:
            result["insufficient"] = "无当日实时价"
            return result

    # basis：截至昨日的收盘序列（今日 forming bar 不计入，MA 用实时价拼）
    basis = closes[:-1] if has_today else closes
    if len(basis) < 10:
        result["insufficient"] = "历史日线不足10根"
        return result
    prev_close = float(basis[-1])
    result["prev_close"] = prev_close

    for period, ma_key, prev_key, broke_key, below_key, dev_key in (
        (5, "ma5", "prev_ma5", "broke5", "below5", "dev5_pct"),
        (10, "ma10", "prev_ma10", "broke10", "below10", "dev10_pct"),
    ):
        prev_ma = float(np.mean(basis[-period:]))
        # 今MA = (最近 period-1 根截至昨日的收盘 + 实时价) / period
        cur_ma = float((np.sum(basis[-(period - 1):]) + price) / period)
        result[ma_key] = round(cur_ma, 3)
        result[prev_key] = round(prev_ma, 3)
        result[broke_key] = bool(prev_close >= prev_ma and price < cur_ma)
        result[below_key] = bool(price < cur_ma)
        result[dev_key] = round((price - cur_ma) / cur_ma * 100, 2) if cur_ma else None

    result["price"] = round(price, 3)
    return result


def scan_ma_breaks(date_str: str = None, silent: bool = False) -> list:
    """扫描候选池+持仓池，返回刚跌破5日线/10日线的事件列表。

    池合并：watchlist_buy.json 中 status=monitoring 的候选股 + holdings.json 中 qty>0 的持仓，
    按基础代码去重（_A/_B 账户后缀归一，持仓标记 is_holding）。"""
    codes = {}

    def _add(code, name, is_holding):
        base = str(code).split("_")[0]
        if base not in codes:
            codes[base] = {"name": name, "is_holding": is_holding}
        elif is_holding:
            codes[base]["is_holding"] = True

    try:
        if WATCHLIST_FILE.exists():
            with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
                wl = json.load(f)
            for code, info in (wl.get("stocks", {}) or {}).items():
                if not isinstance(info, dict) or str(code).startswith("_example"):
                    continue
                if info.get("status") not in ("monitoring", "signal"):
                    continue
                _add(code, info.get("name") or code, False)
    except Exception:
        pass

    try:
        if HOLDINGS_FILE.exists():
            with open(HOLDINGS_FILE, "r", encoding="utf-8") as f:
                holdings = json.load(f)
            for code, h in (holdings.items() if isinstance(holdings, dict) else []):
                if not isinstance(h, dict) or int(h.get("qty") or 0) <= 0:
                    continue
                _add(code, h.get("name") or code, True)
    except Exception:
        pass

    events = []
    for code, info in codes.items():
        r = check_ma_break(code, info, date_str)
        if r is None or r.get("insufficient"):
            if not silent and r and r.get("insufficient"):
                print(f"  {code} {r.get('name', '')} 跳过: {r['insufficient']}")
            continue
        if not r.get("broke5") and not r.get("broke10"):
            continue
        if not silent:
            tags = "".join(
                t for t, broke in (("【破5日线】", r["broke5"]), ("【破10日线】", r["broke10"])) if broke)
            hold = " [持仓]" if r["is_holding"] else ""
            print(f"  {code} {r['name']}{hold} {tags} 现价{r['price']} "
                  f"MA5={r['ma5']}({r['dev5_pct']:+.2f}%) MA10={r['ma10']}({r['dev10_pct']:+.2f}%)")
        events.append(r)
    return events


def run_ma_break_alert(date_str: str = None, dry_run: bool = False, silent: bool = False) -> list:
    """执行一次破5/10日线扫描，并按 (code, date) 当日去重推送飞书。

    返回本次实际推送的 events。dry_run=True 只打印不推送（不写去重）；silent=True 不打印控制台明细。
    供 main.py 盘中调度与 CLI --ma-break 共用。"""
    events = scan_ma_breaks(date_str, silent=silent)
    if not events:
        if not silent:
            print("破线扫描完成：今日无刚跌破5/10日线的股票")
        return []
    sig_date = date_str or datetime.now().strftime("%Y-%m-%d")
    to_push = [e for e in events if e["code"] not in _load_ma_break_dedup().get(sig_date, [])]
    if not to_push:
        if not silent:
            print(f"破线扫描完成：{len(events)} 只触发但当日均已推送过，跳过")
        return []
    pushed_ok = push_ma_break_feishu(to_push, date_str=sig_date, dry_run=dry_run)
    if pushed_ok:
        for e in to_push:
            _mark_ma_break_pushed(e["code"], sig_date)
        if not silent:
            print(f"破线提醒已推送: {len(to_push)} 只")
    elif not silent:
        print(f"破线提醒推送未成功（dry_run={'是' if dry_run else '否'}），未写去重")
    return to_push if pushed_ok else []


def build_ma_break_card(events: list, date_str: str = "") -> dict:
    """构建破5/10日线提醒飞书卡片。无事件返回 None。"""
    if not events:
        return None
    lines = [
        f"**破5/10日线提醒 · 可关注建仓**",
        f"📅 {date_str or datetime.now().strftime('%Y-%m-%d')}（刚跌破事件，盘中实时）",
        "",
    ]
    for r in events:
        hold = " [持仓]" if r.get("is_holding") else ""
        tags = " ".join(
            t for t, broke in (("【破5日线】", r.get("broke5")), ("【破10日线】", r.get("broke10"))) if broke)
        devs = []
        if r.get("ma5"):
            devs.append(f"MA5 {r['ma5']}({r['dev5_pct']:+.2f}%)")
        if r.get("ma10"):
            devs.append(f"MA10 {r['ma10']}({r['dev10_pct']:+.2f}%)")
        lines.append(
            f"🔻 **{r['code']}** {r['name']}{hold} {tags}\n"
            f"　现价 {r['price']} ｜ {' ｜ '.join(devs)}"
        )
    lines += [
        "",
        "📌 候选股破线关注建仓时机；持仓股破线为补仓/加仓观察。",
        "⚠️ 仅供参考，请人工确认后操作。",
    ]
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "template": "red",
                "title": {"tag": "plain_text", "content": "⚠️ 破5/10日线提醒 - 可关注建仓"},
            },
            "elements": [{"tag": "markdown", "content": "\n".join(lines)}],
        },
    }


def push_ma_break_feishu(events: list, date_str: str = "", dry_run: bool = False) -> bool:
    """推送破线提醒到飞书。返回是否成功。"""
    if not events:
        return False
    if not _FEISHU_AVAILABLE:
        return False
    if dry_run:
        print(f"  [DRY-RUN] 跳过飞书推送: {len(events)} 只破线股票")
        return False
    card = build_ma_break_card(events, date_str)
    if card is None:
        return False
    return send_feishu_payload(
        card,
        success_log=f"破5/10日线提醒飞书推送成功: {len(events)} 只",
        error_prefix="破5/10日线提醒飞书推送",
    )


def _py_type(v):
    """将 numpy 类型转换为 Python 原生类型，确保 JSON 可序列化。"""
    if isinstance(v, (np.bool_,)):
        return bool(v)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        if np.isnan(v):
            return None
        return float(v)
    if isinstance(v, np.ndarray):
        return v.tolist()
    return v


def update_watchlist(result: dict, watchlist: dict):
    """将扫描结果写回 watchlist_buy.json。"""
    code = result["code"]
    if code not in watchlist.get("stocks", {}):
        return
    stock = watchlist["stocks"][code]
    stock["last_check_date"] = result.get("date")
    stock["composite_score"] = int(result["composite_score"])
    stock["criteria_met"] = {
        k: bool(v["passed"]) for k, v in result.get("conditions", {}).items()
    }
    # fix P2-2: 每次扫描按最新 verdict 重算 status，非 signal 时清除粘性状态
    if result["verdict"] == "signal":
        stock["status"] = "signal"
    elif stock.get("status") == "signal":
        stock["status"] = "monitoring"
    # fix P2-3: insufficient_data 时不覆写建议仓位字段，保留上次有效建议
    if result["verdict"] != "insufficient_data":
        pos = result.get("position") or {}
        stock["suggested_qty"] = int(pos.get("suggested_qty", 0))
        stock["suggested_price"] = float(pos.get("suggested_price", 0))
        stock["capital_required"] = float(pos.get("capital_required", 0))
    # 追加信号历史（W33 A1: 带通道/择时标注）
    hist_entry = {
        "date": result.get("date"),
        "score": int(result["composite_score"]),
        "verdict": str(result["verdict"]),
        "channel": result.get("channel"),
        "approach_status": result.get("approach_status"),
        "price": float(result.get("latest_price")) if result.get("latest_price") else None,
    }
    if "signal_history" not in stock:
        stock["signal_history"] = []
    stock["signal_history"].append(hist_entry)
    # 只保留最近 20 条
    stock["signal_history"] = stock["signal_history"][-20:]


def print_report(results: list):
    """控制台输出扫描报告。"""
    print("\n" + "=" * 72)
    print("  建仓信号扫描报告")
    print("=" * 72)

    for r in results:
        code = r["code"]
        name = r["name"]
        _astat = r.get("approach_status")
        print(f"\n── {code} {name} ──")
        print(f"  日期: {r.get('date') or '无数据'}")
        print(f"  最新价: {r.get('latest_price') or 'N/A'}")
        print(f"  综合得分: {r['composite_score']}  =>  {r['verdict']}"
              f"  通道: {r.get('channel') or '—'}"
              f"{('  ' + str(_astat)) if _astat else ''}")

        if r.get("errors"):
            for e in r["errors"]:
                print(f"  [WARN] {e}")
            continue

        print("\n  条件检查:")
        for cond_key in COND_LABELS:
            cond_val = r.get("conditions", {}).get(cond_key)
            if cond_val is None:
                continue
            icon = "[PASS]" if cond_val["passed"] else "[FAIL]"
            print(f"    {icon} {COND_LABELS.get(cond_key, cond_key):12s}  {cond_val['detail']}")

        pos = r.get("position") or {}
        if pos:
            print(f"\n  仓位建议（单只上限 {pos.get('max_capital_per_stock','?'):,.0f}）:")
            print(f"    建议买入: {pos.get('suggested_qty',0)} 股 @ {pos.get('suggested_price','?')}")
            print(f"    所需资金: {pos.get('capital_required',0):,.0f}")
        elif r.get("note"):
            # fix 仓位一刀切(A1-A6): 已持仓等无仓位建议时显式说明
            print(f"\n  [NOTE] {r['note']}")

    # 汇总
    signals = [r for r in results if r.get("verdict") == "signal"]
    approaching = [r for r in results if r.get("verdict") == "approaching"]
    print(f"\n{'=' * 72}")
    print(f"  汇总: {len(signals)} 只满足条件, {len(approaching)} 只接近, "
          f"{len(results) - len(signals) - len(approaching)} 只偏弱")
    print("=" * 72 + "\n")

    if signals:
        print(">>> 以下股票满足建仓条件，请人工确认后手动加入 holdings.json：")
        for r in signals:
            pos = r.get("position") or {}
            if r.get("note"):
                # fix 仓位一刀切(A1-A6): 已持仓 signal 只提示，不给买入量
                print(f"  {r['code']} {r['name']}: {r['note']}")
            else:
                print(f"  {r['code']} {r['name']}: "
                      f"{pos.get('suggested_qty',0)}股 @ {pos.get('suggested_price','?')} "
                      f"≈ {pos.get('capital_required',0):,.0f}")
        print()


# ============================================================
# 核心入口（CLI 和 main.py 共用）
# ============================================================

def _build_holdings_gap(total_capital: float) -> dict:
    """W33 A3: 由 holdings.json 构建仓位管理器欠配缺口（config.build_position_gap 同源口径）。
    价格代理用 cost（恒可用）；mkt_val=Σ qty×cost。返回 {ratio_sum, rows} 或 None（无持仓/异常）。"""
    try:
        import config as _cfg
    except Exception:
        _cfg = None
    try:
        if not HOLDINGS_FILE.exists():
            return None
        with open(HOLDINGS_FILE, "r", encoding="utf-8") as f:
            cur = json.load(f)
        if not isinstance(cur, dict) or not cur:
            return None
        merged = {}
        for code, h in cur.items():
            if not isinstance(h, dict):
                continue
            base = str(code).split("_")[0]
            merged.setdefault(base, {"name": h.get("name", code), "qty": 0, "cost": 0.0, "px_sum": 0.0})
            qty = int(h.get("qty") or 0)
            px = float(h.get("cost") or h.get("pre_close") or 0)
            merged[base]["qty"] += qty
            merged[base]["px_sum"] += px * qty
        default_pct = 0.30
        raw = []
        for base, m in merged.items():
            raw_pct = default_pct
            if _cfg is not None:
                raw_pct = float((_cfg.STOCK_PARAMS.get(base, {}) or {}).get("stock_qty_base_pct", default_pct))
            raw.append({"code": base, "name": m["name"], "raw_pct": raw_pct,
                        "mkt_val": m["px_sum"], "total_qty": m["qty"]})
        if _cfg is None:
            return None
        return _cfg.build_position_gap(total_capital, raw, default_pct)
    except Exception:
        return None


def run_position_scan(date_str: str = None, capital: float = None,
                      no_feishu: bool = False, target_code: str = None,
                      silent: bool = False, scan_type: str = "manual") -> list:
    """执行一次建仓信号扫描，返回结果列表。
    scan_type: 'intraday' (盘中) / 'eod' (盘后) / 'manual' (手动)
    当 silent=True 时只记录日志不打印报告（供 main.py 定时调用）。
    """
    if not WATCHLIST_FILE.exists():
        if not silent:
            print(f"[ERROR] 未找到 {WATCHLIST_FILE}，请先创建待买入清单")
        return []

    with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
        watchlist = json.load(f)

    total_capital = capital or watchlist.get("total_capital", 300000)
    max_pct = watchlist.get("max_per_stock_pct", 0.2)
    # W33 A3: 构建一次持仓欠配缺口（欠配补仓候选标注 + 股数对齐），下传 scan_stock
    position_gap = _build_holdings_gap(total_capital)

    stocks = watchlist.get("stocks", {})
    if not stocks:
        if not silent:
            print("[ERROR] watchlist_buy.json 中无候选股票，请先添加")
        return []

    # 过滤
    if target_code:
        if target_code in stocks:
            stocks = {target_code: stocks[target_code]}
        else:
            if not silent:
                print(f"[ERROR] {target_code} 不在 watchlist_buy.json 中")
            return []
    else:
        stocks = {k: v for k, v in stocks.items()
                  if v.get("status") in ("monitoring", "signal")
                  and not k.startswith("_example")
                  and v.get("status") != "archived"}  # A-5(2026-08-21): 排除 archived 停用股

    if not stocks:
        if not silent:
            print("没有需要扫描的股票（status=monitoring/signal）")
        return []

    results = []
    signal_count = 0
    for code, info in stocks.items():
        if not silent:
            print(f"扫描 {code} {info.get('name', '')}...")
        # 盘后(eod)/手动(manual)重跑允许快照陈旧（日线判断独立拉取）；scan_type 传给 scan_stock
        r = scan_stock(code, info, date_str, total_capital, max_pct,
                       allow_stale=(scan_type in ("eod", "manual")), scan_type=scan_type,
                       position_gap=position_gap)
        results.append(r)
        update_watchlist(r, watchlist)

        if r["verdict"] == "signal":
            signal_count += 1
            # fix P1-4: (code, date, channel) 当日 signal 只推一次，防 5 分钟轮询刷屏（W33 A1: 去重键带通道）
            sig_date = r.get("date") or datetime.now().strftime("%Y-%m-%d")
            sig_channel = r.get("channel")
            if not no_feishu and _signal_already_pushed(code, sig_date, sig_channel):
                if not silent:
                    print(f"  => 今日已推送过该通道 signal，跳过重复推送")
            else:
                pushed = push_signal_feishu(r, dry_run=no_feishu)
                if pushed:
                    _mark_signal_pushed(code, sig_date, sig_channel)
                if not silent:
                    if pushed:
                        print(f"  => 飞书推送已发送")
                    elif not no_feishu and not _FEISHU_AVAILABLE:
                        print(f"  => 飞书 Webhook 未配置，跳过推送")

    # 写入结构化日志（供 daily_review.py 消费）
    scan_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_date = date_str or datetime.now().strftime("%Y-%m-%d")
    for r in results:
        _write_trace_line({
            "scan_time": scan_time,
            "scan_type": scan_type,
            "code": r["code"],
            "name": r["name"],
            "price": _py_type(r.get("latest_price")),
            "composite_score": int(r["composite_score"]),
            "score_ceiling": int(r.get("score_ceiling", 100)),
            "signal_reachable": bool(r.get("signal_reachable", True)),
            "verdict": str(r["verdict"]),
            "channel": r.get("channel"),
            "approach_status": r.get("approach_status"),
            "gated": bool(r.get("gated", False)),
            "gated_from": r.get("gated_from"),
            "intraday_confirm": r.get("intraday_confirm"),
            "block_reason": r.get("block_reason"),
            "blockers": r.get("blockers") or [],
            "timing": r.get("timing") or {"regime": None, "go": None, "reason": "未启用"},
            "divergence": r.get("divergence") or {},
            "divergence_detail": r.get("divergence_detail") or {},
            "channels": {k: {"verdict": v.get("verdict"), "score": v.get("score"),
                             "approach_status": v.get("approach_status"),
                             "conditions": v.get("conditions", {})}
                         for k, v in (r.get("channels") or {}).items()},
            # fix 2026-08-20: in_holdings 实时对齐 holdings.json（不再读 watchlist 陈旧字段）
            "in_holdings": r["code"] in _load_holding_codes(),
            "conditions": {k: bool(v["passed"]) for k, v in r.get("conditions", {}).items()},
            "suggested_qty": int((r.get("position") or {}).get("suggested_qty", 0)),
            "suggested_price": _py_type((r.get("position") or {}).get("suggested_price", 0)),
            "capital_required": _py_type((r.get("position") or {}).get("capital_required", 0)),
            "errors": [str(e) for e in r.get("errors", [])],
        }, log_date)

    # 保存更新后的 watchlist
    with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(watchlist, f, ensure_ascii=False, indent=2)

    # 背离飞书提醒（2026-08-19 验证后收窄：仅 60min 连续底背离推送，顶背离不推）
    # 依据：180 天验证单次背离命中率≈随机基线，仅 60min 连续底背离(+12.5pp, 样本40)可信
    try:
        _div_stocks = []
        for r in results:
            _dd = r.get("divergence_detail") or {}
            _m60 = _dd.get("m60") or {}
            if _m60.get("type") == "底背离" and _m60.get("consec"):
                _div_stocks.append((r["code"], r.get("name", r["code"]), ["60分钟连续底背离"]))
        if _div_stocks and not no_feishu:
            _div_date = log_date
            _dedup = _load_divergence_dedup()
            _fresh = [x for x in _div_stocks if x[0] not in _dedup.get(_div_date, [])]
            if _fresh:
                if _push_divergence_feishu(_fresh, _div_date):
                    _mark_divergence_pushed([x[0] for x in _fresh], _div_date)
                    if not silent:
                        print(f"背离提醒已推送: {len(_fresh)} 只")
        elif _div_stocks and not silent:
            print(f"背离检测: {len(_div_stocks)} 只（no_feishu 跳过推送）")
    except Exception:
        pass

    if not silent:
        print_report(results)

    return results


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="建仓信号扫描")
    parser.add_argument("--code", default=None, help="只扫描指定代码")
    parser.add_argument("--date", default=None, help="指定日期 YYYY-MM-DD（默认最新）")
    parser.add_argument("--capital", type=float, default=None, help="覆盖总资金量")
    parser.add_argument("--no-feishu", action="store_true", help="禁用飞书推送（仅控制台输出）")
    parser.add_argument("--ma-break", action="store_true", help="只跑破5/10日线扫描提醒（不跑建仓信号）")
    args = parser.parse_args()

    if args.ma_break:
        run_ma_break_alert(date_str=args.date, dry_run=args.no_feishu, silent=False)
        return

    run_position_scan(
        date_str=args.date,
        capital=args.capital,
        no_feishu=args.no_feishu,
        target_code=args.code,
        silent=False,
    )


if __name__ == "__main__":
    main()
