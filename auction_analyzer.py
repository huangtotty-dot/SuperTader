# -*- coding: utf-8 -*-
"""
auction_analyzer.py — V3.0 竞价阶段三层分析引擎（exec 共享命名空间版）

加载方式：main.py module_order 中置于 preopen 之前 exec 加载。
所有依赖（np/pd/ak/json/os/datetime）从共享命名空间获取，无独立 import。
"""
# ============================================================
# 数据结构（纯 Python，无外部依赖）
# ============================================================

class IndexSignal:
    __slots__ = ('code','name','prev_close','current','change_pct')
    def __init__(self, code="", name="", prev_close=0.0, current=0.0, change_pct=0.0):
        self.code = code; self.name = name
        self.prev_close = prev_close; self.current = current
        self.change_pct = change_pct


class SectorHeat:
    __slots__ = ('name','avg_change','total_amount','up_count','down_count','total_count','rank','heat_label')
    def __init__(self, name="", avg_change=0.0, total_amount=0.0, up_count=0, down_count=0, total_count=0):
        self.name = name; self.avg_change = avg_change; self.total_amount = total_amount
        self.up_count = up_count; self.down_count = down_count; self.total_count = total_count
        self.rank = 99; self.heat_label = ""


class AuctionSignal:
    __slots__ = ('code','name','change_pct','amount_wan','vol_ratio','sector_tags','sector_heat_label','action','confidence')
    def __init__(self, code="", name="", change_pct=0.0, amount_wan=0.0, vol_ratio=1.0,
                 sector_tags=None, sector_heat_label="", action="正常", confidence="中"):
        self.code = code; self.name = name; self.change_pct = change_pct
        self.amount_wan = amount_wan; self.vol_ratio = vol_ratio
        self.sector_tags = sector_tags or []; self.sector_heat_label = sector_heat_label
        self.action = action; self.confidence = confidence


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

def fetch_index_auction():
    """9:25后拉取5大指数竞价涨跌幅。使用共享命名空间的 ak。"""
    results = []
    try:
        spot = ak.stock_zh_index_spot_em()
        if spot is None or (hasattr(spot, 'empty') and spot.empty):
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
    except Exception:
        pass
    return results


def classify_market_bias(index_signals):
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
    sh = next((s for s in index_signals if s.code == "000001"), None)
    kc = next((s for s in index_signals if s.code == "000688"), None)
    if sh and kc and kc.change_pct > sh.change_pct + 0.3:
        bias += "（科技领涨）"
    elif sh and kc and kc.change_pct < sh.change_pct - 0.3:
        bias += "（科技偏弱）"
    return bias


# ============================================================
# 第二层：板块热度分析
# ============================================================

_sector_map_cache = None

def load_sector_map():
    """加载 watchlist_jiuyan.json → code→[sector_tags]。全局缓存。"""
    global _sector_map_cache
    if _sector_map_cache is not None:
        return _sector_map_cache
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)) if '__file__' in dir() else ".", "watchlist_jiuyan.json")
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlist_jiuyan.json") if '__file__' in dir() else "watchlist_jiuyan.json"
    if not os.path.exists(p):
        _sector_map_cache = {}
        return _sector_map_cache
    with open(p, "r", encoding="utf-8") as f:
        raw = json.load(f)
    smap = {}
    for code, info in raw.items():
        sector_str = info.get("sector", "")
        tags = [t.strip() for t in sector_str.split("/") if t.strip()] if sector_str else []
        smap[code] = tags
    _sector_map_cache = smap
    return smap


def build_sector_heat(snapshot_df, sector_map, min_stocks=3):
    """全市场竞价快照按 sector 聚合 → 板块热度排名。"""
    from collections import defaultdict
    sectors = defaultdict(lambda: {"changes": [], "amounts": [], "up": 0, "down": 0})

    if snapshot_df is None or (hasattr(snapshot_df, 'empty') and snapshot_df.empty):
        return {}

    code_col = next((c for c in ["代码", "code"] if c in snapshot_df.columns), None)
    change_col = next((c for c in ["涨跌幅", "change_pct", "pct_chg"] if c in snapshot_df.columns), None)
    amt_col = next((c for c in ["成交额", "amount", "成交金额"] if c in snapshot_df.columns), None)
    if code_col is None:
        return {}

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
        results[tag] = SectorHeat(
            name=tag, avg_change=round(avg_chg, 2),
            total_amount=round(sum(data["amounts"]), 0),
            up_count=data["up"], down_count=data["down"], total_count=n,
        )

    sorted_sectors = sorted(results.items(), key=lambda x: x[1].total_amount, reverse=True)
    all_avgs = [sh.avg_change for _, sh in sorted_sectors]
    if all_avgs:
        p30 = sorted(all_avgs)[int(len(all_avgs) * 0.7)] if len(all_avgs) >= 3 else all_avgs[-1]
        p70 = sorted(all_avgs)[int(len(all_avgs) * 0.3)] if len(all_avgs) >= 3 else all_avgs[0]
        for i, (tag, sh) in enumerate(sorted_sectors):
            sh.rank = i + 1
            if sh.avg_change >= p30:
                sh.heat_label = "hot"
            elif sh.avg_change <= p70:
                sh.heat_label = "cold"
            else:
                sh.heat_label = "warm"

    return {tag: sh for tag, sh in sorted_sectors}


def top_sectors(sector_heat, n=5):
    return sorted(sector_heat.values(), key=lambda s: s.total_amount, reverse=True)[:n]


# ============================================================
# 第三层：持仓竞价量化 + 三维规则引擎
# ============================================================

def _approx_vol_ratio(code, auction_amount):
    """用近5日9:30-9:35平均量估算竞价量比。"""
    try:
        snap_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "t_io", "minute_snapshots")
        recent = []
        for root, dirs, files in os.walk(snap_dir):
            for fn in files:
                if fn.startswith(code) and fn.endswith(".json"):
                    recent.append(os.path.join(root, fn))
        recent.sort(reverse=True)
        total_vol = 0
        count = 0
        for p in recent[:5]:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            bars = data.get("bars", data if isinstance(data, list) else [])
            for bar in bars[:5]:
                total_vol += float(bar.get("amount", 0) or 0)
                count += 1
        if count > 0 and total_vol > 0 and auction_amount > 0:
            return round(auction_amount / (total_vol / count), 2)
    except Exception:
        pass
    return 1.0


def auction_action(change_pct, vol_ratio, sector_label, market_bias):
    """三维规则引擎：指数方向 × 板块热度 × 竞价涨跌量比 → 做T倾向。"""
    # 大前提：指数方向
    if market_bias.startswith("bullish"):
        bias_dir, bias_str = "高抛", "强" if vol_ratio >= 1.5 else "中"
    elif market_bias.startswith("bearish"):
        bias_dir, bias_str = "低吸", "强" if vol_ratio >= 1.5 else "中"
    else:
        bias_dir, bias_str = "", "中"

    # 板块维度
    if sector_label == "hot":
        sector_dir = "高抛"
    elif sector_label == "cold":
        sector_dir = "低吸"
    else:
        sector_dir = ""

    # 个股竞价维度
    if change_pct >= 2.0 and vol_ratio >= 1.5:
        stock_dir, stock_conf = "高抛", "高"
    elif change_pct >= 2.0:
        stock_dir, stock_conf = "高抛", "中"
    elif change_pct <= -2.0 and vol_ratio < 1.0:
        stock_dir, stock_conf = "低吸", "高"
    elif change_pct <= -2.0:
        stock_dir, stock_conf = "低吸", "中"
    else:
        stock_dir, stock_conf = "", "中"

    # 综合投票
    votes_high = sum(1 for v in [bias_dir, sector_dir, stock_dir] if v == "高抛")
    votes_low = sum(1 for v in [bias_dir, sector_dir, stock_dir] if v == "低吸")

    if votes_high >= 2 and stock_dir == "高抛":
        return ("优先高抛", stock_conf)
    elif votes_low >= 2 and stock_dir == "低吸":
        return ("优先低吸", stock_conf)
    elif change_pct >= 2.0:
        return ("关注高抛", "中")
    elif change_pct <= -2.0:
        return ("关注低吸", "中")
    elif vol_ratio >= 2.0:
        return ("关注", "中")
    elif sector_label == "hot":
        return ("关注", "中")
    else:
        return ("正常", "中")


# ============================================================
# 主入口：analyze_auction()
# ============================================================

def analyze_auction(holdings, snapshot_df=None):
    """主入口：执行三层竞价分析。holdings: {code: {name, pre_close, ...}}"""
    # 第一层：指数
    index_signals = fetch_index_auction()
    market_bias = classify_market_bias(index_signals)

    # 快照数据
    if snapshot_df is None or (hasattr(snapshot_df, 'empty') and snapshot_df.empty):
        try:
            snapshot_df = ak.stock_zh_a_spot_em()
        except Exception:
            snapshot_df = None

    # 第二层：板块热度
    sector_map = load_sector_map()
    snapshot_valid = snapshot_df is not None and not (hasattr(snapshot_df, 'empty') and snapshot_df.empty)
    sector_heat = build_sector_heat(snapshot_df, sector_map) if snapshot_valid else {}
    top5 = top_sectors(sector_heat)

    # 第三层：持仓竞价
    auction_signals = []
    snapshot_index = {}
    if snapshot_valid:
        code_col = next((c for c in ["代码", "code"] if c in snapshot_df.columns), None)
        change_col = next((c for c in ["涨跌幅", "change_pct"] if c in snapshot_df.columns), None)
        amt_col = next((c for c in ["成交额", "amount"] if c in snapshot_df.columns), None)
        if code_col:
            for _, row in snapshot_df.iterrows():
                snapshot_index[str(row[code_col]).strip()] = row

    for code, h in holdings.items():
        clean = code.split("_")[0] if "_" in code else code
        name = h.get("name", code)
        pre_close = float(h.get("pre_close", 0) or 0)

        row = snapshot_index.get(clean)
        change_pct = 0.0
        amount_wan = 0.0
        if row is not None and change_col and pre_close > 0:
            cur = float(row.get("最新价", pre_close) or pre_close)
            change_pct = round((cur - pre_close) / pre_close * 100, 2)
            if amt_col:
                amount_wan = round(float(row.get(amt_col, 0) or 0) / 10000, 1)
        vol_ratio = _approx_vol_ratio(clean, amount_wan * 10000) if amount_wan > 0 else 1.0

        tags = sector_map.get(clean, [])
        sector_label = ""
        for tag in tags:
            sh = sector_heat.get(tag)
            if sh:
                if sh.heat_label == "hot":
                    sector_label = "hot"
                    break
                elif sh.heat_label == "cold" and sector_label != "hot":
                    sector_label = "cold"

        action, confidence = auction_action(change_pct, vol_ratio, sector_label, market_bias)

        auction_signals.append(AuctionSignal(
            code=clean, name=name, change_pct=change_pct, amount_wan=amount_wan,
            vol_ratio=vol_ratio, sector_tags=tags, sector_heat_label=sector_label,
            action=action, confidence=confidence,
        ))

    def _to_dict(obj):
        if isinstance(obj, (IndexSignal, SectorHeat, AuctionSignal)):
            return {k: getattr(obj, k) for k in obj.__slots__}
        return obj

    return {
        "index_signals": [_to_dict(s) for s in index_signals],
        "market_bias": market_bias,
        "sector_top5": [_to_dict(s) for s in top5],
        "auction_signals": [_to_dict(s) for s in auction_signals],
    }


# ============================================================
# 飞书卡片格式化（供 preopen.py 调用）
# ============================================================

def format_auction_feishu(result):
    """将 analyze_auction() 输出转为飞书卡片 elements 列表。"""
    elements = []
    index_signals = result.get("index_signals", [])
    market_bias = result.get("market_bias", "unknown")
    sector_top5 = result.get("sector_top5", [])
    auction_signals = result.get("auction_signals", [])

    # 指数行
    if index_signals:
        idx_text = "  ".join(
            f"{s['name']} {s['change_pct']:+.2f}%" for s in index_signals
        )
    else:
        idx_text = "指数数据获取中（非交易时段正常）"
    bias_label = {"bullish": "📈偏多", "bearish": "📉偏空", "divergent": "⚡分化",
                   "neutral": "➡中性"}.get(market_bias.split("（")[0], market_bias)
    elements.append({"tag": "div", "text": {
        "content": f"**指数竞价**：{idx_text}\n**判定**：{bias_label}", "tag": "lark_md"}})
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
    if auction_signals:
        action_icon = {"优先高抛": "🔴", "优先低吸": "🟢", "关注高抛": "🟠",
                       "关注低吸": "🟠", "关注": "🟡", "正常": "⚪"}
        lines = []
        for s in auction_signals:
            icon = action_icon.get(s["action"], "⚪")
            sector_str = f"板块{s['sector_heat_label']}" if s.get("sector_heat_label") else ""
            lines.append(
                f"{icon} **{s['code'][-3:] if len(s['code'])>=3 else s['code']}** {s['name']} "
                f"{s['change_pct']:+.1f}% {s['amount_wan']:.0f}万 "
                f"量比{s['vol_ratio']:.1f}x {sector_str} → **{s['action']}**"
            )
        elements.append({"tag": "div", "text": {"content": "\n".join(lines), "tag": "lark_md"}})
        elements.append({"tag": "hr"})

        # 今日汇总
        priority_high = [s for s in auction_signals if "高抛" in s["action"]]
        priority_low = [s for s in auction_signals if "低吸" in s["action"]]
        summary_parts = []
        if priority_high:
            summary_parts.append(f"优先高抛：{'、'.join(s['name'] for s in priority_high)}")
        if priority_low:
            summary_parts.append(f"优先低吸：{'、'.join(s['name'] for s in priority_low)}")
        if not summary_parts:
            summary_parts.append("今日竞价无明确方向信号，正常做T")
        elements.append({"tag": "div", "text": {
            "content": f"**今日**：{'；'.join(summary_parts)}", "tag": "lark_md"}})

    return elements
