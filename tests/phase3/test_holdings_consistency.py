# -*- coding: utf-8 -*-
"""持仓双文件一致性守卫回归测试。"""
import contextlib
import importlib.util
import io
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "check_holdings_consistency", ROOT / "scripts" / "check_holdings_consistency.py"
)
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


def _entry(pool, *, base=0, qty=0):
    return {
        "name": "测试标的",
        "gm_symbol": "SZSE.000001",
        "type": "stock",
        "pool": pool,
        "qty": qty,
        "cost": 10.0,
        "base": base,
        "pre_close": 10.0,
    }


def _run_guard(manual, auto):
    union = {**manual, **auto}
    stdout = io.StringIO()
    with (
        patch.object(guard, "load_manual", return_value=manual),
        patch.object(guard, "load_auto", return_value=auto),
        patch.object(guard, "load_union", return_value=union),
        patch.object(guard, "load_auto_pool", return_value=set(auto)),
        patch.object(guard._ap, "AUTO_POOL", set(auto)),
        contextlib.redirect_stdout(stdout),
    ):
        result = guard.main()
    return result, stdout.getvalue()


class HoldingsConsistencyTest(TestCase):
    def test_manual_only_positive_base_is_allowed(self):
        """588170 场景：手动-only 监控仓可保留正 base，无需加入自动池。"""
        manual = {"588170": _entry("manual", base=500, qty=20500)}
        auto = {"300054": _entry("auto", base=500)}

        result, output = _run_guard(manual, auto)

        self.assertEqual(result, 0, output)
        self.assertIn("RESULT: PASS", output)

    def test_both_base_mismatch_is_still_rejected(self):
        """放宽手动-only 规则后，both 两侧的 base 仍必须一致。"""
        manual = {"000001": _entry("both", base=500)}
        auto = {"000001": _entry("both", base=0)}

        result, output = _run_guard(manual, auto)

        self.assertEqual(result, 1)
        self.assertIn("共享字段 base 两份不一致", output)
