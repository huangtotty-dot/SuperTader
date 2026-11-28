# -*- coding: utf-8 -*-
"""W48 daily_review 历史 as-of 与空仓口径（全离线，不触网）。"""
import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from datetime import datetime
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "t_io" / "validation" / "daily_review" / "daily_review.py"
HISTORICAL_DATE = "2024-01-02"


class TestDailyReviewAsOf(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        target = self.root / "t_io" / "validation" / "daily_review" / "daily_review.py"
        target.parent.mkdir(parents=True)
        shutil.copy2(SCRIPT, target)
        self.script = target
        (self.root / "t_io" / "state").mkdir(parents=True)
        (self.root / "t_io" / "logs").mkdir(parents=True)
        (self.root / "t_io" / "logs" / "closure_audit.jsonl").write_text("", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _write_json(self, relative, payload):
        fp = self.root / relative
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return fp

    def _run(self, date):
        (self.root / "t_io" / "logs" / f"t_trader_sys_{date}.log").write_text(
            "", encoding="utf-8")
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        return subprocess.run(
            [sys.executable, str(self.script), "--date", date],
            cwd=self.root, env=env, text=True, encoding="utf-8",
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)

    def _result(self, date):
        fp = self.root / "t_io" / "validation" / "daily_review" / f"daily_review_{date}.json"
        return json.loads(fp.read_text(encoding="utf-8"))

    def test_today_legal_empty_holdings_produces_empty_review(self):
        date = datetime.now().strftime("%Y-%m-%d")
        self._write_json("t_io/state/holdings_manual.json", {})
        proc = self._run(date)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        result = self._result(date)
        self.assertEqual(result["data_status"], "empty_holdings")
        self.assertEqual(result["sig_stat"], {})
        self.assertNotIn("000988", proc.stdout)
        self.assertNotIn("588170", proc.stdout)

    def test_historical_empty_snapshot_ignores_current_manual(self):
        self._write_json("t_io/state/holdings_manual.json", {
            "999999": {"name": "当前虚假持仓", "qty": 100, "base": 100}})
        self._write_json(f"t_io/state/holdings_{HISTORICAL_DATE}.json", {})
        proc = self._run(HISTORICAL_DATE)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        result = self._result(HISTORICAL_DATE)
        self.assertEqual(result["data_status"], "empty_holdings")
        self.assertEqual(result["sig_stat"], {})
        self.assertTrue(result["holdings_source"].endswith(
            f"holdings_{HISTORICAL_DATE}.json"))
        self.assertNotIn("999999", proc.stdout)

    def test_historical_missing_snapshot_fails_closed_without_fabrication(self):
        self._write_json("t_io/state/holdings_manual.json", {
            "999999": {"name": "当前虚假持仓", "qty": 100, "base": 100}})
        proc = self._run(HISTORICAL_DATE)
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("insufficient_data", proc.stdout)
        self.assertFalse((self.root / "t_io" / "state" /
                          f"holdings_{HISTORICAL_DATE}.json").exists())
        self.assertFalse((self.root / "t_io" / "validation" / "daily_review" /
                          f"daily_review_{HISTORICAL_DATE}.json").exists())

    def test_tx_daily_requests_end_date_and_filters_future_rows(self):
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        node = next(n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == "_tx_daily")
        seen = {}

        def fake_urlopen(req, timeout):
            seen["url"] = req.full_url
            seen["timeout"] = timeout
            payload = {"data": {"sh600000": {"qfqday": [
                ["2023-12-29", "10", "10.1", "10.2", "9.9"],
                ["2024-01-02", "10.1", "10.2", "10.3", "10.0"],
                ["2024-01-03", "10.2", "10.4", "10.5", "10.1"],
            ]}}}
            return SimpleNamespace(read=lambda: json.dumps(payload).encode("utf-8"))

        ns = {"_urlreq": SimpleNamespace(Request=urllib.request.Request,
                                           urlopen=fake_urlopen),
              "json": json, "DATE": HISTORICAL_DATE}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(SCRIPT), "exec"), ns)
        rows = ns["_tx_daily"]("600000")
        self.assertIn("day,,2024-01-02,80,qfq", seen["url"])
        self.assertEqual(seen["timeout"], 15)
        self.assertEqual([r[0] for r in rows], ["2023-12-29", "2024-01-02"])


if __name__ == "__main__":
    unittest.main()
