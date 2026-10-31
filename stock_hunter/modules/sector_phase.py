# -*- coding: utf-8 -*-
"""板块广度与生命周期阶段（2026-10-24）

背景：猎手 D1–D12 是动量/技术打分，没有“板块现在处在生命周期的哪一段”这一维。
本模块用**韭研公社全天涨停复盘简图**的每日板块涨停家数，算出：

    首现 / 发酵 / 高潮 / 退潮 / 扩散 / 观察 / —

判定口径（全部可复述，不做黑箱）：
  · 首现：今日 > 0，且过去 10 个交易日该板块从未出现。
  · 扩散：今日 5–9 只、过去 10 日曾出现但从未到 5 只，且所属大主题近 15 日活跃 ≥12 天
          （= 大主题跑了很久之后才冒出来的补涨/新分支；10-21 的数据要素就是这一类）。
  · 退潮：近 5 日峰值 ≥5，今日 ≤ 峰值 60%，且今日 < 昨日。
  · 高潮：今日 ≥8、≥ 近 5 日峰值的 75%，且最高连板 ≥3。
  · 发酵：今日 ≥5，且今日 > 昨日（或 > 3 日前）。
  · 观察：其余有量但不满足以上条件的横盘/回踩。

数据文件：`stock_hunter/data/sector_breadth.json`
  {"schema":1,"updated_at":"YYYY-MM-DD","days":{"YYYY-MM-DD":{"sections":{
      "AI应用":{"count":7,"max_board":7,"members":[{"code":"002474","name":"榕基软件","board":1}]},
      ...}}}}
  members 可缺省（历史回填时只存 count/max_board）；新的一天由
  `scripts/daily_recap_ingest.py`  ingested 时带 members，供“池子漏票”对账。

⚠️ 板块名沿用韭研复盘原表；`CATEGORY_SECTIONS` 是猎手一级分类 → 复盘板块的映射，
   改映射要同步改这里（单一真源）。
"""
from __future__ import annotations

import json
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
FP = BASE / "stock_hunter" / "data" / "sector_breadth.json"

# 猎手一级分类 -> 韭研复盘板块（可多个；同一只票在多板块会被重复计数，广度只作相对强弱）
CATEGORY_SECTIONS = {
    "AI应用": ["AI应用", "AI影视", "AI短剧", "短剧", "影视短剧", "AI终端", "端侧AI"],
    "数据要素": ["数据要素", "物理AI"],
    "机器人": ["机器人"],
    "超节点": ["AI硬件", "超节点"],
    "网络安全": ["AI安全"],
    "半导体": ["半导体", "长鑫科技", "硅片"],
    "PCB": ["PCB"],
    "光通信": ["光通信"],
    "医药": ["医药医疗", "创新药"],
    "低空经济": ["低空经济"],
    "军工": ["军工"],
    "大消费": ["大消费"],
    "冰雪经济": ["冰雪经济"],
    "电力": ["电力", "AI配电", "智能电网"],
    "黄金": ["黄金", "贵金属"],
    "煤化工": ["煤化工"],
    "化工": ["化工", "化学制品", "化肥农药", "石油化工", "染料", "氟化工", "MDI"],
    "汽车": ["汽车产业链", "汽车", "智能驾驶", "汽车零部件"],
    "大农业": ["大农业", "农林牧渔"],
    "商业航天": ["商业航天"],
    "算力租赁": ["算力租赁", "算力", "AI基建", "数据中心"],
    "地产": ["地产", "房地产", "地产产业链", "地产基建"],
    "电池": ["电池产业链", "锂电产业链", "固态电池", "新能源汽车"],
    "煤炭": ["煤炭"],
    "金融": ["大金融"],
    "消费电子": ["消费电子"],
    "液冷": ["液冷散热", "AI液冷", "AI散热"],
    "传媒": ["传媒", "出版传媒", "影视短剧", "游戏"],
    "有色金属": ["有色金属", "电解铝"],
    # 2026-10-31 owner：「韭研概念缺 交运/环保」补齐后，把复盘板块名映射上，
    # 否则这两类在主线广度里永远看不到（复盘里出现过 航运/港口航运/生态环保）。
    "交运": ["航运", "港口航运", "交通运输", "高速公路", "港口"],
    "环保": ["生态环保", "环保", "固废", "水务"],
}

# 大主题（跨分类聚合；owner 2026-10-24：AI链 = AI应用 + 数据要素_物理AI + 机器人）
THEMES = {
    "AI链": ["AI应用", "数据要素", "机器人"],
    "电子/算力硬件": ["半导体", "PCB", "光通信", "超节点"],
    "半导体": ["半导体"],
    "PCB": ["PCB"],
    "光通信": ["光通信"],
    "医药医疗": ["医药"],
    "低空/军工": ["低空经济", "军工"],
    # 2026-10-29：`CATEGORY_SECTIONS` 里早就有「商业航天」，但 `THEMES` 一直没把它并进来
    # ⇒ 板块表里有数、主题汇总里**完全看不到**。实报：10-29 商业航天 9 家（当日第一），
    # 而"主题"一行只报了 AI链=退潮(7)，把当天真正的主线漏掉了。
    "商业航天": ["商业航天"],
    "大消费": ["大消费"],
    "冰雪经济": ["冰雪经济"],
    "电力": ["电力"],
    "农林牧渔": ["大农业"],
    "化工": ["煤化工", "化工"],
    "汽车": ["汽车"],
    "地产": ["地产"],
    "电池/新能源": ["电池"],
    "贵金属": ["黄金"],
    "煤炭": ["煤炭"],
    "大金融": ["金融"],
    "消费电子": ["消费电子"],
    "液冷散热": ["液冷"],
    "算力": ["算力租赁"],
    "传媒/出版": ["传媒"],
    "有色金属": ["有色金属"],
    "交运/航运": ["交运"],
    "环保": ["环保"],
}
THEME_OF_CATEGORY = {c: t for t, cs in THEMES.items() for c in cs}
SECTION_CATEGORY = {s: c for c, ss in CATEGORY_SECTIONS.items() for s in ss}

PHASES = ("首现", "发酵", "高潮", "退潮", "扩散", "观察", "—")

# 状态类/统计类分段，不是题材：留档但不当板块展示（“其他板块”榜也排除）
NON_SECTOR = {"公告", "其他", "ST相关", "中报预增(非公告)", "摘帽", "次新"}

_cache = {"mtime": None, "data": {}}


# ── 读取 ──────────────────────────────────────────────────────
def load_store(path: Path | str | None = None) -> dict:
    p = Path(path) if path else FP
    try:
        mt = p.stat().st_mtime
    except OSError:
        return {"schema": 1, "days": {}}
    if _cache["mtime"] == mt and _cache["data"]:
        return _cache["data"]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        data = {"schema": 1, "days": {}}
    data.setdefault("days", {})
    _cache.update({"mtime": mt, "data": data})
    return data


def all_dates(store: dict, asof: str | None = None) -> list[str]:
    ds = sorted(store.get("days", {}))
    if asof:
        ds = [d for d in ds if d <= asof]
    return ds


def trading_calendar(end: str | None = None, n: int = 25) -> list[str]:
    """最近 n 个**真实交易日**（用于表格横轴；缺录的日期保留空列，不要静默跳过）。

    日历来源 = 本地日线缓存 000001 的日期 ∪ 复盘库已有日期。
    这样既排除节假日（如 09-25 中秋、10-01~10-08 国庆），又包含"交易日但未录入"
    的日期（如 10-08），前端会把它显示为空白，而不是让横轴少一天。
    """
    days = set()
    try:
        fp = BASE / "t_io" / "cache" / "daily_kline" / "000001.json"
        d = json.loads(fp.read_text(encoding="utf-8"))
        days |= {str(r.get("date"))[:10] for r in d.get("rows", []) if r.get("date")}
    except Exception:
        pass
    days |= set(load_store().get("days", {}).keys())
    ds = sorted(x for x in days if x and (not end or x <= end))
    return ds[-n:]


def section_count(store: dict, date: str, section: str) -> int:
    rec = (store.get("days", {}).get(date, {}).get("sections", {}) or {}).get(section) or {}
    try:
        return int(rec.get("count") or 0)
    except (TypeError, ValueError):
        return 0


def section_max_board(store: dict, date: str, section: str) -> int:
    rec = (store.get("days", {}).get(date, {}).get("sections", {}) or {}).get(section) or {}
    try:
        return int(rec.get("max_board") or 0)
    except (TypeError, ValueError):
        return 0


def category_series(store: dict, category: str, dates: list[str]) -> list[int]:
    secs = CATEGORY_SECTIONS.get(category, [category])
    return [sum(section_count(store, d, s) for s in secs) for d in dates]


def category_max_board(store: dict, category: str, date: str) -> int:
    secs = CATEGORY_SECTIONS.get(category, [category])
    return max([section_max_board(store, date, s) for s in secs] or [0])


def theme_active_days(store: dict, category: str, dates: list[str], window: int = 15) -> int:
    """所属大主题近 window 个交易日有多少天有涨停（用于区分‘扩散’与‘首现’）。"""
    theme = THEME_OF_CATEGORY.get(category)
    cats = THEMES.get(theme, [category])
    ds = dates[-window:]
    n = 0
    for d in ds:
        if any(sum(section_count(store, d, s) for s in CATEGORY_SECTIONS.get(c, [c])) > 0
               for c in cats):
            n += 1
    return n


# ── 判定 ──────────────────────────────────────────────────────
def classify(series: list[int], max_board: int = 0, theme_days: int = 0) -> dict:
    """返回 {phase, count, delta1, delta3, peak5, max_board, theme_days, reason}。"""
    s = [int(x or 0) for x in series]
    if not s:
        return {"phase": "—", "count": 0, "delta1": 0, "delta3": 0, "peak5": 0,
                "max_board": int(max_board or 0), "theme_days": int(theme_days or 0),
                "reason": "无数据"}
    t = s[-1]
    y = s[-2] if len(s) >= 2 else 0
    d3 = t - s[-4] if len(s) >= 4 else t
    prev10 = s[-11:-1]
    peak5 = max(s[-5:])
    peak6 = max(s[-6:-1]) if len(s) > 1 else 0
    base = {"count": t, "delta1": t - y, "delta3": d3, "peak5": peak5,
            "max_board": int(max_board or 0), "theme_days": int(theme_days or 0)}

    if t == 0:
        ph = "退潮" if peak6 >= 5 else "—"
        return {**base, "phase": ph, "reason": f"今日0只；前6日峰值{peak6}"}

    if max(prev10 or [0]) == 0:
        if 5 <= t < 10 and theme_days >= 12:
            return {**base, "phase": "扩散",
                    "reason": f"首次出现{t}只，但大主题近15日已活跃{theme_days}天（末端新分支）"}
        return {**base, "phase": "首现", "reason": "今日>0，且过去10个交易日从未出现"}

    if 5 <= t < 10 and max(prev10 or [0]) < 5 and theme_days >= 12:
        return {**base, "phase": "扩散",
                "reason": f"今日{t}只但过去10日从未到5只，大主题近15日活跃{theme_days}天"}

    if peak5 >= 5 and t <= 0.6 * peak5 and t < y:
        return {**base, "phase": "退潮",
                "reason": f"今日{t} ≤ 近5日峰值{peak5}的60%，且低于昨日{y}"}

    if t >= 8 and t >= 0.75 * peak5 and int(max_board or 0) >= 3:
        return {**base, "phase": "高潮",
                "reason": f"今日{t} ≥ 近5日峰值{peak5}的75%，最高{max_board}板"}

    if t >= 5 and (t > y or (len(s) >= 4 and t > s[-4])):
        return {**base, "phase": "发酵", "reason": f"今日{t}只，高于昨日{y}/3日前{s[-4] if len(s)>=4 else 0}"}

    return {**base, "phase": "观察", "reason": f"今日{t}只，横盘/回踩"}


def phase_for_category(store: dict, category: str, asof: str | None = None,
                       window: int = 15) -> dict:
    ds = all_dates(store, asof)
    if not ds:
        return {"category": category, "phase": "—", "asof": asof, "series": []}
    series = category_series(store, category, ds)
    mb = category_max_board(store, category, ds[-1])
    td = theme_active_days(store, category, ds, window)
    out = classify(series, mb, td)
    out.update({"category": category, "asof": ds[-1], "series": series, "dates": ds})
    return out


def all_category_phases(asof: str | None = None) -> dict:
    store = load_store()
    return {c: phase_for_category(store, c, asof) for c in CATEGORY_SECTIONS}


def group_series(store: dict, theme: str, dates: list[str]) -> list[int]:
    cats = THEMES.get(theme, [])
    per = [category_series(store, c, dates) for c in cats]
    return [sum(v) for v in zip(*per)] if per else [0] * len(dates)


def group_phase(store: dict, theme: str, asof: str | None = None, window: int = 15) -> dict:
    ds = all_dates(store, asof)
    if not ds:
        return {"theme": theme, "phase": "—", "asof": asof, "series": []}
    series = group_series(store, theme, ds)
    cats = THEMES.get(theme, [])
    mb = max([category_max_board(store, c, ds[-1]) for c in cats] or [0])
    # 大主题自身的活跃天数：窗口内有量的天数
    active = sum(1 for v in series[-window:] if v > 0)
    out = classify(series, mb, active)
    out.update({"theme": theme, "asof": ds[-1], "series": series, "dates": ds})
    return out


def section_phase(store: dict, section: str, asof: str | None = None,
                  window: int = 15) -> dict:
    """单个复盘板块的阶段（与 category_phase 同口径；用于 AI链内部分支视图）。"""
    ds = all_dates(store, asof)
    if not ds:
        return {"section": section, "phase": "—", "asof": asof, "series": []}
    series = [section_count(store, d, section) for d in ds]
    mb = section_max_board(store, ds[-1], section)
    cat = SECTION_CATEGORY.get(section)
    td = theme_active_days(store, cat, ds, window) if cat else 0
    out = classify(series, mb, td)
    out.update({"section": section, "asof": ds[-1], "series": series, "dates": ds})
    return out


def sections_phase(store: dict, sections: list[str], theme_category: str,
                   asof: str | None = None, window: int = 15) -> dict:
    """多个板块合并成一个展示行（如 AI影视 + AI短剧）。"""
    ds = all_dates(store, asof)
    if not ds:
        return {"sections": sections, "phase": "—", "asof": asof, "series": []}
    series = [sum(section_count(store, d, s) for s in sections) for d in ds]
    mb = max([section_max_board(store, ds[-1], s) for s in sections] or [0])
    td = theme_active_days(store, theme_category, ds, window)
    out = classify(series, mb, td)
    out.update({"sections": sections, "asof": ds[-1], "series": series, "dates": ds})
    return out


AI_CHAIN_ROWS = [
    ("AI应用（泛）", ["AI应用"], "AI应用"),
    ("AI影视/短剧", ["AI影视", "AI短剧", "短剧", "影视短剧"], "AI应用"),
    ("AI硬件", ["AI硬件"], "超节点"),
    ("端侧AI", ["端侧AI"], "AI应用"),
    ("AI安全", ["AI安全"], "网络安全"),
    ("数据要素", ["数据要素"], "数据要素"),
    ("物理AI", ["物理AI"], "数据要素"),
    ("机器人", ["机器人"], "机器人"),
]


def snapshot(asof: str | None = None, max_days: int = 25) -> dict:
    """给 GUI 用：真实交易日横轴 + 各主题广度/阶段 + 各分类阶段 + AI链内部分支。

    `dates` 是**真实交易日**（含未录入日，前端显示空白）；`data_dates` 是复盘库
    里实际有数据的日期。阶段仍按有数据的日期序列计算，不受空列影响。
    """
    store = load_store()
    avail = all_dates(store, asof)
    end = avail[-1] if avail else asof
    cal = trading_calendar(end, max_days) or (avail[-max_days:] if avail else [])
    if avail:
        # 从复盘库第一天开始，不把日历里更早的空白日铺进来
        cal = [d for d in cal if d >= avail[0]]
    missing = [d for d in cal if d not in store.get("days", {})]

    def _theme_series(theme):
        out = []
        for d in cal:
            if d not in store.get("days", {}):
                out.append(None)
                continue
            out.append(sum(section_count(store, d, s)
                           for c in THEMES.get(theme, [])
                           for s in CATEGORY_SECTIONS.get(c, [c])))
        return out

    def _sections_series(secs):
        out = []
        for d in cal:
            if d not in store.get("days", {}):
                out.append(None)
                continue
            out.append(sum(section_count(store, d, s) for s in secs))
        return out

    themes = []
    for t in THEMES:
        p = group_phase(store, t, end)
        p["series"] = _theme_series(t)
        p["dates"] = cal
        themes.append(p)

    ai_chain = []
    for label, secs, cat in AI_CHAIN_ROWS:
        p = sections_phase(store, secs, cat, end)
        p["series"] = _sections_series(secs)
        p["label"] = label
        p["dates"] = cal
        ai_chain.append(p)

    # 其他板块：没被主题/AI链覆盖的原始板块，按窗口总量取前 12（避免 79 个板块全铺开）
    used = set()
    for cats_ in THEMES.values():
        for c in cats_:
            used |= set(CATEGORY_SECTIONS.get(c, [c]))
    for _, secs, _ in AI_CHAIN_ROWS:
        used |= set(secs)
    totals = {}
    for d in avail[-max_days:]:
        for s, v in (store["days"].get(d, {}).get("sections") or {}).items():
            n = int((v or {}).get("count") or 0)
            if n:
                totals[s] = totals.get(s, 0) + n
    skip = used | NON_SECTOR
    top = sorted(((s, n) for s, n in totals.items() if s not in skip),
                 key=lambda x: -x[1])[:12]
    others = []
    for s, total in top:
        p = section_phase(store, s, end)
        p["series"] = _sections_series([s])
        p["label"] = s
        p["total"] = total
        p["dates"] = cal
        others.append(p)

    cats = {c: phase_for_category(store, c, end) for c in CATEGORY_SECTIONS}
    return {"asof": end or "", "dates": cal, "data_dates": avail[-max_days:],
            "missing": missing, "themes": themes, "ai_chain": ai_chain,
            "others": others, "categories": cats,
            "updated_at": store.get("updated_at", "")}


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    snap = snapshot()
    print("asof", snap["asof"], "days", len(snap["dates"]))
    for t in snap["themes"]:
        print(f'{t["theme"]:12s} {t["phase"]:4s} count={t["count"]:3d} d3={t["delta3"]:3d} '
              f'peak5={t["peak5"]:3d} board={t["max_board"]}')
    print()
    for c, p in snap["categories"].items():
        print(f'{c:10s} {p["phase"]:4s} count={p["count"]:3d} d3={p["delta3"]:3d} '
              f'peak5={p["peak5"]:3d} board={p["max_board"]} theme_days={p["theme_days"]}')
