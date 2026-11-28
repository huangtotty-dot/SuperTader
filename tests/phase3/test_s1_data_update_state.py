# -*- coding: utf-8 -*-
"""S1 日更完成状态与 IC 依赖闸（全离线，不写生产 panel/status）。"""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import s1_data_update as S1  # noqa: E402


class TestProgressCompletion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.incr = Path(self.tmp.name)
        self.patch = mock.patch.object(S1, "INCR", self.incr)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def _write(self, *records):
        S1.INCR.mkdir(parents=True, exist_ok=True)
        with S1._progress_path("shard_0000.parquet").open("w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")

    def test_fail_and_unverified_nodata_never_complete(self):
        self._write({"symbol": "SHSE.600000", "status": "fail"},
                    {"symbol": "SZSE.000001", "status": "nodata"},
                    {"symbol": "SHSE.600001", "status": "ok", "n_new": 0},
                    {"symbol": "SHSE.600002", "status": "no_data_expected"})
        self.assertEqual(S1._load_done("shard_0000.parquet"),
                         {"SHSE.600001", "SHSE.600002"})

    def test_latest_failure_revokes_earlier_success(self):
        self._write({"symbol": "SHSE.600000", "status": "ok"},
                    {"symbol": "SHSE.600000", "status": "fail", "err": "offline"})
        self.assertEqual(S1._load_done("shard_0000.parquet"), set())

    def test_later_success_can_repair_earlier_failure(self):
        self._write({"symbol": "SHSE.600000", "status": "fail"},
                    {"symbol": "SHSE.600000", "status": "ok", "n_new": 3})
        self.assertEqual(S1._load_done("shard_0000.parquet"), {"SHSE.600000"})


class TestAllStageGate(unittest.TestCase):
    def test_incomplete_fetch_blocks_all_downstream_stages(self):
        args = SimpleNamespace(fetch_rounds=2)
        completion = {"complete": False, "remaining": ["SHSE.600000"]}
        with mock.patch.object(S1, "cmd_probe"), \
                mock.patch.object(S1, "cmd_backup"), \
                mock.patch.object(S1, "cmd_universe"), \
                mock.patch.object(S1, "_block_ic_status"), \
                mock.patch.object(S1, "cmd_fetch", return_value=completion) as fetch, \
                mock.patch.object(S1, "_load_report", return_value={"fetch": {"completion": completion}}), \
                mock.patch.object(S1, "cmd_factors") as factors, \
                mock.patch.object(S1, "cmd_derived") as derived, \
                mock.patch.object(S1, "cmd_verify") as verify, \
                mock.patch.object(S1, "cmd_ic_refresh") as ic:
            with self.assertRaises(SystemExit):
                S1.cmd_all(args)
        self.assertEqual(fetch.call_count, 2)
        factors.assert_not_called()
        derived.assert_not_called()
        verify.assert_not_called()
        ic.assert_not_called()

    def test_success_runs_ic_only_after_verify(self):
        args = SimpleNamespace(fetch_rounds=1)
        calls = []
        with mock.patch.object(S1, "cmd_probe", side_effect=lambda a: calls.append("probe")), \
                mock.patch.object(S1, "cmd_backup", side_effect=lambda a: calls.append("backup")), \
                mock.patch.object(S1, "cmd_universe", side_effect=lambda a: calls.append("universe")), \
                mock.patch.object(S1, "_block_ic_status", side_effect=lambda *a: calls.append("block")), \
                mock.patch.object(S1, "cmd_fetch", return_value={"complete": True, "remaining": []}), \
                mock.patch.object(S1, "cmd_factors", side_effect=lambda a: calls.append("factors")), \
                mock.patch.object(S1, "cmd_derived", side_effect=lambda a: calls.append("derived")), \
                mock.patch.object(S1, "cmd_verify", side_effect=lambda a: calls.append("verify")), \
                mock.patch.object(S1, "cmd_ic_refresh", side_effect=lambda a: calls.append("ic")):
            S1.cmd_all(args)
        self.assertEqual(calls, ["probe", "backup", "universe", "block",
                                 "factors", "derived", "verify", "ic"])

    def test_ic_failure_persists_blocked_status(self):
        with tempfile.TemporaryDirectory() as td:
            fm = Path(td)
            args = SimpleNamespace(ic_timeout=1)
            result = SimpleNamespace(returncode=1, stderr="SDK offline", stdout="")
            with mock.patch.object(S1, "FM", fm), \
                    mock.patch.object(S1, "REPORT", fm / "report.json"), \
                    mock.patch.object(S1, "_load_report", return_value={"factors": {"max_date": "2026-11-27"}}), \
                    mock.patch.object(S1, "_save_report"), \
                    mock.patch.object(S1.subprocess, "run", return_value=result):
                with self.assertRaises(SystemExit):
                    S1.cmd_ic_refresh(args)
            status = json.loads((fm / "results" / "ic_monitor" / "monitor_status.json")
                                .read_text(encoding="utf-8"))
            self.assertEqual(status["data_status"], "ic_refresh_failed")
            self.assertFalse(status["overall"]["production_weight_change_allowed"])
            self.assertIn("SDK offline", status["overall"]["action"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
