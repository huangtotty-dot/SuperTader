# -*- coding: utf-8 -*-
"""parity_check_b7ff.py — B7 择时过滤层生产实现 vs 实验缓存 · 逐点对拍硬闸（2026-09-22）。

owner 审批口径：对拍不过不上线。
  1. 用生产模块 core/b7_factor_filter.py 的「当日切片 → factor_at_1430 → compute_z → decide」
     完整路径，对 _cache_b7ff_legs.json 的 421 条 S1 信号腿逐腿重算 z 与 F3 判定；
  2. 与 _cache_b7ff_z.json（s0#3，实验 gp_miner 路径）逐点比对：
     - 缓存有 z 的腿：重算 z 与缓存差 ≤1e-4（缓存 round 到 6 位小数，容差含 rounding）；
     - 缓存无 z 的腿：重算必须为 None（na_allow）；
     - **放行/拦截集合（z<-1 vs z≥-1）必须 100% 一致**——这是硬闸本体；
  3. 结果落 results_b7ff_parity_2026-09-22.json；不一致打印明细 diff。

复刻要点（与实验口径对齐的证明）：
  - 实验 panel 只保留「模长 241 + 标签一致」的日（gp_miner.build_stock_panel:321-379）；
    本脚本用同一 panel 取 kept dates，生产函数内部的对齐守卫（211 根切片）在这些日上恒过；
  - fast_zscore 的 day_ok 按 panel 行号计历史日数 → 本脚本 hist 只由 kept 日条目构成，
    与生产上线后「bootstrap 只写有效日」的语义一致。

运行：python t_io/validation/t0_schemes/parity_check_b7ff.py
"""
import json
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
FM = os.path.join(ROOT, "t_io", "validation", "factor_mining")
for _p in (ROOT, FM, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import run_b7_overnight as b7                    # noqa: E402  E1 实验脚本（窗口/数据层口径）
from t_io.validation.factor_mining import gp_miner, minute_data  # noqa: E402
from core import b7_factor_filter as b7ff        # noqa: E402  ★ 被对拍的生产模块

CACHE_LEGS = os.path.join(HERE, "_cache_b7ff_legs.json")
CACHE_Z = os.path.join(HERE, "_cache_b7ff_z.json")
OUT_JSON = os.path.join(HERE, "results_b7ff_parity_2026-09-22.json")
FID = "s0#3"
Z_TOL = 1e-4            # 缓存 round(6) + 浮点路径差异容差
_F_CACHE = os.path.join(HERE, "_cache_b7ff_parity_f.json")   # 每票每日 F 重算缓存（加速复跑）


def compute_f_history(codes):
    """对每票：panel kept 日 × 生产 factor_at_1430（当日 ≤14:30 切片）。带缓存。"""
    if os.path.exists(_F_CACHE):
        return json.load(open(_F_CACHE, encoding="utf-8"))
    raw = minute_data.load_pool(codes, b7.WIN_START, b7.WIN_END, verbose=True)
    f_hist = {}
    for i, sym in enumerate(codes):
        df = raw[sym]
        if df.empty:
            continue
        panel = gp_miner.build_stock_panel(sym, df, "close30")
        if panel is None:
            print(f"  [{i+1}/{len(codes)}] {sym}: panel 构建失败，跳过")
            continue
        kept = set(panel.dates)
        entries = []
        n_skip = 0
        for d, day in minute_data.iter_days(df):
            if d not in kept:
                continue                       # 非 kept 日不进历史（对齐 day_ok 语义）
            bars = [{"time": str(t), "close": c_, "volume": v_, "amount": a_}
                    for t, c_, v_, a_ in zip(day["time"], day["close"],
                                             day["volume"], day["amount"])]
            f, diag = b7ff.factor_at_1430(bars)
            if f is None:
                n_skip += 1                    # kept 日守卫失败 = 对齐 bug，必须曝光
            entries.append({"date": d, "f": f})
        f_hist[sym] = entries
        print(f"  [{i+1}/{len(codes)}] {sym}: kept={len(entries)} 日 "
              f"守卫失败={n_skip}（kept 日应为 0）")
    json.dump(f_hist, open(_F_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"[F缓存] -> {_F_CACHE}")
    return f_hist


def main():
    t0 = time.perf_counter()
    legs = json.load(open(CACHE_LEGS, encoding="utf-8"))
    zmap = json.load(open(CACHE_Z, encoding="utf-8"))[FID]
    print(f"[输入] legs={len(legs)}  z缓存票={len(zmap)}")

    codes = sorted({l["code"] for l in legs})
    f_hist = compute_f_history(codes)

    value_diffs, set_diffs, detail = [], [], []
    n_cache_z = n_recomp_z = 0
    cached_pass, recomp_pass = set(), set()
    for l in legs:
        key = (l["code"], l["date"])
        zc = zmap.get(l["code"], {}).get(l["date"])          # 缓存 z（可能 None）
        hist = f_hist.get(l["code"], [])
        # 生产路径：当日 F 已在 hist 里（kept 日）；z 用严格 < date 的历史
        f_today = next((e["f"] for e in hist if e["date"] == l["date"]), None)
        in_kept = any(e["date"] == l["date"] for e in hist)
        if not in_kept:
            f_today = None                                   # 非 kept 日：生产守卫必然 None
        zr, hd = b7ff.compute_z(f_today, hist, l["date"])
        dc = "pass" if (zc is not None and zc < b7ff.Z_THRESHOLD) else (
            "block" if zc is not None else "na_allow")
        dr, _reason = b7ff.decide(zr)
        if zc is not None:
            n_cache_z += 1
            cached_pass.add(key) if dc == "pass" else None
            if zr is None:
                value_diffs.append({"leg": key, "cache_z": zc, "recomp_z": None,
                                    "kind": "cache有_重算无"})
            else:
                diff = abs(zr - zc)
                if diff > Z_TOL:
                    value_diffs.append({"leg": key, "cache_z": zc,
                                        "recomp_z": round(zr, 6),
                                        "abs_diff": diff, "kind": "z值超差"})
            if dc == "pass":
                cached_pass.add(key)
        else:
            if zr is not None:
                value_diffs.append({"leg": key, "cache_z": None,
                                    "recomp_z": round(zr, 6),
                                    "kind": "缓存无_重算有"})
        if zr is not None:
            n_recomp_z += 1
            if dr == "pass":
                recomp_pass.add(key)
        if dc != dr:
            set_diffs.append({"leg": key, "cache_z": zc,
                              "recomp_z": (round(zr, 6) if zr is not None else None),
                              "cache_decision": dc, "recomp_decision": dr})

    only_cache = sorted(cached_pass - recomp_pass)
    only_recomp = sorted(recomp_pass - cached_pass)
    n_pass_cache = len(cached_pass)
    n_pass_recomp = len(recomp_pass)

    verdict = (not value_diffs) and (not set_diffs) \
        and not only_cache and not only_recomp \
        and n_pass_cache == 82 and n_pass_recomp == 82
    result = {
        "meta": {"date": "2026-09-22", "fid": FID,
                 "expr": b7ff.FACTOR_EXPR,
                 "variant": "F3: z < -1 放行",
                 "module": "core/b7_factor_filter.py",
                 "legs_cache": CACHE_LEGS, "z_cache": CACHE_Z,
                 "z_tol": Z_TOL,
                 "gate": "82腿放行/拦截集合100%一致 且 缓存有z处重算差≤1e-4"},
        "legs_total": len(legs),
        "n_with_cache_z": n_cache_z,
        "n_with_recomp_z": n_recomp_z,
        "n_pass_cache": n_pass_cache,
        "n_pass_recomp": n_pass_recomp,
        "value_diffs": value_diffs[:200],
        "decision_diffs": set_diffs[:200],
        "pass_only_in_cache": [list(k) for k in only_cache],
        "pass_only_in_recomp": [list(k) for k in only_recomp],
        "elapsed_s": round(time.perf_counter() - t0, 1),
        "PASS": bool(verdict),
    }
    json.dump(result, open(OUT_JSON, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1, default=str)

    print(f"\n[对拍] 腿={len(legs)}  缓存有z={n_cache_z} 重算有z={n_recomp_z}")
    print(f"[对拍] 放行集合 缓存={n_pass_cache} 重算={n_pass_recomp}（实验 F3 n=82）")
    print(f"[对拍] z值超差={len(value_diffs)}  判定不一致={len(set_diffs)}")
    if value_diffs:
        for d in value_diffs[:10]:
            print("  值差异:", d)
    if set_diffs or only_cache or only_recomp:
        for d in set_diffs[:10]:
            print("  判定差异:", d)
        print("  仅缓存放行:", only_cache[:10])
        print("  仅重算放行:", only_recomp[:10])
    print(f"[{'PASS' if verdict else 'FAIL'}] -> {OUT_JSON} "
          f"（耗时 {result['elapsed_s']}s）")
    return 0 if verdict else 1


if __name__ == "__main__":
    raise SystemExit(main())
