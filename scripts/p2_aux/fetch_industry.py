# -*- coding: utf-8 -*-
"""P2 数据外拉 — 申万一级行业映射（两段式，防 300s 超时）。

stage=sw    : 拉 31 个申万一级行业当前成分 → _ckpt_float/../_ckpt_sw/sw_components.parquet
stage=merge : 合并 universe，退市股走巨潮兜底（仅窗口内退市）→ industry_map.parquet

主口径：申万一级（2021 版）当前成分，akshare index_component_sw（申万宏源官网/乐咕乐股代理）。
退市兜底：巨潮 stock_profile_cninfo（证监会门类行业，口径不同，单独标注 industry_source）。
"""
import os
import sys
import time
import warnings

import pandas as pd

warnings.filterwarnings("ignore")
BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AUX = os.path.join(BASE, "t_io", "validation", "xsection", "panel_aux")
CKPT_SW = os.path.join(AUX, "_ckpt_sw")
import akshare as ak


def stage_sw():
    os.makedirs(CKPT_SW, exist_ok=True)
    out_path = os.path.join(CKPT_SW, "sw_components.parquet")
    first = ak.sw_index_first_info()
    rows, failed = [], []
    for _, r in first.iterrows():
        idx_code = str(r["行业代码"]).split(".")[0]
        ind_name = r["行业名称"]
        part_path = os.path.join(CKPT_SW, f"{idx_code}.parquet")
        if os.path.exists(part_path):
            continue
        for attempt in range(3):
            try:
                comp = ak.index_component_sw(symbol=idx_code)
                comp["industry_sw1"] = ind_name
                comp["sw1_index_code"] = idx_code
                comp.to_parquet(part_path, index=False)
                print(f"[SW] {idx_code} {ind_name}: {len(comp)}", flush=True)
                break
            except Exception as e:
                print(f"[SW] {idx_code} attempt{attempt} FAIL {str(e)[:60]}", flush=True)
                time.sleep(2 + attempt * 2)
        else:
            failed.append(idx_code)
        time.sleep(0.4)
    parts = []
    for f in sorted(os.listdir(CKPT_SW)):
        if f.startswith("80") and f.endswith(".parquet"):
            parts.append(pd.read_parquet(os.path.join(CKPT_SW, f)))
    sw = pd.concat(parts, ignore_index=True)
    sw["code6"] = sw["证券代码"].astype(str).str.zfill(6)
    sw = sw.drop_duplicates("code6", keep="first")[["code6", "industry_sw1", "sw1_index_code", "计入日期"]]
    sw = sw.rename(columns={"计入日期": "sw_entry_date"})
    sw["sw_entry_date"] = sw["sw_entry_date"].astype(str)
    sw.to_parquet(out_path, index=False)
    print(f"[SW] total unique constituents: {len(sw)}, failed idx: {failed}")


def stage_merge():
    uni = pd.read_parquet(os.path.join(BASE, "t_io", "validation", "xsection", "panel", "universe.parquet"))
    uni["code6"] = uni["symbol"].str.split(".").str[1]
    sw = pd.read_parquet(os.path.join(CKPT_SW, "sw_components.parquet"))
    m = uni.merge(sw, on="code6", how="left")
    m["industry_source"] = "SW2021_L1"
    m.loc[m["industry_sw1"].isna(), "industry_source"] = ""

    missing = m[m["industry_sw1"].isna()]
    cut = pd.Timestamp("2023-09-01")
    dl = pd.to_datetime(missing["delisted_date"]).dt.tz_localize(None)
    inwin = missing[dl > cut]
    # 巨潮兜底实测：退市股返回空表（600001/300799 均为空），且其依赖 py_mini_racer
    # 非线程安全（并发即 native crash）。兜底无实际收益，放弃，窗口内退市股如实标 UNCOVERED。
    print(f"[cninfo] skipped (empirically empty for delisted): {len(inwin)} in-window delisted -> UNCOVERED")

    m.loc[m["industry_source"] == "", "industry_source"] = "UNCOVERED"
    m["sw1_index_code"] = m["sw1_index_code"].fillna("")
    m["sw_entry_date"] = m["sw_entry_date"].fillna("")
    out = m[["symbol", "code6", "sec_name", "industry_sw1", "sw1_index_code", "industry_source", "sw_entry_date"]]
    path = os.path.join(AUX, "industry_map.parquet")
    out.to_parquet(path, index=False)
    print("[SAVE]", path, out.shape)
    print(out["industry_source"].value_counts().to_string())


if __name__ == "__main__":
    stage = sys.argv[1] if len(sys.argv) > 1 else "sw"
    {"sw": stage_sw, "merge": stage_merge}[stage]()
