# -*- coding: utf-8 -*-
"""
index_resonance.py — 指数5分钟共振过滤（做T信号，2026-08-14 新增）

做T信号（纯两点 bb_pct_5m + rsi_5m_p6）只看个股自身5分钟指标，弱市/背离时易假信号。
本模块：个股→对应指数映射 → 指数5分钟指标 → 与个股信号共振判定，供 main.py 在推送前门控。

两个共振口径（都计算、都落盘，门控用一个可切换）：
  - same_direction 同向极值：指数也处极值区（低吸需指数也超卖，高抛需指数也超买）
  - non_contrary    不逆势：指数未逆向于交易方向（低吸时指数未深破下轨、高抛时指数不在恐慌底）

用法（实盘）：
    from index_resonance import compute_resonance
    r = compute_resonance("000988", "BUY_LOW", 12.34)   # 含 5 分钟边界缓存
CLI 核对：
    python index_resonance.py --code 000988
"""
import json
import sys
import os
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

BASE = Path(__file__).resolve().parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from analysis.indicators import resample_to_5min, add_5min_indicators  # noqa: E402

try:
    from config import INDEX_RESONANCE_PARAMS as _IRP, INDEX_RESONANCE_MAP as _IRM
except Exception:
    _IRP = {"enabled": True, "gate": "same_direction", "fail_closed": True}
    _IRM = {}

try:
    from index_regime_intraday import fetch_index_minutes_live as _fetch_index_minutes_live
except Exception:
    def _fetch_index_minutes_live(code: str):
        raise RuntimeError("index_regime_intraday 不可用")

TRACE_DIR = BASE / "t_io" / "traces"

# 5 分钟边界缓存：{index_code: (boundary_ts, df_5min)}，同一边界所有个股共享
_5MIN_CACHE = {}


def resolve_index(code: str):
    """个股代码 → (index_code, index_name)。剥离 _A/_B 后缀，先查覆盖表，再按板块默认。"""
    base = str(code).split("_")[0]
    ov = _IRM.get(base)
    if ov:
        return ov[0], ov[1]
    if base.startswith("60"):
        return "sh000001", "上证指数"
    if base.startswith(("68", "588")):
        return "sh000688", "科创50"
    if base.startswith("30"):
        return "sz399006", "创业板指"
    if base.startswith(("00", "001", "002", "003")):
        return "sz399001", "深证成指"
    return "sh000001", "上证指数"


def _params() -> dict:
    try:
        from config import INDEX_RESONANCE_PARAMS
        return INDEX_RESONANCE_PARAMS or _IRP
    except Exception:
        return _IRP


def _add_idx_ma5(df5):
    """指数 5 分钟线附加自身 MA5 列（供 index_ma5_dir 门控，滚动无未来函数）。"""
    if df5 is not None and not df5.empty:
        df5["idx_ma5"] = df5["close"].rolling(5).mean()
    return df5


def get_index_5min(index_code: str, boundary_ts=None, provider: str = "live",
                   date_str: str = None):
    """获取指数 5 分钟 K 线（含指标）。live 用实时通道，backtest 用 tushare 历史通道。

    返回 (df_5min, ok)。live 通道按 (index_code, 5分钟边界) 缓存，同一边界只拉一次；
    backtest 通道按 (index_code, date_str) 缓存。失败/数据不足返回 (空df, False)。
    """
    p = _params()
    min_bars = int(p.get("min_index_5m_bars", 5))
    if provider == "live":
        # 缓存键取 5 分钟边界，保证同一边界内多只个股/多轮扫描共享一次指数拉取
        try:
            key = pd.Timestamp(boundary_ts).floor("5min") if boundary_ts is not None else None
        except Exception:
            key = None
        cached = _5MIN_CACHE.get(index_code)
        if cached and cached[0] == key and cached[1] is not None and not cached[1].empty:
            return cached[1], True
        try:
            df1 = _fetch_index_minutes_live(index_code)
            if df1 is None or df1.empty:
                return pd.DataFrame(), False
            df5 = resample_to_5min(df1)
        except Exception:
            return pd.DataFrame(), False
        if df5 is None or df5.empty or len(df5) < min_bars:
            return df5 if df5 is not None else pd.DataFrame(), False
        df5 = _add_idx_ma5(add_5min_indicators(df5))
        _5MIN_CACHE[index_code] = (key, df5)
        return df5, True
    else:
        # backtest: tushare 指数 1 分钟 → 5 分钟
        try:
            from index_regime_intraday import fetch_index_minutes_backtest as _bt
            ts_code = _index_code_to_ts(index_code)
            if not ts_code:
                return pd.DataFrame(), False
            df1 = _bt(ts_code=ts_code, date=date_str, freq="1min")
            if df1 is None or df1.empty:
                return pd.DataFrame(), False
            df5 = resample_to_5min(df1)
            if df5 is None or df5.empty or len(df5) < min_bars:
                return df5 if df5 is not None else pd.DataFrame(), False
            return _add_idx_ma5(add_5min_indicators(df5)), True
        except Exception:
            return pd.DataFrame(), False


def _index_code_to_ts(index_code: str) -> str:
    """sh000001 → 000001.SH；sz399006 → 399006.SZ。"""
    if not index_code:
        return ""
    prefix, num = index_code[:2], index_code[2:]
    if prefix == "sh":
        return f"{num}.SH"
    if prefix == "sz":
        return f"{num}.SZ"
    return ""


def verdict_from_indicators(code: str, action: str, price: float,
                            ind: dict, missing: bool = False, reason: str = None) -> dict:
    """由指数最新 5 分钟指标给出两个口径结论 + 门控判定（纯函数，实盘/回测共用）。"""
    p = _params()
    if missing:
        return {
            "index_code": ind.get("index_code", ""),
            "index_name": ind.get("index_name", ""),
            "index_bb_pct_5m": None, "index_rsi_6_5m": None,
            "index_dif_5m": None, "index_dea_5m": None,
            "same_direction": {"pass": False, "reason": f"指数数据不足: {reason}"},
            "non_contrary": {"pass": False, "reason": f"指数数据不足: {reason}"},
            "gate": p.get("gate", "contrarian"),
            "gate_pass": False,
            "missing": True, "reason": f"index_data_missing:{reason}",
        }
    bb = ind.get("bb_pct_5m")
    rsi = ind.get("rsi_5m_p6")
    if bb is None or rsi is None or np.isnan(bb) or np.isnan(rsi):
        return verdict_from_indicators(code, action, price, ind, missing=True, reason="指标NaN")
    is_buy = action in ("BUY_LOW", "ADD_POS")
    # 同向极值
    if is_buy:
        sd_pass = float(bb) <= float(p.get("buy_bb_max", 0.25)) and float(rsi) <= float(p.get("buy_rsi_max", 40))
        sd_reason = f"指数bb={bb:.2f}(需≤{p.get('buy_bb_max')}) rsi={rsi:.0f}(需≤{p.get('buy_rsi_max')})"
    else:
        sd_pass = float(bb) >= float(p.get("sell_bb_min", 0.75)) and float(rsi) >= float(p.get("sell_rsi_min", 60))
        sd_reason = f"指数bb={bb:.2f}(需≥{p.get('sell_bb_min')}) rsi={rsi:.0f}(需≥{p.get('sell_rsi_min')})"
    # 不逆势
    if is_buy:
        nc_pass = float(bb) >= float(p.get("buy_floor", -0.30))
        nc_reason = f"指数bb={bb:.2f}(需≥{p.get('buy_floor')}，未深破下轨)"
    else:
        nc_pass = float(bb) >= float(p.get("sell_floor", -0.20))
        nc_reason = f"指数bb={bb:.2f}(需≥{p.get('sell_floor')}，不在恐慌底)"
    # 指数自身 5 分钟 MA5 方向（2026-08-14 全年回测实证为最优方法）
    close = ind.get("close")
    ma5 = ind.get("ma5")
    if close is None or ma5 is None or np.isnan(close) or np.isnan(ma5):
        return verdict_from_indicators(code, action, price, ind, missing=True, reason="指数MA5数据不足")
    if is_buy:
        md_pass = float(close) >= float(ma5)
        # C14修复(2026-08-18): 文案反映实际方向，不再恒写"短趋势向上"
        md_reason = (f"指数价{float(close):.3f} vs 指数MA5 {float(ma5):.3f}"
                     f"（买需≥MA5；实际{'≥' if md_pass else '<'}MA5，"
                     f"指数短趋势{'向上' if md_pass else '向下'}，{'满足' if md_pass else '不满足'}）")
    else:
        md_pass = float(close) <= float(ma5)
        md_reason = (f"指数价{float(close):.3f} vs 指数MA5 {float(ma5):.3f}"
                     f"（卖需≤MA5；实际{'≤' if md_pass else '>'}MA5，"
                     f"指数短趋势{'向下' if md_pass else '向上'}，{'满足' if md_pass else '不满足'}）")
    gate = p.get("gate", "index_ma5_dir")
    if gate == "same_direction":
        gate_pass, gate_reason = sd_pass, sd_reason
    elif gate == "contrarian":
        # 反向（2026-08-14 全年回测实证）：指数与个股同处极值时信号更差
        # （市场同崩时低吸、普涨泡沫时高抛均不佳）→ 同向极值拦截、不同向放行
        gate_pass, gate_reason = (not sd_pass), f"反向拦截同向极值({sd_reason})"
    elif gate == "non_contrary":
        gate_pass, gate_reason = nc_pass, nc_reason
    else:  # index_ma5_dir（默认，全年回测最优：放行组 61.2% vs 拦截 40.8%）
        gate_pass, gate_reason = md_pass, md_reason
    return {
        "index_code": ind.get("index_code", ""),
        "index_name": ind.get("index_name", ""),
        "index_bb_pct_5m": round(float(bb), 4),
        "index_rsi_6_5m": round(float(rsi), 2),
        "index_dif_5m": round(float(ind.get("dif_5m") or 0), 5),
        "index_dea_5m": round(float(ind.get("dea_5m") or 0), 5),
        "index_close": round(float(close), 4),
        "index_ma5_5m": round(float(ma5), 4),
        "same_direction": {"pass": bool(sd_pass), "reason": sd_reason},
        "non_contrary": {"pass": bool(nc_pass), "reason": nc_reason},
        "index_ma5_dir": {"pass": bool(md_pass), "reason": md_reason},
        "gate": gate,
        "gate_pass": bool(gate_pass),
        "gate_reason": gate_reason,
        "missing": False,
        "reason": None,
    }


def compute_resonance(code: str, action: str, price: float,
                      boundary_ts=None, provider: str = "live",
                      date_str: str = None) -> dict:
    """实盘/回测统一的共振判定入口。provider='live' 走实时指数分钟（带边界缓存），
    provider='backtest' 走 tushare 历史指数分钟（date_str 必填）。"""
    index_code, index_name = resolve_index(code)
    if provider == "live":
        df5, ok = get_index_5min(index_code, boundary_ts=boundary_ts, provider="live")
    else:
        df5, ok = get_index_5min(index_code, boundary_ts=None, provider="backtest", date_str=date_str)
    if not ok or df5 is None or df5.empty:
        return verdict_from_indicators(
            code, action, price,
            {"index_code": index_code, "index_name": index_name},
            missing=True, reason="指数分钟不可用")
    last = df5.iloc[-1]
    ind = {
        "index_code": index_code,
        "index_name": index_name,
        "bb_pct_5m": float(last.get("bb_pct_5m")) if pd.notna(last.get("bb_pct_5m")) else None,
        "rsi_5m_p6": float(last.get("rsi_5m_p6")) if pd.notna(last.get("rsi_5m_p6")) else None,
        "dif_5m": float(last.get("dif_5m")) if pd.notna(last.get("dif_5m")) else None,
        "dea_5m": float(last.get("dea_5m")) if pd.notna(last.get("dea_5m")) else None,
        "close": float(last.get("close")) if pd.notna(last.get("close")) else None,
        "ma5": float(last.get("idx_ma5")) if pd.notna(last.get("idx_ma5")) else None,
    }
    return verdict_from_indicators(code, action, price, ind)


def write_resonance_trace(entry: dict, date_str: str = None) -> None:
    """追加一条共振判定到 t_io/traces/index_resonance_{date}.jsonl。"""
    try:
        TRACE_DIR.mkdir(parents=True, exist_ok=True)
        d = date_str or datetime.now().strftime("%Y-%m-%d")
        fp = TRACE_DIR / f"index_resonance_{d}.jsonl"
        with open(fp, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _cli():
    import argparse
    ap = argparse.ArgumentParser(description="指数5分钟共振核对")
    ap.add_argument("--code", required=True, help="个股代码，如 000988")
    ap.add_argument("--action", default="BUY_LOW", help="BUY_LOW / SELL_HIGH")
    ap.add_argument("--provider", default="live", choices=["live", "backtest"])
    ap.add_argument("--date", default=None, help="backtest 提供历史日期 YYYY-MM-DD")
    ap.add_argument("--price", type=float, default=0.0)
    args = ap.parse_args()

    index_code, index_name = resolve_index(args.code)
    print(f"个股 {args.code} → 指数 {index_code} {index_name}")
    r = compute_resonance(args.code, args.action, args.price or None,
                          provider=args.provider, date_str=args.date)
    if r["missing"]:
        print(f"  [数据不足] {r['reason']}")
        return
    print(f"  指数 bb_pct_5m={r['index_bb_pct_5m']}  rsi_6_5m={r['index_rsi_6_5m']}  "
          f"dif={r['index_dif_5m']}  dea={r['index_dea_5m']}")
    print(f"  同向极值: {'✅' if r['same_direction']['pass'] else '❌'} {r['same_direction']['reason']}")
    print(f"  不逆势  : {'✅' if r['non_contrary']['pass'] else '❌'} {r['non_contrary']['reason']}")
    print(f"  门控[{r['gate']}]: {'✅ 通过→推送' if r['gate_pass'] else '❌ 拦截'}")


if __name__ == "__main__":
    _cli()
