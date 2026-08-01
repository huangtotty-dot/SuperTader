# -*- coding: utf-8 -*-
"""
ts_fetch_snapshot_seg.py — X7 统一口径复测: 构建 t_io/minute_snapshots_ts/ 全 Tushare 口径目录
1) 对 minute_snapshots 中 source != tushare 的 stock-day(原腾讯快照段), 用 Tushare stk_mins 重取,
   写入 minute_snapshots_ts/2026/MM/ (绝不触碰原快照目录)
2) 对 source == tushare 的 stock-day, 直接复制到 minute_snapshots_ts/ (内容一致, 保证统一目录完整)
幂等: 目标已存在则跳过。需用户 python (tushare)。
"""
import json, os, re, shutil, sys, time
from datetime import datetime
from pathlib import Path

BASE = Path(r"E:\06_T")
SNAP = BASE / "t_io/minute_snapshots"
TS_DIR = BASE / "t_io/minute_snapshots_ts"
LOG = BASE / "t_io/validation/ts_fetch_snapshot_seg_log.txt"

CODES = {"000988": "000988.SZ", "588170": "588170.SH", "600176": "600176.SH",
         "600481": "600481.SH", "603667": "603667.SH"}

lines = []
def out(s):
    lines.append(s); print(s, flush=True)

def classify(code):
    """返回 (snapshot_dates, tushare_dates) — 按原目录 source 字段分类
    处理 {code}_A_{date} / {code}_B_{date} 账户后缀变体:
    同日存在无后缀文件则以后者为准(harness 加载顺序一致); 仅有后缀文件则该日记为快照段"""
    snap, tus = [], []
    ydir = SNAP / "2026"
    by_date = {}  # d -> (has_plain, src, path)
    for m in sorted(os.listdir(ydir)):
        mdir = ydir / m
        if not mdir.is_dir():
            continue
        for f in sorted(os.listdir(mdir)):
            if not (f.startswith(code + "_") and f.endswith(".json")):
                continue
            key = f[len(code) + 1:-5]
            m2 = re.match(r"^(20\d{2}-\d{2}-\d{2})$", key)
            if m2:
                d = key
                src = json.load(open(mdir / f, encoding="utf-8")).get("source", "snapshot")
                by_date[d] = (True, src, mdir / f)
            else:
                m3 = re.match(r"^[AB]_(20\d{2}-\d{2}-\d{2})$", key)
                if m3:
                    d = m3.group(1)
                    if d not in by_date:  # 仅在无无后缀文件时登记
                        src = json.load(open(mdir / f, encoding="utf-8")).get("source", "snapshot")
                        by_date[d] = (False, src, mdir / f)
    for d in sorted(by_date):
        _, src, fp = by_date[d]
        (tus if "tushare" in src else snap).append((d, fp))
    return snap, tus

def ts_target(d):
    dt = datetime.strptime(d, "%Y-%m-%d")
    return TS_DIR / "2026" / f"{dt.month:02d}"

def main():
    only_copy = "--copy-only" in sys.argv
    total_fetch, total_copy = 0, 0
    pro = None
    if not only_copy:
        import tushare as ts
        ts.set_token("9d15f39266cbbf8a1e5efa1525d7a4d4d1dbc62ec8cbce167d642def")
        pro = ts.pro_api()

    for code, ts_code in CODES.items():
        snap, tus = classify(code)
        # 复制 Tushare 段
        ncp = 0
        for d, fp in tus:
            td = ts_target(d); td.mkdir(parents=True, exist_ok=True)
            tf = td / f"{code}_{d}.json"
            if not tf.exists():
                shutil.copy2(fp, tf); ncp += 1
        out(f"{code}: copied {ncp}/{len(tus)} tushare-seg files")
        total_copy += ncp
        if only_copy:
            continue
        # 拉取快照段
        need = []
        for d, _ in snap:
            tf = ts_target(d) / f"{code}_{d}.json"
            if not tf.exists():
                need.append(d)
        if not need:
            out(f"{code}: snapshot-seg all present, skip fetch")
            continue
        d0, d1 = min(need), max(need)
        # 分块拉取: 每块<=25个自然日窗口, 规避单次~8000行上限
        from datetime import timedelta
        chunks = []
        cur = datetime.strptime(d0, "%Y-%m-%d")
        end_dt = datetime.strptime(d1, "%Y-%m-%d")
        while cur <= end_dt:
            ce = min(cur + timedelta(days=24), end_dt)
            chunks.append((cur.strftime("%Y-%m-%d"), ce.strftime("%Y-%m-%d")))
            cur = ce + timedelta(days=1)
        nw = 0
        got = set()
        for cs, ce in chunks:
            try:
                df = pro.stk_mins(ts_code=ts_code, freq="1min",
                                  start_date=cs + " 09:00:00", end_date=ce + " 19:00:00")
            except Exception as e:
                out(f"{code} {cs}~{ce}: FETCH_ERROR {repr(e)[:120]}"); time.sleep(5); continue
            if df is None or df.empty:
                out(f"{code} {cs}~{ce}: EMPTY"); time.sleep(2); continue
            df = df.sort_values("trade_time")
            df["d"] = df["trade_time"].str[:10]
            for d, g in df.groupby("d"):
                got.add(d)
                if d not in need:
                    continue
                bars = [{"time": r["trade_time"], "open": float(r["open"]), "high": float(r["high"]),
                         "low": float(r["low"]), "close": float(r["close"]),
                         "volume": float(r["vol"]), "amount": float(r["amount"])} for _, r in g.iterrows()]
                td = ts_target(d); td.mkdir(parents=True, exist_ok=True)
                with open(td / f"{code}_{d}.json", "w", encoding="utf-8") as f:
                    json.dump({"code": code, "date": d, "source": "tushare_stk_mins",
                               "bars": bars}, f, ensure_ascii=False)
                nw += 1
            out(f"{code} {cs}~{ce}: rows={len(df)} written_so_far={nw}")
            time.sleep(2)
        missing = [d for d in need if d not in got]
        out(f"{code}: snapshot-seg written={nw}/{len(need)} missing={missing if missing else 0}")
        total_fetch += nw

    out(f"TOTAL: fetched={total_fetch} copied={total_copy}")
    with open(LOG, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return 0

if __name__ == "__main__":
    sys.exit(main())
