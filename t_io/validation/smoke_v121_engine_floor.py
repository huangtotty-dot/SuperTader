# -*- coding: utf-8 -*-
"""smoke_v121_engine_floor.py — V1.2.1 引擎地板闸冒烟：588170_20260511 双跑

case OFF（默认，生产新语义）：卖信号应 ≥ 参照（原地板压制 tick 放行；下游虚拟态变化允许分歧）
case ON（T_SELL_FLOOR_ENABLED=1，复现旧世界）：与 C1p 参照逐字节一致（trace 前缀窗口去重跑追加）
幂等：产物已存在则跳过 harness 直接比对。
"""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

BASE = Path(r"E:\06_T")
C1P = BASE / "t_io/validation/w32_c1p"
REF = C1P / "parts/C1p_588170_20260511"
OUT = BASE / "t_io/validation/v121_smoke"
HOLDINGS_SNAP = C1P / "holdings_snapshot_3d80810.json"


def md5(fp):
    return hashlib.md5(Path(fp).read_bytes()).hexdigest()


def md5_lines(fp):
    return [hashlib.md5(l.encode("utf-8")).hexdigest()
            for l in Path(fp).read_text(encoding="utf-8").splitlines()]


def load(fp):
    return [json.loads(l) for l in Path(fp).read_text(encoding="utf-8").splitlines() if l.strip()]


def run_case(od, extra_env):
    if od.exists() and list(od.glob("signals_v102.jsonl")):
        print(f"  复用产物 {od.name}")
        return 0
    if od.exists():
        import shutil
        shutil.rmtree(od)  # 清理夭折残产（防 trace 双跑追加）
    od.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({"T_HOLDINGS_FILE": str(HOLDINGS_SNAP),
                "T_SNAPSHOT_DIR": str(BASE / "t_io/minute_snapshots_ts")})
    env.update(extra_env)
    r = subprocess.run([sys.executable, str(BASE / "harness_backtest.py"),
                        "--codes", "588170", "--start", "2026-05-11", "--end", "2026-05-15",
                        "--ab", "v102", "--out", str(od)],
                       capture_output=True, text=True, timeout=1200, cwd=str(BASE), env=env)
    print(f"  harness {od.name} rc={r.returncode}")
    if r.returncode != 0:
        print((r.stdout or "")[-1500:])
        print((r.stderr or "")[-1500:])
    return r.returncode


def _norm(o):
    if isinstance(o, dict):
        return {k: _norm(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_norm(v) for v in o]
    if isinstance(o, float) and o != o:
        return None
    return o


def byte_cmp(od):
    ok = True
    for p in sorted(REF.glob("decision_trace_*.jsonl")):
        q = od / p.name
        a, b = md5_lines(p), md5_lines(q)
        if a == b:
            continue
        if len(a) > len(b) and a[: len(b)] == b:
            continue
        # 语义级兜底：_json_safe 加固（08-07 后）NaN→null 属已知非行为差异（v120 冒烟同口径）
        la = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
        lb = [json.loads(l) for l in q.read_text(encoding="utf-8").splitlines() if l.strip()]
        if len(la) == len(lb) and all(_norm(x) == _norm(y) for x, y in zip(la, lb)):
            print(f"  ~ {p.name} 仅 NaN→null 序列化差（已知加固，语义一致）")
            continue
        ok = False
        print(f"  ✗ trace {p.name} 分歧（ref {len(a)} / new {len(b)}）")
    for p in sorted(REF.glob("*.jsonl")) + sorted(REF.glob("*.json")):
        if p.name.startswith("decision_trace"):
            continue
        q = od / p.name
        same = q.exists() and md5(p) == md5(q)
        if not same:
            ok = False
            print(f"  ✗ {p.name} 不一致")
    return ok


def main():
    ok = True
    od_off = OUT / "engine_floor_off_588170"
    od_on = OUT / "engine_floor_on_588170"
    ok &= run_case(od_off, {}) == 0
    ok &= run_case(od_on, {"T_SELL_FLOOR_ENABLED": "1"}) == 0
    if not ok:
        print("V121_ENGINE_FLOOR_SMOKE: FAIL (harness)")
        return 1

    ref_sell = [x for x in load(REF / "signals_v102.jsonl") if x["action"] in ("SELL_HIGH", "PANIC_SELL")]
    off_sell = [x for x in load(od_off / "signals_v102.jsonl") if x["action"] in ("SELL_HIGH", "PANIC_SELL")]
    on_sell = [x for x in load(od_on / "signals_v102.jsonl") if x["action"] in ("SELL_HIGH", "PANIC_SELL")]
    print(f"[卖信号数] 参照(地板开)={len(ref_sell)}  OFF(默认放开)={len(off_sell)}  ON(恢复压制)={len(on_sell)}")

    cond1 = len(off_sell) > len(ref_sell)
    print(("✓" if cond1 else "✗"), f"OFF 卖信号增多（{len(ref_sell)}→{len(off_sell)}，原地板压制 tick 放行）")
    for x in off_sell:
        key = json.dumps(x, ensure_ascii=False, sort_keys=True)
        if key not in {json.dumps(y, ensure_ascii=False, sort_keys=True) for y in ref_sell}:
            print(f"   + {x['ts']} {x['action']} price={x['price']} qty={x.get('qty')}")

    print("[ON vs 参照 逐字节]")
    cond2 = byte_cmp(od_on)
    print("✓ ON 与参照逐字节一致（开关恢复旧世界）" if cond2 else "✗ ON 存在分歧")

    ok = cond1 and cond2
    print("V121_ENGINE_FLOOR_SMOKE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
