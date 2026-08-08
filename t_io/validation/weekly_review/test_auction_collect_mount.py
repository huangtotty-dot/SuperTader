# -*- coding: utf-8 -*-
"""
test_auction_collect_mount.py — 竞价采集调度挂载回归（W32-B2，2026-08-08 用户拍板）
背景：09:20/09:22 竞价快照采集连续断档 4 天（调度未挂载），周一 08-10 盘前必须生效。
挂载：main.py 新增 _maybe_collect_auction_snapshot（scan_once 盘前钩子区）
  + _launch_auction_collector（子进程 fire-and-forget，仿 _launch_gui）
  + _auction_slot_on_disk（断档检查）。
本测试：从 main.py 用 AST 抽取三个函数源码，桩全局环境 exec（subprocess.Popen 打桩记录），
驱动仿真时间点验证调度逻辑——不等真实 09:20：
  ① 窗口外不触发  ② 窗口内触发且每 slot 每日仅一次  ③ 周末不触发
  ④ 晚启动只补当前窗口 slot  ⑤ 09:26 断档检查缺 slot 告警一次且仅一次
"""
import ast
import json
import logging
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from datetime import time as dtime
from pathlib import Path

BASE = Path(r"E:\06_T")
MAIN_PY = BASE / "main.py"


def extract_function_source(path: Path, func_name: str) -> str:
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            return ast.get_source_segment(text, node)
    raise AssertionError(f"function {func_name} not found in {path}")


def build_ns(tmp: Path, launches: list, warnings: list):
    """桩全局环境：真实 os/json/dtime + 记录型 log + 打桩 subprocess.Popen。"""
    class FakeProc:
        pid = 42424

    real_popen = subprocess.Popen

    def fake_popen(args, **kw):
        launches.append({"args": list(args), "cwd": kw.get("cwd")})
        return FakeProc()

    subprocess.Popen = fake_popen  # exec 函数内 import subprocess 拿到的是已打桩模块

    class RecLog:
        def info(self, msg): pass
        def warning(self, msg): warnings.append(str(msg))

    ns = {
        "os": os, "sys": sys, "json": json,
        "datetime": datetime, "dtime": dtime,
        "log": RecLog(),
        "BASE_DIR": str(tmp),
        "_AUCTION_COLLECT_STATE": {},
    }
    for fn in ("_auction_slot_on_disk", "_launch_auction_collector", "_maybe_collect_auction_snapshot"):
        exec(compile(extract_function_source(MAIN_PY, fn), str(MAIN_PY), "exec"), ns)
    return ns, real_popen


def write_auction_file(tmp: Path, date: str, slots):
    d = tmp / "t_io" / "preopen"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"auction_{date}.json").write_text(json.dumps(
        {"date": date, "snapshots": {s: {"rows": {}} for s in slots}}, ensure_ascii=False),
        encoding="utf-8")


def main() -> int:
    MON = "2026-08-10"   # 周一 weekday()=0
    SAT = "2026-08-08"   # 周六 weekday()=5

    # ── 场景①②⑤：正常日 窗口/去重/断档告警 ──
    tmp = Path(tempfile.mkdtemp(prefix="w32_auction_test_"))
    launches, warnings = [], []
    ns, real_popen = build_ns(tmp, launches, warnings)
    try:
        hook = ns["_maybe_collect_auction_snapshot"]
        state = ns["_AUCTION_COLLECT_STATE"]

        hook(datetime(2026, 8, 10, 9, 19, 59))                      # 窗口外
        assert launches == [], "09:19:59 不应触发"

        hook(datetime(2026, 8, 10, 9, 20, 0))                       # 09:20 窗口开启
        assert len(launches) == 1 and "--slot" in launches[0]["args"], "09:20:00 应触发一次"
        i = launches[0]["args"].index("--slot")
        assert launches[0]["args"][i + 1] == "09:20", "首个 slot 应为 09:20"
        assert "auction_collector.py" in launches[0]["args"][1], "应拉起 auction_collector.py"

        hook(datetime(2026, 8, 10, 9, 20, 30))                      # 同窗口重复 tick
        hook(datetime(2026, 8, 10, 9, 21, 59))                      # 窗口末 tick
        assert len(launches) == 1, "09:20 slot 每日仅一次（先占位防重复）"

        hook(datetime(2026, 8, 10, 9, 22, 0))                       # 09:22 窗口开启
        assert len(launches) == 2, "09:22:00 应触发第二次"
        i = launches[1]["args"].index("--slot")
        assert launches[1]["args"][i + 1] == "09:22", "第二个 slot 应为 09:22"

        hook(datetime(2026, 8, 10, 9, 24, 59))
        assert len(launches) == 2, "09:22 slot 每日仅一次"

        # 断档检查：落盘齐全 → 无告警
        write_auction_file(tmp, MON, ["09:20", "09:22"])
        hook(datetime(2026, 8, 10, 9, 26, 1))
        assert warnings == [], f"落盘齐全不应告警: {warnings}"
        assert state.get("_gap_warned") == MON, "断档检查应已占位"

        # 跨日复位：次日状态 dict 按日期比对，自动恢复可触发（无需额外重置）
        hook(datetime(2026, 8, 11, 9, 20, 1))
        assert len(launches) == 3, "次日 09:20 应重新触发（按日期占位）"
    finally:
        subprocess.Popen = real_popen

    # ── 场景③：周末不触发 ──
    tmp2 = Path(tempfile.mkdtemp(prefix="w32_auction_test_"))
    launches2, warnings2 = [], []
    ns2, real_popen = build_ns(tmp2, launches2, warnings2)
    try:
        ns2["_maybe_collect_auction_snapshot"](datetime(2026, 8, 8, 9, 20, 5))
        ns2["_maybe_collect_auction_snapshot"](datetime(2026, 8, 8, 9, 26, 1))
        assert launches2 == [] and warnings2 == [], "周末不应触发也不应告警"
    finally:
        subprocess.Popen = real_popen

    # ── 场景④⑤：晚启动（09:23 才首轮）→ 只补 09:22；09:26 断档告警一次且仅一次 ──
    tmp3 = Path(tempfile.mkdtemp(prefix="w32_auction_test_"))
    launches3, warnings3 = [], []
    ns3, real_popen = build_ns(tmp3, launches3, warnings3)
    try:
        hook3 = ns3["_maybe_collect_auction_snapshot"]
        hook3(datetime(2026, 8, 10, 9, 23, 0))                      # 晚启动：09:20 窗口已过
        assert len(launches3) == 1, "晚启动只触发当前窗口 slot"
        i = launches3[0]["args"].index("--slot")
        assert launches3[0]["args"][i + 1] == "09:22", "晚启动补的应是 09:22"

        # 落盘只有 09:22（09:20 缺口）→ 09:26 告警一次
        write_auction_file(tmp3, MON, ["09:22"])
        hook3(datetime(2026, 8, 10, 9, 26, 2))
        gap_warns = [w for w in warnings3 if "断档" in w]
        assert len(gap_warns) == 1 and "09:20" in gap_warns[0], f"应告警缺 09:20 一次: {warnings3}"
        hook3(datetime(2026, 8, 10, 9, 30, 0))                      # 后续 tick 不重复告警
        gap_warns = [w for w in warnings3 if "断档" in w]
        assert len(gap_warns) == 1, "断档告警每日仅一次"

        # 完全断档（主程序 09:26 后才启动）→ 两个 slot 都缺，告警列出
        tmp4 = Path(tempfile.mkdtemp(prefix="w32_auction_test_"))
        launches4, warnings4 = [], []
        ns4, real_popen4 = build_ns(tmp4, launches4, warnings4)
        try:
            ns4["_maybe_collect_auction_snapshot"](datetime(2026, 8, 10, 9, 26, 30))
            gap4 = [w for w in warnings4 if "断档" in w]
            assert len(gap4) == 1 and "09:20" in gap4[0] and "09:22" in gap4[0], \
                f"完全断档应告警两个 slot: {warnings4}"
        finally:
            subprocess.Popen = real_popen4
    finally:
        subprocess.Popen = real_popen

    print("PASS: 竞价采集调度挂载回归全绿（窗口触发/每日一次/周末跳过/晚启动补当前窗/断档告警一次）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
