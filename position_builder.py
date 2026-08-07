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
BASE = Path(r"E:\06_T")
sys.path.insert(0, str(BASE))

from indicators import resample_to_5min, add_5min_indicators


# ── 飞书推送（可选，Webhook 未配置时静默跳过）──
try:
    from config import send_feishu_payload, FEISHU_WEBHOOK, FEISHU_KEYWORD
    _FEISHU_AVAILABLE = bool(FEISHU_WEBHOOK)
except Exception:
    _FEISHU_AVAILABLE = False
    send_feishu_payload = None
    FEISHU_KEYWORD = "建仓信号"

WATCHLIST_FILE = BASE / "watchlist_buy.json"
SNAPSHOT_DIR = BASE / "t_io" / "minute_snapshots"
TRACE_DIR = BASE / "t_io" / "traces"
TRACE_DIR.mkdir(parents=True, exist_ok=True)


def _write_trace_line(entry: dict, date_str: str):
    """追加一行 JSONL 到当日建仓扫描日志。"""
    fp = TRACE_DIR / f"position_builder_{date_str}.jsonl"
    with open(fp, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


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

def fetch_daily_kline(code: str) -> pd.DataFrame:
    """拉腾讯日线（前复权）。返回 {date, open, close, high, low, volume}。"""
    import urllib.request as _ur, os as _os
    for _k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
               "ALL_PROXY", "all_proxy"]:
        _os.environ.pop(_k, None)
    _os.environ["NO_PROXY"] = "*"
    symbol = ("sh" + code if code[0] in "56" else "sz" + code)
    try:
        url = f"https://ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,150,qfq"
        req = _ur.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                        "Referer": "https://finance.qq.com/"})
        raw = _ur.urlopen(req, timeout=8).read().decode("utf-8", errors="ignore")
        data = json.loads(raw)
        kline = data.get("data", {}).get(symbol, {}).get("day") or \
                data.get("data", {}).get(symbol, {}).get("qfqday") or []
        rows = [{"date": i[0], "open": float(i[1]), "close": float(i[2]),
                 "high": float(i[3]), "low": float(i[4]), "volume": float(i[5])}
                for i in kline if len(i) >= 6]
        return pd.DataFrame(rows)
    except Exception:
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


def check_box_breakout(code: str, price: float = None) -> dict:
    """判定是否突破最近箱体上沿。返回 {broken, box, price, pct_above}。"""
    df = fetch_daily_kline(code)
    if df.empty or len(df) < 30:
        return {"broken": False, "error": "无日线"}
    cur = float(price) if price else float(df["close"].iloc[-1])
    boxes = _detect_boxes_simple(df)
    below = [b for b in boxes if b["high"] < cur]
    if not below:
        return {"broken": False, "price": round(cur, 3)}
    box = max(below, key=lambda b: b["high"])
    pct_above = (cur - box["high"]) / box["high"] * 100 if box["high"] else 0
    if 0.3 <= pct_above <= 25:
        return {"broken": True, "box": box, "price": round(cur, 3),
                "pct_above": round(pct_above, 2)}
    return {"broken": False, "price": round(cur, 3),
            "near_box": box, "pct_above": round(pct_above, 2)}


# ============================================================
# 五个建仓条件
# ============================================================

def check_macd_golden(df_5min: pd.DataFrame) -> tuple:
    """MACD 多头: dif > dea 且 hist > 0，最近 3 根 ≥2 根满足。"""
    if df_5min.empty or len(df_5min) < 3:
        return False, "数据不足（需≥3根5分钟K线）"
    cols = ["dif_5m", "dea_5m", "macd_hist_5m"]
    for c in cols:
        if c not in df_5min.columns:
            return False, f"缺少列 {c}"
    recent = df_5min.tail(3)
    count = ((recent["dif_5m"] > recent["dea_5m"]) & (recent["macd_hist_5m"] > 0)).sum()
    passed = count >= 2
    detail = f"近3根满足{count}/3（dif>dea & hist>0），阈≥2"
    return passed, detail


def check_boll_mid_support(df_5min: pd.DataFrame) -> tuple:
    """BOLL 中轨支撑: bb_pct_5m 在 0.3~0.7 区间，带宽未极端扩张。"""
    if df_5min.empty or "bb_pct_5m" not in df_5min.columns:
        return False, "数据不足"
    latest_bb = df_5min["bb_pct_5m"].iloc[-1]
    latest_width = df_5min.get("bb_width_5m", pd.Series([0])).iloc[-1]
    price = df_5min["close"].iloc[-1]
    # 带宽极端扩张判定：width > 价格 × 5%
    if latest_width > price * 0.05:
        return False, f"BOLL 带宽极端扩张（{latest_width:.3f} > {price*0.05:.3f}）"
    passed = 0.3 <= latest_bb <= 0.7
    detail = f"bb_pct={latest_bb:.3f}（需0.3~0.7），带宽={latest_width:.3f}"
    return passed, detail


def check_rsi_healthy(df_5min: pd.DataFrame) -> tuple:
    """RSI 健康区间: 35~60。"""
    if df_5min.empty or "rsi_5m" not in df_5min.columns:
        return False, "数据不足"
    rsi_val = df_5min["rsi_5m"].iloc[-1]
    if pd.isna(rsi_val):
        return False, "RSI=NaN（纯上涨窗，C语义设计内）"
    passed = 35 <= rsi_val <= 60
    detail = f"rsi_5m={rsi_val:.1f}（需35~60）"
    return passed, detail


def check_volume_shrink(df_1min: pd.DataFrame) -> tuple:
    """成交量缩量: 最近 30 分钟均量 < 前 60 分钟均量 × 0.8。"""
    if df_1min.empty or len(df_1min) < 30:
        return False, "数据不足（需≥30根1分钟K线）"
    vol = df_1min["volume"]
    recent_vol = vol.tail(30).mean()
    prior_vol = vol.iloc[-90:-30].mean() if len(vol) >= 90 else vol.iloc[:-30].mean()
    if prior_vol <= 0:
        return False, "前段成交量为0"
    ratio = recent_vol / prior_vol
    passed = ratio < 0.8
    detail = f"近30分均量={recent_vol:.0f} / 前段均量={prior_vol:.0f} = {ratio:.2f}（需<0.8）"
    return passed, detail


def check_support_retest(df_1min: pd.DataFrame, daily_ctx: dict) -> tuple:
    """回踩支撑不破: 最新价距支撑位 ≤2% 且未破位。"""
    if df_1min.empty:
        return False, "无分钟数据"

    latest_price = df_1min["close"].iloc[-1]
    day_low = df_1min["low"].min()

    # 从 daily_context 提取支撑位
    supports = []
    for key, label in [
        ("daily_ma10", "MA10"), ("daily_ma20", "MA20"),
        ("daily_ma60", "MA60"), ("daily_ma5", "MA5"),
    ]:
        val = daily_ctx.get(key)
        if val and not (isinstance(val, float) and math.isnan(val)):
            supports.append((label, float(val)))

    # 日内 VWAP
    vwap = daily_ctx.get("last_vwap") or daily_ctx.get("daily_support_level")
    if vwap and not (isinstance(vwap, float) and math.isnan(vwap)):
        supports.append(("VWAP", float(vwap)))

    if not supports:
        return False, "daily_context 无支撑位数据"

    # 找最近的支撑位（价格上方最近的）
    nearest = None
    min_dist = float("inf")
    for label, level in supports:
        dist = (latest_price - level) / level
        if abs(dist) < abs(min_dist):
            min_dist = dist
            nearest = (label, level, dist)

    if nearest is None:
        return False, "无法确定支撑位"

    label, level, dist = nearest
    # 回踩判定：距离 ≤2% 且日最低价未明显破位（日低 > 支撑位 × 0.98）
    dist_pct = dist * 100
    near_support = abs(dist_pct) <= 2.0
    not_broken = day_low > level * 0.98
    passed = near_support and not_broken

    detail = (f"最近支撑={label}@{level:.3f}，距={dist_pct:+.2f}%（需≤2%）"
              f"，日低={day_low:.3f}（破位阈={level*0.98:.3f}）")
    return passed, detail


# ============================================================
# 综合评分 & 仓位计算
# ============================================================

def compute_score(conditions: dict) -> int:
    """每满足一个条件 +20 分，满分 100。"""
    return sum(20 for passed, _ in conditions.values() if passed)


def compute_position(latest_price: float, total_capital: float,
                     max_per_stock_pct: float) -> dict:
    """计算建议股数（按手取整）。"""
    max_capital = total_capital * max_per_stock_pct
    raw_qty = math.floor(max_capital / latest_price / 100) * 100
    raw_qty = max(raw_qty, 100)  # 最少 1 手
    required = raw_qty * latest_price
    return {
        "suggested_qty": raw_qty,
        "suggested_price": round(latest_price, 3),
        "capital_required": round(required, 2),
        "max_capital_per_stock": round(max_capital, 2),
    }


# ============================================================
# 主扫描逻辑
# ============================================================

def scan_stock(code: str, stock_info: dict, date_str: str = None,
               total_capital: float = 300000, max_pct: float = 0.2) -> dict:
    """扫描单只股票，返回结果字典。"""
    result = {
        "code": code,
        "name": stock_info.get("name", code),
        "date": None,
        "latest_price": None,
        "conditions": {},
        "composite_score": 0,
        "verdict": "insufficient_data",
        "position": None,
        "errors": [],
    }

    # 加载数据
    df_1min, daily_ctx, snap_date = load_snapshot_df(code, date_str)
    if df_1min.empty:
        result["errors"].append("无分钟快照数据")
        return result

    result["date"] = snap_date
    result["latest_price"] = round(float(df_1min["close"].iloc[-1]), 3)

    # 5 分钟聚合 + 指标
    df_5min = resample_to_5min(df_1min)
    if df_5min.empty or len(df_5min) < 3:
        result["errors"].append("5分钟K线不足（需≥3根）")
        return result
    df_5min = add_5min_indicators(df_5min)

    # 突破箱体（第一优先级）
    bx = check_box_breakout(code, result["latest_price"])
    box_passed = bx.get("broken", False)
    box_detail = (f"突破箱体上沿 {bx['box']['high']}，超出 {bx['pct_above']}%"
                  if box_passed else "未突破箱体")

    # 检查五个条件（box_breakout 独立，不叠加评分）
    conditions = {
        "macd_golden": check_macd_golden(df_5min),
        "boll_mid_support": check_boll_mid_support(df_5min),
        "rsi_healthy": check_rsi_healthy(df_5min),
        "volume_shrink": check_volume_shrink(df_1min),
        "support_retest": check_support_retest(df_1min, daily_ctx),
    }
    result["conditions"] = {
        k: {"passed": v[0], "detail": v[1]} for k, v in conditions.items()
    }
    result["conditions"]["box_breakout"] = {"passed": box_passed, "detail": box_detail}
    result["composite_score"] = compute_score(conditions)

    # 判定：突破箱体 = 第一优先级，直接 signal
    if box_passed:
        result["verdict"] = "signal"
    elif result["composite_score"] >= 70:
        result["verdict"] = "signal"
    elif result["composite_score"] >= 40:
        result["verdict"] = "approaching"
    else:
        result["verdict"] = "weak"

    # 仓位计算
    result["position"] = compute_position(
        result["latest_price"], total_capital, max_pct
    )

    return result


# ============================================================
# 飞书推送
# ============================================================

COND_LABELS = {
    "macd_golden": "MACD多头",
    "boll_mid_support": "BOLL中轨支撑",
    "rsi_healthy": "RSI健康区间",
    "volume_shrink": "成交量缩量",
    "support_retest": "回踩支撑不破",
}


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

    lines = [
        f"**{name}（{code}）** 建仓信号触发",
        "",
        f"📅 日期：{result.get('date') or 'N/A'}",
        f"📊 综合得分：**{score}/100**",
        f"💵 最新价：{result.get('latest_price')}",
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

            # 条件通过情况
            conds = r.get("conditions", {})
            cond_parts = []
            for key in ["macd_golden", "boll_mid_support", "rsi_healthy", "volume_shrink", "support_retest"]:
                c = conds.get(key, {})
                cond_parts.append("●" if c.get("passed") else "○")
            cond_str = "".join(cond_parts)

            lines.append(
                f"{icon} **{r['code']}** {r['name']}  "
                f"得分 **{r['composite_score']}**  "
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
    lines.append("●=通过  ○=未通过  (MACD/BOLL/RSI/量/支撑)")

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
    if result["verdict"] == "signal":
        stock["status"] = "signal"
    pos = result.get("position") or {}
    stock["suggested_qty"] = int(pos.get("suggested_qty", 0))
    stock["suggested_price"] = float(pos.get("suggested_price", 0))
    stock["capital_required"] = float(pos.get("capital_required", 0))
    # 追加信号历史
    hist_entry = {
        "date": result.get("date"),
        "score": int(result["composite_score"]),
        "verdict": str(result["verdict"]),
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
        print(f"\n── {code} {name} ──")
        print(f"  日期: {r.get('date') or '无数据'}")
        print(f"  最新价: {r.get('latest_price') or 'N/A'}")
        print(f"  综合得分: {r['composite_score']}/100  =>  {r['verdict']}")

        if r.get("errors"):
            for e in r["errors"]:
                print(f"  [WARN] {e}")
            continue

        print("\n  条件检查:")
        for cond_key, cond_val in r.get("conditions", {}).items():
            icon = "[PASS]" if cond_val["passed"] else "[FAIL]"
            labels = {
                "macd_golden": "MACD多头",
                "boll_mid_support": "BOLL中轨支撑",
                "rsi_healthy": "RSI健康区间",
                "volume_shrink": "成交量缩量",
                "support_retest": "回踩支撑不破",
            }
            print(f"    {icon} {labels.get(cond_key, cond_key):12s}  {cond_val['detail']}")

        pos = r.get("position") or {}
        if pos:
            print(f"\n  仓位建议（单只上限 {pos.get('max_capital_per_stock','?'):,.0f}）:")
            print(f"    建议买入: {pos.get('suggested_qty',0)} 股 @ {pos.get('suggested_price','?')}")
            print(f"    所需资金: {pos.get('capital_required',0):,.0f}")

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
            print(f"  {r['code']} {r['name']}: "
                  f"{pos.get('suggested_qty',0)}股 @ {pos.get('suggested_price','?')} "
                  f"≈ {pos.get('capital_required',0):,.0f}")
        print()


# ============================================================
# 核心入口（CLI 和 main.py 共用）
# ============================================================

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
                  and not k.startswith("_example")}

    if not stocks:
        if not silent:
            print("没有需要扫描的股票（status=monitoring/signal）")
        return []

    results = []
    signal_count = 0
    for code, info in stocks.items():
        if not silent:
            print(f"扫描 {code} {info.get('name', '')}...")
        r = scan_stock(code, info, date_str, total_capital, max_pct)
        results.append(r)
        update_watchlist(r, watchlist)

        if r["verdict"] == "signal":
            signal_count += 1
            pushed = push_signal_feishu(r, dry_run=no_feishu)
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
            "verdict": str(r["verdict"]),
            "in_holdings": bool(watchlist.get("stocks", {}).get(r["code"], {}).get("in_holdings", False)),
            "conditions": {k: bool(v["passed"]) for k, v in r.get("conditions", {}).items()},
            "suggested_qty": int((r.get("position") or {}).get("suggested_qty", 0)),
            "suggested_price": _py_type((r.get("position") or {}).get("suggested_price", 0)),
            "capital_required": _py_type((r.get("position") or {}).get("capital_required", 0)),
            "errors": [str(e) for e in r.get("errors", [])],
        }, log_date)

    # 保存更新后的 watchlist
    with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(watchlist, f, ensure_ascii=False, indent=2)

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
    args = parser.parse_args()

    run_position_scan(
        date_str=args.date,
        capital=args.capital,
        no_feishu=args.no_feishu,
        target_code=args.code,
        silent=False,
    )


if __name__ == "__main__":
    main()
