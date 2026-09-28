# -*- coding: utf-8 -*-
"""风格/轮动确定性计算（2026-09-27 新增）。

目的：把「风格变化 / 板块轮动」从提示词里的自由心证改成**可复现的数字**，喂给大盘复盘
（`core/market_review.build_prompt` 注入 `data["风格轮动"]`）。口径定义见
`doc/solutions/大盘指数多周期复盘方法论.md` §3.1 与 §四。

关键设计
--------
1. **切换阈值 θ 自适应**：取各指数**自身**近 250 日 |RS5| 的 p75，不用全局固定值。
   实测（799 个公共交易日，2023-06-12~2026-09-23，`t_io/cache/daily_kline/index_*.json`）：
   科创50 的 |RS5| p75 ≈ 1.98pp，沪深300 仅 ≈ 0.37pp——相差 5.4 倍。固定阈值必然
   对一边太松、对另一边太紧，这是"风格判断模糊"的技术根因。
2. **数据不足/退化一律标 `insufficient`**，不输出该段结论，也不用替代量冒充
   （典型陷阱：2026-09-21 当日 fetch_ok=0 致 sector_avgs 全 0.0，排名会退化成任意序）。
3. 只读不写：不触碰 holdings/台账类状态文件。
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]

# 单一真源：与 market_review 共用指数池
from core.market_review import INDEX_POOL  # noqa: E402

SENTIMENT_JSONL = BASE / "t_io" / "logs" / "sentiment_daily.jsonl"
INDEX_LONG_CACHE_DIR = BASE / "t_io" / "cache" / "daily_kline"

# 风格轴：(轴名, 强端, 弱端)。强端=小盘/成长/科创，弱端=权重/价值/主板。
STYLE_AXES = (
    ("规模", "中证500", "沪深300"),
    ("成长/价值", "创业板指", "沪深300"),
    ("科创/主板", "科创50", "上证指数"),
)

_RS_WINDOW = 5           # 切换判定的超额窗口（日）
_THETA_LOOKBACK = 250    # θ 回看交易日数
_THETA_MIN_OBS = 120     # θ 最少样本，不足则不判切换
_SWITCH_MIN_RUN = 5      # 同向连续 ≥N 日才算"已切换"
_FALSE_SWITCH_DAYS = 2   # 切换后 N 日内反号 → 假切换

# 迟滞维持带（2026-09-28 加）：θ 是二阶梯矩、会随数据源/窗口漂 ~10%，
# 贴着 θ 的事件随之抖动 ⇒ 状态标签来回翻（实测同一批日期上科创50/中证500 对调过）。
# 取"进入用 θ、退出用 0.6θ"的经典迟滞：死区 40%，避免 RS5 在 θ 附近反复穿越导致标签跳变。
# 注：0.6 是常用的迟滞比，**未做参数扫描标定**，只作抗抖用，不参与任何交易判定。
_SWITCH_HYSTERESIS = 0.6

_TOP_N = 5               # 板块强势/弱势分界
_OUT_RANK = 9            # 板块"榜外"分界（17 板块 → top5 / 后 8）
_LEADER_MIN_DAYS = 3     # 持续主线：连续在前 N 至少 N 日


# ---------------------------------------------------------------- 基础工具
def _norm_date(d) -> str:
    return str(d or "")[:10]


def _symbol_of(name: str) -> str | None:
    for sym, _ts, n in INDEX_POOL:
        if n == name:
            return sym
    return None


def _read_json(fp: Path):
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        return None


def _p75(vals) -> float | None:
    """四分位 p75（与标定口径一致：statistics.quantiles n=4 取第 3 个）。"""
    xs = [v for v in vals if v is not None]
    if len(xs) < 4:
        return None
    try:
        return float(statistics.quantiles(xs, n=4)[2])
    except Exception:
        return None


# ---------------------------------------------------------------- 指数序列
def load_index_series(name: str, date: str, count: int = 800):
    """返回 ({date: close}, {date: volume}, source)，只含 <= date 的记录。

    优先读 provider 长历史缓存 `t_io/cache/daily_kline/index_{sym}.json`（离线可用，约 800 行）；
    缓存未覆盖 date 时回退**腾讯直连**（`TencentProvider.index_daily`）。
    刻意不走 facade：facade 是 GM 优先，掘金终端未运行时会先吃满一次 60s 超时才降级，
    对本模块（可能连续拉 6 个指数）代价过高。
    """
    sym = _symbol_of(name)
    if not sym:
        return {}, {}, "none"
    fp = INDEX_LONG_CACHE_DIR / f"index_{sym}.json"

    rows, source = [], "none"
    blob = _read_json(fp)
    if isinstance(blob, dict) and blob.get("rows"):
        rows = blob["rows"]
        source = "cache"

    covered = bool(rows) and str(rows[-1].get("date") or "") >= date
    if not covered:
        try:
            from core.market_data.tencent_provider import TencentProvider
            df = TencentProvider().index_daily(sym, count, end_date=date)
            if df is not None and not df.empty:
                got = df.to_dict(orient="records")
                if got:
                    rows, source = got, "tencent"
        except Exception:
            pass

    closes, vols = {}, {}
    for r in rows:
        dv = _norm_date(r.get("date"))
        if not dv or dv > date:
            continue
        c = r.get("close")
        if c is None:
            continue
        try:
            closes[dv] = float(c)
        except (TypeError, ValueError):
            continue
        try:
            vols[dv] = float(r.get("volume") or 0.0)
        except (TypeError, ValueError):
            vols[dv] = 0.0
    return closes, vols, source


def _pct_series(closes: dict) -> dict:
    ds = sorted(closes)
    return {ds[i]: (closes[ds[i]] / closes[ds[i - 1]] - 1) * 100
            for i in range(1, len(ds)) if closes[ds[i - 1]]}


def _rs_series(closes: dict, base_closes: dict, window: int) -> dict:
    """相对基准的 window 日累计超额收益（pp）。"""
    ds = sorted(set(closes) & set(base_closes))
    if len(ds) < window + 1:
        return {}
    out = {}
    for i in range(window, len(ds)):
        w = ds[i - window + 1:i + 1]
        s = 0.0
        for j in range(1, len(w)):
            d, p = w[j], w[j - 1]
            if not closes[p] or not base_closes[p]:
                continue
            s += (closes[d] / closes[p] - 1) - (base_closes[d] / base_closes[p] - 1)
        out[ds[i]] = s * 100
    return out


def _theta_at(rs: dict, date: str) -> float | None:
    """date 之前的 |RS| 近 lookback 日 p75（不含当日，避免自我参照）。"""
    ds = [d for d in sorted(rs) if d < date]
    if len(ds) < _THETA_MIN_OBS:
        return None
    return _p75([abs(rs[d]) for d in ds[-_THETA_LOOKBACK:]])


# ---------------------------------------------------------------- 风格切换状态机
def _switch_events(rs: dict) -> list:
    """切换事件 = sign(RS) 翻转 且 |RS| >= 该时点 θ。"""
    ds = sorted(rs)
    events, prev = [], None
    for d in ds:
        sign = 1 if rs[d] > 0 else -1
        if prev is not None and sign != prev:
            th = _theta_at(rs, d)
            if th is not None and abs(rs[d]) >= th:
                events.append({"date": d, "sign": sign, "rs": rs[d], "theta": th})
        prev = sign
    return events


def _switch_state(name: str, rs: dict, vols: dict, date: str) -> dict:
    base = {"指数": name}
    if not rs or date not in rs:
        return {**base, "状态": "insufficient", "原因": "当日无 RS 数据（日线不足）"}
    cur = rs[date]
    sign = 1 if cur > 0 else -1
    theta = _theta_at(rs, date)
    base.update({"RS5(pp)": round(cur, 2),
                 "阈值θ(pp)": round(theta, 2) if theta is not None else None,
                 "θ依据": f"近{_THETA_LOOKBACK}日|RS5|p75" if theta is not None else None})
    if theta is None:
        return {**base, "状态": "insufficient",
                "原因": f"历史样本<{_THETA_MIN_OBS}日，不判切换"}

    # ── (1) 当前风格：只看当前 RS5 相对 θ 的量级（带维持带防抖）──
    # 直接回答"现在偏强还是偏弱"。这与"有没有发生过切换事件"是**两个不同的问题**
    # （旧版只输出后者，才会出现"RS5 +7.27 却显示未切换"这种反直觉结果）。
    a = abs(cur)
    band_lo = theta * _SWITCH_HYSTERESIS
    if a >= theta:
        base["当前风格"] = "偏强于上证" if sign > 0 else "偏弱于上证"
    elif a >= band_lo:
        base["当前风格"] = "中性偏强于上证" if sign > 0 else "中性偏弱于上证"
    else:
        base["当前风格"] = "中性（未达阈值）"
    base["当前风格依据"] = f"|RS5| {a:.2f} vs θ {theta:.2f}（维持带下限 {band_lo:.2f}）"

    # ── (2) 最近切换事件：sign 翻转当日且幅度达 θ 才算（事件闸）──
    events = _switch_events(rs)
    if not events:
        return {**base, "状态": "未切换",
                "说明": "样本内无有效切换事件（|RS| 从未在翻转当日达 θ）"}

    last = events[-1]
    ds = sorted(rs)
    days = ds.index(date) - ds.index(last["date"])   # 交易日间隔
    dir_cn = "转强于上证" if last["sign"] > 0 else "转弱于上证"

    if last["sign"] != sign:
        if days <= _FALSE_SWITCH_DAYS:
            return {**base, "状态": "假切换(噪声)", "方向": dir_cn,
                    "起始日": last["date"], "反号间隔(日)": days,
                    "说明": f"{days}日内反号，不构成有效切换"}
        return {**base, "状态": "未切换",
                "说明": f"RS 方向已与上次事件({last['date']} {dir_cn})相反，"
                        f"但幅度未达阈值 θ，不构成新的切换事件"}

    # 量能确认：同向段内是否出现过放量（量比>1.0）
    run_ds = ds[ds.index(last["date"]):]
    vol_ok = None
    if len(run_ds) > 6 and vols:
        try:
            ratios = []
            for i in range(6, len(run_ds)):
                d = run_ds[i]
                prev5 = [vols.get(x) for x in run_ds[i - 5:i]]
                prev5 = [v for v in prev5 if v]
                if prev5 and vols.get(d):
                    ratios.append(vols[d] / (sum(prev5) / len(prev5)))
            vol_ok = bool(ratios and max(ratios) > 1.0)
        except Exception:
            vol_ok = None

    if days < _SWITCH_MIN_RUN:
        return {**base, "状态": f"切换中（第{days + 1}日）", "方向": dir_cn,
                "起始日": last["date"], "放量确认": vol_ok}
    # 迟滞闸：事件方向须**仍在维持带之上**，"已切换"才继续成立。
    # 否则陈旧事件会永远挂成"已切换"（旧版问题：事件在 100 日前、当前 RS5 早已微不足道，标签照挂）。
    if a < band_lo:
        return {**base, "状态": "未切换", "方向": dir_cn, "起始日": last["date"],
                "持续(日)": days + 1, "放量确认": vol_ok,
                "说明": f"上次事件({last['date']} {dir_cn})名义持续中，但当前 |RS5| {a:.2f} "
                        f"已跌破维持带下限 {band_lo:.2f} ⇒ 方向已弱化，状态解除"}
    return {**base, "状态": "已切换", "方向": dir_cn, "起始日": last["date"],
            "持续(日)": days + 1, "放量确认": vol_ok}


# ---------------------------------------------------------------- 板块轮动
def _load_sector_days() -> dict:
    """{date: {"avgs": {sector: avg}, "fetch_ok": int|None}}，按日期升序去重（保留最后一条）。"""
    out = {}
    if not SENTIMENT_JSONL.exists():
        return out
    try:
        for line in SENTIMENT_JSONL.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            d = _norm_date(r.get("date"))
            sa = r.get("sector_avgs") or {}
            if not d or not sa:
                continue
            avgs = {str(k): v.get("avg") for k, v in sa.items()
                    if isinstance(v, dict) and v.get("avg") is not None}
            if not avgs:
                continue
            out[d] = {"avgs": {k: float(v) for k, v in avgs.items()},
                      "fetch_ok": r.get("fetch_ok")}
    except Exception:
        return {}
    return out


def _degenerate(day: dict) -> str | None:
    """返回退化原因；None 表示可用。"""
    if day is None:
        return "无当日板块数据"
    avgs = day.get("avgs") or {}
    if len(avgs) < 4:
        return f"板块样本过少({len(avgs)})"
    if day.get("fetch_ok") == 0:
        return "当日 fetch_ok=0（全量拉取失败）"
    if len(set(round(v, 6) for v in avgs.values())) == 1:
        return "板块均分全等（排名退化）"
    return None


def _rank_map(avgs: dict) -> dict:
    """按均分降序排名（1 起），同级按名字排序保证确定性。"""
    ordered = sorted(avgs.items(), key=lambda kv: (-kv[1], kv[0]))
    return {k: i + 1 for i, (k, _v) in enumerate(ordered)}


def build_sector_rotation(date: str) -> dict:
    days = _load_sector_days()
    if not days:
        return {"状态": "insufficient", "原因": "sentiment_daily.jsonl 无板块数据"}
    ds = sorted(days)
    today = _norm_date(date)
    if today not in days:
        # 取不晚于 date 的最近一日，并显式标注实际使用日
        cand = [d for d in ds if d <= today]
        if not cand:
            return {"状态": "insufficient", "原因": f"{today} 及之前无板块数据"}
        used = cand[-1]
    else:
        used = today

    why = _degenerate(days.get(used))
    if why:
        return {"状态": "insufficient", "实际使用日": used, "原因": why}

    i = ds.index(used)
    if i == 0:
        return {"状态": "insufficient", "实际使用日": used, "原因": "无前一交易日，无法算 Δrank"}

    prev = ds[i - 1]
    why_prev = _degenerate(days.get(prev))
    avgs_now, avgs_prev = days[used]["avgs"], days[prev]["avgs"]
    rk_now = _rank_map(avgs_now)

    # 前一交易日退化时，只给强弱榜，不给 Δrank 三态
    if why_prev:
        ordered = sorted(avgs_now.items(), key=lambda kv: (-kv[1], kv[0]))
        return {
            "状态": "partial", "实际使用日": used,
            "原因": f"前一交易日({prev})数据退化({why_prev})，不出 Δrank 三态",
            "强势TOP5": [{"板块": k, "均分": round(v, 2)} for k, v in ordered[:_TOP_N]],
            "弱势BOTTOM5": [{"板块": k, "均分": round(v, 2)} for k, v in ordered[-_TOP_N:]],
        }
    rk_prev = _rank_map(avgs_prev)

    common = [k for k in rk_now if k in rk_prev]
    new_rise = sorted(({"板块": k, "rank昨": rk_prev[k], "rank今": rk_now[k]}
                       for k in common if rk_now[k] <= _TOP_N and rk_prev[k] >= _OUT_RANK),
                      key=lambda x: x["rank今"])
    fade = sorted(({"板块": k, "rank昨": rk_prev[k], "rank今": rk_now[k]}
                   for k in common if rk_prev[k] <= _TOP_N and rk_now[k] >= _OUT_RANK),
                  key=lambda x: x["rank昨"])

    # 持续主线：从 used 往前数，连续在前 _TOP_N 的天数
    leaders = []
    for k in rk_now:
        if rk_now[k] > _TOP_N:
            continue
        streak = 0
        for j in range(i, -1, -1):
            d = ds[j]
            if _degenerate(days.get(d)):
                break
            if _rank_map(days[d]["avgs"]).get(k, 99) <= _TOP_N:
                streak += 1
            else:
                break
        if streak >= _LEADER_MIN_DAYS:
            leaders.append({"板块": k, "连续日数": streak})
    leaders.sort(key=lambda x: -x["连续日数"])

    ordered_now = sorted(avgs_now.items(), key=lambda kv: (-kv[1], kv[0]))
    out = {
        "状态": "ok",
        "实际使用日": used,
        "对照日": prev,
        "强势TOP5": [{"板块": k, "均分": round(v, 2)} for k, v in ordered_now[:_TOP_N]],
        "弱势BOTTOM5": [{"板块": k, "均分": round(v, 2)} for k, v in ordered_now[-_TOP_N:]],
        "新启": new_rise,
        "退潮": fade,
        "持续主线": leaders,
    }
    if fade and new_rise:
        out["迁移对"] = {"流出": fade[0]["板块"], "流入": new_rise[0]["板块"]}
    return out


# ---------------------------------------------------------------- 顶层
def build_style_rotation(date: str) -> dict:
    date = _norm_date(date)
    health: dict = {}
    closes, vols, sources = {}, {}, {}
    for _sym, _ts, name in INDEX_POOL:
        c, v, src = load_index_series(name, date)
        if c:
            closes[name], vols[name], sources[name] = c, v, src
            health[name] = {"ok": True, "来源": src, "交易日数": len(c)}
        else:
            health[name] = {"ok": False, "原因": "日线不可得（缓存未覆盖且回退失败）"}

    out: dict = {"date": date}

    # --- 风格横评（当日% / 近5日% / vs上证 1·5·10·20 日 / 量比）
    base = "上证指数"
    base_closes = closes.get(base) or {}
    rows = []
    for _sym, _ts, name in INDEX_POOL:
        c = closes.get(name)
        if not c or date not in c:
            rows.append({"指数": name, "error": "当日无数据"})
            continue
        ds = sorted(d for d in c if d <= date)
        px = c[date]
        chg = (px / c[ds[-2]] - 1) * 100 if len(ds) >= 2 and c[ds[-2]] else None
        chg5 = (px / c[ds[-6]] - 1) * 100 if len(ds) >= 6 and c[ds[-6]] else None
        vs = {}
        if name != base and base_closes:
            for w in (1, 5, 10, 20):
                rs = _rs_series(c, base_closes, w)
                if date in rs:
                    vs[f"vs上证{w}日(pp)"] = round(rs[date], 2)
        vr = None
        vv = vols.get(name) or {}
        if len(ds) >= 6 and vv.get(date):
            prev5 = [vv.get(d) for d in ds[-6:-1]]
            prev5 = [x for x in prev5 if x]
            if prev5:
                vr = round(vv[date] / (sum(prev5) / len(prev5)), 2)
        rows.append({"指数": name, "当日%": round(chg, 2) if chg is not None else None,
                     "近5日%": round(chg5, 2) if chg5 is not None else None,
                     "量比": vr, **vs})
    out["风格横评"] = rows

    # --- 日内风格差
    valid = [r for r in rows if r.get("当日%") is not None]
    if len(valid) >= 2:
        hi = max(valid, key=lambda r: r["当日%"])
        lo = min(valid, key=lambda r: r["当日%"])
        spread = hi["当日%"] - lo["当日%"]
        out["日内风格差"] = {
            "值(pp)": round(spread, 2),
            "最强": hi["指数"], "最弱": lo["指数"],
            "判定": ("有风格偏向" if spread >= 1.0 else
                     ("普涨/普跌无明显风格" if spread < 0.5 else "风格偏向偏弱")),
        }
    else:
        out["日内风格差"] = {"状态": "insufficient", "原因": "有效指数不足 2 个"}

    # --- 风格轴
    axes = []
    for ax, strong, weak in STYLE_AXES:
        cs, cw = closes.get(strong), closes.get(weak)
        if not cs or not cw:
            axes.append({"轴": ax, "状态": "insufficient",
                         "原因": f"缺数据：{[n for n, c in ((strong, cs), (weak, cw)) if not c]}"})
            continue
        rs = _rs_series(cs, cw, _RS_WINDOW)
        if date not in rs:
            axes.append({"轴": ax, "状态": "insufficient", "原因": "RS 不可算"})
            continue
        gap = rs[date]
        axes.append({"轴": ax, "强端": strong, "弱端": weak,
                     f"价差{_RS_WINDOW}日(pp)": round(gap, 2),
                     "方向": f"{strong if gap > 0 else weak}占优"})
    out["风格轴"] = axes

    # --- 风格切换状态机（逐指数 vs 上证）
    switches = []
    if base_closes:
        for _sym, _ts, name in INDEX_POOL:
            if name == base:
                continue
            c = closes.get(name)
            if not c:
                switches.append({"指数": name, "状态": "insufficient", "原因": "日线不可得"})
                continue
            rs = _rs_series(c, base_closes, _RS_WINDOW)
            switches.append(_switch_state(name, rs, vols.get(name) or {}, date))
    out["风格切换"] = switches

    # --- 板块轮动
    out["板块轮动"] = build_sector_rotation(date)

    # --- 数据健康
    out["数据健康"] = {
        "指数日线": health,
        "板块均分": ({"ok": True} if (out["板块轮动"].get("状态") == "ok")
                     else {"ok": False, "原因": out["板块轮动"].get("原因")}),
        "生成时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    return out
