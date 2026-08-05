# -*- coding: utf-8 -*-
"""
auction_analyzer.py — V3.0 竞价阶段三层分析引擎

三层架构：
  第一层：指数竞价方向（5大指数 9:25 撮合后判定）
  第二层：板块热度分析（watchlist_jiuyan.json 驱动 sector 聚合）
  第三层：持仓竞价量化（五维 + 三维规则引擎 → 做T倾向）

原则：所有判定在 9:25 后执行，数据真实不可撤单。
"""
import json, os, sys
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import Dict, List, Optional, Any, Tuple

import numpy as np
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WATCHLIST_PATH = os.path.join(BASE_DIR, "watchlist_jiuyan.json")

# ============================================================
# 数据结构
# ============================================================

@dataclass
class IndexSignal:
    code: str = ""
    name: str = ""
    prev_close: float = 0.0
    current: float = 0.0
    change_pct: float = 0.0

@dataclass
class SectorHeat:
    name: str = ""
    avg_change: float = 0.0
    total_amount: float = 0.0
    up_count: int = 0
    down_count: int = 0
    total_count: int = 0
    rank: int = 99
    heat_label: str = ""  # hot / warm / cold

@dataclass
class AuctionSignal:
    code: str = ""
    name: str = ""
    change_pct: float = 0.0
    amount_wan: float = 0.0
    vol_ratio: float = 1.0
    sector_tags: List[str] = field(default_factory=list)
    sector_heat_label: str = ""
    action: str = "正常"
    confidence: str = "中"

# ============================================================
# 第一层：指数竞价方向
# ============================================================

INDEX_TARGETS = [
    ("000001", "上证指数"),
    ("399001", "深证成指"),
    ("399006", "创业板指"),
    ("000688", "科创50"),
    ("000300", "沪深300"),
]

def fetch_index_auction() -> List[IndexSignal]:
    """9:25 后拉取5大指数竞价涨跌幅。依赖 akshare stock_zh_index_spot_em()。"""
    results = []
    try:
        import akshare as ak
        spot = ak.stock_zh_index_spot_em()
        if spot.empty:
            return results
        spot["代码"] = spot["代码"].astype(str).str.strip()
        for code, name in INDEX_TARGETS:
            row = spot[spot["代码"] == code]
            if row.empty:
                continue
            r = row.iloc[0]
            prev = float(r.get("昨收", 0) or 0)
            cur = float(r.get("最新价", 0) or 0)
            pct = (cur - prev) / prev * 100 if prev > 0 else 0
            results.append(IndexSignal(code=code, name=name, prev_close=prev,
                                       current=cur, change_pct=round(pct, 2)))
    except Exception as e:
        pass
    return results


def classify_market_bias(index_signals: List[IndexSignal]) -> str:
    """根据指数竞价涨跌幅判定市场宏观方向。"""
    if not index_signals:
        return "unknown"
    pcts = [s.change_pct for s in index_signals]
    all_up = all(p >= 0.3 for p in pcts)
    all_down = all(p <= -0.3 for p in pcts)
    if all_up:
        bias = "bullish"
    elif all_down:
        bias = "bearish"
    elif max(pcts) - min(pcts) > 1.0:
        bias = "divergent"
    else:
        bias = "neutral"
    # 科技 vs 大盘
    tech_lead = ""
    sh = next((s for s in index_signals if s.code == "000001"), None)
    kc = next((s for s in index_signals if s.code == "000688"), None)
    if sh and kc and kc.change_pct > sh.change_pct + 0.3:
        tech_lead = "（科技领涨）"
    elif sh and kc and kc.change_pct < sh.change_pct - 0.3:
        tech_lead = "（科技偏弱）"
    return bias + tech_lead


# ============================================================
# 第二层：板块热度分析
# ============================================================

_sector_map_cache: Optional[Dict[str, list]] = None

def load_sector_map(watchlist_path: str = WATCHLIST_PATH) -> Dict[str, list]:
    """加载 watchlist_jiuyan.json → code→[sector_tags]。全局缓存。"""
    global _sector_map_cache
    if _sector_map_cache is not None:
        return _sector_map_cache
    if not os.path.exists(watchlist_path):
        _sector_map_cache = {}
        return _sector_map_cache
    with open(watchlist_path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    smap = {}
    for code, info in raw.items():
        sector_str = info.get("sector", "")
        tags = [t.strip() for t in sector_str.split("/") if t.strip()] if sector_str else []
        smap[code] = tags
    _sector_map_cache = smap
    return smap


def build_sector_heat(snapshot_df: pd.DataFrame, sector_map: Dict[str, list],
                       min_stocks: int = 3) -> Dict[str, SectorHeat]:
    """全市场竞价快照按 sector 聚合 → 板块热度排名。

    snapshot_df: akshare stock_zh_a_spot_em() 输出，需含 '代码'/'涨跌幅'/'成交额' 列
    """
    from collections import defaultdict
    sectors: Dict[str, dict] = defaultdict(lambda: {"changes": [], "amounts": [],
                                                      "up": 0, "down": 0})

    code_col = None
    for col in ["代码", "code"]:
        if col in snapshot_df.columns:
            code_col = col
            break
    if code_col is None:
        return {}

    change_col = None
    for col in ["涨跌幅", "change_pct", "pct_chg"]:
        if col in snapshot_df.columns:
            change_col = col
            break

    amt_col = None
    for col in ["成交额", "amount", "成交金额"]:
        if col in snapshot_df.columns:
            amt_col = col
            break

    for _, row in snapshot_df.iterrows():
        code = str(row.get(code_col, "")).strip()
        if code not in sector_map:
            continue
        tags = sector_map[code]
        if not tags:
            continue
        pct = float(row.get(change_col, 0) or 0) if change_col else 0
        amt = float(row.get(amt_col, 0) or 0) if amt_col else 0

        for tag in tags:
            sectors[tag]["changes"].append(pct)
            sectors[tag]["amounts"].append(amt)
            if pct > 0:
                sectors[tag]["up"] += 1
            elif pct < 0:
                sectors[tag]["down"] += 1

    results = {}
    for tag, data in sectors.items():
        n = len(data["changes"])
        if n < min_stocks:
            continue
        avg_chg = sum(data["changes"]) / n
        total_amt = sum(data["amounts"])
        sh = SectorHeat(
            name=tag,
            avg_change=round(avg_chg, 2),
            total_amount=round(total_amt, 0),
            up_count=data["up"],
            down_count=data["down"],
            total_count=n,
        )
        results[tag] = sh

    # 按成交额排名
    sorted_sectors = sorted(results.items(), key=lambda x: x[1].total_amount, reverse=True)
    for i, (tag, sh) in enumerate(sorted_sectors):
        sh.rank = i + 1
        # 热度标签
        all_changes = [sh.avg_change for _, sh in sorted_sectors]
        if all_changes:
            p30 = np.percentile(all_changes, 70)
            p70 = np.percentile(all_changes, 30)
            if sh.avg_change >= p30:
                sh.heat_label = "hot"
            elif sh.avg_change <= p70:
                sh.heat_label = "cold"
            else:
                sh.heat_label = "warm"

    return {tag: sh for tag, sh in sorted_sectors}


def top_sectors(sector_heat: Dict[str, SectorHeat], n: int = 5) -> List[SectorHeat]:
    return sorted(sector_heat.values(), key=lambda s: s.total_amount, reverse=True)[:n]


# ============================================================
# 第三层：持仓竞价量化 + 三维规则引擎
# ============================================================

def _approx_vol_ratio(code: str, auction_amount: float) -> float:
    """用近5日9:30-9:35平均量估算竞价量比。快照不可得时返回1.0。"""
    try:
        snap_dir = os.path.join(BASE_DIR, "t_io", "minute_snapshots")
        recent = []
        for root, dirs, files in os.walk(snap_dir):
            for fn in files:
                if fn.startswith(code) and fn.endswith(".json"):
                    recent.append(os.path.join(root, fn))
        recent.sort(reverse=True)
        avg_vol = 0
        count = 0
        for p in recent[:5]:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            bars = data.get("bars", data if isinstance(data, list) else [])
            for bar in bars[:5]:  # 前5根1分钟K线 ≈ 9:30-9:35
                avg_vol += float(bar.get("amount", 0) or 0)
                count += 1
        if count > 0 and avg_vol > 0:
            return round(auction_amount / (avg_vol / count), 2) if auction_amount > 0 else 1.0
    except Exception:
        pass
    return 1.0


def auction_action(change_pct: float, vol_ratio: float,
                   sector_label: str, market_bias: str) -> Tuple[str, str]:
    """三维规则引擎：指数方向 × 板块热度 × 竞价涨跌量比 → 做T倾向。

    Returns: (action, confidence)
    """
    # 大前提：指数方向
    if market_bias.startswith("bullish"):
        bias_倾向 = "高抛"
        bias_强度 = "强" if vol_ratio >= 1.5 else "中"
    elif market_bias.startswith("bearish"):
        bias_倾向 = "低吸"
        bias_强度 = "强" if vol_ratio >= 1.5 else "中"
    else:
        bias_倾向 = ""
        bias_强度 = "中"

    # 板块维度
    if sector_label == "hot":
        sector_倾向 = "高抛"
    elif sector_label == "cold":
        sector_倾向 = "低吸"
    else:
        sector_倾向 = ""

    # 个股竞价维度
    if change_pct >= 2.0 and vol_ratio >= 1.5:
        stock_倾向 = "高抛"
        stock_置信 = "高"
    elif change_pct >= 2.0:
        stock_倾向 = "高抛"
        stock_置信 = "中"
    elif change_pct <= -2.0 and vol_ratio < 1.0:
        stock_倾向 = "低吸"
        stock_置信 = "高"
    elif change_pct <= -2.0:
        stock_倾向 = "低吸"
        stock_置信 = "中"
    else:
        stock_倾向 = ""
        stock_置信 = "中"

    # 综合投票
    votes_高抛 = sum(1 for v in [bias_倾向, sector_倾向, stock_倾向] if v == "高抛")
    votes_低吸 = sum(1 for v in [bias_倾向, sector_倾向, stock_倾向] if v == "低吸")

    if votes_高抛 >= 2 and stock_倾向 == "高抛":
        return ("优先高抛", stock_置信)
    elif votes_低吸 >= 2 and stock_倾向 == "低吸":
        return ("优先低吸", stock_置信)
    elif change_pct >= 2.0:
        return ("关注高抛", "中")
    elif change_pct <= -2.0:
        return ("关注低吸", "中")
    elif vol_ratio >= 2.0:
        return ("关注", "中")  # 放量平开，变盘前兆
    elif sector_label == "hot":
        return ("关注", "中")  # 板块强但个股未动
    else:
        return ("正常", "中")


def analyze_auction(holdings: Dict[str, dict], snapshot_df: pd.DataFrame = None) -> dict:
    """主入口：执行三层竞价分析。

    Args:
        holdings: {code: {name, pre_close, qty, ...}}
        snapshot_df: akshare stock_zh_a_spot_em() 输出，None 时自动拉取

    Returns:
        {index_signals, market_bias, sector_top5, auction_signals}
    """
    # 第一层：指数
    index_signals = fetch_index_auction()
    market_bias = classify_market_bias(index_signals)

    # 快照数据
    if snapshot_df is None or snapshot_df.empty:
        try:
            import akshare as ak
            snapshot_df = ak.stock_zh_a_spot_em()
        except Exception:
            snapshot_df = pd.DataFrame()

    # 第二层：板块热度
    sector_map = load_sector_map()
    sector_heat = build_sector_heat(snapshot_df, sector_map) if not snapshot_df.empty else {}
    top5 = top_sectors(sector_heat)

    # 第三层：持仓竞价
    auction_signals = []
    # 快照索引：代码→行
    if not snapshot_df.empty:
        code_col = next((c for c in ["代码", "code"] if c in snapshot_df.columns), None)
        change_col = next((c for c in ["涨跌幅", "change_pct"] if c in snapshot_df.columns), None)
        amt_col = next((c for c in ["成交额", "amount"] if c in snapshot_df.columns), None)
        snapshot_index = {}
        if code_col:
            for _, row in snapshot_df.iterrows():
                snapshot_index[str(row[code_col]).strip()] = row
    else:
        snapshot_index = {}

    for code, h in holdings.items():
        clean = code.split("_")[0] if "_" in code else code
        name = h.get("name", code)
        pre_close = float(h.get("pre_close", 0) or 0)

        row = snapshot_index.get(clean)
        change_pct = 0.0
        amount_wan = 0.0
        if row is not None and change_col and pre_close > 0:
            cur = float(row.get("最新价", pre_close) or pre_close)
            change_pct = (cur - pre_close) / pre_close * 100
            if amt_col:
                amount_wan = float(row.get(amt_col, 0) or 0) / 10000
        vol_ratio = _approx_vol_ratio(clean, amount_wan * 10000) if amount_wan > 0 else 1.0

        # 板块热度
        tags = sector_map.get(clean, [])
        sector_label = ""
        for tag in tags:
            sh = sector_heat.get(tag)
            if sh and sh.heat_label == "hot":
                sector_label = "hot"
                break
            elif sh and sh.heat_label == "cold" and sector_label != "hot":
                sector_label = "cold"

        action, confidence = auction_action(change_pct, vol_ratio, sector_label, market_bias)

        auction_signals.append(AuctionSignal(
            code=clean, name=name,
            change_pct=round(change_pct, 2),
            amount_wan=round(amount_wan, 1),
            vol_ratio=vol_ratio,
            sector_tags=tags,
            sector_heat_label=sector_label,
            action=action,
            confidence=confidence,
        ))

    return {
        "index_signals": [s.__dict__ for s in index_signals],
        "market_bias": market_bias,
        "sector_top5": [s.__dict__ for s in top5],
        "auction_signals": [s.__dict__ for s in auction_signals],
    }


# ============================================================
# 飞书卡片格式化
# ============================================================

def format_auction_feishu(result: dict) -> list:
    """将 analyze_auction() 输出转为飞书卡片元素列表。"""
    elements = []
    index_signals = result.get("index_signals", [])
    market_bias = result.get("market_bias", "unknown")
    sector_top5 = result.get("sector_top5", [])
    auction_signals = result.get("auction_signals", [])

    # 指数行
    idx_text = "  ".join(
        f"{s['name']} {s['change_pct']:+.2f}%" for s in index_signals
    ) if index_signals else "指数数据获取中..."
    bias_label = {"bullish": "📈偏多", "bearish": "📉偏空", "divergent": "⚡分化",
                   "neutral": "➡中性"}.get(market_bias.split("（")[0], market_bias)
    elements.append({"tag": "div", "text": {
        "content": f"**指数竞价**：{idx_text}\n**判定**：{bias_label}",
        "tag": "lark_md"}})
    elements.append({"tag": "hr"})

    # 板块TOP3
    if sector_top5:
        top3_text = "  |  ".join(
            f"{s['name']} {s['avg_change']:+.1f}%" for s in sector_top5[:3]
        )
        elements.append({"tag": "div", "text": {
            "content": f"**板块TOP3**：{top3_text}", "tag": "lark_md"}})
        elements.append({"tag": "hr"})

    # 持仓竞价明细
    lines = []
    action_icon = {
        "优先高抛": "🔴", "优先低吸": "🟢", "关注高抛": "🟠", "关注低吸": "🟠",
        "关注": "🟡", "正常": "⚪"
    }
    for s in auction_signals:
        sector_str = f"板块{s['sector_heat_label']}" if s.get("sector_heat_label") else ""
        icon = action_icon.get(s["action"], "⚪")
        lines.append(
            f"{icon} **{s['code'][-3:]}** {s['name']} "
            f"{s['change_pct']:+.1f}% {s['amount_wan']:.0f}万 "
            f"量比{s['vol_ratio']:.1f}x {sector_str} → **{s['action']}**"
        )
    elements.append({"tag": "div", "text": {
        "content": "\n".join(lines), "tag": "lark_md"}})
    elements.append({"tag": "hr"})

    # 今日汇总
    priority_high = [s for s in auction_signals if "高抛" in s["action"]]
    priority_low = [s for s in auction_signals if "低吸" in s["action"]]
    summary_parts = []
    if priority_high:
        names = "、".join(s["name"] for s in priority_high)
        summary_parts.append(f"优先高抛：{names}")
    if priority_low:
        names = "、".join(s["name"] for s in priority_low)
        summary_parts.append(f"优先低吸：{names}")
    if not summary_parts:
        summary_parts.append("今日竞价无明确方向信号，正常做T")
    elements.append({"tag": "div", "text": {
        "content": f"**今日**：{'；'.join(summary_parts)}", "tag": "lark_md"}})

    return elements


# ============================================================
# CLI 测试入口
# ============================================================

if __name__ == "__main__":
    print("=== auction_analyzer 测试 ===\n")
    # 模拟持仓
    holdings = {
        "000988": {"name": "华工科技", "pre_close": 110.0, "qty": 300},
        "588170": {"name": "科创芯片ETF", "pre_close": 1.08, "qty": 4000},
        "600176": {"name": "中国巨石", "pre_close": 42.0, "qty": 300},
        "600481": {"name": "双良节能", "pre_close": 3.80, "qty": 1400},
        "603667": {"name": "五洲新春", "pre_close": 52.0, "qty": 400},
    }

    result = analyze_auction(holdings)

    print(f"Index signals: {len(result['index_signals'])}")
    for s in result["index_signals"]:
        print(f"  {s['name']} {s['change_pct']:+.2f}%")
    print(f"Market bias: {result['market_bias']}")
    print(f"Sector TOP5: {len(result['sector_top5'])}")
    for s in result["sector_top5"][:3]:
        print(f"  {s['name']} {s['avg_change']:+.1f}% {s['heat_label']}")
    print(f"Auction signals: {len(result['auction_signals'])}")
    for s in result["auction_signals"]:
        print(f"  {s['name']} {s['change_pct']:+.1f}% {s['amount_wan']:.0f}万 → {s['action']} ({s['confidence']})")
