# -*- coding: utf-8 -*-
"""smoke_v121_unfreeze.py — V1.2.1 冒烟：600481_20260622 单块 vs C1p 参照

预期差异形态（唯一允许）：
- decision_trace / trend_timeline：逐字节一致（引擎决策时机/分数不受 sizing 影响）
- signals/capped_buys/summary：仅多出"原 qty=0 被拦、现 qty>=100 推送+记录"的买信号；
  既有信号行逐字节一致（时机/分数/价格不变）
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

BASE = Path(r"E:\06_T")
C1P = BASE / "t_io/validation/w32_c1p"
OUT = BASE / "t_io/validation/v121_smoke/unfreeze_600481"
REF = C1P / "parts/C1p_600481_20260622"
HOLDINGS_SNAP = C1P / "holdings_snapshot_3d80810.json"


def md5(fp):
    import hashlib
    return hashlib.md5(Path(fp).read_bytes()).hexdigest()


def load(fp):
    return [json.loads(l) for l in Path(fp).read_text(encoding="utf-8").splitlines() if l.strip()]


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({"T_HOLDINGS_FILE": str(HOLDINGS_SNAP),
                "T_SNAPSHOT_DIR": str(BASE / "t_io/minute_snapshots_ts")})
    r = subprocess.run([sys.executable, str(BASE / "harness_backtest.py"),
                        "--codes", "600481", "--start", "2026-06-22", "--end", "2026-06-26",
                        "--ab", "v102", "--out", str(OUT)],
                       capture_output=True, text=True, timeout=1500, cwd=str(BASE), env=env)
    print("harness rc =", r.returncode)
    if r.returncode != 0:
        print(r.stdout[-2000:])
        print(r.stderr[-2000:])
        return 1

    ok = True
    # 1) trace/trend 逐字节一致
    for pat in ("decision_trace_*.jsonl", "trend_timeline_v102.jsonl"):
        for p in sorted(REF.glob(pat)):
            q = OUT / p.name
            same = q.exists() and md5(p) == md5(q)
            ok &= same
            print(("✓" if same else "✗"), p.name, "逐字节一致" if same else "不一致！")
    # 2) signals：参照每行必须原样存在于新块（既有信号不变），新块可多出记录
    ref_sig = load(REF / "signals_v102.jsonl")
    new_sig = load(OUT / "signals_v102.jsonl")
    ref_lines = [json.dumps(x, ensure_ascii=False, sort_keys=True) for x in ref_sig]
    new_lines = [json.dumps(x, ensure_ascii=False, sort_keys=True) for x in new_sig]
    missing = [l for l in ref_lines if l not in new_lines]
    added = [l for l in new_lines if l not in ref_lines]
    if missing:
        ok = False
        print(f"✗ signals: {len(missing)} 条参照信号在新块中缺失/变形（不允许）")
        for l in missing[:3]:
            print("   MISS", l[:160])
    print(f"signals: 参照 {len(ref_sig)} 条全部保留；新增 {len(added)} 条（应全为原 qty=0 冻结信号放出）")
    for l in added[:8]:
        d = json.loads(l)
        print(f"   + {d.get('ts')} {d.get('action')} score={d.get('buy_score', d.get('sell_score'))} qty={d.get('qty')}")
    print("V121_SMOKE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
