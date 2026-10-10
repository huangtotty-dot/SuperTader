#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
弱转强研究线 · 数据铺设：全池 981 只 5min 分钟线（对齐 30min d540 面板）
=====================================================================
拉取 t_io/cache/tushare_mins/*_30min_d540.json 里全部标的的 5min 线，
覆盖 2025-03-31 ~ 2026-09-18（与 30min 面板同窗口），写 {ts_code}_5min_d540.json。

tushare stk_mins 单次上限 8000 行且**静默截断**（实测 6 个月 6223 行完整、12 个月被截到
8000 并丢最老 4 个月），故按**季度**分段（~3100 行，留 2.5× 余量）。
断点续传：已存在且末行时间到窗口末尾即跳过；失败重试 3 次，仍失败记入 failures 供重跑。
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta
from glob import glob
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import pandas as pd
import tushare as ts

TOKEN = os.environ.get("TUSHARE_TOKEN") or "9d15f39266cbbf8a1e5efa1525d7a4d4d1dbc62ec8cbce167d642def"
CACHE = BASE / "t_io" / "cache" / "tushare_mins"
FREQ = "5min"
START = datetime(2025, 3, 31)
END = datetime(2026, 9, 18)
SLEEP = 0.4          # 连续调用间隔（防限流）
ROW_LIMIT = 8000     # tushare 单次上限（静默截断，须留余量）
FAIL_FP = BASE / "scripts" / "fetch_min5_failures.json"


def quarter_segments():
    """2025-03-31 ~ 2026-09-18 按季度切段（约 3 个月/段 ≈ 3100 行）。"""
    segs = []
    cur = START
    while cur < END:
        # 季度末 = 3 个月后的首日 - 1 天
        y, m = cur.year, cur.month
        nxt = datetime(y + (m + 2) // 12, (m + 2) % 12 + 1, 1)
        seg_end = min(nxt - timedelta(days=1), END)
        segs.append((cur, seg_end))
        cur = nxt
    return segs


def ts_codes():
    """从既有 30min d540 文件抽取全部 tushare 代码（000001.SZ 形态）。"""
    codes = []
    for fp in sorted(glob(str(CACHE / "*_30min_d540.json"))):
        name = Path(fp).name
        code = name.split("_30min_d540")[0]
        if "." in code:
            codes.append(code)
    return codes


def fetch_segment(pro, ts_code, s, e):
    """拉一段 5min，失败重试 3 次（代理偶发 ReadTimeout）。返回 df 或 None。"""
    for attempt in range(3):
        try:
            df = pro.stk_mins(ts_code=ts_code, freq=FREQ,
                              start_date=f"{s.strftime('%Y-%m-%d')} 09:00:00",
                              end_date=f"{e.strftime('%Y-%m-%d')} 19:00:00")
            return df
        except Exception as ex:
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
            else:
                return ("err", str(ex)[:80])
    return None


def load_existing(ts_code):
    """读已有 5min d540 档，返回 (rows, last_time)。"""
    fp = CACHE / f"{ts_code}_5min_d540.json"
    if not fp.exists():
        return [], ""
    try:
        d = json.load(open(fp, encoding="utf-8"))
        rows = d.get("rows") or []
        last = rows[-1].get("time", "") if rows else ""
        return rows, str(last)
    except Exception:
        return [], ""


def main():
    codes = ts_codes()
    segs = quarter_segments()
    print(f"[铺数] {len(codes)} 只 × {len(segs)} 季 = {len(codes)*len(segs)} 次调用, "
          f"{START.date()}~{END.date()}, {FREQ}", flush=True)
    pro = ts.pro_api(TOKEN)

    ok = fail = skip = 0
    failures = {}
    t0 = time.time()
    for i, code in enumerate(codes, 1):
        rows, last = load_existing(code)
        # 断点：已有且末行覆盖到窗口末尾则跳过
        if last and last[:10] >= END.strftime("%Y-%m-%d"):
            skip += 1
            continue
        frames = []
        for (s, e) in segs:
            # 已有数据覆盖到该段末则跳过该段
            if last and last[:10] >= e.strftime("%Y-%m-%d"):
                continue
            r = fetch_segment(pro, code, s, e)
            if r is None:
                continue
            if isinstance(r, tuple):  # err
                failures.setdefault(code, []).append(f"{s.date()}~{e.date()}: {r[1]}")
                continue
            if r is not None and len(r):
                if len(r) >= ROW_LIMIT - 100:
                    print(f"  ⚠️ {code} {s.date()} 段 {len(r)} 行接近上限，可能有截断", flush=True)
                frames.append(r)
            time.sleep(SLEEP)

        if not frames:
            # 有已有数据则保留，否则记失败
            if not rows:
                failures.setdefault(code, []).append("全窗口无数据")
                fail += 1
            continue

        full = pd.concat(frames, ignore_index=True)
        full = full.rename(columns={"trade_time": "time", "vol": "volume"})
        full["time"] = pd.to_datetime(full["time"], errors="coerce")
        full = full.dropna(subset=["time"]).drop_duplicates(subset=["time"])
        full = full.sort_values("time").reset_index(drop=True)
        # 合并旧 rows（断点续传不丢已拉数据）
        out = {time: r for r in rows for time in [r.get("time")]}
        for _, row in full.iterrows():
            out[str(row["time"])] = {
                "time": str(row["time"]), "open": float(row["open"]),
                "high": float(row["high"]), "low": float(row["low"]),
                "close": float(row["close"]), "volume": float(row["volume"]),
                "amount": float(row["amount"]),
            }
        merged = [out[k] for k in sorted(out)]
        rec = {"date": datetime.now().strftime("%Y-%m-%d"),
               "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "rows": merged}
        tmp = CACHE / f".{code}_5min_d540.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False))
        os.replace(tmp, CACHE / f"{code}_5min_d540.json")
        ok += 1
        if i % 25 == 0 or i == len(codes):
            el = time.time() - t0
            print(f"  [{i}/{len(codes)}] 累计 ok={ok} skip={skip} fail={fail} "
                  f"({el/60:.1f}min) 末={merged[-1]['time'][:10] if merged else '-'}", flush=True)

    FAIL_FP.write_text(json.dumps(failures, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n===== 完成: ok={ok} skip={skip} fail={fail} =====")
    if failures:
        print(f"失败 {len(failures)} 只 → {FAIL_FP}")


if __name__ == "__main__":
    main()
