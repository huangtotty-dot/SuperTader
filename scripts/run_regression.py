#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""scripts/run_regression.py —— 全量回归统一入口（测试基建 E2，2026-11-23 固化）。

用法
----
  python scripts/run_regression.py                  全量回归（几分钟属正常，215+ 模块各自导入 gui.py 链）。
                                                    失败集 ⊆ 基线 ⇒ 退出码 0；出现基线外新失败 ⇒ 退出码 1。
  python scripts/run_regression.py --fast           只跑静态守卫层 5 文件（秒级），合并前快速闸。
  python scripts/run_regression.py --update-baseline 用当前失败集重写文件顶部基线（人工确认后才提交）。

工程事实（2026-11-23 实测）
---------------------------
- tests/ 下无 __init__.py，Python 3.12 的 unittest discover 无法从仓根递归
  （`discover -s tests` 实测 0 tests），故按平铺目录逐目录 discover。
- 托管 Python 无 pytest：pytest 风格文件 import 即炸，在 discover 下表现为 loader error，
  全部收入基线；要在有 pytest 的环境里跑它们请另行安装 pytest。
- 测试 id 统一打来源目录前缀：phase3.* / stage0_2.* / smoke.*，避免跨目录模块重名歧义；
  subTest 归一到父测试 id，loader 失败归一到模块名（保证基线匹配稳定）。
- 2026-11-23 实测警示：全量跑期间若有并发进程改写 gui.py 等生产文件，会造成瞬时失败
  （inspect.getsource 行号错位 / 缓存状态漂移），此类不稳定项**不收入基线**，需重跑确认。
- 必须逐套「边 discover 边跑」（_iter_suites 惰性）：smoke 脚本 import 时做模块级全局补丁
  （core.market_data.get_provider 换假桩不还原），预先全量 discover 会污染 phase3
  （2026-11-23 实测：test_chart_cache_freshness.test_无缓存则下载 因此由绿变红）。

分层口径（启发式，按测试模块名）
-------------------------------
- 守卫层：模块名含 "guard"（静态源码断言，秒级）或属于 --fast 五件套。
- 集成层：其余全部（多数需导入 gui.py 业务链 / 数据文件 / 外部工具）。
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import unittest
from pathlib import Path

# 收录规则：每条必须是全量实跑观察到的真实失败 id + 可解释原因，禁止编造。
# 首次固化：2026-11-23，托管 Python 3.12.16（无 pytest）全量实跑。
# 更新方式：python scripts/run_regression.py --update-baseline（会保留既有原因，新条目需人工补原因）。
BASELINE: dict[str, str] = {
    "phase3.test_688_volume_unit": 'A: pytest 风格文件，托管 Python 无 pytest，import 即炸 → loader error',
    "phase3.test_abnormal_pool": 'A: pytest 风格文件，托管 Python 无 pytest，import 即炸 → loader error',
    "phase3.test_chart_gap_window": 'A: pytest 风格文件，托管 Python 无 pytest，import 即炸 → loader error',
    "phase3.test_chart_open_no_minute_storm": 'A: pytest 风格文件，托管 Python 无 pytest，import 即炸 → loader error',
    "phase3.test_company_profile_cache": 'A: pytest 风格文件，托管 Python 无 pytest，import 即炸 → loader error',
    "phase3.test_daily_partial_bar_guard": 'A: pytest 风格文件，托管 Python 无 pytest，import 即炸 → loader error',
    "phase3.test_daily_today_patch": 'A: pytest 风格文件，托管 Python 无 pytest，import 即炸 → loader error',
    "phase3.test_gm_param_drift": 'A: pytest 风格文件，托管 Python 无 pytest，import 即炸 → loader error',
    "phase3.test_gm_pool_race": 'A: pytest 风格文件，托管 Python 无 pytest，import 即炸 → loader error',
    "phase3.test_hunter_bulk_fastpath": 'A: pytest 风格文件，托管 Python 无 pytest，import 即炸 → loader error',
    "phase3.test_hunter_category_guides.TestCategoryGuideFrontend.test_renderer_escapes_all_backend_copy": 'B: subprocess 调 node 渲染 UI harness，node 不在子进程 PATH → WinError 2',
    "phase3.test_ma5_pullback": 'A: pytest 风格文件，托管 Python 无 pytest，import 即炸 → loader error',
    "phase3.test_market_turnover_fallback": 'A: pytest 风格文件，托管 Python 无 pytest，import 即炸 → loader error',
    "phase3.test_multi_watch": 'A: pytest 风格文件，托管 Python 无 pytest，import 即炸 → loader error',
    "phase3.test_pb_score_trend_and_badge": 'A: pytest 风格文件，托管 Python 无 pytest，import 即炸 → loader error',
    "phase3.test_pool_live_alert": 'A: pytest 风格文件，托管 Python 无 pytest，import 即炸 → loader error',
    "phase3.test_sector_contrib": 'A: pytest 风格文件，托管 Python 无 pytest，import 即炸 → loader error',
    "phase3.test_sector_ma_weekly_macd.TestFlags.test_最后一天与状态同口径": 'D: 603028 最后一天红柱判定钉在历史快照，周线随交易日滚动 → False != True',
    "phase3.test_tags_ttl": 'A: pytest 风格文件，托管 Python 无 pytest，import 即炸 → loader error',
    "phase3.test_tdx_local_readers": 'A: pytest 风格文件，托管 Python 无 pytest，import 即炸 → loader error',
    "phase3.test_thread_watch": 'A: pytest 风格文件，托管 Python 无 pytest，import 即炸 → loader error',
    "phase3.test_today_forming_bar_tx_first": 'A: pytest 风格文件，托管 Python 无 pytest，import 即炸 → loader error',
    "phase3.test_trade_history_ui_render.TestTradeHistoryUiRender.test_五张图全部渲染": 'B: subprocess 调 node 渲染 UI harness，node 不在子进程 PATH → WinError 2',
    "phase3.test_volume_amount_panel": 'A: pytest 风格文件，托管 Python 无 pytest，import 即炸 → loader error',
    "phase3.test_w45_regime_board_gate.TestRealCacheFormingReplay.test_1105_1106_regime_score_version_effective_date_and_temp_artifact": 'C: 钉死 input_version 历史 sha256，真实 regime 缓存已演进 → 哈希漂移（4 个 subTest 同 id）',
    "phase3.test_weekly_macd_scan.TestBeaconWm.test_按代码取状态且带记忆": "D: st['red'] 钉在历史快照，周线数据滚动后翻红状态漂移 → False is not true",
}
# ============================= 既存失败基线（END） =============================

REPO_ROOT = Path(__file__).resolve().parents[1]

# --fast 合并闸：静态守卫层五件套（AGENTS.md 新端点 checklist 第 5 条）
FAST_MODULES = [
    "test_architecture_guards",   # TTL/相位、裸 urlopen、QuoteBus 直实例化等静态源码断言
    "test_http_pool_shared",      # 出网必须走 pooled_get 连接池
    "test_regime_parity",         # regime 判定前后端口径一致
    "test_feishu_rate_limit",     # 飞书限频纪律
    "test_feishu_retry",          # 飞书重试/死信纪律
]

# 全量 discover 计划：(id 前缀, start_dir, pattern)
# tests/ 根只收 smoke_*（脚本式冒烟：import 时执行模块级断言，失败即 loader error）
DISCOVER_PLAN = [
    ("phase3",   REPO_ROOT / "tests" / "phase3",   "test*.py"),
    ("stage0_2", REPO_ROOT / "tests" / "stage0_2", "test*.py"),
    ("smoke",    REPO_ROOT / "tests",              "smoke_*.py"),
]

# 分层启发式：模块名含 guard 或在 --fast 五件套 ⇒ 守卫层；其余 ⇒ 集成层
GUARD_EXTRA = set(FAST_MODULES)


def classify(tagged_id: str) -> str:
    """tagged_id 形如 'phase3.test_x.Class.m' → '守卫层' / '集成层'。"""
    parts = tagged_id.split(".")
    module = parts[1] if len(parts) > 1 else tagged_id
    if "guard" in module or module in GUARD_EXTRA:
        return "守卫层"
    return "集成层"


def _norm_id(test) -> str:
    """归一测试 id，保证基线匹配稳定：
    - subTest：id 带参数后缀 ' (day=..., mode=...)' ⇒ 归一到父测试 id（基线不按参数展开）；
    - loader 失败：id 为 'unittest.loader._FailedTest.<module>' ⇒ 归一到模块名本身。
    """
    tid = test.id()
    if " (" in tid:
        tid = tid.split(" (", 1)[0]
    pfx = "unittest.loader._FailedTest."
    if pfx in tid:
        tid = tid.split(pfx, 1)[1]
    return tid


class _ProgressResult(unittest.TestResult):
    """只负责进度打印（每 25 条一行）；失败收集在跑完后从 failures/errors 读，
    这样 subTest 失败（addSubTest 直接写 self.failures，不过 addFailure）也不会漏。"""

    def __init__(self, stream):
        super().__init__()
        self.stream = stream
        self.ran = 0

    def startTest(self, test):
        super().startTest(test)
        self.ran += 1
        if self.ran % 25 == 0:
            print(f"    … 已跑 {self.ran} 条", file=self.stream, flush=True)


def _iter_suites():
    """惰性产出 (tag, suite)：discover 一套跑一套，控制 import 时序污染。"""
    loader = unittest.TestLoader()
    for tag, start_dir, pattern in DISCOVER_PLAN:
        yield tag, loader.discover(str(start_dir), pattern=pattern, top_level_dir=str(start_dir))


def _build_fast_suite() -> list[tuple[str, unittest.TestSuite]]:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for mod in FAST_MODULES:
        m = __import__(f"tests.phase3.{mod}", fromlist=[mod])
        suite.addTests(loader.loadTestsFromModule(m))
    return [("phase3", suite)]


def run(suites) -> tuple[int, dict[str, list[str]], dict[str, str], float]:
    """跑全部 suite（可传生成器，惰性 discover），返回 (总条数, 分层失败映射, traceback 映射, 耗时)。"""
    total = 0
    failed: dict[str, list[str]] = {"守卫层": [], "集成层": []}
    tracebacks: dict[str, str] = {}
    t0 = time.perf_counter()
    for tag, suite in suites:
        n = suite.countTestCases()
        total += n
        print(f"  ▶ {tag}: {n} 条", flush=True)
        result = _ProgressResult(sys.stderr)
        suite.run(result)
        for kind, entries in (("ERROR", result.errors), ("FAIL", result.failures)):
            for test, tb in entries:
                tid = f"{tag}.{_norm_id(test)}"
                failed.setdefault(classify(tid), []).append(tid)
                tracebacks.setdefault(tid, tb)
                print(f"    [{kind}] {tid}", file=sys.stderr, flush=True)
    elapsed = time.perf_counter() - t0
    return total, failed, tracebacks, elapsed


def _print_summary(total: int, failed: dict[str, list[str]], elapsed: float,
                   extra_note: str = "", check_stale: bool = True) -> list[str]:
    """打印分层汇总，返回基线外新失败 id 列表。check_stale=False 时跳过「基线已过时」段
    （--fast 只跑守卫层，未跑到的基线条目不算已绿）。"""
    raw_events = len(failed.get("守卫层", [])) + len(failed.get("集成层", []))
    all_failed = sorted(set(failed.get("守卫层", []) + failed.get("集成层", [])))
    failed_set = set(all_failed)
    baseline_set = set(BASELINE)
    new_failures = sorted(failed_set - baseline_set)
    stale = sorted(baseline_set - failed_set) if check_stale else []  # 基线里但这次没失败（可能已被修好）

    guard_failed = sorted(set(failed.get("守卫层", [])))
    integ_failed = sorted(set(failed.get("集成层", [])))
    guard_new = [t for t in new_failures if classify(t) == "守卫层"]
    integ_new = [t for t in new_failures if classify(t) == "集成层"]

    print("\n" + "=" * 68)
    print("回归汇总")
    print("=" * 68)
    print(f"  总条数        : {total}")
    print(f"  失败/错误     : {len(all_failed)} 个唯一 id（{raw_events} 个失败事件，subTest 同 id 会合并）"
          f"——基线内 {len(all_failed) - len(new_failures)}，基线外新增 {len(new_failures)}")
    print(f"  耗时          : {elapsed:.1f}s")
    if extra_note:
        print(f"  备注          : {extra_note}")
    print("  ── 分层统计 ─────────────────────────────────────────────")
    print(f"  守卫层(秒级)     : 失败 {len(guard_failed)}（其中基线外 {len(guard_new)}）")
    print(f"  集成层(需数据文件): 失败 {len(integ_failed)}（其中基线外 {len(integ_new)}）")
    if all_failed:
        print("  ── 基线内失败（既存问题，逐条原因见脚本顶部 BASELINE） ──")
        for tid in all_failed:
            if tid not in new_failures:
                print(f"    [基线] {tid} —— {BASELINE.get(tid, '（基线内）')}")
    if new_failures:
        print("  ── ❌ 基线外新失败（必须处理，退出码 1） ──")
        for tid in new_failures:
            print(f"    [新增] {tid}")
    if stale:
        print("  ── 基线已过时（本次未再失败，可考虑 --update-baseline 清理） ──")
        for tid in stale:
            print(f"    [已绿] {tid}")
    print("=" * 68)
    return new_failures


def update_baseline(current_failed: list[str]) -> None:
    """把当前失败集写回脚本顶部 BASELINE（保留既有原因；新条目留 TODO）。"""
    script = Path(__file__).resolve()
    text = script.read_text(encoding="utf-8")
    begin = text.index("# ============================ 既存失败基线（BEGIN）")
    end = text.index("# ============================= 既存失败基线（END）")

    lines = []
    for tid in sorted(set(current_failed)):
        reason = BASELINE.get(tid, "TODO 待人工填写原因（--update-baseline 自动收录）")
        lines.append(f'    "{tid}": {reason!r},')
    header_notes = (
        "# 收录规则：每条必须是全量实跑观察到的真实失败 id + 可解释原因，禁止编造。\n"
        "# 首次固化：2026-11-23，托管 Python 3.12.16（无 pytest）全量实跑。\n"
        "# 更新方式：python scripts/run_regression.py --update-baseline（会保留既有原因，新条目需人工补原因）。\n"
    )
    new_block = (
        header_notes
        + "BASELINE: dict[str, str] = {\n"
        + ("\n".join(lines) + "\n" if lines else "")
        + "}\n"
    )
    head = text[:begin]
    tail = text[end:]
    text = head + new_block + tail
    script.write_text(text, encoding="utf-8")

    old = set(BASELINE)
    new = set(current_failed)
    print("!" * 68)
    print("⚠️  --update-baseline 已重写脚本顶部 BASELINE，请务必人工逐条确认：")
    print(f"    保留 {len(old & new)} 条 / 新增 {len(new - old)} 条（原因待补）/ 移除 {len(old - new)} 条")
    for tid in sorted(new - old):
        print(f"    [新增] {tid}")
    for tid in sorted(old - new):
        print(f"    [移除] {tid}")
    print("    确认无误并补全新条目原因后再提交 git。")
    print("!" * 68)


def main() -> int:
    ap = argparse.ArgumentParser(description="全量回归统一入口（基线比对 + 分层统计）")
    ap.add_argument("--fast", action="store_true", help="只跑静态守卫层 5 文件（秒级），合并前闸")
    ap.add_argument("--update-baseline", action="store_true",
                    help="全量跑完后用当前失败集重写基线（人工确认后才提交）")
    args = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    import os
    os.chdir(REPO_ROOT)
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    if args.fast:
        print("【--fast】静态守卫层（合并前快速闸）", flush=True)
        suites = _build_fast_suite()
    else:
        print("【全量回归】discover tests/（215+ 模块各自导入 gui.py 链，几分钟属正常）", flush=True)
        suites = _iter_suites()

    total, failed, tracebacks, elapsed = run(suites)
    new_failures = _print_summary(total, failed, elapsed,
                                  extra_note="--fast 守卫层" if args.fast else "",
                                  check_stale=not args.fast)

    if args.update_baseline:
        all_failed = sorted(failed.get("守卫层", []) + failed.get("集成层", []))
        update_baseline(all_failed)

    if new_failures:
        print("\n基线外新失败的 traceback：")
        for tid in new_failures:
            print(f"\n----- {tid} -----")
            print(tracebacks.get(tid, "（无 traceback）"))
        return 1
    any_failed = any(failed.values())
    if args.fast:
        print("\n✅ 守卫层全绿" if not any_failed else "\n✅ 守卫层失败均在基线内")
    else:
        print("\n✅ 通过：失败集 ⊆ 基线" if any_failed else "\n✅ 全绿：无任何失败")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
