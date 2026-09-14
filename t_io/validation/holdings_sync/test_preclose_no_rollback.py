# -*- coding: utf-8 -*-
"""Q-20260914-1 回归：尾部/主审计归账不得回滚磁盘 pre_close（2026-09-14 事故）。

事故：eod_pre_close(14:59) 只改局部副本写盘、未同步内存 HOLDINGS；15:02 尾部归账
`save_held_merged(HOLDINGS)` 把内存整写回磁盘，pre_close 回滚成启动时的周五值。
修复：写入口改 `build_virtual_qty_patch`（磁盘为基 + 仅补丁 virtual_qty）。

运行：python t_io/validation/holdings_sync/test_preclose_no_rollback.py
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import src.holdings_repo as repo  # noqa: E402

CODE = "600176"


class TestPreCloseNoRollback(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="holdings_q1_")
        self._old = (repo.HOLDINGS_FILE, repo._AUDIT_DIR, repo._ROOT)
        repo._ROOT = self._tmp
        repo.HOLDINGS_FILE = os.path.join(self._tmp, "holdings.json")
        repo._AUDIT_DIR = os.path.join(self._tmp, "logs")
        # 磁盘态：eod_pre_close 已把 pre_close 更新为当日收盘 45.29，virtual_qty 已被前一轮归账置 1900
        self._write({
            CODE: {"name": "中国巨石", "qty": 1600, "base": 1600, "t_qty": 1600,
                   "cost": 42.599, "pre_close": 45.29, "virtual_qty": 1900, "pool": "manual"},
            "000988": {"name": "华工科技", "qty": 300, "pre_close": 100.01, "virtual_qty": 300,
                       "pool": "manual"},
        })

    def tearDown(self):
        repo.HOLDINGS_FILE, repo._AUDIT_DIR, repo._ROOT = self._old
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write(self, data):
        with open(repo.HOLDINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _read(self):
        return repo.load_full()

    def _audit(self):
        p = os.path.join(repo._AUDIT_DIR, f"holdings_write_audit_{_today()}.jsonl")
        if not os.path.exists(p):
            return []
        with open(p, encoding="utf-8") as f:
            return [json.loads(ln) for ln in f if ln.strip()]

    def test_stale_memory_preclose_does_not_clobber_disk(self):
        # 内存 HOLDINGS 停留在启动时刻：pre_close = 周五 44.66（陈旧），virtual_qty 已被尾部归账改为 2200
        mem = {CODE: {"name": "中国巨石", "qty": 1600, "base": 1600, "t_qty": 1600,
                      "cost": 42.599, "pre_close": 44.66, "virtual_qty": 2200, "pool": "manual"}}

        repo.save_held_merged(repo.build_virtual_qty_patch(mem, [CODE]),
                              actor="main", reason="tail_reconcile")

        disk = self._read()
        self.assertEqual(disk[CODE]["pre_close"], 45.29, "磁盘 pre_close 被内存陈旧值回滚")
        self.assertEqual(disk[CODE]["virtual_qty"], 2200, "virtual_qty 未按补丁更新")
        self.assertEqual(disk["000988"]["pre_close"], 100.01, "未涉及的票被改写")
        self.assertEqual(disk["000988"]["virtual_qty"], 300)

    def test_audit_records_virtual_qty_only(self):
        mem = {CODE: {"name": "中国巨石", "qty": 1600, "pre_close": 44.66, "virtual_qty": 2200}}
        repo.save_held_merged(repo.build_virtual_qty_patch(mem, [CODE]),
                              actor="main", reason="tail_reconcile")

        recs = [r for r in self._audit() if r["code"] == CODE]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["changed_fields"], ["virtual_qty"], "审计显示越界写了其它字段")
        self.assertEqual(recs[0]["reason"], "tail_reconcile")
        self.assertEqual(recs[0]["actor"], "main")

    def test_unknown_code_skipped(self):
        # 磁盘中不存在的 code 不得被补丁新建（防幽灵条目）
        patch = repo.build_virtual_qty_patch({"999999": {"virtual_qty": 100}}, ["999999"])
        self.assertEqual(patch, {})
        repo.save_held_merged(patch, actor="main", reason="tail_reconcile")
        self.assertNotIn("999999", self._read())


def _today():
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d")


if __name__ == "__main__":
    unittest.main(verbosity=2)
