# -*- coding: utf-8 -*-
"""新旧面板口径对照：不复权（旧）vs 前复权（新）波动因子包结果。

输出：
  results/volatility_screen_2026-09-18_prevadj/compare_vs_unadj.json
  results/volatility_screen_2026-09-18_prevadj/compare_vs_unadj.md

对照内容：
  1. 各因子 RankIC(h=1/3/5, buy_open) / ICIR(h=5) / 胜率 / 十分位单调性 / MC null_std, mc_rank
  2. TOP100 候选池：重合率、名次挪移榜（重合票的名次变化、新进/跌出名单）
"""
import json
import sys
from pathlib import Path

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")

BASE = Path(__file__).resolve().parent.parent / "t_io" / "validation" / "factor_mining" / "results"
OLD = BASE / "volatility_screen_2026-09-18"
NEW = BASE / "volatility_screen_2026-09-18_prevadj"

FACTORS = ["ATR20", "AMP", "AMP_Q20", "VOLVOL", "AMOUNT20", "COMBO", "TR_SHARE"]


def load_check(d: Path, name: str) -> dict:
    return json.loads((d / f"check_{name}.json").read_text(encoding="utf-8"))


def metric_row(chk: dict) -> dict:
    ev = chk["evaluate"]
    dec = chk.get("decile", {})
    mc = chk.get("mc", {})
    row = {}
    for h in ("1", "3", "5"):
        st = ev.get(h, {}).get("buy_open", {})
        row[f"ic_h{h}"] = st.get("rank_ic_mean")
    st5 = ev.get("5", {}).get("buy_open", {})
    row["icir_h5"] = st5.get("icir")
    row["win_h5"] = st5.get("win_rate")
    # 十分位单调性：找 spearman 相关字段（键名以 ic_layer 实现为准，做容错搜索）
    mono = None
    for k, v in dec.items():
        if isinstance(v, (int, float)) and ("mono" in k.lower() or "spearman" in k.lower() or "corr" in k.lower()):
            mono = v
    row["monotonicity"] = mono
    row["mc_null_std"] = mc.get("null_std")
    row["mc_rank"] = mc.get("mc_rank")
    return row


def main():
    rows = []
    for f in FACTORS:
        old = metric_row(load_check(OLD, f))
        new = metric_row(load_check(NEW, f))
        r = {"factor": f}
        for k in old:
            r[f"old_{k}"] = old[k]
            r[f"new_{k}"] = new.get(k)
            if isinstance(old[k], (int, float)) and isinstance(new.get(k), (int, float)):
                r[f"delta_{k}"] = new[k] - old[k]
        rows.append(r)
    cmp_df = pd.DataFrame(rows)

    # ── TOP100 对照（symbol 格式归一：旧榜 'SHSE.xxxxxx' / 新榜纯代码） ──
    def norm(s):
        p = str(s).split(".")
        return p[1] if len(p) == 2 and p[1].isdigit() else p[0]
    old_pool = pd.read_csv(OLD / "top100_pool.csv", dtype={"symbol": str})
    new_pool = pd.read_csv(NEW / "top100_pool.csv", dtype={"symbol": str})
    old_pool["symbol"] = old_pool["symbol"].map(norm)
    new_pool["symbol"] = new_pool["symbol"].map(norm)
    old_set, new_set = set(old_pool["symbol"]), set(new_pool["symbol"])
    both = old_set & new_set
    overlap = len(both)

    old_rank = dict(zip(old_pool["symbol"], old_pool["rank"]))
    new_rank = dict(zip(new_pool["symbol"], new_pool["rank"]))
    moves = []
    for s in both:
        moves.append({
            "symbol": s,
            "old_rank": old_rank[s],
            "new_rank": new_rank[s],
            "rank_delta": old_rank[s] - new_rank[s],  # 正=名次上升
            "board": new_pool.loc[new_pool["symbol"] == s, "board"].iloc[0],
        })
    moves_df = pd.DataFrame(moves).sort_values("rank_delta")
    dropped = sorted(old_set - new_set, key=lambda s: old_rank[s])
    entered = sorted(new_set - old_set, key=lambda s: new_rank[s])

    summary = {
        "overlap": overlap,
        "overlap_rate": overlap / 100.0,
        "dropped_count": len(dropped),
        "entered_count": len(entered),
        "big_movers_down": moves_df.head(10).to_dict("records"),   # 名次大跌
        "big_movers_up": moves_df.tail(10)[::-1].to_dict("records"),  # 名次大升
        "dropped": [{"symbol": s, "old_rank": old_rank[s],
                     "board": old_pool.loc[old_pool["symbol"] == s, "board"].iloc[0]} for s in dropped],
        "entered": [{"symbol": s, "new_rank": new_rank[s],
                     "board": new_pool.loc[new_pool["symbol"] == s, "board"].iloc[0]} for s in entered],
    }

    out = {"factor_metrics": rows, "top100": summary}
    (NEW / "compare_vs_unadj.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    # ── Markdown 对照表 ──
    md = ["# 新旧口径对照（不复权 vs 前复权）", ""]
    md.append("## 因子体检指标（buy_open）")
    md.append("")
    md.append("| 因子 | RankIC h=1 旧→新 | RankIC h=5 旧→新 | ICIR h=5 旧→新 | 胜率h=5 旧→新 | 单调性 旧→新 | MC rank 旧→新 |")
    md.append("|---|---|---|---|---|---|---|")
    for r in rows:
        def fmt(k, pct=False):
            o, n = r.get(f"old_{k}"), r.get(f"new_{k}")
            if o is None or n is None:
                return f"{'缺失' if o is None else f'{o:.4f}'} → {'缺失' if n is None else f'{n:.4f}'}"
            f_ = (lambda x: f"{x:.1%}") if pct else (lambda x: f"{x:.4f}")
            return f"{f_(o)} → {f_(n)}"
        md.append(f"| {r['factor']} | {fmt('ic_h1')} | {fmt('ic_h5')} | {fmt('icir_h5')} "
                  f"| {fmt('win_h5', pct=True)} | {fmt('monotonicity')} | {fmt('mc_rank')} |")
    md.append("")
    md.append(f"## TOP100 候选池对照：重合 **{overlap}/100（{overlap/100:.0%}）**")
    md.append("")
    md.append(f"- 跌出 {len(dropped)} 只：" + "、".join(f"{d['symbol']}(旧#{d['old_rank']})" for d in summary["dropped"]))
    md.append(f"- 新进 {len(entered)} 只：" + "、".join(f"{e['symbol']}(新#{e['new_rank']})" for e in summary["entered"]))
    md.append("")
    md.append("名次挪移 TOP10（重合票内，升/降各 5）：")
    md.append("")
    md.append("| symbol | 板块 | 旧名次 | 新名次 | 变化 |")
    md.append("|---|---|---|---|---|")
    for m in summary["big_movers_up"][:5] + summary["big_movers_down"][:5]:
        md.append(f"| {m['symbol']} | {m['board']} | {m['old_rank']} | {m['new_rank']} | {m['rank_delta']:+d} |")
    md.append("")
    (NEW / "compare_vs_unadj.md").write_text("\n".join(md), encoding="utf-8")

    print(f"[done] 重合率 {overlap}/100；对照表 -> {NEW / 'compare_vs_unadj.md'}")
    print(cmp_df[["factor", "old_ic_h5", "new_ic_h5", "old_icir_h5", "new_icir_h5"]].to_string(index=False))


if __name__ == "__main__":
    main()
