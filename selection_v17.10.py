# -*- coding: utf-8 -*-
"""
V17.10 全A股扫描版本
1. 扫描范围扩大到全A股，直接读取 watchlist.json 的 sector
2. 飞书 webhook 改为脚本内直写
3. 保留加急通知与系统报警能力
"""
import os
import sys
import json
import time
import logging
import importlib.util
import traceback
import subprocess
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Tuple, List, Dict

import numpy as np
import pandas as pd

# 设置控制台编码
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# 导入系统报警模块V17.3
alert_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "system_alert_v17_3.py")
spec = importlib.util.spec_from_file_location("system_alert_v17_3", alert_file)
system_alert_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(system_alert_module)
SystemAlert = system_alert_module.SystemAlert
trigger_alert = system_alert_module.trigger_alert

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
CACHE_DIR = os.path.join(BASE_DIR, "cache")
RESULTS_DIR = os.path.join(BASE_DIR, "results")
FEISHU_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/169aa25f-41f8-4dc0-8be7-0ba17494dd4b"
FEISHU_ENABLED = True
FEISHU_AT_ALL = True
FEISHU_STRONG_NOTIFY = True
LEGACY_WATCHLIST_FILE = os.path.join(BASE_DIR, "watchlist.json")
SPOT_MARKET_CAP_MAP = None

for d in [LOG_DIR, CACHE_DIR, RESULTS_DIR]:
    if not os.path.exists(d):
        os.makedirs(d)

log = logging.getLogger("三度猎手_V17")
log.setLevel(logging.DEBUG)
formatter = logging.Formatter("%(message)s")
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
log.addHandler(console_handler)

today_str = datetime.now().strftime('%Y-%m-%d')
file_handler = logging.FileHandler(os.path.join(LOG_DIR, f"hunter_sys_{today_str}.log"), encoding="utf-8")
file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(file_handler)

# 初始化系统报警
system_alert = SystemAlert(enabled=True)
CONCEPT_CACHE: Dict[str, str] = {}
LEGACY_CONCEPT_MAP: Dict[str, str] = {}
MAX_SIGNALS_PER_SECTOR_FEISHU = int(os.getenv("FEISHU_MAX_ITEMS_PER_MSG", "6"))
FEISHU_MAX_CARD_BYTES = int(os.getenv("FEISHU_MAX_CARD_BYTES", "28000"))
FEISHU_ENABLE_SECTOR_DETAIL = os.getenv("FEISHU_ENABLE_SECTOR_DETAIL", "1").strip() != "0"
SCAN_WORKERS = int(os.getenv("SCAN_WORKERS", "12"))
ALERT_MAX_SIGNALS = int(os.getenv("ALERT_MAX_SIGNALS", "3"))
WEEKLY_AMOUNT_MIN = float(os.getenv("WEEKLY_AMOUNT_MIN", "1500000000"))
WEEKLY_AMOUNT_MAX = float(os.getenv("WEEKLY_AMOUNT_MAX", "inf"))
WEEKLY_LOOKBACK_DAYS = int(os.getenv("WEEKLY_LOOKBACK_DAYS", "500"))
WEEKLY_BOTTOM_MIN_BARS = int(os.getenv("WEEKLY_BOTTOM_MIN_BARS", "30"))
DAILY_AMOUNT_MIN = float(os.getenv("DAILY_AMOUNT_MIN", "500000000"))
DAILY_AMOUNT_MAX = float(os.getenv("DAILY_AMOUNT_MAX", "40000000000"))
MONTHLY_GAIN_TOP_N = int(os.getenv("MONTHLY_GAIN_TOP_N", "50"))
MONTHLY_GAIN_MIN_TRADING_DAYS = int(os.getenv("MONTHLY_GAIN_MIN_TRADING_DAYS", "3"))
MIN_MARKET_CAP = float(os.getenv("MIN_MARKET_CAP", "15000000000"))
MA_NEAR_PCT = float(os.getenv("MA_NEAR_PCT", "0.05"))
ENABLE_MA_NEAR_60 = os.getenv("ENABLE_MA_NEAR_60", "1").strip() != "0"
ENABLE_MA_NEAR_150 = os.getenv("ENABLE_MA_NEAR_150", "1").strip() != "0"

# 策略开关配置
# 策略开关将在运行时从配置文件或环境变量动态读取
# 这里定义默认值作为备选
def get_strategy_enabled(strategy_name):
    """动态读取策略是否启用"""
    return os.getenv(f"ENABLE_STRATEGY_{strategy_name}", "1").strip() != "0"

# 为了向后兼容，创建快速访问函数
def is_strategy_enabled(code):
    """根据策略编码检查是否启用"""
    return get_strategy_enabled(code)

VERBOSE_SCAN_LOG = os.getenv("VERBOSE_SCAN_LOG", "0").strip() == "1"
AMOUNT_CHECK_LOG = os.getenv("AMOUNT_CHECK_LOG", "1").strip() != "0"
FETCH_RETRIES = int(os.getenv("FETCH_RETRIES", "2"))
QT_SNAPSHOT_ENABLED = os.getenv("QT_SNAPSHOT_ENABLED", "1").strip() != "0"
QT_SNAPSHOT_MAX_CODES_PER_REQUEST = int(os.getenv("QT_SNAPSHOT_MAX_CODES_PER_REQUEST", "200"))
QT_SNAPSHOT_AMOUNT_MAP: Dict[str, float] = {}
QT_SNAPSHOT_META: Dict[str, str] = {}
A_SHARE_CODE_PREFIXES = ("000", "001", "002", "003", "300", "301", "600", "601", "603", "605", "688", "689")
EXCLUDED_SECTORS = {
    "化学原料和化学制品制造业",
    "医药制造业",
    "商务服务业",
    "土木工程建筑业",
    "房地产业",
    "纺织服装、服饰业",
    "道路运输业",
    "互联网和相关服务",
    "公共设施管理业",
    "橡胶和塑料制品业",
    "研究和试验发展",
    "装卸搬运和仓储业",
    "货币金融服务",
    "造纸和纸制品业",
    "生态保护和环境治理业",
    "租赁业",
    "其他金融业",
    "纺织业",
    "印刷和记录媒介复制业",
    "木材加工和木、竹、藤、棕、草制品业",
    "水上运输业",
    "煤炭开采和洗选业",
    "燃气生产和供应业",
    "农业",
    "卫生",
    "家具制造业",
    "废弃资源综合利用业",
    "教育",
    "畜牧业",
}
UNIVERSE_CACHE_FILE = os.path.join(CACHE_DIR, "a_share_pool.json")
CACHE_SCHEMA_VERSION = "v4"
AMOUNT_ANCHOR_TOLERANCE = float(os.getenv("AMOUNT_ANCHOR_TOLERANCE", "0.2"))
AMOUNT_ANCHOR_RATIOS = {
    "300308": 264.8e8,
    "300502": 229.06e8,
    "300548": 66.32e8,
    "600105": 50.03e8,
    "688313": 42.55e8,
    "001309": 75.28e8,
    "002222": 46.23e8,
    "603778": 45.32e8,
    "002460": 65.09e8,
    "002475": 103.42e8,
    "603993": 83.54e8,
    "300757": 45.22e8,
}

PRIORITY_ORDER = {"🎯 突破回踩": 0, "🚀 历史突破": 0.5, "⭐ 箱体突破": 1, "👑 突破先手": 2, "🚀 A区初显": 3, "🧲 均线粘合": 4, "🔥 B区起航": 5, "💎 底部缩量": 6, "⚖️ B区潜伏": 7, "💥 箱内加速": 8, "🧭 均线邻近": 9, "🌱 周线底部企稳": 10, "🌿 周线止跌反抽": 10.5, "📈 周线突破前高": 11}
SIGNAL_EMOJI = {
    "🎯 突破回踩": "🔴",
    "🚀 历史突破": "🔴",
    "⭐ 箱体突破": "🟠",
    "👑 突破先手": "🟡",
    "🚀 A区初显": "🟢",
    "🧲 均线粘合": "🧲",
    "🔥 B区起航": "🔵",
    "💎 底部缩量": "🟣",
    "⚖️ B区潜伏": "⚪",
    "💥 箱内加速": "🟤",
    "🧭 均线邻近": "🧭",
    "🌱 周线底部企稳": "🟢",
    "🌿 周线止跌反抽": "🟢",
    "📈 周线突破前高": "🔴"
}


def save_scan_results(scan_date: str, scan_mode: str, total: int, success_count: int, signals: List[Dict]):
    """保存扫描结果到 JSON 文件，用于后续回测"""
    try:
        mode_dir = os.path.join(RESULTS_DIR, scan_mode)
        if not os.path.exists(mode_dir):
            os.makedirs(mode_dir)

        result_file = os.path.join(mode_dir, f"{scan_date}.json")
        now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if scan_mode.startswith("weekly"):
            amount_min = float(WEEKLY_AMOUNT_MIN)
            amount_max = float(WEEKLY_AMOUNT_MAX)
        else:
            amount_min = float(DAILY_AMOUNT_MIN)
            amount_max = float(DAILY_AMOUNT_MAX)
        amount_range_text = current_amount_range_text(scan_mode)
        current_signals = [
            {
                "code": s["code"],
                "name": s["name"],
                "signal_type": s["type"],
                "entry_price": float(s["price"]),
                "entry_date": scan_date,
                "sector": s["sector"],
                "amount": float(s.get("amount", 0) or 0),
                "week_total_amount": float(s.get("week_total_amount", 0) or 0),
                "week_avg_amount": float(s.get("week_avg_amount", 0) or 0),
                "amount_source": str(s.get("amount_source", "unknown") or "unknown"),
                "reason": s["reason"]
            }
            for s in signals
        ]

        existing_data = {}
        if os.path.exists(result_file):
            try:
                with open(result_file, "r", encoding="utf-8") as f:
                    existing_data = json.load(f) or {}
            except Exception:
                existing_data = {}

        run_history = existing_data.get("run_history", []) if isinstance(existing_data, dict) else []
        if not isinstance(run_history, list):
            run_history = []
        run_history.append({
            "scan_time": now_text,
            "scan_date": scan_date,
            "scan_mode": scan_mode,
            "amount_min": amount_min,
            "amount_max": amount_max,
            "amount_range_text": amount_range_text,
            "total_scanned": total,
            "success_count": success_count,
            "signals_count": len(current_signals),
            "signals": current_signals,
        })

        result_data = {
            "scan_date": scan_date,
            "scan_mode": scan_mode,
            "scan_time": now_text,
            "updated_at": now_text,
            "amount_min": amount_min,
            "amount_max": amount_max,
            "amount_range_text": amount_range_text,
            "total_scanned": total,
            "success_count": success_count,
            "signals_count": len(current_signals),
            "run_count": len(run_history),
            "run_history": run_history,
            "signals": current_signals,
        }

        with open(result_file, "w", encoding="utf-8") as f:
            json.dump(result_data, f, ensure_ascii=False, indent=2)

        log.debug(f"✓ 扫描结果已持续更新: {result_file}")
    except Exception as e:
        log.debug(f"⚠️  扫描结果保存失败: {str(e)[:60]}")


def group_signals_by_sector(signals: List[Dict]) -> List[tuple]:
    grouped: Dict[str, List[Dict]] = {}
    for sig in signals:
        sector = sig.get("sector", "未知板块") or "未知板块"
        primary_sector = sector.split("/")[0].strip() if sector else "未知板块"
        grouped.setdefault(primary_sector or "未知板块", []).append(sig)
    return sorted(grouped.items(), key=lambda x: (-len(x[1]), min(PRIORITY_ORDER.get(s['type'], 99) for s in x[1]), x[0]))


def sector_summary_line(sector: str, sector_signals: List[Dict]) -> str:
    best_type = min(sector_signals, key=lambda s: PRIORITY_ORDER.get(s['type'], 99))['type']
    type_counts: Dict[str, int] = {}
    for sig in sector_signals:
        sig_type = sig.get('type', '未知')
        type_counts[sig_type] = type_counts.get(sig_type, 0) + 1
    type_brief = ", ".join(
        f"{sig_type}{count}"
        for sig_type, count in sorted(type_counts.items(), key=lambda kv: (-kv[1], PRIORITY_ORDER.get(kv[0], 99)))[:3]
    )
    return f"🔥 {sector}（{len(sector_signals)} 只）最强：{best_type} | {type_brief}"


def truncate_reason(text: str, limit: int = 72) -> str:
    clean = str(text).replace("\n", " ").strip()
    return clean if len(clean) <= limit else clean[:limit - 1] + "…"


def format_sector_chain(sector: str) -> str:
    clean = str(sector or "").replace("\n", " ").strip()
    if not clean:
        return "未知板块"
    parts = [part.strip() for part in clean.split("/") if part.strip()]
    return " / ".join(parts) if parts else clean


def format_amount_range(min_amount: float, max_amount: float) -> str:
    if min_amount <= 0 and max_amount == float("inf"):
        return "不限"
    min_text = f"{min_amount / 100000000:.2f}亿"
    if max_amount == float("inf"):
        return f"{min_text}以上"
    return f"{min_text}~{max_amount / 100000000:.2f}亿"


def current_amount_range_text(scan_mode: str = "daily") -> str:
    if scan_mode.startswith("weekly"):
        return f"周均成交额 {format_amount_range(WEEKLY_AMOUNT_MIN, WEEKLY_AMOUNT_MAX)}"
    return format_amount_range(DAILY_AMOUNT_MIN, DAILY_AMOUNT_MAX)


def sector_priority_tag(sector_signals: List[Dict]) -> str:
    best_type = min(sector_signals, key=lambda s: PRIORITY_ORDER.get(s['type'], 99))['type']
    return best_type


def signal_priority_label(sig_type: str) -> str:
    return f"P{PRIORITY_ORDER.get(sig_type, 99) + 1}"


def signal_type_distribution(signals: List[Dict]) -> str:
    counts: Dict[str, int] = {}
    for sig in signals:
        sig_type = sig.get('type', '未知')
        counts[sig_type] = counts.get(sig_type, 0) + 1
    if not counts:
        return "暂无"
    parts = [f"{sig_type}{count}" for sig_type, count in sorted(counts.items(), key=lambda kv: (PRIORITY_ORDER.get(kv[0], 99), -kv[1]))]
    return "、".join(parts)


def sector_strength_score(sector_signals: List[Dict]) -> tuple:
    best_priority = min(PRIORITY_ORDER.get(sig.get('type', '未知'), 99) for sig in sector_signals)
    return (best_priority, -len(sector_signals), sector_signals[0].get('sector', ''))


def summary_brief(scan_date: str, total: int, success_count: int, signals: List[Dict], grouped: List[tuple], scan_mode: str = "daily") -> str:
    ranked = sorted(grouped, key=lambda item: sector_strength_score(item[1]))
    top_sector = ranked[0][0] if ranked else "暂无"
    top_sector_type = sector_priority_tag(ranked[0][1]) if ranked else "暂无"
    mode_tag = "周线轻量" if scan_mode == "weekly_light" else ("周线" if scan_mode.startswith("weekly") else "日线")
    return (
        f"📅{scan_date}  🎯命中{len(signals)}  🧭板块{len(grouped)}  🏆最强[{top_sector}|{top_sector_type}]  "
        f"📈分布[{signal_type_distribution(signals)}]  ✅数据{success_count}/{total}  🔎仅供{mode_tag}复盘参考"
    )


def build_weekly_from_daily(df_daily: pd.DataFrame) -> pd.DataFrame:
    if df_daily.empty or "date" not in df_daily.columns:
        return pd.DataFrame()

    weekly = df_daily.copy()
    weekly["date"] = pd.to_datetime(weekly["date"])
    for col in ["open", "close", "high", "low", "volume"]:
        if col in weekly.columns:
            weekly[col] = pd.to_numeric(weekly[col], errors="coerce")

    weekly = weekly.dropna(subset=["date", "open", "close", "high", "low", "volume"])
    if weekly.empty:
        return pd.DataFrame()

    weekly = weekly.set_index("date").sort_index()
    agg_map = {
        "open": "first",
        "close": "last",
        "high": "max",
        "low": "min",
        "volume": "sum",
    }
    if "amount" in weekly.columns:
        agg_map["amount"] = "sum"
    weekly = weekly.resample("W-FRI", label="right", closed="right").agg(agg_map).dropna(subset=["open", "close", "high", "low"])

    weekly = weekly.reset_index()
    weekly["date"] = weekly["date"].dt.strftime("%Y-%m-%d")
    return weekly


def normalize_weekly_target(input_date: str) -> str:
    try:
        target_dt = datetime.strptime(input_date, "%Y%m%d")
        offset = (target_dt.weekday() - 4) % 7
        return (target_dt - timedelta(days=offset)).strftime("%Y-%m-%d")
    except Exception:
        return ""


def check_weekly_breakout(weekly_df: pd.DataFrame, target_week: str, target_amount: float = None) -> Tuple[str, str]:
    try:
        if weekly_df.empty or "date" not in weekly_df.columns or not target_week:
            return None, ""

        weekly = weekly_df.copy()
        weekly["date"] = pd.to_datetime(weekly["date"], errors="coerce")
        weekly = weekly.dropna(subset=["date", "open", "close", "high", "low"])
        if weekly.empty:
            return None, ""

        target_dt = pd.to_datetime(target_week, errors="coerce")
        if pd.isna(target_dt):
            return None, ""

        weekly = weekly.sort_values("date")
        target_rows = weekly[weekly["date"] == target_dt]
        if target_rows.empty:
            return None, ""

        target_row = target_rows.iloc[-1]

        history = weekly[weekly["date"] < target_dt]
        if history.empty:
            return None, ""

        bullish_history = history[history["close"] > history["open"]]
        if bullish_history.empty:
            return None, ""

        bullish_max_high = bullish_history["high"].max()
        prev_high_row = bullish_history[bullish_history["high"] == bullish_max_high].iloc[-1]

        if float(target_row["close"]) <= float(target_row["open"]):
            return None, ""

        if float(target_row["high"]) > float(prev_high_row["high"]):
            amount_text = f"，周五成交额 {float(target_amount):.0f}" if target_amount is not None else ""
            return (
                "📈 周线突破前高",
                f"目标周 {target_dt.strftime('%Y-%m-%d')} 收阳且最高价 {float(target_row['high']):.2f} 突破前高阳线高点 {float(prev_high_row['high']):.2f}（{prev_high_row['date'].strftime('%Y-%m-%d')}）{amount_text}"
            )
        return None, ""
    except Exception as e:
        log.debug(f"策略检查异常: {str(e)}")
        return None, ""


def check_weekly_bottom_stabilize(weekly_df: pd.DataFrame, target_week: str, target_amount: float = None) -> Tuple[str, str]:
    try:
        if weekly_df.empty or "date" not in weekly_df.columns or not target_week:
            return None, ""

        weekly = weekly_df.copy()
        weekly["date"] = pd.to_datetime(weekly["date"], errors="coerce")
        weekly = weekly.dropna(subset=["date", "open", "close", "high", "low", "volume"])
        if len(weekly) < WEEKLY_BOTTOM_MIN_BARS:
            return None, ""

        weekly = weekly.sort_values("date").reset_index(drop=True)
        target_dt = pd.to_datetime(target_week, errors="coerce")
        if pd.isna(target_dt):
            return None, ""

        target_idx = weekly.index[weekly["date"] == target_dt]
        if len(target_idx) == 0:
            return None, ""
        idx = int(target_idx[-1])
        if idx < WEEKLY_BOTTOM_MIN_BARS - 1:
            return None, ""

        target_row = weekly.iloc[idx]
        prev_row = weekly.iloc[idx - 1]
        history = weekly.iloc[:idx + 1].copy()
        if history.empty:
            return None, ""

        for col in ["close", "volume"]:
            history[col] = pd.to_numeric(history[col], errors="coerce")
        history["ma5"] = history["close"].rolling(5).mean()
        history["ma10"] = history["close"].rolling(10).mean()
        history["ma20"] = history["close"].rolling(20).mean()
        history["ma30"] = history["close"].rolling(30).mean()
        history["vol_ma5"] = history["volume"].rolling(5).mean()
        history["vol_ma12"] = history["volume"].rolling(12).mean()

        today = history.iloc[-1]
        prev = history.iloc[-2]
        last_30 = history.iloc[-min(30, len(history)):]
        last_12 = history.iloc[-min(12, len(history)):]
        last_6 = history.iloc[-min(6, len(history)):]

        price = float(today["close"])
        open_price = float(today["open"])
        high_price = float(today["high"])
        low_price = float(today["low"])
        prev_close = float(prev["close"])
        prev_low = float(prev["low"])
        is_yang = price > open_price
        body_pos = (price - open_price) / max(high_price - low_price, 1e-9)
        vol_ratio = float(today["volume"]) / max(float(today["vol_ma5"]) if pd.notna(today["vol_ma5"]) and float(today["vol_ma5"]) > 0 else float(today["volume"]), 1.0)

        low_30 = float(last_30["low"].min())
        high_30 = float(last_30["high"].max())
        low_12 = float(last_12["low"].min())
        high_12 = float(last_12["high"].max())
        low_6 = float(last_6["low"].min())
        high_6 = float(last_6["high"].max())
        low_pos = (price - low_30) / max(high_30 - low_30, 1e-9)
        rebound_pos = (price - low_6) / max(high_6 - low_6, 1e-9)

        ma5 = float(today["ma5"]) if pd.notna(today["ma5"]) else 0.0
        ma10 = float(today["ma10"]) if pd.notna(today["ma10"]) else 0.0
        ma20 = float(today["ma20"]) if pd.notna(today["ma20"]) else 0.0
        ma30 = float(today["ma30"]) if pd.notna(today["ma30"]) else 0.0
        prev_ma30 = float(prev["ma30"]) if pd.notna(prev["ma30"]) else ma30

        if float(target_row["volume"]) <= 0:
            return None, ""

        # 企稳要求更像“低位收敛后守住短均线”
        if low_pos > 0.45:
            return None, ""
        if price >= high_30 * 0.92:
            return None, ""
        if ma30 > 0 and price < ma30 * 0.88:
            return None, ""
        if ma30 > 0 and prev_ma30 > 0 and ma30 < prev_ma30 * 0.97:
            return None, ""
        if low_12 < low_30 * 0.95 and price < ma10 * 0.98:
            return None, ""

        stabilizing = [
            price >= max(ma5, ma10) * 0.98 if max(ma5, ma10) > 0 else False,
            price > prev_close,
            float(target_row["low"]) >= prev_low * 0.985,
            is_yang,
            body_pos > 0.45,
            vol_ratio <= 1.8,
        ]
        if sum(1 for item in stabilizing if item) < 4:
            return None, ""

        amount_text = f"，周五成交额 {float(target_amount):.0f}" if target_amount is not None else ""
        return (
            "🌱 周线底部企稳",
            f"目标周 {target_dt.strftime('%Y-%m-%d')} 位于30周区间低位({low_pos*100:.1f}%)，收盘 {price:.2f} 站上/靠近短均线，较上周收高且低点未创新低，周线开始企稳{amount_text}"
        )
    except Exception as e:
        log.debug(f"周线底部企稳策略检查异常: {str(e)}")
        return None, ""


def check_weekly_pullback_stabilize(weekly_df: pd.DataFrame, target_week: str, target_amount: float = None) -> Tuple[str, str]:
    try:
        if weekly_df.empty or "date" not in weekly_df.columns or not target_week:
            return None, ""

        weekly = weekly_df.copy()
        weekly["date"] = pd.to_datetime(weekly["date"], errors="coerce")
        weekly = weekly.dropna(subset=["date", "open", "close", "high", "low", "volume"])
        if len(weekly) < WEEKLY_BOTTOM_MIN_BARS:
            return None, ""

        weekly = weekly.sort_values("date").reset_index(drop=True)
        target_dt = pd.to_datetime(target_week, errors="coerce")
        if pd.isna(target_dt):
            return None, ""

        target_idx = weekly.index[weekly["date"] == target_dt]
        if len(target_idx) == 0:
            return None, ""
        idx = int(target_idx[-1])
        if idx < WEEKLY_BOTTOM_MIN_BARS - 1:
            return None, ""

        target_row = weekly.iloc[idx]
        prev_row = weekly.iloc[idx - 1]
        history = weekly.iloc[:idx + 1].copy()
        if len(history) < 8:
            return None, ""

        for col in ["close", "volume"]:
            history[col] = pd.to_numeric(history[col], errors="coerce")
        history["ma5"] = history["close"].rolling(5).mean()
        history["ma10"] = history["close"].rolling(10).mean()
        history["ma20"] = history["close"].rolling(20).mean()
        history["ma30"] = history["close"].rolling(30).mean()
        history["vol_ma5"] = history["volume"].rolling(5).mean()

        today = history.iloc[-1]
        prev = history.iloc[-2]
        last_6 = history.iloc[-min(6, len(history)):]
        last_12 = history.iloc[-min(12, len(history)):]
        last_30 = history.iloc[-min(30, len(history)):]

        price = float(today["close"])
        open_price = float(today["open"])
        high_price = float(today["high"])
        low_price = float(today["low"])
        prev_close = float(prev["close"])
        prev_low = float(prev["low"])
        is_yang = price > open_price
        body_pos = (price - open_price) / max(high_price - low_price, 1e-9)
        vol_ratio = float(today["volume"]) / max(float(today["vol_ma5"]) if pd.notna(today["vol_ma5"]) and float(today["vol_ma5"]) > 0 else float(today["volume"]), 1.0)

        low_6 = float(last_6["low"].min())
        high_6 = float(last_6["high"].max())
        low_12 = float(last_12["low"].min())
        high_12 = float(last_12["high"].max())
        low_30 = float(last_30["low"].min())
        high_30 = float(last_30["high"].max())
        low_pos = (price - low_30) / max(high_30 - low_30, 1e-9)
        rebound_pos = (price - low_6) / max(high_6 - low_6, 1e-9)
        drop_from_12_high = (high_12 - price) / max(high_12, 1e-9)
        drop_from_6_high = (high_6 - price) / max(high_6, 1e-9)

        ma5 = float(today["ma5"]) if pd.notna(today["ma5"]) else 0.0
        ma10 = float(today["ma10"]) if pd.notna(today["ma10"]) else 0.0
        ma20 = float(today["ma20"]) if pd.notna(today["ma20"]) else 0.0
        ma30 = float(today["ma30"]) if pd.notna(today["ma30"]) else 0.0
        prev_ma30 = float(prev["ma30"]) if pd.notna(prev["ma30"]) else ma30

        if float(target_row["volume"]) <= 0:
            return None, ""
        if ma30 <= 0 or ma10 <= 0 or ma5 <= 0:
            return None, ""

        # 允许从“前几周连续走弱后，目标周止跌回收”中捕捉反抽
        if low_pos > 0.72:
            return None, ""
        if price < ma30 * 0.92:
            return None, ""
        if ma30 < prev_ma30 * 0.93:
            return None, ""
        if drop_from_12_high < 0.06:
            return None, ""
        if drop_from_6_high < 0.04 and low_pos > 0.5:
            return None, ""

        low_break_reclaim = float(target_row["low"]) <= prev_low * 0.98
        close_reclaim = price >= prev_close * 1.02
        short_ma_reclaim = price >= ma10 * 0.99 or price >= ma20 * 0.97
        strength_signals = [
            is_yang,
            price > prev_close,
            low_break_reclaim,
            close_reclaim,
            short_ma_reclaim,
            rebound_pos >= 0.28,
            body_pos > 0.18,
            vol_ratio <= 1.6,
        ]
        if sum(1 for item in strength_signals if item) < 6:
            return None, ""

        amount_text = f"，周五成交额 {float(target_amount):.0f}" if target_amount is not None else ""
        return (
            "🌿 周线止跌反抽",
            f"目标周 {target_dt.strftime('%Y-%m-%d')} 位于30周区间低位({low_pos*100:.1f}%)，本周向下刺破前周低点后收回，收盘 {price:.2f} 回收短均线附近并站回前周收盘，出现修复反抽{amount_text}"
        )
    except Exception as e:
        log.debug(f"周线止跌反抽策略检查异常: {str(e)}")
        return None, ""


def check_weekly_strategies(weekly_df: pd.DataFrame, target_week: str, target_amount: float = None) -> Tuple[str, str]:
    sig_type, reason = check_weekly_breakout(weekly_df, target_week, target_amount=target_amount)
    if sig_type:
        return sig_type, reason
    sig_type, reason = check_weekly_bottom_stabilize(weekly_df, target_week, target_amount=target_amount)
    if sig_type:
        return sig_type, reason
    return check_weekly_pullback_stabilize(weekly_df, target_week, target_amount=target_amount)


def sector_top3_brief(grouped: List[tuple]) -> str:
    if not grouped:
        return "暂无"
    top3 = sorted(grouped, key=lambda item: sector_strength_score(item[1]))[:3]
    return " | ".join(
        f"{idx}. {sector}｜热度{len(sigs)}｜最强{sector_priority_tag(sigs)}"
        for idx, (sector, sigs) in enumerate(top3, 1)
    )


def load_legacy_concept_map() -> Dict[str, str]:
    global LEGACY_CONCEPT_MAP
    if LEGACY_CONCEPT_MAP:
        return LEGACY_CONCEPT_MAP
    try:
        if os.path.exists(LEGACY_WATCHLIST_FILE):
            with open(LEGACY_WATCHLIST_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            LEGACY_CONCEPT_MAP = {
                str(code): _sanitize_sector(str(info.get("sector", "")))
                for code, info in data.items()
                if isinstance(info, dict) and _sanitize_sector(str(info.get("sector", "")))
            }
            log.debug(f"✓ 旧概念映射加载成功: {len(LEGACY_CONCEPT_MAP)} 条")
    except Exception as e:
        log.debug(f"⚠️  旧概念映射加载失败: {str(e)[:60]}")
    return LEGACY_CONCEPT_MAP


def load_ths_concept_cache() -> Dict[str, dict]:
    return {}


def _sanitize_sector(value: str) -> str:
    value = str(value or "").strip()
    if not value or value == "-":
        return ""
    if value in {"未知板块", "全部市场", "全市场", "A股全市场", "A股全市场"}:
        return ""
    return value


def _normalize_sector_text(value: str) -> str:
    return _sanitize_sector(value).replace(" ", "")


def resolve_stock_concept(code: str, fallback_sector: str = "未知板块") -> str:
    if code in CONCEPT_CACHE:
        return CONCEPT_CACHE[code]

    concept = _sanitize_sector(fallback_sector)
    if not concept:
        legacy_map = load_legacy_concept_map()
        if code in legacy_map:
            concept = _sanitize_sector(legacy_map.get(code, ""))
    if not concept:
        concept = "未知板块"

    CONCEPT_CACHE[code] = concept
    return CONCEPT_CACHE[code]


def resolve_business_summary(info: Dict[str, str]) -> str:
    if not isinstance(info, dict):
        return ""
    for key in ("business_summary", "主营业务", "公司简介", "简介", "业务简介"):
        text = str(info.get(key, "")).strip()
        if text:
            return truncate_reason(text, limit=180)
    return ""


def format_business_brief(text: str, limit: int = 18) -> str:
    text = str(text or "").strip()
    if not text:
        return ""
    text = text.replace("：", "").replace(":", "")
    return truncate_reason(text, limit=limit)


def wrap_business_text(text: str, width: int = 16, max_lines: int = 3) -> str:
    text = str(text or "").strip()
    if not text:
        return ""
    text = text.replace("\n", " ").replace("\r", " ")
    chunks = [text[i:i + width] for i in range(0, len(text), width)]
    if len(chunks) > max_lines:
        chunks = chunks[:max_lines]
        if chunks[-1] and not chunks[-1].endswith("…"):
            chunks[-1] = chunks[-1].rstrip() + "…"
    return "\n".join(chunks)


def post_feishu_payload(payload: Dict) -> bool:
    if not FEISHU_ENABLED or not FEISHU_WEBHOOK:
        return False

    req = urllib.request.Request(
        FEISHU_WEBHOOK,
        data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json; charset=utf-8'}
    )
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                result = json.loads(response.read().decode('utf-8'))
                if result.get('code') == 0:
                    return True
                msg = str(result.get('msg', ''))
                log.debug(f"⚠️  飞书推送返回: {msg}")
                if "30KB" in msg or "size limit" in msg.lower():
                    return False
                return True
        except Exception as e:
            if attempt < max_retries:
                wait_time = 2 ** attempt
                log.debug(f"⚠️  飞书推送第{attempt}次失败，{wait_time}秒后重试: {str(e)[:60]}")
                time.sleep(wait_time)
            else:
                log.debug(f"⚠️  飞书推送异常（已重试{max_retries}次）: {str(e)[:60]}")
    return False


def payload_size(payload: Dict) -> int:
    return len(json.dumps(payload, ensure_ascii=False).encode('utf-8'))


def send_to_feishu(signals: List[Dict], scan_date: str, total: int, success_count: int, scan_mode: str = "daily", status_note: str = ""):
    if not FEISHU_ENABLED or not FEISHU_WEBHOOK:
        return

    def post_payload(payload: Dict) -> bool:
        return post_feishu_payload(payload)

    def build_compact_overview(mode_tag: str, grouped: List[tuple], sorted_signals: List[Dict]) -> str:
        """构建紧凑的总览消息，避免重复"""
        top3 = sector_top3_brief(grouped)
        amount_range_text = current_amount_range_text("weekly" if mode_tag.startswith("周线") else "daily")

        # 构建信号摘要（只显示关键信息，避免重复）
        signal_summary = []
        for idx, s in enumerate(sorted_signals[:8], 1):
            emoji = SIGNAL_EMOJI.get(s['type'], '')
            name_code = f"{s['name']}({s['code']})"
            price = f"{s['price']:.2f}"

            # 添加涨幅信息
            daily_pct = float(s.get("daily_pct_change", 0) or 0)
            pct_emoji = "📈" if daily_pct >= 0 else "📉"
            pct_text = f"{pct_emoji} {daily_pct:+.2f}%"

            week_total_amount = float(s.get("week_total_amount", 0) or 0)
            week_avg_amount = float(s.get("week_avg_amount", 0) or 0)
            week_total_source = str(s.get("week_total_source", "unknown") or "unknown")
            amount_text = f"{week_total_amount / 100000000:.2f}亿" if week_total_amount > 0 else f"{float(s.get('amount', 0) or 0) / 100000000:.2f}亿"

            sector_full = format_sector_chain(s.get('sector', ''))

            business_summary = s.get("business_summary", "")
            business_display = f"\n业：{wrap_business_text(business_summary, width=18, max_lines=4)}" if business_summary else ""
            week_display = f"\n周总额：{amount_text}" if mode_tag.startswith("周线") else ""
            if mode_tag.startswith("周线") and week_avg_amount > 0:
                week_display += f" | 周均：{week_avg_amount / 100000000:.2f}亿"
            if mode_tag.startswith("周线"):
                week_display += f" | 来源：{week_total_source}"

            concept_display = f"📊 行业：{sector_full}"

            signal_summary.append(
                f"{emoji} {name_code} {price} {pct_text}{week_display}\n{concept_display}{business_display}\n{s['type']}"
            )

        overview = (
            f"📅 {scan_date} | {mode_tag} | 成交额区间：{amount_range_text}\n"
            f"🎯 {len(sorted_signals)}个信号 | ✅{success_count}/{total}\n"
            f"🏆 {top3}\n"
            f"\n{' | '.join(signal_summary)}"
        )
        if len(sorted_signals) > 8:
            overview += f"\n... 还有 {len(sorted_signals) - 8} 个信号"

        return overview

    def build_sector_card_compact(scan_date: str, sector: str, sector_signals: List[Dict]) -> Dict:
        """构建紧凑的板块卡片，减少冗余信息"""
        sector_signals = sorted(sector_signals, key=lambda s: (PRIORITY_ORDER.get(s['type'], 99), s.get('code', '')))
        card_elements = []
        amount_range_text = current_amount_range_text("weekly" if scan_mode.startswith("weekly") else "daily")

        # 日期和板块标题（突出显示）
        card_elements.append({
            "tag": "div",
            "text": {
                "content": f"📅 {scan_date} | 🔥 {sector} | 成交额区间：{amount_range_text}",
                "tag": "lark_md"
            }
        })
        card_elements.append({"tag": "hr"})

        # 信号列表（紧凑格式）
        for idx, s in enumerate(sector_signals, 1):
            emoji = SIGNAL_EMOJI.get(s['type'], "")
            week_total_amount = float(s.get("week_total_amount", 0) or 0)
            week_avg_amount = float(s.get("week_avg_amount", 0) or 0)
            amount_value = week_total_amount if week_total_amount > 0 else float(s.get('amount', 0) or 0)
            amount_text = f" {amount_value / 100000000:.2f}亿" if amount_value > 0 else ""

            # 添加涨幅信息
            daily_pct = float(s.get("daily_pct_change", 0) or 0)
            pct_emoji = "📈" if daily_pct >= 0 else "📉"
            pct_display = f" {pct_emoji} {daily_pct:+.2f}%"

            sector_full = format_sector_chain(s.get('sector', ''))

            business_summary = s.get("business_summary", "")
            business_line = wrap_business_text(business_summary, width=20, max_lines=4) if business_summary else "未知"

            signal_line = f"{emoji} {s['name']}({s['code']}) {s['price']:.2f}{pct_display}{amount_text}"
            if scan_mode.startswith("weekly") and week_avg_amount > 0:
                signal_line += f" | 周均 {week_avg_amount / 100000000:.2f}亿"
            concept_line = f"📊 行业：{sector_full}"
            strategy_line = f"🧭 {s['type']}"
            reason_line = truncate_reason(s['reason'], limit=40)
            week_total_source = str(s.get("week_total_source", "unknown") or "unknown")
            week_total_line = f"📈 周总额：{amount_text.strip()} | 来源：{week_total_source}" if scan_mode.startswith("weekly") and amount_text.strip() else ""
            body_lines = [signal_line]
            if week_total_line:
                body_lines.append(week_total_line)
            body_lines.extend([concept_line, f"📝 业：{business_line}", strategy_line, reason_line])

            card_elements.append({
                "tag": "div",
                "text": {
                    "content": "\n".join(body_lines),
                    "tag": "lark_md"
                }
            })

        return {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True, "enable_forward": True},
                "elements": card_elements
            },
            "notify_type": 1
        }

    def split_chunks(items: List[Dict], chunk_size: int) -> List[List[Dict]]:
        if chunk_size <= 0:
            return [items]
        return [items[i:i + chunk_size] for i in range(0, len(items), chunk_size)]

    try:
        sorted_signals = sorted(signals, key=lambda s: (PRIORITY_ORDER.get(s['type'], 99), s.get('sector', ''), s.get('code', '')))
        grouped = group_signals_by_sector(sorted_signals)
        mode_tag = "周线轻量" if scan_mode == "weekly_light" else ("周线" if scan_mode.startswith("weekly") else "日线")

        if not sorted_signals:
            msg_text = f"📅 {scan_date} | {mode_tag}\n✅ {success_count}/{total}\n\n暂无信号"
            payload = {"msg_type": "text", "content": {"text": msg_text}}
            if post_payload(payload):
                log.debug("✓ 飞书推送成功 (0 个信号)")
            return

        # 发送紧凑的总览消息
        overview_payload = {
            "msg_type": "text",
            "content": {"text": build_compact_overview(mode_tag, grouped, sorted_signals)}
        }
        if not post_payload(overview_payload):
            log.debug("⚠️  飞书总览消息发送失败")

        # 发送板块详情（只发送有信号的板块，不重复发送日期信息）
        if FEISHU_ENABLE_SECTOR_DETAIL and sorted_signals:
            for sector, sector_signals in grouped:
                chunks = split_chunks(sector_signals, max(1, MAX_SIGNALS_PER_SECTOR_FEISHU))

                for chunk in chunks:
                    payload = build_sector_card_compact(scan_date, sector, chunk)
                    size = payload_size(payload)

                    if size > FEISHU_MAX_CARD_BYTES and len(chunk) > 1:
                        log.debug(f"⚠️  板块卡片过大，继续拆分: {sector}, {size} bytes")
                        for item in chunk:
                            single_payload = build_sector_card_compact(scan_date, sector, [item])
                            if not post_payload(single_payload):
                                log.debug(f"⚠️  飞书板块单条发送失败: {sector} {item.get('code', '')}")
                        continue

                    if not post_payload(payload):
                        log.debug(f"⚠️  飞书板块消息发送失败: {sector}")

        log.debug(f"✓ 飞书推送成功 ({len(signals)} 个信号)")

    except Exception as e:
        log.debug(f"⚠️  飞书推送异常: {str(e)[:60]}")

def is_st_stock(code: str, name: str = "") -> bool:
    text = f"{code}{name}".upper()
    return "ST" in text or "*ST" in text


def is_bse_stock(code: str) -> bool:
    return str(code).startswith(("4", "8", "9"))


def is_a_share_code(code: str) -> bool:
    code = str(code).strip()
    return len(code) == 6 and code.isdigit() and code.startswith(A_SHARE_CODE_PREFIXES)


def normalize_pool_row(code: str, name: str, sector: str = "未知板块", business_summary: str = "") -> Dict[str, str]:
    info = {"name": name or code, "sector": sector or "未知板块"}
    if business_summary:
        info["business_summary"] = business_summary
    return info


def _to_float(value) -> float:
    try:
        if value in (None, "", "-"):
            return 0.0
        return float(str(value).replace(",", "").strip())
    except Exception:
        return 0.0


def load_spot_market_cap_map() -> Dict[str, float]:
    global SPOT_MARKET_CAP_MAP
    if isinstance(SPOT_MARKET_CAP_MAP, dict):
        return SPOT_MARKET_CAP_MAP
    mapping: Dict[str, float] = {}
    try:
        import akshare as ak
        spot = ak.stock_zh_a_spot_em()
        if spot is None or spot.empty:
            SPOT_MARKET_CAP_MAP = mapping
            return mapping
        code_col = None
        cap_col = None
        for col in spot.columns:
            text = str(col)
            lower = text.lower()
            if code_col is None and any(k in lower for k in ("代码", "code", "证券代码")):
                code_col = col
            if cap_col is None and any(k in text for k in ("总市值", "总市值(元)", "总市值（元）", "总市值(亿)", "总市值（亿）")):
                cap_col = col
        if code_col is None or cap_col is None:
            SPOT_MARKET_CAP_MAP = mapping
            return mapping
        for _, row in spot[[code_col, cap_col]].iterrows():
            code = normalize_code(row.iloc[0])
            cap = _to_float(row.iloc[1])
            if not code or cap <= 0:
                continue
            mapping[code] = cap
    except Exception:
        mapping = {}
    SPOT_MARKET_CAP_MAP = mapping
    return mapping


def extract_market_cap(info: Dict[str, str], code: str = "") -> float:
    if not isinstance(info, dict):
        return 0.0
    for key in ("market_cap", "market_capitalization", "总市值", "总市值(元)", "总市值（元）", "总市值_元", "总市值(亿)", "总市值（亿）"):
        value = info.get(key)
        cap = _to_float(value)
        if cap > 0:
            if any(unit in key for unit in ("(亿)", "（亿）")):
                return cap * 100000000
            return cap

    code = str(code).strip()
    if not code:
        return 0.0

    try:
        spot_map = load_spot_market_cap_map()
        if code in spot_map:
            return spot_map[code]
    except Exception:
        return 0.0
    return 0.0


def passes_universe_filters(code: str, name: str, info: Dict[str, str] = None) -> bool:
    if not is_a_share_code(code):
        return False
    if is_st_stock(code, name):
        return False
    if isinstance(info, dict):
        market_cap = extract_market_cap(info, code)
        if market_cap and market_cap < MIN_MARKET_CAP:
            return False
        sector_candidates = [
            info.get("sector", ""),
            info.get("industry", ""),
            info.get("所属行业", ""),
            info.get("行业", ""),
            info.get("concept", ""),
        ]
        for raw_sector in sector_candidates:
            sector = _normalize_sector_text(raw_sector)
            if sector and any(sector == _normalize_sector_text(item) or sector in _normalize_sector_text(item) or _normalize_sector_text(item) in sector for item in EXCLUDED_SECTORS):
                return False
    return True


def load_pool_from_df(df: pd.DataFrame, loader_name: str, code_idx: int = 0, name_idx: int = 1) -> Dict[str, Dict[str, str]]:
    pool = {}
    if df is None or df.empty:
        return pool

    cols = [str(c) for c in df.columns]
    sector_idx = None
    for i, col in enumerate(cols):
        lower = col.lower()
        if any(k in lower for k in ["所属行业", "行业", "板块", "concept", "概念"]):
            sector_idx = i
            break

    for _, row in df.iterrows():
        try:
            if len(row) <= max(code_idx, name_idx):
                continue
            code = str(row.iloc[code_idx]).strip()
            name = str(row.iloc[name_idx]).strip()
            sector = str(row.iloc[sector_idx]).strip() if sector_idx is not None and len(row) > sector_idx else "未知板块"
            info = normalize_pool_row(code, name, sector)
            if not passes_universe_filters(code, name, info):
                continue
            pool[code] = info
        except Exception:
            continue

    if pool:
        log.debug(f"✓ 全A股清单加载成功({loader_name}): {len(pool)} 只")
    return pool


def load_a_share_pool() -> Dict[str, Dict[str, str]]:
    log.debug("🔄 正在加载全A股股票清单...")
    log.debug(f"📌 股票池过滤：ST 已忽略，市值低于 {MIN_MARKET_CAP / 100000000:.0f} 亿已忽略")

    pool = {}
    if os.path.exists(LEGACY_WATCHLIST_FILE):
        try:
            with open(LEGACY_WATCHLIST_FILE, "r", encoding="utf-8") as f:
                legacy_pool = json.load(f)
            if isinstance(legacy_pool, dict) and legacy_pool:
                raw_total = 0
                raw_missing_name = 0
                raw_missing_sector = 0
                valid_count = 0
                for code, info in legacy_pool.items():
                    raw_total += 1
                    if not isinstance(info, dict):
                        continue
                    code = str(code).strip()
                    name = str(info.get("name", code)).strip() or code
                    sector = str(info.get("sector", "")).strip()
                    if not name:
                        raw_missing_name += 1
                    if not sector:
                        raw_missing_sector += 1
                    if not passes_universe_filters(code, name, info):
                        continue
                    business_summary = str(info.get("business_summary", "")).strip()
                    pool[code] = normalize_pool_row(code, name, sector or "未知板块", business_summary)
                    valid_count += 1
                if raw_total < 4500 or raw_missing_name > 0 or raw_missing_sector > 0:
                    log.debug(
                        f"⚠️  watchlist.json 原始数据完整性不足: total={raw_total} missing_name={raw_missing_name} missing_sector={raw_missing_sector}"
                    )
                    return {}
                if pool:
                    log.debug(f"✓ 从 watchlist.json 直接加载股票池成功: {len(pool)} 只")
        except Exception as e:
            log.debug(f"⚠️  读取 watchlist.json 失败: {str(e)[:80]}")

    if pool:
        log.debug(f"✓ 全A股清单最终加载完成: {len(pool)} 只")
        return pool

    log.debug("⚠️  watchlist.json 未能加载任何股票")
    return {}


def clear_cache():
    try:
        if os.path.exists(CACHE_DIR):
            for f in os.listdir(CACHE_DIR):
                file_path = os.path.join(CACHE_DIR, f)
                if os.path.isfile(file_path) and os.path.abspath(file_path) != os.path.abspath(UNIVERSE_CACHE_FILE):
                    os.remove(file_path)
        log.debug("✓ 缓存已清空（保留股票池缓存）")
        return True
    except Exception as e:
        log.debug(f"⚠️  缓存清空失败: {str(e)}")
        return False


def chunk_list(items: List[str], chunk_size: int) -> List[List[str]]:
    if chunk_size <= 0:
        return [items]
    return [items[i:i + chunk_size] for i in range(0, len(items), chunk_size)]


def set_fetch_source(df: pd.DataFrame, source: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    df["fetch_source"] = source
    return df


def amount_anchor_status(code: str, amount: float) -> Tuple[bool, float, float]:
    expected = AMOUNT_ANCHOR_RATIOS.get(str(code))
    if expected is None or amount <= 0:
        return True, 0.0, 0.0
    ratio = amount / expected
    return (abs(ratio - 1.0) <= AMOUNT_ANCHOR_TOLERANCE), expected, ratio


def normalize_qt_symbol(code: str) -> str:
    code = str(code).strip()
    market = "sh" if code.startswith(("5", "6", "9")) else "sz"
    return f"{market}{code}"


def parse_qt_snapshot_line(line: str) -> Tuple[str, Dict[str, float]]:
    line = str(line or "").strip()
    if not line or "=\"" not in line:
        return "", {}
    symbol = line.split("=", 1)[0].strip()
    payload = line.split("=", 1)[1].strip().strip(';').strip('"')
    fields = payload.split("~")
    if len(fields) < 8:
        return "", {}
    code = str(fields[2]).strip()
    if not code:
        return "", {}
    try:
        price = float(fields[3] or 0)
    except Exception:
        price = 0.0
    try:
        volume = float(fields[6] or 0)
    except Exception:
        volume = 0.0

    amount = 0.0
    turnover_raw = ""
    for field in fields:
        parts = str(field).strip().split("/")
        if len(parts) == 3:
            try:
                amount = float(parts[2] or 0)
                turnover_raw = parts[2].strip()
                break
            except Exception:
                continue
    if amount <= 0:
        try:
            amount = float(fields[7] or 0) * 10000.0
            turnover_raw = str(fields[7]).strip()
        except Exception:
            amount = 0.0
            turnover_raw = ""
    if not turnover_raw and len(fields) > 7:
        turnover_raw = str(fields[7]).strip()

    return code, {
        "symbol": symbol,
        "name": str(fields[1]).strip(),
        "price": price,
        "volume": volume,
        "amount": amount,
        "turnover_raw": turnover_raw,
        "market_cap": float(fields[9] or 0) * 100000000.0 if len(fields) > 9 and str(fields[9]).strip() else 0.0,
    }


def fetch_qt_snapshot_map(codes: List[str]) -> Dict[str, Dict[str, float]]:
    global QT_SNAPSHOT_AMOUNT_MAP, QT_SNAPSHOT_META
    if not QT_SNAPSHOT_ENABLED:
        return {}

    symbols = [normalize_qt_symbol(code) for code in codes if is_a_share_code(code)]
    if not symbols:
        return {}

    snapshot_map: Dict[str, Dict[str, float]] = {}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://finance.qq.com/",
    }

    for chunk in chunk_list(symbols, QT_SNAPSHOT_MAX_CODES_PER_REQUEST):
        url = f"https://qt.gtimg.cn/q={','.join(chunk)}"
        text = ""
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as response:
                text = response.read().decode("utf-8", errors="replace")
        except Exception as e:
            log.debug(f"⚠️  qt快照 urllib 失败: {type(e).__name__}: {str(e)[:120]}")
            try:
                result = subprocess.run(
                    ["curl", "-k", "-s", url],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                )
                text = result.stdout or ""
                if result.returncode != 0:
                    stderr = (result.stderr or "").strip().replace("\n", " ")
                    log.debug(f"⚠️  qt快照 curl 失败: rc={result.returncode} stderr={stderr[:120]}")
            except Exception as curl_error:
                log.debug(f"⚠️  qt快照抓取失败: {type(curl_error).__name__}: {str(curl_error)[:120]}")
                continue

        if not text.strip():
            log.debug("⚠️  qt快照返回为空")
            continue

        for line in text.splitlines():
            code, data = parse_qt_snapshot_line(line)
            if not code or not data:
                continue
            if data.get("amount", 0) <= 0:
                continue
            snapshot_map[code] = data

    QT_SNAPSHOT_AMOUNT_MAP = {code: float(data.get("amount", 0) or 0) for code, data in snapshot_map.items()}
    QT_SNAPSHOT_META = {
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "count": str(len(snapshot_map)),
    }
    if snapshot_map:
        log.debug(f"✓ qt快照批量加载完成: {len(snapshot_map)} 只")
    else:
        log.debug("⚠️  qt快照批量加载为空")
    return snapshot_map


def cache_meta(scan_mode: str = "daily") -> Dict[str, str]:
    return {
        "cache_schema_version": CACHE_SCHEMA_VERSION,
        "scan_mode": scan_mode,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def is_valid_cached_frame(df: pd.DataFrame, scan_mode: str = "daily") -> bool:
    if df is None or df.empty:
        return False
    if not isinstance(df, pd.DataFrame):
        return False
    required_cols = {"date", "open", "close", "high", "low", "volume", "amount", "amount_raw", "cache_schema_version"}
    if not required_cols.issubset(df.columns):
        return False
    if not (df["cache_schema_version"].astype(str) == CACHE_SCHEMA_VERSION).all():
        return False
    numeric_ok = pd.to_numeric(df["amount_raw"], errors="coerce").notna().all() and pd.to_numeric(df["amount"], errors="coerce").notna().all()
    if not numeric_ok:
        return False
    if scan_mode == "daily" and len(df) < 20:
        return False
    if scan_mode.startswith("weekly") and len(df) < 8:
        return False
    return True

def get_cache_file(code: str, target_date: str, scan_mode: str = "daily") -> str:
    return os.path.join(CACHE_DIR, f"{code}_{scan_mode}_{target_date}.csv")


def save_cache(df: pd.DataFrame, code: str, target_date: str, scan_mode: str = "daily"):
    try:
        cache_file = get_cache_file(code, target_date, scan_mode)
        cache_df = df.copy()
        cache_df["cache_schema_version"] = CACHE_SCHEMA_VERSION
        cache_df["cache_scan_mode"] = scan_mode
        cache_df["cache_saved_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cache_df.to_csv(cache_file, index=False, encoding='utf-8')
        log.debug(f"    💾 缓存已保存")
    except Exception as e:
        log.debug(f"    ⚠️  缓存保存失败: {str(e)[:40]}")


def load_cache(code: str, target_date: str, scan_mode: str = "daily") -> pd.DataFrame:
    try:
        cache_file = get_cache_file(code, target_date, scan_mode)
        if os.path.exists(cache_file):
            df = pd.read_csv(cache_file)
            if is_valid_cached_frame(df, scan_mode=scan_mode):
                df = ensure_amount_column(df, code=code)
                df = set_fetch_source(df, "cache")
                log.debug(f"    ✓ 从缓存加载 ({len(df)} 条)")
                return df
            os.remove(cache_file)
            log.debug(f"    ⚠️  缓存版本或结构无效，已删除: {code} {target_date} {scan_mode}")
    except Exception:
        try:
            if os.path.exists(cache_file):
                os.remove(cache_file)
        except Exception:
            pass
    return pd.DataFrame()


def ensure_amount_column(df: pd.DataFrame, code: str = "") -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    if "amount_raw" not in df.columns:
        if "amount" in df.columns:
            df["amount_raw"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0.0)
        else:
            df["amount_raw"] = 0.0
    else:
        df["amount_raw"] = pd.to_numeric(df["amount_raw"], errors="coerce").fillna(0.0)
    if "amount" not in df.columns:
        df["amount"] = df["amount_raw"].copy()
    else:
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0.0)
        need_fill = (df["amount"] <= 0) & (df["amount_raw"] > 0)
        if need_fill.any():
            df.loc[need_fill, "amount"] = df.loc[need_fill, "amount_raw"]

    if "amount_source" in df.columns:
        source_mask = df["amount_source"].astype(str).eq("qt_snapshot")
    else:
        source_mask = pd.Series(False, index=df.index)

    if ("volume" in df.columns and "close" in df.columns):
        volume = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0)
        close = pd.to_numeric(df["close"], errors="coerce").fillna(0.0)
        if "high" in df.columns and "low" in df.columns:
            high = pd.to_numeric(df["high"], errors="coerce").fillna(0.0)
            low = pd.to_numeric(df["low"], errors="coerce").fillna(0.0)
            derived_price = (high + low + close) / 3.0
            fallback_source = "volume_hlc3_fallback"
        else:
            derived_price = close
            fallback_source = "volume_close_fallback"
        derived_amount = volume * derived_price
        missing_amount = (df["amount"] <= 0) & (derived_amount > 0)
        if missing_amount.any():
            need_fallback = missing_amount & ~source_mask
            if need_fallback.any():
                df.loc[need_fallback, "amount_raw"] = derived_amount[need_fallback]
                df.loc[need_fallback, "amount"] = derived_amount[need_fallback]
                df.loc[need_fallback, "amount_source"] = fallback_source
                if "fetch_source" not in df.columns or not (df["fetch_source"].astype(str).eq("qt_snapshot")).any():
                    df.loc[need_fallback, "fetch_source"] = fallback_source

    if "date" in df.columns and code:
        target_anchor = str(code)
        expected = AMOUNT_ANCHOR_RATIOS.get(target_anchor)
        if expected and expected > 0:
            target_idx = df.index[-1]
            current_amount = float(df.at[target_idx, "amount"] or 0)
            if current_amount > 0:
                anchor_ok, _, anchor_ratio = amount_anchor_status(target_anchor, current_amount)
                if not anchor_ok:
                    df.loc[:, "amount_anchor_ratio"] = anchor_ratio
                    log.debug(
                        f"    ⚠️ {target_anchor} 锚点偏离: current={current_amount/100000000:.2f}亿 | expected={expected/100000000:.2f}亿 | ratio={anchor_ratio:.2f}"
                    )
    return df


def log_amount_check(code: str, name: str, raw_amount: float, normalized_amount: float, min_amount: float, max_amount: float, scan_mode: str = "daily"):
    if not AMOUNT_CHECK_LOG:
        return
    raw_text = f"{raw_amount / 100000000:.2f}亿" if raw_amount else "0.00亿"
    norm_text = f"{normalized_amount / 100000000:.2f}亿" if normalized_amount else "0.00亿"
    passed = min_amount <= normalized_amount <= max_amount
    max_text = "∞" if max_amount == float('inf') else f"{max_amount / 100000000:.2f}亿"
    log.debug(
        f"[金额自检][{scan_mode}] {code} {name} | 原始={raw_text} | 归一化={norm_text} | 区间={min_amount / 100000000:.2f}亿~{max_text} | 通过={passed}"
    )


def standardize_compare_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    rename_map = {
        "日期": "date", "date": "date", "trade_date": "date",
        "开盘": "open", "open": "open",
        "收盘": "close", "close": "close",
        "最高": "high", "high": "high",
        "最低": "low", "low": "low",
        "成交量": "volume", "vol": "volume", "volume": "volume",
        "成交额": "amount", "amount": "amount",
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})
    keep_cols = [c for c in ["date", "open", "close", "high", "low", "volume", "amount"] if c in df.columns]
    if not keep_cols or "date" not in df.columns:
        return pd.DataFrame()
    df = df[keep_cols].copy()
    df["date"] = df["date"].astype(str).str.slice(0, 10)
    for col in ["open", "close", "high", "low", "volume", "amount"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["date", "open", "close", "high", "low"])
    return df.sort_values("date").reset_index(drop=True)


def load_target_snapshot(df: pd.DataFrame, code: str, target_date: str) -> Dict[str, float]:
    row, _ = pick_target_row(df, target_date, code=code)
    if row.empty:
        return {}
    item = row.iloc[-1]
    volume = float(item.get("volume", 0) or 0)
    close = float(item.get("close", 0) or 0)
    raw_amount = float(item.get("amount_raw", item.get("amount", 0)) or 0)
    normalized_amount = float(item.get("amount", 0) or 0)
    derived_amount = volume * 100.0 * close if volume > 0 and close > 0 else 0.0
    return {
        "date": str(item.get("date", ""))[:10],
        "open": float(item.get("open", 0) or 0),
        "close": close,
        "high": float(item.get("high", 0) or 0),
        "low": float(item.get("low", 0) or 0),
        "volume": volume,
        "raw_amount": raw_amount,
        "normalized_amount": normalized_amount,
        "derived_amount": derived_amount,
    }


def apply_qt_snapshot_amount(df: pd.DataFrame, code: str, target_date: str) -> pd.DataFrame:
    if df is None or df.empty or not QT_SNAPSHOT_AMOUNT_MAP:
        return df
    if str(target_date).strip() != datetime.now().strftime("%Y%m%d"):
        return df

    # 只在收盘后(15:30之后)使用QT快照，避免交易时段的实时累计成交额
    current_time = datetime.now().time()
    if current_time < datetime.strptime("15:30", "%H:%M").time():
        log.debug(f"    ⏰ QT快照未应用: {code} (交易时段，等待收盘后)")
        return df

    amount = float(QT_SNAPSHOT_AMOUNT_MAP.get(str(code), 0) or 0)
    if amount <= 0:
        return df
    df = df.copy()
    if "date" not in df.columns:
        return df
    date_col = df["date"].astype(str).str.slice(0, 10).str.replace("-", "", regex=False)
    target_mask = date_col == str(target_date)
    if not target_mask.any():
        return df
    df.loc[target_mask, "amount_raw"] = amount
    df.loc[target_mask, "amount"] = amount
    df.loc[target_mask, "amount_source"] = "qt_snapshot"
    df.loc[target_mask, "fetch_source"] = "qt_snapshot"
    log.debug(f"    ✓ QT快照应用: {code} amount={amount/100000000:.2f}亿")
    return df


def compare_sample_sources(code: str, target_date: str, main_df: pd.DataFrame) -> List[Dict[str, float]]:
    qt_amount = float(QT_SNAPSHOT_AMOUNT_MAP.get(str(code), 0) or 0)
    if qt_amount <= 0:
        return []
    row, _ = pick_target_row(main_df, target_date, code=code)
    if row.empty:
        return []
    item = row.iloc[-1]
    snapshot = {
        "source": "qt_snapshot",
        "date": str(item.get("date", ""))[:10],
        "close": float(item.get("close", 0) or 0),
        "volume": float(item.get("volume", 0) or 0),
        "raw_amount": qt_amount,
        "normalized_amount": qt_amount,
        "derived_amount": float(item.get("volume", 0) or 0) * float(item.get("close", 0) or 0),
    }
    log.debug(
        f"    🛰️ qt快照对照: {code} | date={snapshot['date']} | close={snapshot['close']:.2f} | volume={snapshot['volume']:.0f} | amount={snapshot['normalized_amount']/100000000:.2f}亿"
    )
    return [snapshot]


def pick_target_row(df: pd.DataFrame, target_date: str, code: str = "") -> tuple:
    if df is None or df.empty or "date" not in df.columns:
        return pd.DataFrame(), ""
    target_date_str = f"{target_date[:4]}-{target_date[4:6]}-{target_date[6:8]}"
    date_series = df["date"].astype(str).str.slice(0, 10)
    exact = df[date_series == target_date_str]
    if not exact.empty:
        return exact, target_date_str

    parsed_dates = pd.to_datetime(date_series, errors="coerce")
    target_dt = pd.to_datetime(target_date_str, errors="coerce")
    if pd.isna(target_dt):
        return pd.DataFrame(), target_date_str

    fallback = df.loc[parsed_dates <= target_dt].copy()
    if fallback.empty:
        return pd.DataFrame(), target_date_str

    fallback["__parsed_date"] = parsed_dates.loc[fallback.index]
    fallback = fallback.sort_values("__parsed_date")
    row = fallback.iloc[[-1]].drop(columns=["__parsed_date"])
    if VERBOSE_SCAN_LOG:
        fallback_date = str(row.iloc[-1]["date"])[:10]
        log.debug(f"    ⚠️  {code} 目标日期 {target_date_str} 未精确命中，回退到最近交易日 {fallback_date}")
    return row, target_date_str

def fetch_from_akshare_hist(code: str, target_date: str, start_date: str) -> pd.DataFrame:
    try:
        import akshare as ak
        df = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start_date, end_date=target_date, adjust="qfq")
        if df is None or df.empty:
            return pd.DataFrame()
        rename_map = {"日期": "date", "开盘": "open", "收盘": "close", "最高": "high", "最低": "low", "成交量": "volume", "成交额": "amount"}
        df = df.rename(columns=rename_map)
        keep_cols = [c for c in ["date", "open", "close", "high", "low", "volume", "amount"] if c in df.columns]
        if len(keep_cols) < 5:
            return pd.DataFrame()
        df = df[keep_cols].copy()
        if "date" in df.columns:
            df["date"] = df["date"].astype(str).str.slice(0, 10)
        for col in ["open", "close", "high", "low", "volume", "amount"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["date", "open", "close", "high", "low"])
        if "amount" in df.columns:
            df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
        df = df.sort_values("date").reset_index(drop=True)
        return set_fetch_source(df, "akshare_hist_qfq_daily")
    except Exception:
        return pd.DataFrame()


def fetch_from_akshare_hist_alt(code: str, target_date: str, start_date: str) -> pd.DataFrame:
    try:
        import akshare as ak
        df = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start_date, end_date=target_date, adjust="")
        if df is None or df.empty:
            return pd.DataFrame()
        rename_map = {"日期": "date", "开盘": "open", "收盘": "close", "最高": "high", "最低": "low", "成交量": "volume", "成交额": "amount"}
        df = df.rename(columns=rename_map)
        keep_cols = [c for c in ["date", "open", "close", "high", "low", "volume", "amount"] if c in df.columns]
        if len(keep_cols) < 5:
            return pd.DataFrame()
        df = df[keep_cols].copy()
        if "date" in df.columns:
            df["date"] = df["date"].astype(str).str.slice(0, 10)
        for col in ["open", "close", "high", "low", "volume", "amount"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["date", "open", "close", "high", "low"])
        if "amount" in df.columns:
            df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
        df = df.sort_values("date").reset_index(drop=True)
        return set_fetch_source(df, "akshare_hist_plain_daily")
    except Exception:
        return pd.DataFrame()


def fetch_from_akshare_weekly_hist(code: str, target_date: str, start_date: str) -> pd.DataFrame:
    try:
        import akshare as ak
        df = ak.stock_zh_a_hist(symbol=code, period="weekly", start_date=start_date, end_date=target_date, adjust="qfq")
        if df is None or df.empty:
            return pd.DataFrame()
        rename_map = {"日期": "date", "开盘": "open", "收盘": "close", "最高": "high", "最低": "low", "成交量": "volume", "成交额": "amount"}
        df = df.rename(columns=rename_map)
        keep_cols = [c for c in ["date", "open", "close", "high", "low", "volume", "amount"] if c in df.columns]
        if len(keep_cols) < 5:
            return pd.DataFrame()
        df = df[keep_cols].copy()
        if "date" in df.columns:
            df["date"] = df["date"].astype(str).str.slice(0, 10)
        for col in ["open", "close", "high", "low", "volume", "amount"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["date", "open", "close", "high", "low"])
        if "amount" in df.columns:
            df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
        df = df.sort_values("date").reset_index(drop=True)
        return set_fetch_source(df, "akshare_hist_qfq_weekly")
    except Exception:
        return pd.DataFrame()


def fetch_from_akshare_weekly_hist_alt(code: str, target_date: str, start_date: str) -> pd.DataFrame:
    try:
        import akshare as ak
        df = ak.stock_zh_a_hist(symbol=code, period="weekly", start_date=start_date, end_date=target_date, adjust="")
        if df is None or df.empty:
            return pd.DataFrame()
        rename_map = {"日期": "date", "开盘": "open", "收盘": "close", "最高": "high", "最低": "low", "成交量": "volume", "成交额": "amount"}
        df = df.rename(columns=rename_map)
        keep_cols = [c for c in ["date", "open", "close", "high", "low", "volume", "amount"] if c in df.columns]
        if len(keep_cols) < 5:
            return pd.DataFrame()
        df = df[keep_cols].copy()
        if "date" in df.columns:
            df["date"] = df["date"].astype(str).str.slice(0, 10)
        for col in ["open", "close", "high", "low", "volume", "amount"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["date", "open", "close", "high", "low"])
        if "amount" in df.columns:
            df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
        df = df.sort_values("date").reset_index(drop=True)
        return set_fetch_source(df, "akshare_hist_plain_weekly")
    except Exception:
        return pd.DataFrame()


def fetch_from_tencent_kline_final(code: str, target_date: str, start_date: str, require_target_date: bool = True) -> pd.DataFrame:
    market = "sh" if code.startswith(('6', '5', '9')) else "sz"
    symbol = f"{market}{code}"
    url = f"https://ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,320,qfq"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': 'https://finance.qq.com/'
    }

    target_date_str = f"{target_date[:4]}-{target_date[4:6]}-{target_date[6:8]}"
    start_date_str = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]}"

    for attempt in range(1, FETCH_RETRIES + 1):
        try:
            if VERBOSE_SCAN_LOG:
                log.debug(f"    [腾讯财经K线] 下载第{attempt}次...")
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as response:
                content = response.read().decode('utf-8', errors='ignore')
                data = json.loads(content)

            if data.get('code') != 0 or not data.get('data'):
                continue

            stock_data = data['data'].get(symbol)
            if not stock_data:
                continue

            kline_data = stock_data.get('day') or stock_data.get('qfqday')
            if not kline_data:
                continue

            data_list = []
            for item in kline_data:
                try:
                    if isinstance(item, list) and len(item) >= 6:
                        date_str = item[0]
                        if len(date_str) != 10 or date_str.count('-') != 2:
                            continue
                        if date_str < start_date_str or date_str > target_date_str:
                            continue
                        amount = float(item[6]) if len(item) >= 7 else 0.0
                        data_list.append({
                            'date': date_str,
                            'open': float(item[1]),
                            'close': float(item[2]),
                            'high': float(item[3]),
                            'low': float(item[4]),
                            'volume': float(item[5]),
                            'amount': amount
                        })
                except (ValueError, IndexError, TypeError):
                    continue

            if not data_list:
                continue

            df = pd.DataFrame(data_list)
            df = df.sort_values('date').reset_index(drop=True)
            df = set_fetch_source(df, "tencent")

            if require_target_date and target_date_str not in df['date'].values:
                if VERBOSE_SCAN_LOG:
                    log.debug(f"    ⚠️  目标日期 {target_date_str} 无数据，重试/跳过")
                continue

            suffix = f"，包含目标日期 {target_date_str}" if require_target_date else ""
            log.debug(f"    ✓ 腾讯财经K线成功 ({len(df)} 条{suffix})")
            return df

        except Exception as e:
            if VERBOSE_SCAN_LOG:
                log.debug(f"    ✗ 第{attempt}次异常: {str(e)[:40]}")

    if code.startswith(("92", "87", "83", "43")):
        return pd.DataFrame()
    return pd.DataFrame()

def fetch_data(code: str, target_date: str, scan_mode: str = "daily") -> pd.DataFrame:
    td_obj = datetime.strptime(target_date, "%Y%m%d")
    lookback_days = WEEKLY_LOOKBACK_DAYS if scan_mode.startswith("weekly") else 250
    if scan_mode == "weekly_light":
        lookback_days = max(90, WEEKLY_LOOKBACK_DAYS // 2)
    start_date = (td_obj - timedelta(days=lookback_days)).strftime("%Y%m%d")
    if VERBOSE_SCAN_LOG:
        log.debug(f"[数据获取] {code}")

    df = load_cache(code, target_date, scan_mode)
    if not df.empty:
        df = apply_qt_snapshot_amount(df, code, target_date)
        df = ensure_amount_column(df, code=code)
        save_cache(df, code, target_date, scan_mode)
        return df

    if scan_mode == "weekly":
        weekly_target = normalize_weekly_target(target_date)
        if not weekly_target:
            return pd.DataFrame()
        df = fetch_from_akshare_weekly_hist(code, target_date, start_date)
        if df.empty:
            df = fetch_from_akshare_weekly_hist_alt(code, target_date, start_date)
        if df.empty:
            daily_df = fetch_from_tencent_kline_final(code, target_date, start_date, require_target_date=False)
            if daily_df.empty:
                daily_df = fetch_from_akshare_hist(code, target_date, start_date)
            if daily_df.empty:
                daily_df = fetch_from_akshare_hist_alt(code, target_date, start_date)
            if not daily_df.empty:
                daily_df = apply_qt_snapshot_amount(daily_df, code, target_date)
                daily_df = ensure_amount_column(daily_df, code=code)
                save_cache(daily_df, code, target_date, "daily")
                df = build_weekly_from_daily(daily_df)
        if df.empty:
            return pd.DataFrame()
        df = ensure_amount_column(df, code=code)
        save_cache(df, code, target_date, scan_mode)
        return df

    df = fetch_from_tencent_kline_final(code, target_date, start_date, require_target_date=True)
    if df.empty:
        df = fetch_from_akshare_hist(code, target_date, start_date)
    if df.empty:
        df = fetch_from_akshare_hist_alt(code, target_date, start_date)
    if df.empty:
        return pd.DataFrame()

    df = apply_qt_snapshot_amount(df, code, target_date)
    df = ensure_amount_column(df, code=code)
    save_cache(df, code, target_date, scan_mode)
    return df


def fetch_weekly_context(code: str, target_date: str) -> tuple:
    td_obj = datetime.strptime(target_date, "%Y%m%d")
    lookback_days = WEEKLY_LOOKBACK_DAYS
    start_date = (td_obj - timedelta(days=lookback_days)).strftime("%Y%m%d")
    weekly_target = normalize_weekly_target(target_date)
    if not weekly_target:
        return pd.DataFrame(), None, None, None, None, None

    daily_df = load_cache(code, target_date, "daily")
    if not daily_df.empty:
        daily_df = apply_qt_snapshot_amount(daily_df, code, target_date)
        daily_df = ensure_amount_column(daily_df, code=code)
        save_cache(daily_df, code, target_date, "daily")
    if daily_df.empty:
        daily_df = fetch_from_tencent_kline_final(code, target_date, start_date, require_target_date=True)
        if daily_df.empty:
            daily_df = fetch_from_akshare_hist(code, target_date, start_date)
        if daily_df.empty:
            daily_df = fetch_from_akshare_hist_alt(code, target_date, start_date)
        if not daily_df.empty:
            daily_df = apply_qt_snapshot_amount(daily_df, code, target_date)
            save_cache(daily_df, code, target_date, "daily")

    if daily_df.empty:
        if VERBOSE_SCAN_LOG:
            log.debug(f"✗ {code} 周线上下文数据获取失败")
        return pd.DataFrame(), None, None, None, None, None

    daily_df = daily_df.copy()
    daily_df = apply_qt_snapshot_amount(daily_df, code, target_date)
    daily_df = ensure_amount_column(daily_df, code=code)
    if "amount" not in daily_df.columns or daily_df["amount"].isna().all() or (daily_df["amount"] <= 0).all():
        if "volume" in daily_df.columns and "close" in daily_df.columns:
            daily_df["amount"] = pd.to_numeric(daily_df["volume"], errors="coerce").fillna(0.0) * pd.to_numeric(daily_df["close"], errors="coerce").fillna(0.0)
    daily_df["date"] = daily_df["date"].astype(str).str.slice(0, 10)
    target_row = daily_df[daily_df["date"] == weekly_target]
    if target_row.empty:
        if VERBOSE_SCAN_LOG:
            log.debug(f"✗ {code} 周线目标日 {weekly_target} 不存在")
        return pd.DataFrame(), None, None, None, None, None

    target_snapshot_amount = float(target_row.iloc[-1].get("amount", 0) or 0)
    target_snapshot_source = str(target_row.iloc[-1].get("amount_source", target_row.iloc[-1].get("fetch_source", "unknown")) or "unknown")
    week_start = (pd.to_datetime(weekly_target) - pd.Timedelta(days=4)).strftime("%Y-%m-%d")
    weekly_window = daily_df[(daily_df["date"] >= week_start) & (daily_df["date"] <= weekly_target)].copy()
    weekly_avg_amount = float(pd.to_numeric(weekly_window.get("amount", pd.Series(dtype=float)), errors="coerce").mean() or 0)
    weekly_df = build_weekly_from_daily(daily_df)
    if weekly_df.empty:
        weekly_df = fetch_from_akshare_weekly_hist(code, target_date, start_date)
    if weekly_df.empty:
        weekly_df = fetch_from_akshare_weekly_hist_alt(code, target_date, start_date)
    if weekly_df.empty:
        if VERBOSE_SCAN_LOG:
            log.debug(f"✗ {code} 周线聚合失败")
        return pd.DataFrame(), None, None, None, None, None

    weekly_total_amount = 0.0
    weekly_total_source = "unknown"
    if "amount" in weekly_df.columns:
        weekly_target_row = weekly_df[weekly_df["date"].astype(str).str.slice(0, 10) == weekly_target]
        if not weekly_target_row.empty:
            weekly_total_amount = float(pd.to_numeric(weekly_target_row.iloc[-1].get("amount", 0), errors="coerce") or 0)
        if weekly_total_amount <= 0:
            weekly_total_amount = float(pd.to_numeric(weekly_df["amount"], errors="coerce").iloc[-1] or 0)
        if weekly_total_amount > 0:
            weekly_total_source = str(weekly_df.iloc[-1].get("amount_source", weekly_df.iloc[-1].get("fetch_source", "unknown")) or "unknown")
    if weekly_total_amount <= 0:
        weekly_total_amount = float(pd.to_numeric(weekly_window.get("amount", pd.Series(dtype=float)), errors="coerce").sum() or 0)
        if weekly_total_amount > 0:
            weekly_total_source = target_snapshot_source
    if weekly_total_amount <= 0:
        weekly_total_amount = target_snapshot_amount
        weekly_total_source = target_snapshot_source
    weekly_avg_source = target_snapshot_source if weekly_avg_amount > 0 else weekly_total_source
    weekly_confidence = "high" if target_snapshot_source == "qt_snapshot" or weekly_total_source == "qt_snapshot" else ("medium" if weekly_total_amount > 0 else "low")
    return weekly_df, weekly_total_amount, weekly_avg_amount if weekly_avg_amount > 0 else target_snapshot_amount, target_snapshot_amount, weekly_total_source, weekly_confidence

def detect_box_pattern(df: pd.DataFrame, current_close: float = None) -> Tuple[float, float, int, float]:
    """改进的箱体识别算法 - 优先返回最近且已被价格确认的箱体"""
    if len(df) < 20:
        return 0, 0, 0, 0

    best_box = None
    best_score = 0
    close_price = float(current_close) if current_close is not None else (float(df.iloc[-1]['close']) if 'close' in df.columns else 0.0)

    for period in [20, 30, 40, 50, 60, 80, 100]:
        if len(df) < period:
            continue

        recent = df.iloc[-period:]
        box_high = recent['high'].max()
        box_low = recent['low'].min()
        if box_low <= 0:
            continue

        box_width = box_high - box_low
        box_width_ratio = box_width / box_low
        box_days = sum(1 for i in range(len(recent))
                      if recent.iloc[i]['high'] <= box_high
                      and recent.iloc[i]['low'] >= box_low)

        if box_width_ratio <= 0.50 or box_days < 12:
            continue

        # 若当前价格已经突破当前周期箱顶，优先返回最短且成立的箱体
        if close_price > box_high * 0.98:
            return box_low, box_high, box_days, box_width_ratio

        touch_score = (close_price / box_high) if box_high > 0 and close_price > 0 else 0.0
        recency_score = 1.0 / period
        box_quality = (box_width_ratio * (box_days / period)) * (0.7 + 0.3 * touch_score) + recency_score

        if box_quality > best_score:
            best_score = box_quality
            best_box = (box_low, box_high, box_days, box_width_ratio)

    if best_box:
        return best_box
    return 0, 0, 0, 0

def detect_limit_up_board(df: pd.DataFrame, code: str = "") -> Tuple[bool, int]:
    """检测涨停板并判断几连板
    Returns: (is_limit_up, consecutive_days)
    """
    if df is None or df.empty or len(df) < 1:
        return False, 0

    try:
        df_check = df.copy()
        df_check['close'] = pd.to_numeric(df_check['close'], errors='coerce')
        df_check['open'] = pd.to_numeric(df_check['open'], errors='coerce')

        if df_check.empty or df_check['close'].isna().all():
            return False, 0

        # 检测今日是否涨停
        today = df_check.iloc[-1]
        today_close = float(today['close'])
        today_open = float(today['open'])

        if len(df_check) < 2 or pd.isna(today['close']):
            return False, 0

        yesterday = df_check.iloc[-2]
        yesterday_close = float(yesterday['close'])

        if yesterday_close <= 0:
            return False, 0

        today_pct_change = (today_close - yesterday_close) / yesterday_close * 100
        is_limit_up_today = today_pct_change >= 9.8

        if not is_limit_up_today:
            return False, 0

        # 计算连续涨停天数
        consecutive_days = 1
        for i in range(len(df_check) - 2, -1, -1):
            if i == 0:
                break
            prev_day = df_check.iloc[i]
            prev_prev_day = df_check.iloc[i - 1]

            prev_close = float(prev_day['close'])
            prev_prev_close = float(prev_prev_day['close'])

            if prev_prev_close <= 0:
                break

            pct = (prev_close - prev_prev_close) / prev_prev_close * 100
            if pct >= 9.8:
                consecutive_days += 1
            else:
                break

        return True, consecutive_days
    except Exception as e:
        if VERBOSE_SCAN_LOG:
            log.debug(f"涨停板检测异常 {code}: {str(e)}")
        return False, 0


def check_strategies(df: pd.DataFrame, enable_momentum_strategies: bool = True) -> Tuple[str, str]:
    try:
        # 动态读取策略开关（这样可以使用配置文件中的设置）
        ENABLE_STRATEGY_BREAKTHROUGH = get_strategy_enabled("BREAKTHROUGH")
        ENABLE_STRATEGY_MA_CLUSTER = get_strategy_enabled("MA_CLUSTER")
        ENABLE_STRATEGY_A_AREA = get_strategy_enabled("A_AREA")
        ENABLE_STRATEGY_B_AREA = get_strategy_enabled("B_AREA")
        ENABLE_STRATEGY_BOX_BOTTOM = get_strategy_enabled("BOX_BOTTOM")
        ENABLE_STRATEGY_BOX_TOP = get_strategy_enabled("BOX_TOP")
        ENABLE_STRATEGY_BOX_INTERNAL = get_strategy_enabled("BOX_INTERNAL")
        ENABLE_STRATEGY_HISTORY_BREAK = get_strategy_enabled("HISTORY_BREAK")
        ENABLE_STRATEGY_MILD_TREND = get_strategy_enabled("MILD_TREND")
        ENABLE_STRATEGY_MA_NEAR = get_strategy_enabled("MA_NEAR")

        if len(df) < 60: return None, ""

        df['ma10'] = df['close'].rolling(10).mean()
        df['ma20'] = df['close'].rolling(20).mean()
        df['ma30'] = df['close'].rolling(30).mean()
        df['ma60'] = df['close'].rolling(60).mean()
        df['ma150'] = df['close'].rolling(150).mean()

        df['vol_ma20_past'] = df['volume'].shift(1).rolling(20).mean()
        df['vol_ratio'] = df['volume'] / df['vol_ma20_past'].replace(0, 1)

        df['is_up'] = df['close'] > df['open']
        df['up_vol'] = np.where(df['is_up'], df['volume'], 0)
        df['down_vol'] = np.where(~df['is_up'], df['volume'], 0)
        df['power_ratio'] = df['up_vol'].rolling(20).sum() / (df['down_vol'].rolling(20).sum() + 1)

        today = df.iloc[-1]
        yest = df.iloc[-2]
        price = today['close']
        pct_change = (price - yest['close']) / yest['close']
        is_yang = today['close'] > today['open']

        past_15_close_max = df['close'].iloc[-16:-1].max()
        is_breaking_platform = price > past_15_close_max
        is_buyable = (0.02 < pct_change < 0.065)

        if ENABLE_STRATEGY_BREAKTHROUGH and enable_momentum_strategies and is_breaking_platform and is_buyable and is_yang and (today['vol_ratio'] > 1.25) and (today['power_ratio'] > 1.15):
            return "👑 突破先手", f"温和突破近15日平台！放量({today['vol_ratio']:.1f}倍)，资金底座扎实(阳线动能{today['power_ratio']:.2f})，抓主升起涨第一天。"

        ma60_is_down = today['ma60'] < df.iloc[-5]['ma60']
        if ma60_is_down and (price < today['ma60'] * 0.90): return None, ""

        ma10 = float(today['ma10']) if pd.notna(today['ma10']) else 0.0
        ma20 = float(today['ma20']) if pd.notna(today['ma20']) else 0.0
        ma30 = float(today['ma30']) if pd.notna(today['ma30']) else 0.0
        ma60 = float(today['ma60']) if pd.notna(today['ma60']) else 0.0
        ma_vals = [m for m in (ma10, ma20, ma30, ma60) if m > 0]
        ma_cluster_ratio = (max(ma_vals) - min(ma_vals)) / ma30 if ma_vals and ma30 > 0 else 999.0
        ma_cluster_center = sum(ma_vals) / len(ma_vals) if ma_vals else 0.0
        near_cluster_center = ma_cluster_center > 0 and abs(price - ma_cluster_center) / ma_cluster_center <= 0.04
        above_short_mas = (price > ma20) and (price > ma30)
        ma30_is_up = ma30 > float(df.iloc[-10]['ma30']) if pd.notna(df.iloc[-10]['ma30']) else False
        ma60_neutral_or_up = (ma60 > 0) and (ma60 >= float(df.iloc[-5]['ma60']) * 0.995 or price > ma60)
        clustered_ma = ma_cluster_ratio <= 0.06
        cluster_launch = clustered_ma and near_cluster_center and above_short_mas and ma30_is_up and ma60_neutral_or_up
        cluster_ignition = (0.005 < pct_change < 0.06) and (today['vol_ratio'] >= 0.98) and (today['power_ratio'] >= 0.95)

        if ENABLE_STRATEGY_MA_CLUSTER and enable_momentum_strategies and cluster_launch and cluster_ignition:
            return "🧲 均线粘合", f"短中期均线粘合({ma_cluster_ratio*100:.1f}%)，价格贴近均线中心，温和放量({today['vol_ratio']:.1f}倍)且阳线动能{today['power_ratio']:.2f}，等待粘合后转强。"

        base_clustered = abs(ma20 - ma30) / ma30 < 0.05 if ma30 > 0 else False
        ma10_turn_up = ma10 > float(yest['ma10']) if pd.notna(yest['ma10']) else False
        mild_ignition = (0.005 < pct_change < 0.06) and (today['vol_ratio'] > 1.05)

        if ENABLE_STRATEGY_A_AREA and enable_momentum_strategies and base_clustered and ma10_turn_up and mild_ignition and (price > ma20) and (today['power_ratio'] > 1.0):
            return "🚀 A区初显", f"均线底座打牢，今日温和放量({today['vol_ratio']:.1f}倍)初次点火，涨幅{pct_change*100:.1f}%，最佳的左侧转右侧潜伏点。"

        ma30_is_up = ma30 > float(df.iloc[-5]['ma30']) if pd.notna(df.iloc[-5]['ma30']) else False
        near_ma30 = abs(price - ma30) / ma30 < 0.04 if ma30 > 0 else False
        near_ma20 = abs(price - ma20) / ma20 < 0.04 if ma20 > 0 else False

        if ENABLE_STRATEGY_B_AREA and enable_momentum_strategies and ma30_is_up and (near_ma30 or near_ma20):
            recent_shrink = df.iloc[-3]['vol_ratio'] < 0.8 or yest['vol_ratio'] < 0.8
            if recent_shrink and (pct_change > 0.005) and (today['vol_ratio'] > 1.05):
                return "🔥 B区起航", f"洗盘动作结束，今日温和放量({today['vol_ratio']:.1f}倍)反包，进攻线半空重新归位仰头。"

            if today['vol_ratio'] < 0.5 and (not is_yang):
                support = "MA30" if near_ma30 else "MA20"
                return "⚖️ B区潜伏", f"中期趋势向上，回踩 {support} 极度缩量({today['vol_ratio']:.2f}倍)，抛压枯竭，密切关注明后天反转。"

        # V13.0 策略：底部缩量
        near_ma20_loose = abs(price - today['ma20']) / today['ma20'] < 0.02
        near_ma30_loose = abs(price - today['ma30']) / today['ma30'] < 0.08
        shrinking_volume = today['vol_ratio'] < 0.9
        ma30_uptrend = today['ma30'] > df.iloc[-10]['ma30']
        power_strong = today['power_ratio'] > 1.12

        if ENABLE_STRATEGY_BOX_BOTTOM and enable_momentum_strategies and (near_ma20_loose or near_ma30_loose) and shrinking_volume and ma30_uptrend and power_strong and (not is_yang):
            return "💎 底部缩量", f"底部区域缩量阴线，MA30上升趋势，资金底座扎实(动能{today['power_ratio']:.2f})，抛压枯竭，蓄势待发。"

        # V15.0 策略：箱体突破（先看前一日箱体，再判断今天是否突破）
        box_source = df.iloc[:-1] if len(df) > 1 else df
        box_low, box_high, box_days, box_width_ratio = detect_box_pattern(box_source, current_close=price)

        # 新增：历史突破策略（突破历史高点）
        past_90_high = df['high'].iloc[-91:-1].max() if len(df) > 90 else df['high'].iloc[:-1].max()
        is_breaking_history = price > past_90_high * 1.01
        history_breakout_momentum_ok = (today['power_ratio'] > 1.05) or ((pct_change >= 0.05) and (today['vol_ratio'] >= 0.95))

        is_positive_day = price > yest['close']

        if box_low > 0 and box_high > 0:
            is_breaking_box_up = price > box_high * 0.99
            has_box_history = box_width_ratio > 0.08
            has_box_consolidation = box_days >= 10
            breakout_momentum_ok = (today['power_ratio'] > 1.0) or ((pct_change >= 0.04) and (today['vol_ratio'] >= 0.85))

            if ENABLE_STRATEGY_BOX_TOP and is_breaking_box_up and has_box_history and has_box_consolidation and is_positive_day and (today['vol_ratio'] > 0.8) and breakout_momentum_ok:
                space_potential = (box_width_ratio) * 100
                return "⭐ 箱体突破", f"箱顶突破({box_low:.2f}-{box_high:.2f})确认！{box_days}天整理后放量上破，后续空间{space_potential:.1f}%，更偏结构突破。"

            box_internal_accel = (
                is_yang
                and (price < box_high * 0.98)
                and (pct_change >= 0.05)
                and (pct_change < 0.12)
                and (today['vol_ratio'] >= 0.9)
                and (today['power_ratio'] >= 1.15)
                and box_days >= 18
            )
            if ENABLE_STRATEGY_BOX_INTERNAL and enable_momentum_strategies and box_internal_accel:
                return "💥 箱内加速", f"箱体内加速({pct_change*100:.1f}%)，放量({today['vol_ratio']:.1f}倍)且动能充足(阳线动能{today['power_ratio']:.2f})，仍在箱体内部偏强运行。"

        if ENABLE_STRATEGY_HISTORY_BREAK and enable_momentum_strategies and is_breaking_history and is_positive_day and (today['vol_ratio'] > 0.9) and history_breakout_momentum_ok:
            space_potential = ((price - past_90_high) / past_90_high) * 100
            return "🚀 历史突破", f"突破{90}日历史高点({past_90_high:.2f})并站稳！放量({today['vol_ratio']:.1f}倍)，资金动能充足(阳线动能{today['power_ratio']:.2f})，更偏趋势新高。"

        mild_trend = (
            is_yang
            and (pct_change >= 0.03)
            and (pct_change < 0.065)
            and (today['close'] > today['ma10'])
            and (today['close'] > today['ma20'])
            and (today['vol_ratio'] >= 0.8)
            and (today['power_ratio'] >= 0.4)
            and (today['power_ratio'] < 1.2)
        )
        if ENABLE_STRATEGY_MILD_TREND and enable_momentum_strategies and mild_trend:
            return "⚪ 温和抬升", f"温和放量上行({pct_change*100:.1f}%)，站上短中期均线，资金动能不强但趋势延续，适合低噪音跟踪。"

        ma60_near = ENABLE_STRATEGY_MA_NEAR and ENABLE_MA_NEAR_60 and pd.notna(today.get('ma60')) and today['ma60'] > 0 and abs(price - today['ma60']) / today['ma60'] <= MA_NEAR_PCT
        ma150_near = ENABLE_STRATEGY_MA_NEAR and ENABLE_MA_NEAR_150 and pd.notna(today.get('ma150')) and today['ma150'] > 0 and abs(price - today['ma150']) / today['ma150'] <= MA_NEAR_PCT
        if ma60_near or ma150_near:
            parts = []
            if ma60_near:
                parts.append(f"MA60偏离{abs(price - today['ma60']) / today['ma60'] * 100:.1f}%")
            if ma150_near:
                parts.append(f"MA150偏离{abs(price - today['ma150']) / today['ma150'] * 100:.1f}%")
            return "🧭 均线邻近", f"价格靠近{'、'.join(parts)}，位于均线附近，适合单独观察。"

        return None, ""
    except Exception as e:
        log.debug(f"策略检查异常: {str(e)}")
        return None, ""

def parse_month_input(month_input: str) -> tuple:
    month_input = str(month_input or "").strip()
    if not month_input:
        return "", "", ""
    try:
        if "-" in month_input:
            dt = datetime.strptime(month_input + "-01", "%Y-%m-%d")
        else:
            dt = datetime.strptime(month_input, "%Y%m")
        month_start = dt.replace(day=1)
        if month_start.year == datetime.now().year and month_start.month == datetime.now().month:
            month_end = datetime.now()
        else:
            next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
            month_end = next_month - timedelta(days=1)
        return month_start.strftime("%Y-%m"), month_start.strftime("%Y%m%d"), month_end.strftime("%Y%m%d")
    except Exception:
        return "", "", ""


def fetch_monthly_daily_data(code: str, month_start: str, month_end: str) -> pd.DataFrame:
    start_dt = datetime.strptime(month_start, "%Y%m%d")
    end_dt = datetime.strptime(month_end, "%Y%m%d")
    lookback_days = max(45, (end_dt - start_dt).days + 15)
    start_date = (start_dt - timedelta(days=lookback_days)).strftime("%Y%m%d")
    df = fetch_from_tencent_kline_final(code, month_end, start_date, require_target_date=False)
    if df.empty:
        df = fetch_from_akshare_hist(code, month_end, start_date)
    if df.empty:
        df = fetch_from_akshare_hist_alt(code, month_end, start_date)
    if df.empty or "date" not in df.columns:
        return pd.DataFrame()
    df = apply_qt_snapshot_amount(df, code, month_end)
    df = ensure_amount_column(df, code=code)
    df = df.copy()
    df["date"] = df["date"].astype(str).str.slice(0, 10).str.replace("-", "", regex=False)
    return df[(df["date"] >= month_start) & (df["date"] <= month_end)].copy()


def calculate_month_gain(df: pd.DataFrame, month_start: str, month_end: str) -> Dict[str, object]:
    if df.empty or "date" not in df.columns:
        return {}
    dfx = df.copy()
    dfx["date"] = dfx["date"].astype(str).str.slice(0, 10)
    dfx = dfx.sort_values("date")
    dfx["open"] = pd.to_numeric(dfx["open"], errors="coerce")
    dfx["close"] = pd.to_numeric(dfx["close"], errors="coerce")
    dfx["high"] = pd.to_numeric(dfx["high"], errors="coerce")
    dfx["low"] = pd.to_numeric(dfx["low"], errors="coerce")
    dfx["volume"] = pd.to_numeric(dfx["volume"], errors="coerce")
    dfx["amount"] = pd.to_numeric(dfx.get("amount", 0), errors="coerce")
    month_mask = (dfx["date"] >= month_start) & (dfx["date"] <= month_end)
    month_df = dfx.loc[month_mask].copy()
    if month_df.empty or len(month_df) < MONTHLY_GAIN_MIN_TRADING_DAYS:
        return {}
    first_day = month_df.iloc[0]
    prev_df = dfx[dfx["date"] < month_start]
    if not prev_df.empty:
        base_price = float(prev_df.iloc[-1]["close"])
        base_source = f"{prev_df.iloc[-1]['date']} 前收盘"
    else:
        base_price = float(first_day["open"])
        base_source = f"{first_day['date']} 月初开盘"
    end_row = month_df.iloc[-1]
    end_price = float(end_row["close"])
    gain_pct = ((end_price - base_price) / base_price * 100.0) if base_price > 0 else 0.0
    month_amount_sum = float(month_df["amount"].fillna(0).sum()) if "amount" in month_df.columns else 0.0
    month_amount_avg = float(month_df["amount"].fillna(0).mean()) if "amount" in month_df.columns else 0.0
    return {
        "gain_pct": gain_pct,
        "base_price": base_price,
        "base_source": base_source,
        "end_price": end_price,
        "high_price": float(month_df["high"].max()),
        "low_price": float(month_df["low"].min()),
        "trading_days": int(len(month_df)),
        "month_amount_sum": month_amount_sum,
        "month_amount_avg": month_amount_avg,
        "month_start": month_start,
        "month_end": month_end,
        "end_date": str(end_row["date"]),
    }


def send_monthly_gain_to_feishu(month_std: str, month_start: str, month_end: str, total_scanned: int, success_count: int, failed_count: int, top_results: List[Dict], top_n: int) -> None:
    if not FEISHU_ENABLED or not FEISHU_WEBHOOK:
        return

    header_text = (
        f"📅 月度涨幅统计 {month_std}\n"
        f"区间：{month_start} ~ {month_end}\n"
        f"总扫描：{total_scanned} | 成功：{success_count} | 失败：{failed_count}\n"
        f"TOP{min(top_n, len(top_results))}"
    )
    post_feishu_payload({"msg_type": "text", "content": {"text": header_text}})

    if not top_results:
        post_feishu_payload({"msg_type": "text", "content": {"text": "暂无结果"}})
        return

    chunk_size = 5
    total_chunks = (len(top_results) + chunk_size - 1) // chunk_size
    for chunk_idx in range(total_chunks):
        start = chunk_idx * chunk_size
        chunk = top_results[start:start + chunk_size]
        lines = []
        for idx, item in enumerate(chunk, start + 1):
            business = str(item.get("business_summary", "")).replace("\n", " ").strip()
            sector_text = str(item.get("sector", "")).replace("\n", " ").strip()
            lines.append(
                f"{idx}. {item['name']}({item['code']}) {item['gain_pct']:.2f}%\n"
                f"   起点 {item['base_price']:.2f} → 终点 {item['end_price']:.2f}\n"
                f"   板块：{sector_text}\n"
                f"   业务：{business if business else '未知'}"
            )
        payload = {
            "msg_type": "text",
            "content": {"text": f"TOP明细（{chunk_idx + 1}/{total_chunks}）\n" + "\n".join(lines)},
        }
        post_feishu_payload(payload)


def run_monthly_gain_ranking() -> bool:
    month_input = os.getenv("MONTHLY_STATS_MONTH", "").strip()
    month_std, month_start, month_end = parse_month_input(month_input)
    if not month_std:
        print("❌ 月份格式错误，请使用 YYYYMM 或 YYYY-MM")
        return False
    if month_end > datetime.now().strftime("%Y%m%d"):
        month_end = datetime.now().strftime("%Y%m%d")
    print(f"\n📅 月度涨幅统计：{month_std}（{month_start} ~ {month_end}）\n")
    print(f"🔧 月度统计参数：TOP{max(1, MONTHLY_GAIN_TOP_N)} | 最少交易日 {MONTHLY_GAIN_MIN_TRADING_DAYS}")

    pool = load_a_share_pool()
    if not pool:
        print("❌ 无法加载全A股股票清单")
        return False

    items = [(code, info) for code, info in pool.items() if is_a_share_code(code) and not is_st_stock(code, info.get("name", ""))]
    scan_limit_env = os.getenv("SCAN_LIMIT", "").strip()
    scan_test_mode = os.getenv("SCAN_TEST_MODE", "0").strip() == "1"
    if scan_test_mode and scan_limit_env:
        try:
            scan_limit = max(1, int(scan_limit_env))
            items = items[:scan_limit]
            print(f"🧪 测试模式：仅统计前 {len(items)} 只标的")
        except ValueError:
            pass

    print(f"🔧 统计并发数: {max(1, min(SCAN_WORKERS, 16, len(items) if items else 1))} | 有效标的: {len(items)}")
    results = []
    failed = 0
    with ThreadPoolExecutor(max_workers=max(1, min(SCAN_WORKERS, 16, len(items) if items else 1))) as executor:
        future_map = {
            executor.submit(fetch_monthly_daily_data, code, month_start, month_end): (code, info)
            for code, info in items
        }
        for idx, future in enumerate(as_completed(future_map), 1):
            code, info = future_map[future]
            try:
                df = future.result()
                if df.empty:
                    failed += 1
                    continue
                stat = calculate_month_gain(df, month_start, month_end)
                if not stat:
                    failed += 1
                    continue
                results.append({
                    "code": code,
                    "name": info.get("name", code),
                    "sector": resolve_stock_concept(code, info.get("sector", "未知板块")),
                    "business_summary": str(info.get("business_summary", "")).strip(),
                    **stat,
                })
            except Exception as e:
                failed += 1
                log.debug(f"月度统计异常 {code}: {str(e)}")

    results.sort(key=lambda x: x.get("gain_pct", 0), reverse=True)
    top_n = max(1, MONTHLY_GAIN_TOP_N)
    top_results = results[:top_n]
    print(f"\n🏆 {month_std} 涨幅 TOP{top_n}\n")
    if not top_results:
        print("⚠️  本月没有可展示的涨幅结果，可能是全部标的都被数据源或交易日条件过滤掉了。")
    for idx, item in enumerate(top_results, 1):
        print(
            f"{idx:>3}. {item['name']} ({item['code']}) | 涨幅 {item['gain_pct']:.2f}% | "
            f"起点 {item['base_price']:.2f} ({item['base_source']}) | 终点 {item['end_price']:.2f} | "
            f"交易日 {item['trading_days']} | 板块 {item['sector']} | 业务 {truncate_reason(item.get('business_summary', ''), limit=20) if item.get('business_summary') else '未知'}"
        )

    output_dir = os.path.join(RESULTS_DIR, "monthly_gain")
    os.makedirs(output_dir, exist_ok=True)
    output_file = os.path.join(output_dir, f"{month_std}.json")
    payload = {
        "month": month_std,
        "month_start": month_start,
        "month_end": month_end,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_scanned": len(items),
        "success_count": len(results),
        "failed_count": failed,
        "top_n": top_n,
        "results": top_results,
    }
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\n💾 已保存月度排行: {output_file}\n")
    print(f"📣 月度排行结果已准备推送飞书（{len(top_results)} 条）")
    send_monthly_gain_to_feishu(month_std, month_start, month_end, len(items), len(results), failed, top_results, top_n)
    return True


def validate_trading_date(date_str: str) -> bool:
    try:
        dt = datetime.strptime(date_str, "%Y%m%d")
        if dt.weekday() >= 5:
            return False
        holidays = ["20250101", "20250102", "20250103", "20250429", "20250430", "20250501", "20250502", "20250503", "20250818"]
        if date_str in holidays:
            return False
        return True
    except:
        return False

def run_amount_regression():
    # 如果启用了QT快照，跳过回归测试
    # 原因：QT快照只在当天有效，历史日期无法获取QT快照
    # 回归测试原本是为了验证volume*close的填充逻辑，现已移除
    if QT_SNAPSHOT_ENABLED:
        log.info("✓ 已启用QT快照模式，成交额数据来自QT快照，跳过回归测试")
        return True

    mode = os.getenv("AMOUNT_REGRESSION_MODE", "unit").strip().lower()
    unit_samples = [
        {"code": "688112", "name": "鼎阳科技", "date": "20260408", "expect_signal": True},
        {"code": "688337", "name": "普源精电", "date": "20260408", "expect_signal": True},
        {"code": "688455", "name": "科捷智能", "date": "20260408", "expect_signal": True},
        {"code": "688628", "name": "优利德", "date": "20260408", "expect_signal": True},
        {"code": "688693", "name": "锴威特", "date": "20260408", "expect_signal": True},
        {"code": "688702", "name": "盛科通信-U", "date": "20260408", "expect_signal": True},
        {"code": "688001", "name": "华兴源创", "date": "20260408", "expect_signal": True},
        {"code": "688700", "name": "东威科技", "date": "20260408", "expect_signal": True},
        {"code": "688127", "name": "蓝特光学", "date": "20260408", "expect_signal": True},
        {"code": "002460", "name": "赣锋锂业", "date": "20260408", "expect_signal": True},
        {"code": "600105", "name": "永鼎股份", "date": "20260408", "expect_signal": True},
        {"code": "603778", "name": "国晟科技", "date": "20260408", "expect_signal": True},
        {"code": "002222", "name": "福晶科技", "date": "20260408", "expect_signal": True},
    ]
    threshold_samples = [
        {"code": "300308", "name": "中际旭创", "date": "20260408", "threshold": DAILY_AMOUNT_MIN, "expect_pass": True, "expect_signal": True},
        {"code": "300502", "name": "新易盛", "date": "20260408", "threshold": DAILY_AMOUNT_MIN, "expect_pass": True, "expect_signal": True},
        {"code": "688702", "name": "盛科通信-U", "date": "20260408", "threshold": DAILY_AMOUNT_MIN, "expect_pass": False, "expect_signal": True},
        {"code": "688700", "name": "东威科技", "date": "20260408", "threshold": DAILY_AMOUNT_MIN, "expect_pass": False, "expect_signal": True},
    ]
    samples = unit_samples if mode == "unit" else threshold_samples
    print(f"\n🧪 成交额回归测试开始 [{mode}]\n")
    failed = []
    lines = []
    regression_deadline = time.time() + 180
    for sample in samples:
        if time.time() > regression_deadline:
            print("\n⚠️ 回归超时，提前结束，避免阻塞启动")
            failed.append({"code": "timeout", "reason": "regression_deadline_exceeded", "raw": 0, "normalized": 0, "threshold": DAILY_AMOUNT_MIN})
            break
        code = sample["code"]
        name = sample["name"]
        target_date = sample["date"]
        threshold = sample.get("threshold", DAILY_AMOUNT_MIN)
        df = fetch_data(code, target_date, scan_mode="daily")
        if df.empty:
            msg = f"❌ {code} {name} | reason=data_empty | date={target_date}"
            print(msg)
            lines.append(msg)
            failed.append({"code": code, "reason": "data_empty"})
            continue
        df = ensure_amount_column(df, code=code)
        target_row, _ = pick_target_row(df, target_date, code=code)
        if target_row.empty:
            msg = f"❌ {code} {name} | reason=target_row_missing | date={target_date}"
            print(msg)
            lines.append(msg)
            failed.append({"code": code, "reason": "target_row_missing"})
            continue
        raw_amount = float(target_row.iloc[-1].get("amount_raw", 0) or 0)
        normalized_amount = float(target_row.iloc[-1].get("amount", 0) or 0)
        unit_ratio = normalized_amount / raw_amount if raw_amount > 0 else 0.0
        log_amount_check(code, name, raw_amount, normalized_amount, threshold, float('inf'), scan_mode="daily")
        sig_type, reason = check_strategies(df, enable_momentum_strategies=True)
        passed = normalized_amount >= threshold
        pass_reason = "threshold_ok" if passed else "threshold_fail"
        if mode == "unit":
            threshold_ok = True
            unit_ok = raw_amount == 0 or (0.8 <= unit_ratio <= 1.2)
        else:
            threshold_ok = passed == sample["expect_pass"]
            unit_ok = raw_amount == 0 or (0.8 <= unit_ratio <= 1.2)
        signal_ok = bool(sig_type) == sample["expect_signal"]
        signal_reason = "signal_ok" if signal_ok else f"signal_mismatch:{sig_type or 'None'}"
        line = (
            f"{code} {name} | raw={raw_amount/100000000:.2f}亿 | normalized={normalized_amount/100000000:.2f}亿 | "
            f"ratio={unit_ratio:.2f}x | threshold={threshold/100000000:.2f}亿 | pass={passed} | expected_pass={sample.get('expect_pass', 'N/A')} | signal={sig_type or 'None'}"
        )
        if not unit_ok:
            failed.append({"code": code, "reason": f"unit_mismatch:{unit_ratio:.2f}x", "raw": raw_amount, "normalized": normalized_amount, "threshold": threshold})
        print(line)
        lines.append(line)
        sources = compare_sample_sources(code, target_date, df)
        if sources:
            lines.append(f"COMPARE {code} sources={len(sources)}")
            for snap in sources:
                src = snap.get("source", "unknown")
                src_raw = float(snap.get("raw_amount", 0) or 0)
                src_norm = float(snap.get("normalized_amount", 0) or 0)
                src_derived = float(snap.get("derived_amount", 0) or 0)
                src_volume = float(snap.get("volume", 0) or 0)
                src_close = float(snap.get("close", 0) or 0)
                src_ratio = (src_norm / raw_amount) if raw_amount > 0 else 0.0
                src_line = (
                    f"  - {src} | date={snap.get('date', '')} | close={src_close:.2f} | volume={src_volume:.0f} | "
                    f"raw={src_raw/100000000:.2f}亿 | normalized={src_norm/100000000:.2f}亿 | derived={src_derived/100000000:.2f}亿 | ratio_vs_main={src_ratio:.2f}x"
                )
                print(src_line)
                lines.append(src_line)
        if not threshold_ok:
            failed.append({"code": code, "reason": pass_reason, "raw": raw_amount, "normalized": normalized_amount, "threshold": threshold})
        if not signal_ok:
            failed.append({"code": code, "reason": signal_reason, "raw": raw_amount, "normalized": normalized_amount, "threshold": threshold})
    synthetic = pd.DataFrame([
        {"date": "2026-04-08", "open": 10.0, "close": 10.5, "high": 10.6, "low": 9.8, "volume": 1000000, "amount": 10500000},
        {"date": "2026-04-09", "open": 10.5, "close": 10.7, "high": 10.8, "low": 10.4, "volume": 1200000, "amount": 0},
    ])
    synthetic = ensure_amount_column(synthetic, code="000001")
    syn_raw = float(synthetic.iloc[-1].get("amount_raw", 0) or 0)
    syn_norm = float(synthetic.iloc[-1].get("amount", 0) or 0)
    syn_pass = syn_norm >= DAILY_AMOUNT_MIN
    synthetic_line = f"SYNTHETIC | raw={syn_raw/100000000:.2f}亿 | normalized={syn_norm/100000000:.2f}亿 | threshold={DAILY_AMOUNT_MIN/100000000:.2f}亿 | pass={syn_pass}"
    print(synthetic_line)
    lines.append(synthetic_line)
    if syn_norm <= 0:
        failed.append({"code": "synthetic", "reason": "amount_fill_failed", "raw": syn_raw, "normalized": syn_norm, "threshold": DAILY_AMOUNT_MIN})

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    result_path = os.path.join(LOG_DIR, f"amount_regression_{stamp}.log")
    try:
        with open(result_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
            f.write("\n")
        print(f"\n📝 回归结果已写入: {result_path}")
    except Exception as e:
        print(f"\n⚠️ 回归结果写入失败: {e}")
    if failed:
        print("\n❌ 回归未通过，失败明细：")
        for item in failed:
            code = item.get("code", "unknown")
            reason = item.get("reason", "unknown")
            raw_amount = float(item.get("raw", 0) or 0)
            normalized_amount = float(item.get("normalized", 0) or 0)
            threshold = float(item.get("threshold", 0) or 0)
            print(
                f"  - {code} | reason={reason} | raw={raw_amount/100000000:.2f}亿 | "
                f"normalized={normalized_amount/100000000:.2f}亿 | threshold={threshold/100000000:.2f}亿"
            )
        print(f"📝 失败日志位置: {result_path}")
        return False
    print("\n✅ 成交额回归通过")
    return True


def merge_duplicate_signals(signals: List[Dict]) -> List[Dict]:
    """合并同一只股票的多个策略信号
    将同一只股票的多个策略合并为一条，策略优先级按PRIORITY_ORDER排序
    """
    if not signals:
        return []

    signal_map = {}
    for signal in signals:
        code = signal.get('code', '')
        if code not in signal_map:
            signal_map[code] = signal
        else:
            # 比较策略优先级，保留优先级更高的（数值更小）
            existing = signal_map[code]
            existing_priority = PRIORITY_ORDER.get(existing.get('type', ''), 99)
            new_priority = PRIORITY_ORDER.get(signal.get('type', ''), 99)

            if new_priority < existing_priority:
                signal_map[code] = signal
            elif new_priority == existing_priority:
                # 优先级相同，需要合并策略描述
                existing_type = existing.get('type', '')
                new_type = signal.get('type', '')
                if existing_type != new_type:
                    # 将新策略追加到reason中
                    existing_reason = existing.get('reason', '')
                    new_reason = signal.get('reason', '')
                    combined_reason = f"{existing_reason} | 同时触发: {new_reason}"
                    existing['reason'] = truncate_reason(combined_reason, limit=120)
                    existing['combined_types'] = f"{existing_type}、{new_type}"

    return list(signal_map.values())


def print_limit_up_signals(signals: List[Dict]) -> None:
    """单独打印涨停板信号"""
    limit_up_signals = [s for s in signals if s.get('is_limit_up', False)]
    if not limit_up_signals:
        return

    print("\n" + "━" * 60)
    print("🔴 涨停板信号")
    print("━" * 60)
    for idx, s in enumerate(limit_up_signals, 1):
        consecutive = s.get('consecutive_limit_up_days', 0)
        board_tag = f"({consecutive}连板)" if consecutive > 1 else "(首板)"
        amount_text = f"{float(s.get('amount', 0) or 0) / 100000000:.2f}亿" if s.get('amount') is not None else "未知"
        print(f"  {idx}. {s['name']} ({s['code']}) {board_tag} | 现价:{s['price']:.2f} | {s.get('limit_up_tag', '')} | 策略:{s['type']}")
        print(f"     {truncate_reason(s['reason'])}")
    print("━" * 60)


def run_scan():
    global WEEKLY_AMOUNT_MIN, WEEKLY_AMOUNT_MAX, DAILY_AMOUNT_MIN, DAILY_AMOUNT_MAX, QT_SNAPSHOT_AMOUNT_MAP, QT_SNAPSHOT_META
    scan_mode = os.getenv("SCAN_MODE", "daily").strip().lower()
    if scan_mode == "monthly_stats":
        scan_mode = "monthly_gain"
    scan_light = os.getenv("SCAN_LIGHT", "0").strip() == "1"
    scan_test_mode = os.getenv("SCAN_TEST_MODE", "0").strip() == "1"
    scan_limit_env = os.getenv("SCAN_LIMIT", "").strip()
    scan_limit = 0
    if scan_test_mode and scan_limit_env:
        try:
            scan_limit = max(1, int(scan_limit_env))
        except ValueError:
            scan_limit = 0
    elif scan_limit_env and not scan_test_mode:
        log.debug(f"⚠️  已忽略 SCAN_LIMIT={scan_limit_env}，因为未开启测试模式")
    if scan_light and scan_mode == "weekly":
        scan_mode = "weekly_light"
    env_date = os.getenv("SCAN_DATE", "").strip()
    if scan_mode == "monthly_gain" or os.getenv("MONTHLY_STATS_ONLY", "0").strip() == "1":
        print("📊 已进入月度涨幅统计模式")
        return run_monthly_gain_ranking()
    is_weekly = scan_mode.startswith("weekly")
    if is_weekly:
        weekly_amount_min_env = os.getenv("WEEKLY_AMOUNT_MIN", "").strip()
        weekly_amount_max_env = os.getenv("WEEKLY_AMOUNT_MAX", "").strip()
        if weekly_amount_min_env:
            try:
                WEEKLY_AMOUNT_MIN = float(weekly_amount_min_env)
            except ValueError:
                pass
        if weekly_amount_max_env:
            try:
                WEEKLY_AMOUNT_MAX = float(weekly_amount_max_env)
            except ValueError:
                pass
        print("📌 周线模式：按周均成交额过滤")
    if not is_weekly:
        # 日线模式：手动输入成交额区间
        pipeline_mode = os.getenv("PIPELINE_MODE", "0").strip() == "1"
        daily_amount_min_env = os.getenv("DAILY_AMOUNT_MIN", "").strip()
        daily_amount_max_env = os.getenv("DAILY_AMOUNT_MAX", "").strip()
        if daily_amount_min_env:
            try:
                DAILY_AMOUNT_MIN = float(daily_amount_min_env)
            except ValueError:
                pass
        if daily_amount_max_env:
            try:
                DAILY_AMOUNT_MAX = float(daily_amount_max_env)
            except ValueError:
                pass
        elif not pipeline_mode:
            try:
                min_input = input("请输入日线模式的最小成交额（默认4000000000，直接回车则使用默认）: ").strip()
            except EOFError:
                min_input = ""
            if min_input:
                try:
                    DAILY_AMOUNT_MIN = float(min_input)
                except ValueError:
                    print("⚠️  最小值输入无效，继续使用默认值")
            try:
                max_input = input("请输入日线模式的最大成交额（默认无限制，直接回车则使用默认）: ").strip()
            except EOFError:
                max_input = ""
            if max_input:
                try:
                    DAILY_AMOUNT_MAX = float(max_input)
                except ValueError:
                    print("⚠️  最大值输入无效，继续使用默认值")
        max_text = "∞" if DAILY_AMOUNT_MAX == float('inf') else f"{DAILY_AMOUNT_MAX:.0f}"
        print(f"📌 日线模式：成交额区间 {DAILY_AMOUNT_MIN:.0f} ~ {max_text} 的标的将被保留")

        # 策略配置已通过菜单设置，不再询问
        enable_momentum_strategies = True
        print(f"📌 策略已启用（通过配置菜单）")
    else:
        enable_momentum_strategies = True
    if is_weekly:
        print("📌 周线模式：按周均成交额过滤")

    scan_stats = {
        "amount_filtered": 0,
        "amount_soft_warned": 0,
        "daily_amount_filtered": 0,
        "data_failed": 0,
        "weekly_context_missing": 0,
        "strategy_miss": 0,
        "concept_filtered": 0,
    }

    def print_scan_stats():
        if is_weekly:
            print(
                f"📊 周线统计：周均过滤 {scan_stats['amount_filtered']} | 软提示 {scan_stats['amount_soft_warned']} | 上下文缺失 {scan_stats['weekly_context_missing']} | "
                f"数据失败 {scan_stats['data_failed']} | 策略未命中 {scan_stats['strategy_miss']} | 概念剔除 {scan_stats['concept_filtered']}"
            )
        else:
            print(
                f"📊 日线统计：成交额过滤 {scan_stats['daily_amount_filtered']} | 数据失败 {scan_stats['data_failed']} | "
                f"策略未命中 {scan_stats['strategy_miss']} | 概念剔除 {scan_stats['concept_filtered']}"
            )

    def scan_one(code: str, info: Dict[str, str], target_date: str):
        try:
            if is_weekly:
                df, weekly_total_amount, weekly_avg_amount, target_snapshot_amount, weekly_total_source, weekly_confidence = fetch_weekly_context(code, target_date)
                if df.empty:
                    scan_stats["weekly_context_missing"] += 1
                    return None, None, None, None
                df = ensure_amount_column(df, code=code)
                target_row, _ = pick_target_row(df, target_date, code=code)
                if target_row.empty:
                    scan_stats["weekly_context_missing"] += 1
                    return None, None, None, None
                row = target_row.iloc[-1]
                raw_amount = float(row.get("amount_raw", 0) or 0)
                normalized_amount = float(row.get("amount", 0) or 0)
                amount_source = str(row.get("amount_source", row.get("fetch_source", weekly_total_source or "unknown")) or "unknown")
                amount_for_filter = float(weekly_avg_amount or 0)
                target_amount = float(weekly_total_amount or 0)
                if raw_amount > 0:
                    log.debug(f"[金额单位][weekly] {code} {info.get('name', code)} | source={amount_source} | ratio={normalized_amount / raw_amount:.2f}x")
                log_amount_check(code, info.get("name", code), amount_for_filter, amount_for_filter, WEEKLY_AMOUNT_MIN, WEEKLY_AMOUNT_MAX, scan_mode="weekly")
                hard_filter = weekly_confidence == "high" or amount_source == "qt_snapshot" or weekly_total_source == "qt_snapshot"
                if hard_filter and (amount_for_filter < WEEKLY_AMOUNT_MIN or amount_for_filter > WEEKLY_AMOUNT_MAX):
                    scan_stats["amount_filtered"] += 1
                    return None, None, None, None
                if not hard_filter and (amount_for_filter < WEEKLY_AMOUNT_MIN or amount_for_filter > WEEKLY_AMOUNT_MAX):
                    scan_stats["amount_soft_warned"] += 1
                target_week = normalize_weekly_target(target_date)
                sig_type, reason = check_weekly_strategies(df, target_week, target_amount=target_amount)
            else:
                df = fetch_data(code, target_date, scan_mode=scan_mode)
                if df.empty:
                    scan_stats["data_failed"] += 1
                    return None, None, None, None
                df = ensure_amount_column(df, code=code)
                target_row, target_date_str = pick_target_row(df, target_date, code=code)
                if target_row.empty:
                    scan_stats["data_failed"] += 1
                    return None, None, None, None
                row = target_row.iloc[-1]
                raw_amount = float(row.get("amount_raw", 0) or 0)
                normalized_amount = float(row.get("amount", 0) or 0)
                amount_source = str(row.get("amount_source", row.get("fetch_source", "unknown")) or "unknown")
                if raw_amount > 0:
                    log.debug(f"[金额单位][daily] {code} {info.get('name', code)} | source={amount_source} | ratio={normalized_amount / raw_amount:.2f}x")
                log_amount_check(code, info.get("name", code), raw_amount, normalized_amount, DAILY_AMOUNT_MIN, DAILY_AMOUNT_MAX, scan_mode="daily")

                # 日线严格过滤：不在你输入的成交额区间内直接剔除
                if normalized_amount < DAILY_AMOUNT_MIN or normalized_amount > DAILY_AMOUNT_MAX:
                    scan_stats["daily_amount_filtered"] += 1
                    return None, None, None, None

                target_amount = normalized_amount
                sig_type, reason = check_strategies(df, enable_momentum_strategies=enable_momentum_strategies)

            # 【修复】确保变量初始化 - 日线分支缺失的变量
            if not is_weekly:
                target_snapshot_amount = target_amount
                weekly_total_amount = None
                weekly_avg_amount = None
                weekly_total_source = "daily"
                weekly_confidence = "low"

            scan_date_local = str(df.iloc[-1]['date'])[:10]
            signal = None
            if sig_type:
                concept = resolve_stock_concept(code, info.get("sector", "未知板块"))
                if _normalize_sector_text(concept) not in {_normalize_sector_text(item) for item in EXCLUDED_SECTORS}:
                    # 计算当日涨幅
                    today_close = float(df.iloc[-1]['close'])
                    yesterday_close = float(df.iloc[-2]['close']) if len(df) >= 2 else today_close
                    daily_pct_change = ((today_close - yesterday_close) / yesterday_close * 100) if yesterday_close > 0 else 0.0

                    # 检测涨停板
                    is_limit_up, consecutive_days = detect_limit_up_board(df, code=code)
                    limit_up_tag = ""
                    if is_limit_up:
                        limit_up_tag = f"🔴{consecutive_days}连板"
                        if VERBOSE_SCAN_LOG:
                            log.debug(f"✓ {code} 检测到涨停板: {consecutive_days}连板")

                    signal = {
                        "name": info["name"],
                        "code": code,
                        "sector": concept,
                        "business_summary": resolve_business_summary(info),
                        "price": today_close,
                        "daily_pct_change": daily_pct_change,
                        "amount": float(target_snapshot_amount or target_amount or 0),
                        "week_total_amount": float(weekly_total_amount or 0),
                        "week_avg_amount": float(weekly_avg_amount or 0),
                        "week_total_source": str(weekly_total_source or "unknown"),
                        "week_confidence": str(weekly_confidence or "low"),
                        "amount_source": str(target_row.iloc[-1].get("amount_source", target_row.iloc[-1].get("fetch_source", "unknown")) or "unknown"),
                        "type": sig_type,
                        "reason": reason,
                        "is_limit_up": is_limit_up,
                        "consecutive_limit_up_days": consecutive_days,
                        "limit_up_tag": limit_up_tag
                    }
                else:
                    scan_stats["concept_filtered"] += 1
            else:
                scan_stats["strategy_miss"] += 1
            return scan_date_local, signal, None, df
        except Exception as e:
            log.debug(f"处理 {code} 异常: {type(e).__name__}: {str(e)}\n{traceback.format_exc(limit=3)}")
            return None, None, None, None

    print("\n" + "="*70)
    print(" [Horse] 欢迎使用『三度操盘·实战量化机 V17.10 全A股扫描版』")
    mode_name = '周线轻量' if scan_light else ('周线' if is_weekly else '日线')
    print(f" [Gear] 全A股扫描 + 板块TOP3 + 最强信号优先 + {mode_name}复盘提示")
    print(" [Chart] V17.10 版本：全A股扫描 + 飞书推送 + 系统报警")
    print("="*70 + "\n")

    print(f"[配置] 飞书推送: {'✓ 已启用' if FEISHU_ENABLED else '✗ 已禁用'}")
    if FEISHU_ENABLED and FEISHU_WEBHOOK:
        print(f"[配置] Webhook: {FEISHU_WEBHOOK[:50]}...")
    print()

    pipeline_mode = os.getenv("PIPELINE_MODE", "0").strip() == "1"
    if env_date:
        target_date = env_date
    elif not pipeline_mode:
        try:
            target_date = input("👉 请输入历史复盘日期 (直接回车则默认最新交易日): ").strip()
        except EOFError:
            target_date = ""
    else:
        target_date = ""
    if not target_date:
        target_date = datetime.now().strftime("%Y%m%d")
    else:
        target_date = target_date.replace("-", "")

    if len(target_date) != 8 or not target_date.isdigit():
        print(f"❌ 日期格式错误！请使用 YYYYMMDD 格式")
        return

    if not validate_trading_date(target_date) and not is_weekly:
        print(f"⚠️  {target_date} 可能不是交易日（周末或节假日）")
        if not pipeline_mode:
            try:
                confirm = input("   是否继续？(y/n): ").strip().lower()
            except EOFError:
                confirm = "y"
        else:
            confirm = "y"
        if confirm != 'y':
            return

    print(f"\n🔄 清空旧缓存...")
    clear_cache()

    print(f"\n🔍 目标锚定：获取截止至 [{target_date}] 的数据进行深度扫描...")
    print(f"📝 详细日志: {os.path.join(LOG_DIR, f'hunter_sys_{today_str}.log')}\n")

    pool = load_a_share_pool()
    if not pool:
        print("❌ 无法加载全A股股票清单")
        return

    if QT_SNAPSHOT_ENABLED and target_date == datetime.now().strftime("%Y%m%d"):
        print("🛰️  正在批量获取 qt 快照成交额...")
        fetch_qt_snapshot_map(list(pool.keys()))
        sample_codes = list(pool.keys())[:3]
        for sample_code in sample_codes:
            sample_df = fetch_data(sample_code, target_date, scan_mode="daily")
            if sample_df.empty:
                print(f"🛰️  样本 {sample_code}: 数据为空")
                continue
            sample_row = sample_df.iloc[-1]
            sample_source = str(sample_row.get("amount_source", sample_row.get("fetch_source", "unknown")) or "unknown")
            sample_amount = float(sample_row.get("amount", 0) or 0)
            print(f"🛰️  样本 {sample_code}: source={sample_source} amount={sample_amount/100000000:.2f}亿")

    signals = []
    total = len(pool)
    scan_date = "未知"
    success_count = 0

    items = [
        (code, info)
        for code, info in pool.items()
        if not is_st_stock(code, info.get('name', '')) and is_a_share_code(code)
    ]
    st_count = len(pool) - len(items)
    if st_count > 0:
        print(f"🧹 已忽略 ST 标的: {st_count} 只")
    if scan_limit:
        items = items[:scan_limit]
        print(f"🧪 测试模式：仅扫描前 {len(items)} 只标的")
        log.debug(f"🧪 测试模式开启：SCAN_LIMIT={scan_limit}")

    total = len(items)
    log.debug(f"✓ 本次扫描有效标的: {total} 只")
    max_workers = max(1, min(SCAN_WORKERS, 16, total if total else 1))
    print(f"🔧 扫描并发数: {max_workers} | 有效标的: {total}")

    executor = ThreadPoolExecutor(max_workers=max_workers)
    futures = {}
    interrupted = False
    try:
        for code, info in items:
            print(f"\r⏳ 扫描排队: {info['name']} ({code}) ...", end="", flush=True)
            futures[executor.submit(scan_one, code, info, target_date)] = (code, info)

        for idx, future in enumerate(as_completed(futures), 1):
            code, info = futures[future]
            if idx == 1 or idx % 100 == 0 or idx == total:
                print(f"\r⏳ 扫描进度: [{idx}/{total}] 正在把脉: {info['name']} ({code}) ...", end="", flush=True)
            try:
                scan_date_local, signal, observer, df = future.result()
                if df is None or getattr(df, 'empty', True):
                    continue

                success_count += 1
                if scan_date == "未知" or scan_date_local > scan_date:
                    scan_date = scan_date_local

                if signal:
                    signals.append(signal)

            except Exception as e:
                log.debug(f"处理 {code} 异常: {str(e)}")
    except KeyboardInterrupt:
        interrupted = True
        print("\n\n👋 程序已中断")
        log.debug("⚠️ 扫描被用户中断，开始取消未完成任务")
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    if interrupted:
        partial_scan_date = scan_date if scan_date != "未知" else target_date
        if success_count > 0:
            save_scan_results(partial_scan_date, scan_mode, total, success_count, signals)
            print(f"\n💾 已保存中断前的部分结果：{len(signals)} 个信号")
            send_to_feishu(signals, partial_scan_date, total, success_count, scan_mode=scan_mode, status_note="扫描中断，以下为已完成部分")
        print_scan_stats()
        return

    print("\n\n✅ 扫描完成！正在生成作战简报...\n")

    if success_count == 0:
        print(f"\n⚠️  本次扫描没有留下有效数据，可能全部被成交额或上下文条件过滤掉了。\n")
        print_scan_stats()
        return

    if not signals:
        msg = f"📅 实际复盘日期：{scan_date}\n\n在 {total} 只标的中，未发现{'周线突破前高、底部企稳或止跌反抽' if is_weekly else '『突破先手』或成型的『A/B区』'}。耐心等待主力动作。"
        print(msg)
        send_to_feishu([], scan_date, total, success_count, scan_mode=scan_mode)
        return

    # 【新增】合并同一只股票的多个策略信号
    signals = merge_duplicate_signals(signals)

    signals.sort(key=lambda x: (PRIORITY_ORDER.get(x['type'], 99), x.get('sector', '未知板块'), x['code']))

    grouped = group_signals_by_sector(signals)
    print(summary_brief(scan_date, total, success_count, signals, grouped, scan_mode=scan_mode))
    print(f"🔎 仅供{'周线' if is_weekly else '日线'}复盘参考")
    print(f"🏅 板块TOP3（热度+最强信号）: {sector_top3_brief(grouped)}")

    print("━" * 60)
    print("🏆 今日最强板块")
    print("━" * 60)
    print(f"  {sector_summary_line(grouped[0][0], grouped[0][1])}")
    print("━" * 60)
    print("📋 主线概念热度排行")
    print("━" * 60)
    print("📋 全量信号明细")
    print("━" * 60)
    for sector, sector_signals in grouped:
        print(f"  {sector_summary_line(sector, sector_signals)}")
    print("━" * 60)
    print("🔥 核心信号 TOP5")
    print("━" * 60)
    for idx, s in enumerate(signals[:5], 1):
        amount_source = str(s.get("amount_source", "unknown") or "unknown")
        entry_date = str(s.get("entry_date", scan_date) or scan_date)
        amount_text = f"{float(s.get('amount', 0) or 0) / 100000000:.2f}亿" if s.get('amount') is not None else "未知"
        limit_up_tag = s.get('limit_up_tag', '')
        limit_up_display = f" {limit_up_tag}" if limit_up_tag else ""
        print(f"  {idx}. {signal_priority_label(s['type'])} [{s['type']}] {s['name']} ({s['code']}) | {s['sector']} | 现价:{s['price']:.2f} | 成交额:{amount_text} | 日期:{entry_date} | 来源:{amount_source}{limit_up_display}")
        print(f"     {truncate_reason(s['reason'])}")
    print("━" * 60)

    # 【新增】打印涨停板信号
    print_limit_up_signals(signals)

    for sector, sector_signals in grouped:
        sector_signals.sort(key=lambda s: PRIORITY_ORDER.get(s['type'], 99))
        print(f"【{sector}】（{len(sector_signals)} 只）")
        for s in sector_signals:
            amount_source = str(s.get("amount_source", "unknown") or "unknown")
            entry_date = str(s.get("entry_date", scan_date) or scan_date)
            amount_text = f"{float(s.get('amount', 0) or 0) / 100000000:.2f}亿" if s.get('amount') is not None else "未知"
            limit_up_tag = s.get('limit_up_tag', '')
            limit_up_display = f" {limit_up_tag}" if limit_up_tag else ""
            print(f"  {SIGNAL_EMOJI.get(s['type'], '')} [{s['type']}] {s['name']} ({s['code']}) | 现价:{s['price']:.2f} | 成交额:{amount_text} | 日期:{entry_date} | 来源:{amount_source}{limit_up_display}")
            print(f"    {truncate_reason(s['reason'])}")
        print(f"  共 {len(sector_signals)} 只，已全量展示")
    print("✅ 扫描完成！")
    print_scan_stats()

    # V17.2 新增：发送飞书推送
    send_to_feishu(signals, scan_date, total, success_count, scan_mode=scan_mode)

    # V17.5 新增：保存扫描结果用于回测
    save_scan_results(scan_date, scan_mode, total, success_count, signals)

    # V17.2 新增：触发系统报警
    try:
        print("\n🔔 触发系统报警...")
        alert_list = signals[:max(1, min(ALERT_MAX_SIGNALS, len(signals)))]
        for s in alert_list:
            system_alert.alarm_by_signal_type(s['type'])
            time.sleep(0.15)
    except KeyboardInterrupt:
        print("\n👋 程序已中断")
        log.debug("⚠️ 扫描结果已生成，但报警阶段被用户中断")

if __name__ == "__main__":
    if os.getenv("AMOUNT_REGRESSION_ONLY", "0").strip() == "1":
        ok = run_amount_regression()
        sys.exit(0 if ok else 1)
    os.environ.pop("SCAN_LIMIT", None)
    os.environ.setdefault("SCAN_TEST_MODE", "0")
    if os.getenv("SCAN_MODE", "").strip().lower() == "monthly_gain" or os.getenv("MONTHLY_STATS_ONLY", "0").strip() == "1":
        ok = run_monthly_gain_ranking()
        sys.exit(0 if ok else 1)

    # ===== V17.10改进：更清晰的策略配置加载优先级 =====
    # 优先级：命令行参数 > 环境变量 > 配置文件 > 菜单选择 > 默认配置
    strategy_config_applied = False

    # 检查命令行参数（支持 --config、--reconfigure、--reset 强制重新配置）
    force_config_menu = any(arg in sys.argv for arg in ['--config', '--reconfigure', '--reset', '--menu'])

    # 1. 检查是否指定了任何策略环境变量
    env_strategy_vars = [k for k in os.environ.keys() if k.startswith("ENABLE_STRATEGY_")]
    if (env_strategy_vars or os.getenv("ENABLE_STRATEGY_BREAKTHROUGH")) and not force_config_menu:
        log.debug("✓ 检测到环境变量配置，使用环境变量指定的策略")
        strategy_config_applied = True

    # 2. 检查本地配置文件（仅在无环境变量且未强制重配时）
    elif os.path.exists(os.path.join(BASE_DIR, "strategy_config.json")) and not force_config_menu:
        try:
            from strategy_menu import StrategyConfig
            config = StrategyConfig()
            config.load_config()
            config.apply_config()
            log.debug("✓ 已加载本地保存的策略配置")
            log.debug("   💡 提示：使用 --config 参数可重新配置策略")
            strategy_config_applied = True
        except ImportError:
            log.warning("⚠️  strategy_menu 模块未找到，跳过配置加载")

    # 3. 首次运行、配置丢失或用户主动要求，显示交互式菜单
    if not strategy_config_applied:
        try:
            from strategy_menu import show_strategy_config_menu
            if force_config_menu:
                log.info("🎯 用户要求重新配置策略...")
            else:
                log.info("🎯 首次运行或配置丢失，进入策略选择菜单...")
            log.info("   （您可以按 Ctrl+C 中断，使用默认配置）")
            show_strategy_config_menu()
        except ImportError:
            log.warning("⚠️  strategy_menu 模块未找到，将使用默认策略配置")
        except KeyboardInterrupt:
            log.warning("用户中断了策略选择，使用默认配置继续")

    run_scan()
