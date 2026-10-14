# -*- coding: utf-8 -*-
"""超跌跳空确认 · 预筛（两层漏斗）——包内模块 `prescreen`（2026-10-10 施工，原名 weak_strong_poll）。

**关键优化（owner 2026-10-10 强调「轮询速度是硬约束」）**：绝不逐股轮询。两层漏斗把
10:00 要看的股票从全池 ~5000 砍到几十只：

  盘前 09:25~09:55（每天一次，全池一次批量拉日线）
    「前一日大涨(≥9.5%)家数」top-N 板块（exp15 定稿的涨停数替身，**不用**
    daily_summary.json 的排名）→ 只留板块内**超跌候选**（昨收 < MA20）
    → 得 {code: prev_close/ma20/board/name}。
  10:00（一次性，单次批量调用）
    超跌候选一次批量拉 `1800s`（GM `history` list form，~1 次调用覆盖全部候选）
    → 10:00 棒 {code: low/close/amount/volume} → 交同包的 `decision.evaluate`。

**宇宙 = 全市场（2026-10-14 owner 决策，原「猎手板块范围内」扩展）**：
候选宇宙从「猎手表（watchlist_jiuyan.json）里有韭研主板块的 ~1550 只」扩到
**全 A 股**（`t_io/rotation/industry_map.csv` 申万口径 ~5000 只，剔 ST/退市/北交所/B股）。
板块强度排名保留、但按全市场口径现算；板块标签 = 猎手表韭研主板块**优先**
（猎手池口径不变式），无韭研分类的票回退申万一级行业（见 `universe_board_map`）。

⚠️ **标定失效警告**：exp24 的 86% 触发率、exp9 的 Sharpe 顶点（osc_top_n=4）、
exp15 的板块排名口径，**全部基于猎手池宇宙标定**。宇宙扩展到全市场后，
候选池成分/板块热度分布/信号密度都会变，**原标定结论需在全市场宇宙下重新验证**
（尤其 osc_top_n 与 osc_board_top_n 的最优点可能漂移）。

**纯逻辑 + 注入数据源**：不 import gm.api（分钟取数由 gm_main 注入 `fetch_fn`），
日线走 facade（与 hunter_ma5_alert 同款），故可离线单测。
"""
from __future__ import annotations

import json
import os

# 仓库根：本文件在 execution/auto/oversold_gap_confirm/ ⇒ 上溯 3 层
_ROOT = os.environ.get("SUPERTRADER_ROOT") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_WL_FP = os.path.join(_ROOT, "stock_hunter", "watchlist_jiuyan.json")
# 全市场申万一级行业映射（代码/名称/行业名称，~5.6k 行；scripts/p2_aux/fetch_industry.py 产出）
_INDUSTRY_MAP_FP = os.environ.get("OSC_INDUSTRY_MAP_FP") or os.path.join(
    _ROOT, "t_io", "rotation", "industry_map.csv")

MERGES = {"创新药": "医药", "中药": "医药"}
_BATCH = 200          # daily_many 分片（对齐 hunter_ma5_alert）
_DAYS = 40            # 日线根数（MA20 预热 + 裕度）


def default_provider():
    """默认数据源 = `core/market_data/facade.py`（gm 主源、腾讯降级）。

    ⚠️ 2026-10-12 实测：GM 终端里 sys.path 只有 `execution/auto` 与 `_gm/`，
    `import core` 会 ModuleNotFoundError ⇒ 盘前预筛**天天 fail-closed、当日永不下单**。
    修法见宿主 `gm_main.py` 顶部：**把仓根 append 到 sys.path 末尾**（append 而非 insert，
    以保住 `_gm/` 对 `analysis`/`config` 的优先级）。此处仍保留注入参数 `provider=`，
    供干跑/单测替换数据源。
    """
    from core.market_data import facade           # noqa: PLC0415（宿主负责路径）
    return facade.get_provider()


def sym2board_map() -> dict:
    """6位代码 -> 主板块（第一个韭研分类，含 merge；与 exp12 同口径，单一真源）。"""
    try:
        with open(_WL_FP, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    m = {}
    for code, info in (data.items() if isinstance(data, dict) else []):
        if not isinstance(info, dict) or not str(code).isdigit():
            continue
        cats = []
        for i in range(1, 10):
            c = str(info.get(f"jiuyan_category{i}", "") or "").strip()
            if c:
                cats.append(MERGES.get(c, c))
        if not cats:
            c = str(info.get("jiuyan_category", "") or "").strip()
            cats = [MERGES.get(cc.strip(), cc.strip()) for cc in c.split("|") if cc.strip()]
        m[str(code)] = cats[0] if cats else ""
    return m


_LU_THRESHOLD = 0.095   # 前一日涨幅 ≥9.5% 计涨停（对齐 exp12 lu_known 替身口径）


def full_market_meta() -> dict:
    """全 A 股元信息 → {code: {'name','sw'}}（申万一级行业，industry_map.csv 单一真源）。

    剔除规则（OSC 买侧边界，2026-10-14 定）：
      · **ST / 退市**：名称含 "ST" 或 "退" —— 涨跌幅 5% 且风险属性不符超跌跳空策略；
      · **北交所**：代码 4/8/920 开头 —— OSC 本来就不买北交所（30% 涨跌幅、流动性另算）；
      · **B 股**：200/900 开头 —— 外币计价，本账户通道买不了（industry_map 里混入 ~78 只）。
    文件缺失/读坏 ⇒ {}（调用方退化为猎手池旧行为，**不是** fail-closed 停一天）。
    """
    import csv
    out = {}
    try:
        with open(_INDUSTRY_MAP_FP, "r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                code = str(row.get("代码") or "").strip().zfill(6)
                name = str(row.get("名称") or "").strip()
                sw = str(row.get("行业名称") or "").strip()
                if not (code.isdigit() and len(code) == 6):
                    continue
                if code[:1] in "48" or code[:3] in ("920", "200", "900"):
                    continue                                  # 北交所 / B 股
                up = name.upper()
                if "ST" in up or "退" in name:
                    continue                                  # ST / 退市整理
                out[code] = {"name": name, "sw": sw}
    except Exception:
        return {}
    return out


def universe_board_map() -> dict:
    """**全市场口径** code -> 板块标签（2026-10-14 宇宙扩展）。

    两层来源，**韭研主板块优先**：
      1. 全 A 股（剔 ST/退市/北交所/B股）→ 申万一级行业（`full_market_meta`）；
      2. 猎手表韭研主板块（`sym2board_map`，exp12 同口径）**覆盖**其上
         —— 猎手池内的票的板块标签与扩展前**逐码一致**（行为不变式）。

    注意：这制造了两个标签命名空间（韭研板块名 vs 申万行业名），同一只票只取其一；
    涨停家数按各自标签计数，top-N 排名在两个命名空间混排 —— 这是「猎手池口径不变 +
    全市场扩展」的保守折中，exp15 的板块排名口径结论需在全市场下重验证（见模块 docstring）。

    industry_map.csv 缺失 ⇒ 退化为旧行为（仅猎手表有韭研板块的票）。
    """
    m = {c: v["sw"] for c, v in full_market_meta().items() if v.get("sw")}
    for code, board in sym2board_map().items():
        if board:
            m[code] = board        # 韭研主板块优先（猎手池口径不变式）
    return m


def _codes_with_board(board_map: dict) -> list:
    """有板块标签的 6 位码列表（供全池批量取日线）。"""
    return [c for c in board_map if board_map.get(c)]


def premarket_candidates(top_n: int, date: str = None, provider=None) -> dict:
    """盘前超跌候选 → {code: {'prev_close','ma20','board','name'}}。

    **板块口径（exp15 全池复验定稿，2026-10-10）**：取「前一日大涨(≥9.5%)家数」top-N 板块
    （对齐 exp12 `lu_known` 替身），**不用** `daily_summary.json` 的排名——exp15 里平均分与
    涨停数两种真实排名在 top-4 组合都塌成负 Sharpe，只有从日线 bar 现算的涨停家数替身通过。

    **宇宙（2026-10-14 owner 决策扩展为全市场）**：候选池 = 全 A 股（剔 ST/退市/北交所/B股），
    板块强度排名按**全市场口径**现算（`universe_board_map`：韭研主板块优先、申万行业回退）。
    扩展前只有「猎手表里有韭研主板块」的 ~1550 只参与；扩展后 ~5000 只参与。
    ⚠️ exp24 的 86% 触发率、exp9 的 Sharpe 顶点均基于**猎手池**标定，
    宇宙扩展后原标定结论需重验证（osc_top_n / osc_board_top_n 最优点可能漂移）。

    实现：全池一次批量拉日线(40 根)，同批算 ①板块涨停家数排名 ②超跌(昨收<MA20) 预筛，
    只留 top-N 板块内超跌票。取不到日线/板块无分类的票自动缺省跳过。
    """
    from collections import Counter
    if provider is None:
        try:
            provider = default_provider()
        except Exception as _e:
            print(f"[OSC] ⛔ 数据源不可用（core.market_data.facade）⇒ 当日不下单: "
                  f"{type(_e).__name__}: {_e}")
            return {}
    meta = full_market_meta()
    board_map = universe_board_map()
    if not meta:
        print("[OSC] ⚠️ industry_map.csv 缺失/读坏 ⇒ 宇宙退化为猎手池（旧行为），"
              "全市场扩展未生效")
    codes = _codes_with_board(board_map)
    if not codes:
        return {}
    frames = {}
    for i in range(0, len(codes), _BATCH):
        try:
            frames.update(provider.daily_many(codes[i:i + _BATCH], days=_DAYS) or {})
        except Exception:
            pass

    # ① 前一日大涨家数 → 板块排名（复用各票 past 收盘序列）
    lu = Counter()
    pasts = {}
    for c in codes:
        fr = frames.get(c)
        if fr is None or getattr(fr, "empty", True):
            continue
        try:
            dates = fr["date"].astype(str)
            closes = fr["close"].astype(float)
        except Exception:
            continue
        # 昨收口径：取 date < 今日（盘中 forming bar 不参与）的最后一根完整日线收盘
        if date:
            past = closes[dates < date]
        else:
            from datetime import datetime
            past = closes[dates < datetime.now().strftime("%Y-%m-%d")]
        if len(past) < 2:
            continue
        pasts[c] = past
        if float(past.iloc[-1]) / float(past.iloc[-2]) - 1 >= _LU_THRESHOLD:
            lu[board_map[c]] += 1
    hot = {b for b, n in lu.most_common(int(top_n)) if n > 0}
    if not hot:
        return {}

    # ② 只留 top-N 板块内「昨收 < MA20」的超跌票（超跌跳空确认 osc_W1（原 weak_W1） 预筛）
    out = {}
    for c in codes:
        if board_map.get(c) not in hot:
            continue
        past = pasts.get(c)
        if past is None or len(past) < 20:
            continue
        prev_close = float(past.iloc[-1])
        ma20 = float(past.tail(20).mean())
        if not (prev_close > 0 and prev_close < ma20):
            continue
        out[c] = {"prev_close": prev_close, "ma20": ma20,
                  "board": board_map[c], "name": (meta.get(c) or {}).get("name", "")}
    return out


def ten_am_bars(codes, now, fetch_fn) -> dict:
    """10:00 棒 → {code: {'low','close','amount','volume'}}。

    `fetch_fn(codes, now)` 由 gm_main 注入（GM `history` list form，frequency="1800s"，
    起止=今日，取 10:00 那一根）。本函数只做**防御式抽取**：缺关键价的票不入选。
    """
    out = {}
    try:
        raw = fetch_fn(list(codes), now)
    except Exception:
        return {}
    for c in list(codes):
        try:
            b = raw.get(c)
            if not b:
                continue
            low = float(b.get("low") or 0)
            close = float(b.get("close") or 0)
            amount = float(b.get("amount") or 0)
            volume = float(b.get("volume") or 0)
            if not (low > 0 and close > 0):
                continue
            out[c] = {"low": low, "close": close, "amount": amount, "volume": volume}
        except Exception:
            continue
    return out


def split_into_prev_close_ma20(candidates: dict) -> tuple[dict, dict]:
    """把 premarket_candidates 的输出拆成 evaluate 要的 {code:prev_close}/{code:ma20}。"""
    pc, ma = {}, {}
    for c, v in (candidates or {}).items():
        pc[c] = v.get("prev_close")
        ma[c] = v.get("ma20")
    return pc, ma
