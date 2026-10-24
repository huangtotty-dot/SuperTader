# -*- coding: utf-8 -*-
"""AI应用/数据要素 主线演化图（2026-09-16 ~ 10-23，21个交易日）

数据来源：韭研公社「全天涨停复盘简图」逐日截图，人工转录各板块涨停家数。
只做广度（板块内涨停家数）统计，不含个股涨幅。

输出：doc/research/2026-10-23_AI应用数据要素_主线演化.png
"""
import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.gridspec import GridSpec
import numpy as np

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei"]
plt.rcParams["axes.unicode_minus"] = False

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"

DATES = ["09-16", "09-17", "09-18", "09-21", "09-22", "09-23", "09-24", "09-28",
         "09-29", "09-30", "10-09", "10-12", "10-13", "10-14", "10-15", "10-16",
         "10-19", "10-20", "10-21", "10-22", "10-23"]

# ── 人工转录：各板块当日涨停家数 ──────────────────────────────
ROWS = [
    ("AI应用（泛）",      [0,0,6,4,11,3,0,5,9,2,0,4,5,3,0,2,0,4,6,0,7], True),
    ("AI影视/短剧",       [0,0,0,0,0,0,0,0,0,0,12,10,5,5,3,4,0,0,0,0,0], True),
    ("AI硬件/端侧AI",     [7,0,6,4,8,3,0,0,0,0,0,0,4,0,0,0,7,7,2,0,3], True),
    ("AI安全",            [0,0,0,0,0,0,0,0,0,0,5,0,0,0,0,0,0,0,0,0,0], True),
    ("数据要素",          [0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,9,2,0], True),
    ("物理AI",            [0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,3,0,0,0,14], True),
    ("机器人",            [4,6,5,3,2,0,8,4,2,4,2,0,0,0,0,9,9,20,4,13,27], False),
    ("电子/算力硬件*",     [37,10,14,8,12,20,9,0,8,2,2,0,10,0,5,4,2,0,0,0,4], False),
    ("医药医疗",          [0,2,3,21,5,2,3,3,0,9,5,2,0,2,4,2,2,2,4,0,0], False),
    ("低空/军工*",         [0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,27,9,5,0,2,0], False),
]
NOTE_ROWS = "* 电子/算力硬件 = PCB+光通信+半导体；低空/军工 = 低空经济+军工"
CLUSTER = [sum(v) for v in zip(*[r[1] for r in ROWS if r[0] in
           ("AI应用（泛）", "AI影视/短剧", "AI硬件/端侧AI", "AI安全")])]

# ── 图 ──────────────────────────────────────────────────────
fig = plt.figure(figsize=(18, 13.2), dpi=200)
fig.patch.set_facecolor(SURFACE)
gs = GridSpec(3, 1, height_ratios=[0.16, 1.0, 1.18], hspace=0.30,
              left=0.13, right=0.985, top=0.915, bottom=0.075)

# 阶段条
ax_phase = fig.add_subplot(gs[0])
ax_phase.set_facecolor(SURFACE)
PHASES = [(0, 1, "无AI主线", "#f0efec"),
          (2, 9, "AI首现 · 反复脉冲", "#e8f0fb"),
          (10, 15, "分支轮动：影视/短剧", "#fbeee7"),
          (16, 20, "硬件/具身接棒", "#e7f6f0")]
for a, b, label, color in PHASES:
    ax_phase.axvspan(a - 0.5, b + 0.5, color=color, lw=0)
    ax_phase.text((a + b) / 2, 0.5, label, ha="center", va="center",
                  fontsize=11, color=INK2, fontweight="bold")
ax_phase.set_xlim(-0.5, len(DATES) - 0.5)
ax_phase.set_ylim(0, 1)
ax_phase.axis("off")

# 热力图
ax_h = fig.add_subplot(gs[1])
ax_h.set_facecolor(SURFACE)
mat = np.array([r[1] for r in ROWS], dtype=float)
cmap = LinearSegmentedColormap.from_list("breadth", [
    (0.000, "#f4f3ef"),
    (0.030, "#cde2fb"),
    (0.120, "#9ec5f4"),
    (0.280, "#6da7ec"),
    (0.480, "#3987e5"),
    (0.700, "#1c5cab"),
    (1.000, "#0d366b"),
])
im = ax_h.imshow(mat, aspect="auto", cmap=cmap, vmin=0, vmax=37,
                 interpolation="nearest")
ax_h.set_xticks(range(len(DATES)))
ax_h.set_xticklabels(DATES, rotation=45, ha="right", fontsize=9, color=MUTED)
ax_h.set_yticks(range(len(ROWS)))
ax_h.set_yticklabels([r[0] for r in ROWS], fontsize=10.5)
for tick, row in zip(ax_h.get_yticklabels(), ROWS):
    tick.set_color(INK if row[2] else INK2)
    tick.set_fontweight("bold" if row[2] else "normal")
ax_h.tick_params(length=0)
for s in ax_h.spines.values():
    s.set_visible(False)
for i in range(mat.shape[0]):
    for j in range(mat.shape[1]):
        v = int(mat[i, j])
        if v:
            ax_h.text(j, i, str(v), ha="center", va="center", fontsize=7.6,
                      color="#ffffff" if v >= 15 else INK)
ax_h.set_title("每日各板块涨停家数（颜色越深 = 家数越多；数字直接标注）",
               fontsize=11, color=INK2, loc="left", pad=8)

# 趋势线
ax_l = fig.add_subplot(gs[2])
ax_l.set_facecolor(SURFACE)
for a, b, _, color in PHASES:
    ax_l.axvspan(a - 0.5, b + 0.5, color=color, lw=0, alpha=0.55, zorder=0)

x = np.arange(len(DATES))
series = [
    ("AI应用大类*", CLUSTER, "#2a78d6", 2.2),
    ("数据要素", [r[1] for r in ROWS if r[0] == "数据要素"][0], "#eb6834", 2.2),
    ("物理AI", [r[1] for r in ROWS if r[0] == "物理AI"][0], "#1baf7a", 2.2),
    ("机器人（对照）", [r[1] for r in ROWS if r[0] == "机器人"][0], "#898781", 1.6),
]
for name, vals, color, lw in series:
    ax_l.plot(x, vals, color=color, lw=lw, solid_capstyle="round",
              solid_joinstyle="round", zorder=3,
              marker="o", ms=5.5, mfc=color, mec=SURFACE, mew=1.6,
              label=name)

ax_l.set_xlim(-0.5, len(DATES) - 0.5)
ax_l.set_ylim(0, 30)
ax_l.set_yticks(range(0, 31, 5))
ax_l.grid(axis="y", color=GRID, lw=0.8, ls="-", zorder=0)
ax_l.set_axisbelow(True)
for side in ("top", "right"):
    ax_l.spines[side].set_visible(False)
for side in ("left", "bottom"):
    ax_l.spines[side].set_color(AXIS)
    ax_l.spines[side].set_linewidth(0.8)
ax_l.tick_params(axis="x", length=0, labelsize=9, colors=MUTED)
ax_l.tick_params(axis="y", length=0, labelsize=9, colors=MUTED)
ax_l.set_xticks(x)
ax_l.set_xticklabels(DATES, rotation=45, ha="right")
ax_l.set_ylabel("涨停家数", fontsize=10, color=INK2)

# 直接标签（线条末端）
ax_l.text(len(DATES) - 0.65, 27, "机器人 27", ha="left", va="center",
          fontsize=10, color=INK2, fontweight="bold")
ax_l.text(len(DATES) - 0.65, 14, "物理AI 14", ha="left", va="center",
          fontsize=10, color=INK2, fontweight="bold")
ax_l.text(len(DATES) - 0.65, 10, "AI应用大类 10", ha="left", va="center",
          fontsize=10, color=INK2, fontweight="bold")

# 关键节点标注
ANN = [
    (2, CLUSTER[2], "09-18 AI首现\n12只", (-8, 34)),
    (4, CLUSTER[4], "09-22 二次放量\n19只（确认日）", (-70, 40)),
    (10, CLUSTER[10], "10-09 AI短剧分支\n12只（大标签仅0）", (-10, 45)),
    (18, 9, "10-21 数据要素 9只\n次日2 → 0（一日扩散）", (-118, 18)),
]
for xi, yi, txt, off in ANN:
    ax_l.annotate(txt, xy=(xi, yi), xytext=off, textcoords="offset points",
                  fontsize=8.6, color=INK2, ha="center", va="bottom",
                  arrowprops=dict(arrowstyle="-", color=MUTED, lw=0.8,
                                  shrinkA=0, shrinkB=3), zorder=5)

ax_l.legend(loc="upper left", frameon=False, fontsize=9.5, ncol=2,
            labelcolor=INK2, handlelength=1.6, columnspacing=1.2)

fig.suptitle("AI链的涨停广度演化：首现 → 反复脉冲 → 分支接力 → 硬件/具身接棒",
             fontsize=17, color=INK, x=0.13, ha="left", y=0.975,
             fontweight="bold")
fig.text(0.13, 0.947,
         "2026-09-16 ~ 10-23，共21个交易日 · 数据：韭研公社全天涨停复盘简图（人工转录）",
         fontsize=10.5, color=INK2, ha="left")
fig.text(0.13, 0.030,
         "* AI应用大类 = AI应用 + AI影视/短剧 + AI硬件/端侧AI + AI安全；"
         "机器人以灰色作对照。" + NOTE_ROWS + "。"
         " 完整数值见配套 Markdown 表格。",
         fontsize=8.6, color=MUTED, ha="left")

out = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "doc", "research",
    "2026-10-23_AI应用数据要素_主线演化.png")
os.makedirs(os.path.dirname(out), exist_ok=True)
fig.savefig(out, facecolor=SURFACE, bbox_inches="tight", pad_inches=0.25)
print("saved:", out)
print("cluster:", CLUSTER)
