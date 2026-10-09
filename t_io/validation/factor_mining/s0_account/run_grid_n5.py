# -*- coding: utf-8 -*-
"""G5 shard runner: n=5 full grid (36 combos). Records failures, never silently skips."""
import json, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = HERE / "results" / "grid_spec.json"

spec = json.loads(SPEC.read_text(encoding="utf-8"))
combos = [c for c in spec["combos"] if c["n"] == 5]
assert len(combos) == 36, f"expected 36 combos, got {len(combos)}"

ok, failed = [], []
for i, c in enumerate(combos, 1):
    t0 = time.time()
    p = subprocess.run([sys.executable, "s1_sim.py", "--n", str(c["n"]),
                        "--m", str(c["m"]), "--min-hold", str(c["min_hold"]),
                        "--tp-arm", c["tp_arm"], "--out", c["out"]],
                       cwd=HERE, capture_output=True, text=True, timeout=280)
    dt = time.time() - t0
    out_path = HERE / c["out"]
    if p.returncode == 0 and out_path.exists():
        ok.append(c["run_id"])
        print(f"[{i:02d}/36] OK  {c['run_id']} ({dt:.1f}s)")
    else:
        reason = (p.stderr or p.stdout or "unknown").strip()[-500:]
        failed.append({"run_id": c["run_id"], "rc": p.returncode, "reason": reason})
        print(f"[{i:02d}/36] FAIL {c['run_id']} rc={p.returncode}")

report = {"shard": "n=5", "n_ok": len(ok), "n_failed": len(failed),
          "ok": ok, "failed": failed}
(HERE / "results" / "s1_runs" / "_g5_n5_report.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\nDONE: {len(ok)}/36 ok, {len(failed)} failed")
if failed:
    for f in failed:
        print("FAIL:", f["run_id"], "->", f["reason"][:200])
