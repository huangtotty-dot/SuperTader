# P0-4(2026-08-31): 补齐 V3.0 迁移遗漏——本模块此前仅经 main.py exec 共享命名空间注入依赖，
# 作为独立模块 import（如 src/data_fetcher 引入）时 NameError。补模块级 import/常量使其自包含。
import os
import json
import time
import numpy as np
import pandas as pd
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

# T-2(2026-09-02): main.py exec 加载只切 __name__ 不切 __file__ → 自解析算到 E:\（仓库外）→ 快照误写 E:\t_io。
# 免疫模式（同 index_regime/daily_sentiment）：优先宿主注入的 BASE_DIR，回落自解析。
BASE = Path(globals().get("BASE_DIR") or Path(__file__).resolve().parents[1])
TRACE_DIR = BASE / "t_io" / "traces"
SNAPSHOT_DIR = BASE / "t_io" / "minute_snapshots"
PREOPEN_DIR = BASE / "t_io" / "preopen"
_now = datetime.now


def get_today_str() -> str:
    return _now().strftime("%Y-%m-%d")


AI_REVIEW_STATS: Dict[str, dict] = {}
DAILY_DECISION_STATS: Dict[str, dict] = {}
_ORPHAN_TMP_COUNT = 0


def _ensure_ai_review_stats(code: str, holding: dict) -> dict:
    if code not in AI_REVIEW_STATS:
        AI_REVIEW_STATS[code] = {"名称": holding.get("name", code), "最大多头分": 0, "最大空头分": 0, "最大振幅": 0.0, "触发买入次数": 0, "触发卖出次数": 0, "触发买入股数": 0, "触发卖出股数": 0}
    AI_REVIEW_STATS[code]["名称"] = holding.get("name", code)
    return AI_REVIEW_STATS[code]


def _ensure_daily_decision_stats(code: str, holding: dict) -> dict:
    default_price = float(holding.get("cost", 0) or 0)
    if code not in DAILY_DECISION_STATS:
        DAILY_DECISION_STATS[code] = {
            "name": holding.get("name", code),
            "buy_signals": [],
            "buy_low_signals": [],
            "buy_add_signals": [],
            "sell_signals": [],
            "sell_high_signals": [],
            "panic_sell_signals": [],
            "last_price": default_price,
            "last_vwap": default_price,
            "close_price": default_price,
            "last_score": 0.0,
            "last_buy_score": 0.0,
            "last_sell_score": 0.0,
            "last_amp": 0.0,
            "last_scan_time": "",
            "last_status": "未扫描",
            "last_status_detail": "",
            "last_market_state": "unknown",
            "last_benchmark_code": "",
            "last_benchmark_name": "",
            "last_benchmark_state": "unknown",
            "last_benchmark_gate": "unknown",
            "last_benchmark_reason": "",
            "last_buy_limit_reason": "",
            "minute_status": "未拉取",
            "minute_detail": "",
        }
    stats = DAILY_DECISION_STATS[code]
    stats["name"] = holding.get("name", code)
    return stats


def _snapshot_file(code: str, day: str) -> str:
    folder = os.path.join(SNAPSHOT_DIR, day[:4], day[5:7])
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, f"{code}_{day}.json")


def _trace_path(kind: str, day: Optional[str] = None) -> str:
    day = day or get_today_str()
    return os.path.join(TRACE_DIR, f"{kind}_{day}.jsonl")


def _preopen_path(day: Optional[str] = None) -> str:
    day = day or get_today_str()
    return os.path.join(PREOPEN_DIR, f"preopen_{day}.json")


def _json_safe(obj):
    """fix D12: 递归把 NaN/inf 转 None，避免 json.dump 产出非法 JSON"""
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, float) and (obj != obj or obj in (float("inf"), float("-inf"))):
        return None
    return obj


def _append_jsonl(path: str, record: dict) -> None:
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(_json_safe(record), ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _snapshot_write(code: str, holding: dict, df: pd.DataFrame, indicators: dict, signal: Optional[dict] = None, daily_context: Optional[dict] = None) -> None:
    if df.empty:
        return
    day = str(df.iloc[-1].get("time", ""))[:10]
    if len(day) != 10:
        return
    path = _snapshot_file(code, day)
    existing = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = json.load(f) or {}
        except Exception:
            existing = {}
    old_bars = existing.get("bars", []) if isinstance(existing, dict) else []
    new_bars = df[[c for c in ["time", "open", "high", "low", "close", "volume", "amount"] if c in df.columns]].to_dict(orient="records")
    merged_bars: Dict[str, dict] = {}
    for row in old_bars + new_bars:
        ts = str(row.get("time", ""))
        if ts:
            merged_bars[ts] = row
    bars = [merged_bars[k] for k in sorted(merged_bars.keys())]
    rec = {
        "code": code,
        "name": holding.get("name", code),
        "date": day,
        "saved_at": _now().strftime("%Y-%m-%d %H:%M:%S"),
        "row_count": int(len(bars)),
        "last_time": str(df.iloc[-1].get("time", "")),
        "last_close": float(df.iloc[-1].get("close", 0) or 0),
        "last_vwap": float(indicators.get("vwap", df.iloc[-1].get("close", 0)) or 0),
        "market_state": indicators.get("market_state", "unknown"),
        "benchmark_code": indicators.get("benchmark_code", ""),
        "benchmark_name": indicators.get("benchmark_name", ""),
        "benchmark_state": indicators.get("benchmark_state", "unknown"),
        "benchmark_gate": indicators.get("benchmark_gate", "neutral"),
        "benchmark_reason": indicators.get("benchmark_reason", ""),
        "signal": signal or existing.get("signal", {}) if isinstance(existing, dict) else (signal or {}),
        "daily_context": daily_context or (existing.get("daily_context", {}) if isinstance(existing, dict) else {}),
        "bars": bars,
    }
    # C19-1/2/3 修复(2026-08-18): 唯一 tmp 名 + os.replace 指数退避重试 + 故障隔离
    # 案发：杀软/Windows 索引器定时扫描锁文件 → WinError 5；原 os.replace 无重试且异常上抛打断信号主流程。
    tmp = f"{path}.{os.getpid()}.{int(time.time() * 1000)}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            # P0-1(2026-09-01): default=str 兼容 pd.Timestamp(分钟线 time 已改 datetime64)——
            # 此前 str(Timestamp) 不可序列化必抛，被 C19-3 静默吞掉 → 快照 2 日零落盘。
            json.dump(_json_safe(rec), f, ensure_ascii=False, default=str)  # fix D12: NaN→None
    except Exception as _se:
        try:
            os.remove(tmp)
        except Exception:
            pass
        _lg = globals().get("log")
        if _lg is not None:
            _lg.warning(f"⚠️ 快照写 tmp 失败（不再静默）: {type(_se).__name__}: {str(_se)[:120]} → {path}")
        else:
            print(f"[WARN] 快照写 tmp 失败: {path} ({type(_se).__name__})", flush=True)
        return  # C19-3: 写 tmp 失败自吞，不打断当轮信号扫描
    for _attempt in range(5):  # 指数退避 0.2/0.4/0.8/1.6/3.2s，覆盖 AV 秒级锁窗
        try:
            os.replace(tmp, path)
            return
        except OSError:
            if _attempt < 4:
                time.sleep(0.2 * (2 ** _attempt))
    # 重试耗尽：保留 tmp 供人工恢复，warning + 计数孤儿（数据不丢）
    try:
        _ORPHAN_TMP_COUNT = globals().get("_ORPHAN_TMP_COUNT", 0) + 1
        globals()["_ORPHAN_TMP_COUNT"] = _ORPHAN_TMP_COUNT
        _lg = globals().get("log")
        if _lg is not None:
            _lg.warning(f"⚠️ 快照写盘失败（保留 tmp 供恢复）: {tmp} → {path}；孤儿 tmp 累计 {_ORPHAN_TMP_COUNT}（C19 修复）")
        else:
            print(f"[WARN] 快照写盘失败（保留 tmp）: {tmp} → {path}", flush=True)
    except Exception:
        pass


def _default_daily_context(code: str, status: str = "unavailable", reason: str = "") -> Dict[str, Any]:
    return {
        "daily_status": status,
        "daily_reason": reason,
        "daily_asof": get_today_str(),
        "daily_price_ref": 0.0,
        "daily_prev_close": 0.0,
        "daily_prev_high": 0.0,
        "daily_prev_low": 0.0,
        "daily_prev_close_real": 0.0,
        "daily_day_ret": 0.0,
        "daily_ma5": 0.0,
        "daily_ma5_slope": 0.0,
        "daily_above_ma5": False,
        "daily_ma5_gap": 0.0,
        "daily_ma5_state": "unknown",
        "daily_ma10": 0.0,
        "daily_ma20": 0.0,
        "daily_ma30": 0.0,
        "daily_ma60": 0.0,
        "daily_ma10_slope": 0.0,
        "daily_ma20_slope": 0.0,
        "daily_ma30_slope": 0.0,
        "daily_ma60_slope": 0.0,
        "daily_trend_bg": "unknown",
        "daily_gate": "neutral",
        "daily_support_name": "",
        "daily_support_level": 0.0,
        "daily_support_gap": 0.0,
        "daily_near_support": False,
        "daily_pullback_support": False,
        "daily_breakdown_risk": False,
        "daily_hard_breakdown": False,
        "daily_overheated": False,
        "daily_ma_clustered": False,
        "daily_bull_aligned": False,
        # V1.24: 压力/支撑位计算所需字段
        "daily_high_10d": 0.0,
        "daily_low_10d": 0.0,
        "pre2_close": 0.0,
        "daily_ma150": 0.0,
    }


