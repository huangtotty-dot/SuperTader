# -*- coding: utf-8 -*-
"""GUI 性能三条铁律的静态源码守卫（2026-11-22）。

依据：`doc/solutions/2026-11-22_UI实时性深度诊断与架构优化.md` 第五节「三条铁律工程化」
——11 月的 UI 卡顿病全是"新代码绕过老纪律"，把纪律从注释变成闸门。
先例：tests/phase3/test_http_pool_shared.py 的「读源码做断言」模式。

**全离线、秒级**：只读 `gui.py` / `web/app.js` / `core/**` 源码做正则断言——
不发任何网络请求、不 import gui、不碰 t_io 数据；解析对换行/空白宽容。

铁律① TTL ≠ 前端轮询周期：TTL 落在危险带 `(0.8×周期, 周期]` 即相位病
         （每拍穿透缓存打网络；同型已复发 3 次：P1-9 → mw_head_stats → sentiment_live）。
铁律② js_api 调用线程禁裸网络：`urlopen(` 必须 0（东财 3 处已收编 http_pool）；
         `requests.get/post` 登记基线防新增（既有登记即可，不强制清零）。
铁律③ 行情消费必走 QuoteBus：`TencentProvider()` 直实例化 = 冻结白名单；
         fail-open 回退位置必须在同一函数体内带 `quote_bus` 引用。
         扫描范围（2026-11-23 P1-4 扩）：gui.py + core/ + analysis/ + stock_hunter/
         + sector_rotation/ + scripts/；扩范围部分的首轮实例按裁决登记于
         _TENCENT_EXEMPT_OUTER（带同文本行出现次数钉）。

行号漂移说明：铁律②③的白名单按 (文件, 行号) 冻结。业务代码正常演进导致行号移动时，
按失败信息**更新行号**即可（顺带核对理由仍成立）；**新增**位置则必须先「走 pooled_get /
走 QuoteBus」，或按格式登记豁免理由——这正是闸门要拦的事。

运行：python -m unittest tests.phase3.test_architecture_guards -v
"""
import re
import sys
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]

_GUI_SRC = (_ROOT / "gui.py").read_text(encoding="utf-8")
_APP_JS_SRC = (_ROOT / "web" / "app.js").read_text(encoding="utf-8")


def _parse_number(src, pattern, what):
    """从源码解析一个数值常量/字面量。**解析不到即失败**（不许跳过）——
    常量改名/挪位时必须同步更新本守卫的注册表。"""
    m = re.search(pattern, src)
    if not m:
        raise AssertionError(
            f"解析不到 {what}（正则 {pattern!r} 无匹配）——"
            f"请核对常量是否改名/挪位，并同步更新 test_architecture_guards 的注册表"
        )
    return float(m.group(1))


# ════════════════════════════════════════════════════════════════
# 铁律① TTL 相位守卫：注册表（数据驱动，新端点必须登记）
# ════════════════════════════════════════════════════════════════
# 每条字段：
#   endpoint : 端点名（js_api 入口）
#   ttl_name : gui.py 中 TTL 常量/缓存的定位名（仅用于报告）
#   ttl_re   : 从 gui.py 捕获 TTL 秒数的正则（唯一捕获组）
#   poll_re  : 从 web/app.js 捕获前端轮询毫秒数的正则（唯一捕获组）
#   exempt   : 豁免理由（SWR / 刻意取舍），None ⇒ 必须满足相位关系
# 关系合法 ⇔ TTL > 周期（隔拍命中，网络减半）
#           或 TTL ≤ 0.8×周期（拍内去重，每拍至多一次真取数）
#           或 登记了豁免理由。
_LIVE_BUNDLE_RE = r'_startStaggered\("live"[\s\S]{0,300}?,\s*(\d+)\s*,\s*\d+\s*\)'

_TTL_REGISTRY = [
    dict(endpoint="load_mw_head_stats", ttl_name="_SECTOR_PCT_MEM",
         # 2026-11-24：改为 **SWR**（有旧值立即返，过期才后台单飞刷新；js_api 零网络），
         # 判定式由 `< 12.0`（同步直返）变为 `age >= 12.0`（触发后台刷新）。
         ttl_re=r"_SECTOR_PCT_MEM[\s\S]{0,1600}?age\s*>=\s*(\d+(?:\.\d+)?)",
         poll_re=r"setInterval\(mwSectorTick,\s*(\d+)\)",
         exempt=None),                       # 12s > 10s：隔拍命中内存热缓存（SWR 后不再阻塞桥）
    dict(endpoint="load_sentiment_live", ttl_name="_SENT_LIVE_MEM（盘中 TTL）",
         ttl_re=r"_ttl\s*=\s*(\d+(?:\.\d+)?)\s+if\s+_in_sess\b",
         poll_re=r"setTimeout\(loadSentimentLive,\s*(\d+)\)",
         exempt=None),                       # 75s > 60s：P0-5 二次修正（55s 在「完成后重排」
                                             # 轮询模型下不成立，经守卫批次实测指出后调大）
    dict(endpoint="load_turnover_live", ttl_name="_turnover_live_mem（实例缓存）",
         ttl_re=r'"_turnover_live_mem"[\s\S]{0,300}?<\s*(\d+(?:\.\d+)?)',
         poll_re=r"setInterval\(mwTurnTick,\s*(\d+)\)",
         exempt=None),                       # 6s ≤ 8s(=0.8×10s)：P0-4 拍内去重
    dict(endpoint="load_indices", ttl_name="_INDICES_TTL",
         ttl_re=r"_INDICES_TTL\s*=\s*(\d+(?:\.\d+)?)",
         poll_re=_LIVE_BUNDLE_RE,            # 经 poll_live_bundle 随 10s live 拍回来
         exempt=None),                       # 12s > 10s
    dict(endpoint="load_multi_chart（九宫格）", ttl_name="_MW_TTL",
         ttl_re=r"_MW_TTL\s*=\s*(\d+(?:\.\d+)?)",
         poll_re=r"MW_PERIOD_MS\s*=\s*(\d+)",
         exempt=None),                       # 4.6s > 4s
    dict(endpoint="load_quotes", ttl_name="_QUOTES_SWR_FRESH",
         ttl_re=r"_QUOTES_SWR_FRESH\s*=\s*(\d+(?:\.\d+)?)",
         poll_re=_LIVE_BUNDLE_RE,            # 顶部行情条经 poll_live_bundle 随 10s live 拍回来
         exempt=("P0-2 起语义为：fresh 直返；stale **同步**走 QuoteBus（共享档 max_age=8s，"
                 "bus 命中本征毫秒级），异常回旧值不抛。TTL 不再承担「省网络」职责"
                 "（stale 拍经 bus 同步取数，无相位病），登记豁免。")),
    dict(endpoint="load_index_leaders", ttl_name="_IDX_LEADERS_TTL",
         ttl_re=r"_IDX_LEADERS_TTL\s*=\s*(\d+(?:\.\d+)?)",
         poll_re=r"_IDX_LEADERS_TTL_MS\s*=\s*(\d+)",
         exempt=None),                       # 86400s > 60s：名单日频（市值前10），
                                             # 重建在 core.index_leaders 后台单飞，前端只读缓存
    dict(endpoint="load_auto_pnl", ttl_name="_AUTO_PNL_TTL",
         ttl_re=r"_AUTO_PNL_TTL\s*=\s*(\d+(?:\.\d+)?)",
         poll_re=r'_startStaggered\("auto"[\s\S]{0,500}?,\s*(\d+)\s*,\s*\d+\s*\)',
         exempt=("P1-3 起为到期 SWR：旧值立返 + 后台单飞重算（全量 fill 重算 3~5s "
                 "不进调用线程）——TTL 到期不阻塞调用方，无相位病，登记豁免。"
                 "（120s > 10s 本也满足错位关系。）")),
    dict(endpoint="load_market_turnover（成交额窗口）", ttl_name="_TURNOVER_TTL",
         ttl_re=r"_TURNOVER_TTL\s*=\s*(\d+(?:\.\d+)?)",
         poll_re=r"TURNOVER_WIN_PERIOD_MS\s*=\s*(\d+)",
         exempt=None),                       # 60s > 30s（P1-9 窗口轮询 10s→30s）
]


class TestTtlPhaseGuard(unittest.TestCase):
    """铁律①：TTL 必须 ≠ 前端轮询周期（或登记豁免）。"""

    def test_registry_well_formed(self):
        seen = set()
        for e in _TTL_REGISTRY:
            self.assertNotIn(e["endpoint"], seen, f"注册表端点重复: {e['endpoint']}")
            seen.add(e["endpoint"])
            for k in ("ttl_name", "ttl_re", "poll_re", "exempt"):
                self.assertIn(k, e, f"{e['endpoint']} 缺字段 {k}")
            re.compile(e["ttl_re"])
            re.compile(e["poll_re"])

    def test_entries_parse(self):
        """每条注册项都必须能解析出 TTL 与轮询周期的**当前值**（解析不到即失败，不跳过）。"""
        for e in _TTL_REGISTRY:
            with self.subTest(endpoint=e["endpoint"]):
                _parse_number(_GUI_SRC, e["ttl_re"], f"gui.py TTL 常量 {e['ttl_name']}")
                _parse_number(_APP_JS_SRC, e["poll_re"], f"app.js 轮询周期（{e['endpoint']}）")

    def test_ttl_phase_relation(self):
        for e in _TTL_REGISTRY:
            ttl = _parse_number(_GUI_SRC, e["ttl_re"], f"gui.py TTL 常量 {e['ttl_name']}")
            poll_ms = _parse_number(_APP_JS_SRC, e["poll_re"], f"app.js 轮询周期（{e['endpoint']}）")
            period = poll_ms / 1000.0
            with self.subTest(endpoint=e["endpoint"], ttl=ttl, period=period):
                if e["exempt"] is not None:
                    continue  # 已登记豁免（SWR / 刻意取舍），理由见注册表
                ok = (ttl > period) or (ttl <= period * 0.8)
                self.assertTrue(
                    ok,
                    f"铁律①违反：{e['endpoint']} TTL({ttl}s) 落在相位危险带 "
                    f"({period * 0.8:.1f}s, {period}s]（0.8×周期 < TTL ≤ 周期）。"
                    f"修法二选一：① 把 TTL 调到 >{period:.0f}s（隔拍命中、网络减半）"
                    f"或 ≤{period * 0.8:.1f}s（拍内去重）；"
                    f"② 若该端点已 SWR 化/刻意取舍，在 _TTL_REGISTRY 对应条登记豁免理由。",
                )


# ════════════════════════════════════════════════════════════════
# 铁律② js_api 调用线程禁裸网络
# ════════════════════════════════════════════════════════════════
class TestCallThreadNoBareNetwork(unittest.TestCase):
    # urlopen 白名单：{行号: 理由}。http_pool（2026-10-20 core 5 处 + 2026-11-22 P1-6
    # 东财 3 处收编）后必须为 0；确有刻意保留的裸 urlopen，逐条登记行号+理由。
    _URL_OPEN_WHITELIST: dict = {}

    # requests 基线（2026-11-22 登记，防新增裸 requests；既有登记即可，不强制清零）：
    #   requests.get ×7 → gui.py:483/544/593（同花顺涨停/板块）、635（腾讯指数三腿）、
    #                      680（同花顺板块）、706（东财 ulist 家数）、743（选股宝风向标）
    #   requests.post ×0
    _REQUESTS_GET_BASELINE = 7
    _REQUESTS_POST_BASELINE = 0

    def test_no_bare_urlopen(self):
        found = {i for i, ln in enumerate(_GUI_SRC.splitlines(), 1) if "urlopen(" in ln}
        want = set(self._URL_OPEN_WHITELIST)
        self.assertEqual(
            found, want,
            "铁律②违反：gui.py 裸 urlopen 位置与白名单不一致。\n"
            f"  源码扫出: {sorted(found) or '无'}\n  白名单登记: {sorted(want) or '无'}\n"
            "修法：新出网一律走 core.market_data.http_pool.pooled_get（keep-alive 复用，"
            "裸 urlopen 每次新建 TLS ≈290ms）；刻意保留的在 _URL_OPEN_WHITELIST 登记行号+理由；"
            "行号漂移则更新白名单行号。",
        )
        for ln_no, reason in self._URL_OPEN_WHITELIST.items():
            self.assertTrue(str(reason).strip(), f"urlopen 白名单 gui.py:{ln_no} 缺理由")

    def test_requests_baseline(self):
        n_get = _GUI_SRC.count("requests.get(")
        n_post = _GUI_SRC.count("requests.post(")
        self.assertEqual(
            n_get, self._REQUESTS_GET_BASELINE,
            f"铁律②基线漂移：gui.py `requests.get(` {n_get} 处 ≠ 基线 {self._REQUESTS_GET_BASELINE}。"
            "新增裸 requests 前先评估改走 http_pool.pooled_get；刻意保留则更新基线并在注释登记理由。",
        )
        self.assertEqual(
            n_post, self._REQUESTS_POST_BASELINE,
            f"铁律②基线漂移：gui.py `requests.post(` {n_post} 处 ≠ 基线 {self._REQUESTS_POST_BASELINE}。"
            "新增裸 requests 前先评估改走 http_pool.pooled_get；刻意保留则更新基线并在注释登记理由。",
        )


# ════════════════════════════════════════════════════════════════
# 铁律③ 行情消费必走 QuoteBus：TencentProvider() 直实例化冻结白名单
# ════════════════════════════════════════════════════════════════
# (文件相对路径, 行内容片段): (类别, 理由)
#   片段 = 含 TencentProvider() 那一行 strip 后的完整文本——**行号漂移免疫**
#   （2026-11-22 修订：原按行号冻结，gui.py 任何编辑都会误报失配）；
#   只有这行代码本体被改动才失配（正是需要重新评审的时刻）。
#   类别 "failopen"   ⇒ bus 异常/取空后的回退直调；断言**同一函数体内**出现 quote_bus 引用
#   类别 "registered" ⇒ 基础设施/用户触发一次性/历史日线等登记豁免（非轮询实时行情消费）
# 新增未登记位置 ⇒ 测试失败：走 QuoteBus（core.market_data.quote_bus.get_quotes）或登记豁免。
_TENCENT_FREEZE = {
    ("gui.py", "snaps = TencentProvider().snapshot_auction_many(want) or {}"): (
        "failopen", "load_quotes_for：bus 异常 ⇒ 回退直调（2026-10-26 P2 收编）"),
    ("gui.py", "snaps = TencentProvider().snapshot_auction(list(symbols.keys()))"): (
        "failopen", "_load_quotes_build：bus 异常 ⇒ 回退直调（2026-10-26 P2 收编）"),
    ("gui.py", "_snaps = TencentProvider().snapshot_auction([base]) or {}"): (
        "failopen", "_fetch_live_quote（九宫格）：bus 空/异常 ⇒ 回退直调（2026-11-22 P1-5 收编）"),
    ("gui.py", "lambda: TencentProvider().snapshot([_direct]), 2.0)"): (
        "registered", "K线弹窗开图前存在性校验：用户手输码的一次性校验（~60ms），非轮询行情消费"),
    ("gui.py", "d = TencentProvider().daily(_c, min_rows=int(min_rows or 0))"): (
        "registered", "_fetch_daily_bounded：K线弹窗日线取数腾讯优先（历史日线，非实时快照；GM 兜底链上游）"),
    ("gui.py", "snap = TencentProvider().snapshot([base]).get(base)"): (
        "registered", "forming bar 单只快照回退：K线弹窗用户触发一次性，腾讯优先 GM 兜底"),
    ("gui.py", "snap = TencentProvider().snapshot_auction([query])"): (
        "registered", "search_stock 精确码名称直查：用户触发一次性"),
    ("core/ma5_pullback.py", "return TencentProvider().snapshot_auction_many(codes) or {}"): (
        "failopen", "P0-6（2026-11-22）：bus 空/异常 ⇒ 回退直调"),
    ("core/market_data/facade.py", "self._tx = TencentProvider()"): (
        "registered", "provider 门面本体：facade 是 GM/腾讯兜底链基础设施"),
    ("core/market_data/quote_bus.py", "return TencentProvider().snapshot_auction_many(list(codes)) or {}"): (
        "registered", "QuoteBus 出网腿本体（bus 唯一腾讯直连点）"),
    ("core/style_rotation.py", "df = TencentProvider().index_daily(sym, count, end_date=date)"): (
        "registered", "风格轮动指数日线补缺：盘后批算历史日线（index_daily），非实时快照消费"),
}

# ── 扩范围登记豁免表（2026-11-23 P1-4）────────────────────────────────
# 扫描范围自 gui.py + core/ 扩至 analysis/、stock_hunter/、sector_rotation/、scripts/
# 后的首轮盘点登记（审计逐处复核为活路径/离线批处理/人工工具，均非 QuoteBus 轮询
# 实时行情消费语义，故全部为 registered 豁免，无 failopen）。
# (文件相对路径, 行内容片段): (类别, 理由, 该片段在文件内的预期出现次数)
#   片段冻结与 _TENCENT_FREEZE 同款（行号漂移免疫）；
#   **次数钉**专门对付「同文件内多行同文本」的情形（equity_daily.py 两条各 2 处）——
#   只按 (rel, 片段) 判存在性会漏掉同文本行的增删，次数不一致即失败。
_TENCENT_EXEMPT_OUTER = {
    ("analysis/index_regime_intraday.py", "df = TencentProvider().index_minute(code)"): (
        "registered",
        "盘中指数背离/共振分析链独立取数：index_minute 拉当日分时序列做规整，"
        "非 QuoteBus 实时快照共享档语义（分析链自建通道，P1-4 裁决登记豁免）",
        1),
    ("stock_hunter/modules/market_data.py", "snap = TencentProvider().snapshot_auction(chunk)"): (
        "registered",
        "猎手管线跑在独立子进程：子进程内 QuoteBus 单飞无共享增益，"
        "批量快照由 provider 内分块直取（P1-4 裁决登记豁免）",
        1),
    ("sector_rotation/data_fetch.py", "snap = TencentProvider().snapshot_auction(keep[i : i + SNAPSHOT_BATCH])"): (
        "registered",
        "gui 端点批量取数：板块轮动全市场快照按 SNAPSHOT_BATCH 分块拉取，"
        "属批量快照非 QuoteBus 共享档轮询消费（P1-4 裁决登记豁免）",
        1),
    ("scripts/equity_daily.py", "df = TencentProvider().index_daily(idx)"): (
        "registered",
        "离线批处理（收盘后子进程）：指数日线强制刷新直取 + 网络失败回退旧缓存重取，"
        "同函数内 2 处同文本行；历史日线非实时行情（P1-4 裁决登记豁免）",
        2),
    ("scripts/equity_daily.py", "df = TencentProvider().daily(code)"): (
        "registered",
        "离线批处理（收盘后子进程）：个股日线强制刷新 / _tencent_close 日线回退，"
        "同文件 2 处同文本行；历史日线非实时行情（P1-4 裁决登记豁免）",
        2),
    ("scripts/data_source_parity.py", "gm, tx = GmProvider(), TencentProvider()"): (
        "registered",
        "人工工具：GM/腾讯数据源一致性对照脚本，两 provider 并排实例化即其本职，"
        "非行情消费链（P1-4 裁决登记豁免）",
        1),
}


def _tencent_freeze_all():
    """原冻结表 + 扩范围豁免表的合并视图（值统一为 (类别, 理由)）。"""
    merged = dict(_TENCENT_FREEZE)
    for key, (kind, reason, _cnt) in _TENCENT_EXEMPT_OUTER.items():
        merged[key] = (kind, reason)
    return merged


def _scan_tencent_provider():
    """扫 `TencentProvider()` 直实例化位置 → {(rel, 行文本): 行号}。

    范围（2026-11-23 P1-4 扩）：gui.py + core/ + analysis/ + stock_hunter/
    + sector_rotation/ + scripts/ 全部 .py（原仅 gui.py + core/）。"""
    files = [_ROOT / "gui.py"]
    for _sub in ("core", "analysis", "stock_hunter", "sector_rotation", "scripts"):
        files += sorted((_ROOT / _sub).rglob("*.py"))
    found = {}
    for fp in files:
        try:
            text = fp.read_text(encoding="utf-8")
        except Exception:
            continue
        rel = fp.relative_to(_ROOT).as_posix()
        for i, line in enumerate(text.splitlines(), 1):
            if "TencentProvider()" in line:
                found[(rel, line.strip())] = i
    return found


def _enclosing_def_body(lines, lineno):
    """lineno（1-based）所在的**最内层** def 的源码块（容忍换行/空白）；找不到返回空串。

    向上逐个 def 候选检查「目标行是否真的落在其函数体内」——否则嵌套 def
    （如 gui.py:6338 `_to_symbol`）会把其实位于外层函数的误判进内层。
    """
    i = lineno - 1
    while i >= 0:
        m = re.match(r"^(\s*)def\s+\w+", lines[i])
        if not m:
            i -= 1
            continue
        start, indent = i, len(m.group(1))
        end = len(lines)
        for j in range(start + 1, len(lines)):
            s = lines[j]
            if not s.strip() or s.lstrip().startswith("#"):
                continue
            if (len(s) - len(s.lstrip())) <= indent:
                end = j
                break
        if lineno <= end:                      # 目标行确实落在该 def 块内
            return "\n".join(lines[start:end])
        i = start - 1                          # 否则继续向上找外层 def
    return ""


class TestQuoteBusConsumptionGuard(unittest.TestCase):
    """铁律③：行情消费必走 QuoteBus（直实例化冻结白名单 + fail-open 必须挂 bus）。"""

    def test_freeze_set_matches(self):
        found = _scan_tencent_provider()
        registered_all = _tencent_freeze_all()
        new_ones = sorted(set(found) - set(registered_all))
        gone = sorted(set(registered_all) - set(found))
        msg = []
        if new_ones:
            msg.append(
                "新增未登记的 TencentProvider() 直实例化 ⇒ 走 QuoteBus "
                "（core.market_data.quote_bus.get_quotes）或登记豁免"
                "（gui.py/core/ 入 _TENCENT_FREEZE；analysis/、stock_hunter/、"
                "sector_rotation/、scripts/ 入 _TENCENT_EXEMPT_OUTER）：\n  "
                + "\n  ".join(f"{rel}:{found[(rel, snip)]}  {snip}" for rel, snip in new_ones)
            )
        if gone:
            msg.append(
                "白名单登记的代码行已不存在（行内容被修改/删除）⇒ 核对后更新片段：\n  "
                + "\n  ".join(f"{rel}  {snip}" for rel, snip in gone)
            )
        self.assertFalse(new_ones or gone, "铁律③违反：\n" + "\n".join(msg))

    def test_outer_exemptions_still_present(self):
        """反向断言（防豁免烂尾）：扩范围豁免表 _TENCENT_EXEMPT_OUTER 中每条登记的
        位置必须**仍存在** TencentProvider()，且同文本行**出现次数**与登记一致——
        equity_daily.py 两条豁免各 2 处同文本行，靠次数钉住增删。"""
        found = _scan_tencent_provider()
        for (rel, snip), (kind, reason, cnt) in sorted(_TENCENT_EXEMPT_OUTER.items()):
            with self.subTest(at=f"{rel}  {snip[:40]}"):
                fp = _ROOT / rel
                self.assertTrue(fp.exists(), f"{rel} 不存在（豁免条目失效：{reason}）")
                self.assertIn(
                    (rel, snip), found,
                    f"{rel} 中找不到豁免登记的 TencentProvider() 行（{reason}）：{snip}"
                    "——代码已改请同步更新/删除豁免登记")
                hits = sum(1 for ln in fp.read_text(encoding="utf-8").splitlines()
                           if ln.strip() == snip)
                self.assertEqual(
                    hits, cnt,
                    f"{rel} 中豁免行出现 {hits} 次 ≠ 登记 {cnt} 次（{reason}）——"
                    "同文本行被增删：新增须改走 QuoteBus 或更新登记，删除须同步减计数")

    def test_failopen_has_bus_reference(self):
        """fail-open 回退位置：同一函数体内必须同时出现 `quote_bus` 引用
        （直连只许作为 bus 的兜底存在，不许独立于 bus）。"""
        found = _scan_tencent_provider()
        for (rel, snip), (kind, reason) in sorted(_TENCENT_FREEZE.items()):
            if kind != "failopen":
                continue
            with self.subTest(at=f"{rel}  {snip[:50]}"):
                fp = _ROOT / rel
                if not fp.exists():
                    self.fail(f"{rel} 不存在（白名单条目失效：{reason}）")
                ln = found.get((rel, snip))
                self.assertIsNotNone(
                    ln, f"{rel} 中找不到登记行（{reason}）：{snip}")
                body = _enclosing_def_body(fp.read_text(encoding="utf-8").splitlines(), ln)
                self.assertIn(
                    "quote_bus", body,
                    f"{rel}:{ln} 登记为 fail-open 回退（{reason}），但其所在函数体内"
                    "找不到 quote_bus 引用——直连回退必须挂在 bus 调用之后；"
                    "若语义已变，请修正代码或改为 registered 并写明豁免理由。",
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
