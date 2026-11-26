# -*- coding: utf-8 -*-
"""H4 回测 · H6 路径分层分析（D4 关键工序提前跑，2026-11-26）。

问题：H4 波段持股 vs OSC 出场规则（同入场集 C 臂）的收益差，在 H6 的
A 路径（直达平稳/强势）与 B 路径（二次探底）下是否显著不同？
若 H4 只在 A 路径胜出 ⇒ H4 不直接改规则，转为 H6 判定器输入（排期文档判定标准第 3 条）。

分层口径（与复盘清单 H6 登记一致）：
  冰点段（≤25）拐点日 T 起 20 个交易日内——
    A：未回 ≤25（本组窗口全部已 ≥40，故 A 即「未回冰点」）
    B：再回 ≤25（二次探底）
  窗口按 cross_date 归属；sent_ma 与 comp 序列各自分层。
  注意：H6 原定义针对「≤25 冰点段拐点」，本分层把它推广到所有 <40→≥40 跨越窗口
  （披露：推广口径，冰点段外的 A/B 语义弱化，仅作分组变量）。

输入：t_io/validation/h4_backtest/{windows.json, trades_h4.csv, trades_oscexit.csv}
      + t_io/index_regime/composite_temp.json
"""
from __future__ import annotations

import csv
import json
import os
from collections import defaultdict

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_DIR = os.path.join(_ROOT, "t_io", "validation", "h4_backtest")
TEMP_FP = os.path.join(_ROOT, "t_io", "index_regime", "composite_temp.json")


def classify_windows(wins, series_key):
    """每个窗口 → 'A' / 'B' / 'unknown'（序列尾部不足 20 日）。"""
    temp = json.load(open(TEMP_FP, encoding="utf-8"))
    dates = sorted(temp.keys())
    pos = {d: i for i, d in enumerate(dates)}
    result = {}
    for w in wins:
        cd = w["cross_date"]
        i = pos.get(cd)
        if i is None:
            cls = "unknown"
        else:
            fwd = [temp[d].get(series_key) for d in dates[i + 1:i + 21]]
            fwd = [v for v in fwd if v is not None]
            if len(fwd) < 10:
                cls = "unknown"
            else:
                cls = "B" if any(v <= 25 for v in fwd) else "A"
        result[w["window_id"]] = cls
    return result


def main():
    wins = json.load(open(os.path.join(OUT_DIR, "windows.json"), encoding="utf-8"))
    cls_sent = classify_windows(wins, "sent_ma")
    cls_comp = classify_windows(wins, "comp")

    def load(fp):
        return list(csv.DictReader(open(os.path.join(OUT_DIR, fp), encoding="utf-8-sig")))

    h4_rows = load("trades_h4.csv")
    c_rows = load("trades_oscexit.csv")

    h4_by_key = defaultdict(list)
    for r in h4_rows:
        h4_by_key[(r["window_id"], r["code"])].append(float(r["ret_net"]))
    c_by_key = {(r["window_id"], r["code"]): float(r["ret_net"]) for r in c_rows}

    # 逐候选配对差（H4 复合 − C 臂），按窗口路径类别分桶
    buckets = defaultdict(list)   # (cls) -> [diff]
    h4_abs = defaultdict(list)    # (cls) -> [h4 comp ret]
    c_abs = defaultdict(list)
    for k, legs in h4_by_key.items():
        if k not in c_by_key:
            continue
        cls = cls_sent.get(k[0]) or cls_comp.get(k[0]) or "unknown"
        comp_ret = 1.0
        for x in legs:
            comp_ret *= 1 + x / 100.0
        comp_ret = (comp_ret - 1) * 100
        buckets[cls].append(comp_ret - c_by_key[k])
        h4_abs[cls].append(comp_ret)
        c_abs[cls].append(c_by_key[k])

    print("== H6 路径分层 · H4复合 − C臂 配对差 ==")
    print(f"{'路径':<8}{'n':>8}{'均值pp':>10}{'中位pp':>10}{'H4胜出率':>10}"
          f"{'H4均值%':>10}{'C臂均值%':>10}")
    summ = {}
    for cls in ("A", "B", "unknown"):
        d = sorted(buckets.get(cls, []))
        if not d:
            continue
        n = len(d)
        row = {"n": n, "diff_mean": sum(d) / n, "diff_median": d[n // 2],
               "h4_win": sum(1 for x in d if x > 0) / n * 100,
               "h4_mean": sum(h4_abs[cls]) / n, "c_mean": sum(c_abs[cls]) / n}
        summ[cls] = row
        print(f"{cls:<8}{n:>8}{row['diff_mean']:>+10.3f}{row['diff_median']:>+10.3f}"
              f"{row['h4_win']:>9.1f}%{row['h4_mean']:>+10.3f}{row['c_mean']:>+10.3f}")

    with open(os.path.join(OUT_DIR, "h6_layering.json"), "w", encoding="utf-8") as f:
        json.dump({"note": "H4复合−C臂 配对差按 H6 路径分层；unknown=序列尾部不足20日",
                   "layers": summ}, f, ensure_ascii=False, indent=1)
    print(f"\n产物: {OUT_DIR}/h6_layering.json")


if __name__ == "__main__":
    main()
