# -*- coding: utf-8 -*-
"""
smoke_w32.py — W32 §4 冒烟：开关默认关 + 历史 holdings 快照 → 与 e1_final/T36b 同块逐字节一致
目的：验证 ①T_BUYBACK_BYPASS_GATES 默认关不改变行为 ②V1.1.2(RSI NaN 兜底) 对全管线的漂移量
      ③holdings@3d80810 确为 T36b 运行时持仓
用法: python smoke_w32.py   （4 块并行，~200s）
"""
import hashlib
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = Path(r"E:\06_T")
ROOT = BASE / "t_io/validation/w32_c1"
SMOKE = ROOT / "smoke"
T36B_PARTS = BASE / "t_io/validation/e1_final/parts"
HOLDINGS_SNAP = ROOT / "holdings_snapshot_3d80810.json"
PY = sys.executable
BLOCKS = [("000988", "2026-03-16", "2026-03-20"),
          ("603667", "2026-04-06", "2026-04-10"),
          ("588170", "2026-05-18", "2026-05-22"),
          ("600176", "2026-07-20", "2026-07-24")]   # 600176=钉平窗敏感股，V1.1.2 探针


def md5(fp):
    return hashlib.md5(open(fp, "rb").read()).hexdigest()


def run_block(code, s, e):
    od = SMOKE / f"ctl_{code}_{s.replace('-', '')}"
    od.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["T_SNAPSHOT_DIR"] = str(BASE / "t_io/minute_snapshots_ts")
    env["T_HOLDINGS_FILE"] = str(HOLDINGS_SNAP)
    # 不注入 T_BUYBACK_BYPASS_GATES（默认关=生产行为）
    t0 = time.time()
    r = subprocess.run([PY, str(BASE / "harness_backtest.py"), "--codes", code,
                        "--start", s, "--end", e, "--ab", "v102", "--out", str(od)],
                       capture_output=True, text=True, timeout=900, cwd=str(BASE), env=env)
    return od, time.time() - t0, r.returncode


def main():
    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(lambda b: run_block(*b), BLOCKS))
    all_ok = True
    for (code, s, e), (od, el, rc) in zip(BLOCKS, results):
        ref = T36B_PARTS / f"T36b_{code}_{s.replace('-', '')}"
        print(f"\n[{code} {s}~{e}] {el:.0f}s rc={rc}")
        ref_files = sorted(p.name for p in ref.glob("*.jsonl"))
        new_files = sorted(p.name for p in od.glob("*.jsonl"))
        if ref_files != new_files:
            all_ok = False
            print(f"  ✗ 文件集不一致: ref={ref_files} new={new_files}")
            continue
        for name in ref_files:
            a, b = md5(ref / name), md5(od / name)
            if a == b:
                print(f"  ✓ {name} 逐字节一致")
            else:
                all_ok = False
                ra = open(ref / name, encoding="utf-8").read().splitlines()
                rb = open(od / name, encoding="utf-8").read().splitlines()
                ndiff = sum(1 for x, y in zip(ra, rb) if x != y) + abs(len(ra) - len(rb))
                print(f"  ✗ {name} 不一致: ref {len(ra)} 行 vs new {len(rb)} 行, 差异行 {ndiff}")
                # 写差异明细供诊断
                diag = SMOKE / f"diff_{code}_{name}.json"
                diffs = [{"ref": x, "new": y} for x, y in zip(ra, rb) if x != y][:20]
                json.dump({"ref_lines": len(ra), "new_lines": len(rb), "sample": diffs},
                          open(diag, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
                print(f"    差异样本: {diag}")
    print("\nSMOKE:", "PASS 全部逐字节一致" if all_ok else "FAIL 存在不一致（查差异样本）")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
