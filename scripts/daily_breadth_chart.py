# -*- coding: utf-8 -*-
"""每日主线广度图（store 驱动，自动跟随 sector_breadth.json 更新）

用法：
  python scripts/daily_breadth_chart.py            # 用最新日期
  python scripts/daily_breadth_chart.py --asof 2026-10-23 --days 25

输出：
  t_io/validation/daily_review/主线广度_{asof}.png
  t_io/validation/daily_review/主线广度_latest.png
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.gridspec import GridSpec

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "stock_hunter"))

from modules import sector_phase as sp  # noqa: E402

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei"]
plt.rcParams["axes.unicode_minus"] = False

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"

PHASE_COLORS = {"首现": "#2a78d6", "发酵": "#1baf7a", "高潮": "#eb6834",
                "退潮": "#898781", "扩散": "#6250d6", "观察": "#c3c2b7", "—": "#c3c2b7"}


def build_rows(snap):
    """热力图行：AI链内部 5 行 + 外部对照 4 行。"""
    ai = {x.get("label"): x for x in snap["ai_chain"]}
    th = {x["theme"]: x for x in snap["themes"]}
    rows = [
        ("AI应用（泛）", ai["AI应用（泛）"]["series"], ai["AI应用（泛）"]["phase"]),
        ("AI影视/短剧", ai["AI影视/短剧"]["series"], ai["AI影视/短剧"]["phase"]),
        ("AI硬件", ai["AI硬件"]["series"], ai["AI硬件"]["phase"]),
        ("端侧AI", ai["端侧AI"]["series"], ai["端侧AI"]["phase"]),
        ("AI安全", ai["AI安全"]["series"], ai["AI安全"]["phase"]),
        ("数据要素", ai["数据要素"]["series"], ai["数据要素"]["phase"]),
        ("物理AI", ai["物理AI"]["series"], ai["物理AI"]["phase"]),
        ("机器人", ai["机器人"]["series"], ai["机器人"]["phase"]),
        ("电子/算力硬件", th["电子/算力硬件"]["series"], th["电子/算力硬件"]["phase"]),
        ("医药医疗", th["医药医疗"]["series"], th["医药医疗"]["phase"]),
        ("低空/军工", th["低空/军工"]["series"], th["低空/军工"]["phase"]),
        ("大消费", th["大消费"]["series"], th["大消费"]["phase"]),
    ]
    return rows


def render(asof=None, days=25):
    snap = sp.snapshot(asof=asof, max_days=days)
    dates = snap["dates"]
    if not dates:
        raise SystemExit("sector_breadth.json 为空")
    asof = snap["asof"]
    rows = build_rows(snap)
    labels = [r[0] for r in rows]
    mat = np.array([[np.nan if v is None else v for v in r[1]] for r in rows], dtype=float)
    phases = [r[2] for r in rows]
    finite = mat[np.isfinite(mat)]
    vmax = max(5, int(finite.max()) if finite.size else 5)

    fig = plt.figure(figsize=(max(14, 0.62 * len(dates) + 6), 10.6), dpi=200)
    fig.patch.set_facecolor(SURFACE)
    gs = GridSpec(2, 1, height_ratios=[1.0, 1.12], hspace=0.28,
                  left=0.13, right=0.90, top=0.90, bottom=0.10)

    # ── 热力图 ──
    ax_h = fig.add_subplot(gs[0])
    ax_h.set_facecolor(SURFACE)
    cmap = LinearSegmentedColormap.from_list("breadth", [
        (0.000, "#f4f3ef"), (0.030, "#cde2fb"), (0.120, "#9ec5f4"),
        (0.280, "#6da7ec"), (0.480, "#3987e5"), (0.700, "#1c5cab"), (1.000, "#0d366b")])
    ax_h.imshow(mat, aspect="auto", cmap=cmap, vmin=0, vmax=vmax,
                interpolation="nearest")
    ax_h.set_xticks(range(len(dates)))
    ax_h.set_xticklabels([d[5:] for d in dates], rotation=45, ha="right",
                         fontsize=8.5, color=MUTED)
    ax_h.set_yticks(range(len(labels)))
    ax_h.set_yticklabels(labels, fontsize=10)
    for tick, lab in zip(ax_h.get_yticklabels(), labels):
        tick.set_color(INK if lab in ("AI应用（泛）", "AI影视/短剧", "数据要素",
                                      "物理AI", "机器人") else INK2)
        tick.set_fontweight("bold" if lab in ("AI应用（泛）", "AI影视/短剧", "数据要素",
                                             "物理AI", "机器人") else "normal")
    ax_h.tick_params(length=0)
    for s in ax_h.spines.values():
        s.set_visible(False)
    for i in range(mat.shape[0]):
        for j in range(mat.shape[1]):
            if not np.isfinite(mat[i, j]):
                continue
            v = int(mat[i, j])
            if v:
                ax_h.text(j, i, str(v), ha="center", va="center", fontsize=7.2,
                          color="#ffffff" if v >= vmax * 0.45 else INK)
    # 行尾阶段
    for i, ph in enumerate(phases):
        ax_h.text(len(dates) - 0.35, i, ph, ha="left", va="center", fontsize=9.5,
                  color=PHASE_COLORS.get(ph, MUTED), fontweight="bold",
                  clip_on=False)
    ax_h.set_xlim(-0.5, len(dates) + 1.0)
    ax_h.set_title("每日涨停广度（数字=当日该板块涨停家数；行尾=今日阶段）",
                   fontsize=11, color=INK2, loc="left", pad=8)

    # ── 趋势线 ──
    ax_l = fig.add_subplot(gs[1])
    ax_l.set_facecolor(SURFACE)
    x = np.arange(len(dates))
    cats = snap["categories"]
    rd = {r[0]: r for r in rows}

    def _vals(x):
        return np.array([np.nan if v is None else v for v in x], dtype=float)

    series = [
        ("AI应用大类", _vals(cats["AI应用"]["series"][-len(dates):]), "#2a78d6", 2.2),
        ("数据要素", _vals(rd["数据要素"][1]), "#eb6834", 2.2),
        ("物理AI", _vals(rd["物理AI"][1]), "#1baf7a", 2.2),
        ("机器人（对照）", _vals(rd["机器人"][1]), "#898781", 1.6),
    ]
    for name, vals, color, lw in series:
        ax_l.plot(x, vals, color=color, lw=lw, solid_capstyle="round",
                  solid_joinstyle="round", zorder=3, marker="o", ms=5,
                  mfc=color, mec=SURFACE, mew=1.5, label=name)
    ax_l.set_xlim(-0.5, len(dates) - 0.5)
    _allv = [v for _, vals, _, _ in series for v in vals if np.isfinite(v)]
    ax_l.set_ylim(0, max(10, int(max(_allv or [10]) * 1.15)))
    ax_l.grid(axis="y", color=GRID, lw=0.8, ls="-", zorder=0)
    ax_l.set_axisbelow(True)
    for side in ("top", "right"):
        ax_l.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax_l.spines[side].set_color(AXIS)
        ax_l.spines[side].set_linewidth(0.8)
    ax_l.tick_params(axis="x", length=0, labelsize=8.5, colors=MUTED)
    ax_l.tick_params(axis="y", length=0, labelsize=9, colors=MUTED)
    ax_l.set_xticks(x)
    ax_l.set_xticklabels([d[5:] for d in dates], rotation=45, ha="right")
    ax_l.set_ylabel("涨停家数", fontsize=10, color=INK2)
    ax_l.legend(loc="upper left", frameon=False, fontsize=9.5, ncol=4,
                labelcolor=INK2, handlelength=1.6)

    # 端点标签（取最后一个有数据的点；未录入日不画）
    def _last(vals):
        return next((float(v) for v in vals[::-1] if np.isfinite(v)), None)

    for name, vals, color, lw in series:
        last = _last(vals)
        if last is None:
            continue
        ax_l.text(len(dates) - 0.6, last, f"{name} {int(last)}", ha="left",
                  va="center", fontsize=9.5, color=INK2, fontweight="bold")

    # 峰值标注（数据驱动）
    def peak_note(name, vals, color):
        if not np.isfinite(vals).any():
            return
        i = int(np.nanargmax(vals))
        if not np.isfinite(vals[i]) or vals[i] <= 0:
            return
        ax_l.annotate(f"{dates[i][5:]} {name} {int(vals[i])}", xy=(i, vals[i]),
                      xytext=(0, 12), textcoords="offset points", fontsize=8.2,
                      color=INK2, ha="center",
                      arrowprops=dict(arrowstyle="-", color=MUTED, lw=0.7))
    peak_note("数据要素", _vals(rd["数据要素"][1]), "#eb6834")
    peak_note("物理AI", _vals(rd["物理AI"][1]), "#1baf7a")

    total = snap["themes"][0]  # AI链
    _core = ["AI应用（泛）", "AI影视/短剧", "数据要素", "物理AI", "机器人"]
    _core_txt = " · ".join(
        f"{lab} {int(_last(rd[lab][1])) if _last(rd[lab][1]) is not None else 0}（{rd[lab][2]}）"
        for lab in _core)
    fig.suptitle(f"AI链主线演化 · 截至 {asof}", fontsize=16.5, color=INK,
                 x=0.13, ha="left", y=0.965, fontweight="bold")
    fig.text(0.13, 0.935,
             f"AI链合计 {total['count']}（{total['phase']}） · " + _core_txt
             + f" ｜ 数据：韭研公社涨停复盘（{len(dates)}个交易日；空白=未录入）",
             fontsize=9.6, color=INK2, ha="left")
    fig.text(0.13, 0.035,
             "阶段口径：首现=10日内首次出现；扩散=大主题已活跃后出现的中等新分支；"
             "发酵=≥5只且上升；高潮=≥8只且≥5日峰值75%、最高连板≥3；退潮=≤5日峰值60%且下降。"
             " 完整数值见 GUI「每日复盘 → 主线广度」表。",
             fontsize=8.2, color=MUTED, ha="left")

    outdir = BASE / "t_io" / "validation" / "daily_review"
    outdir.mkdir(parents=True, exist_ok=True)
    outs = [outdir / f"主线广度_{asof}.png", outdir / "主线广度_latest.png"]
    for out in outs:
        fig.savefig(out, facecolor=SURFACE, bbox_inches="tight", pad_inches=0.22)
    plt.close(fig)
    return outs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", default=None)
    ap.add_argument("--days", type=int, default=25)
    args = ap.parse_args()
    outs = render(args.asof, args.days)
    for o in outs:
        print("saved:", o)


if __name__ == "__main__":
    main()
