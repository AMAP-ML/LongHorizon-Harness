#!/usr/bin/env python3
"""Render the benchmark positioning table as a branded PNG matching the
leaderboard_table*.png style (purple→pink gradient header, highlighted
'ours' row, alternating row tint). Outputs docs/media/comparison_table.png.

Run: python docs/media/make_comparison_table.py
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap
import numpy as np

COLS = ["Benchmark", "Platform", "Multi-\nchannel", "Non-\nsubstitutable",
        "In-the-\nwild", "Cross-\napp", "Traj.\njudge", "Deployed\nruntime"]
ROWS = [
    ["WebArena", "Web", "✗", "✗", "P", "P", "✗", "✗"],
    ["OSWorld", "Real OS", "✗", "✗", "P", "P", "✗", "✗"],
    ["WindowsAgentArena", "Real OS", "✗", "✗", "P", "P", "✗", "✗"],
    ["OSWorld-MCP", "Real OS", "✓", "✗", "P", "P", "✗", "✗"],
    ["ScienceBoard", "Real OS", "✓", "P", "P", "P", "✗", "✗"],
    ["WildClawBench", "Headless OS", "✗", "✗", "✓", "P", "P", "✓"],
    ["WeaveBench (ours)", "Real OS", "✓", "✓", "✓", "✓", "✓", "✓"],
]

TITLE = "WeaveBench vs. prior computer-use benchmarks"
SUBTITLE = "✓ = satisfied   ·   ✗ = not satisfied   ·   P = partial"

# Brand palette (matches leaderboard_table*.png).
PURPLE = "#5b2be0"
PINK = "#e0218a"
HEADER_CMAP = LinearSegmentedColormap.from_list("hdr", [PURPLE, PINK])
OURS_TINT = "#fce7f3"   # light pink for the ours row
ALT_TINT = "#f7f7fa"    # zebra
SYM_COLOR = {"✓": "#16a34a", "✗": "#cbd5e1", "P": "#f59e0b"}

n_rows = len(ROWS)
n_cols = len(COLS)

# Header occupies 1.4 row-heights to fit the two-line labels; rows start below.
HDR_H = 1.5
fig_w, fig_h = 13.0, 0.72 * (n_rows + HDR_H + 1.4)
fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=200)
ax.set_xlim(0, n_cols)
ax.set_ylim(0, n_rows + HDR_H)
ax.axis("off")
ax.invert_yaxis()

# Column widths: first column wider for benchmark names, second a bit wider.
col_w = np.array([2.2, 1.25] + [1.0] * (n_cols - 2))
col_w = col_w / col_w.sum() * n_cols
col_x = np.concatenate([[0], np.cumsum(col_w)])

def cell_center(c):
    return (col_x[c] + col_x[c + 1]) / 2

# --- header row (gradient) ---
hy = 0.0
for c in range(n_cols):
    grad = HEADER_CMAP(c / (n_cols - 1))
    ax.add_patch(plt.Rectangle((col_x[c], hy), col_w[c], HDR_H,
                               facecolor=grad, edgecolor="white", lw=1.5, zorder=1))
    ha = "left" if c == 0 else "center"
    tx = col_x[c] + 0.12 if c == 0 else cell_center(c)
    ax.text(tx, hy + HDR_H / 2, COLS[c], color="white", fontsize=14,
            fontweight="bold", ha=ha, va="center", linespacing=1.15, zorder=2)

# --- data rows ---
for r, row in enumerate(ROWS):
    y = HDR_H + r
    is_ours = "ours" in row[0]
    if is_ours:
        face = OURS_TINT
    else:
        face = ALT_TINT if r % 2 == 1 else "white"
    ax.add_patch(plt.Rectangle((0, y), n_cols, 1.0, facecolor=face,
                               edgecolor="#e5e7eb", lw=0.8, zorder=0))
    for c, val in enumerate(row):
        if c == 0:
            color = PINK if is_ours else "#111827"
            ax.text(col_x[0] + 0.12, y + 0.5, val, color=color, fontsize=14,
                    fontweight="bold" if is_ours else "normal",
                    ha="left", va="center", zorder=2)
        elif c == 1:
            ax.text(cell_center(c), y + 0.5, val, color="#374151",
                    fontsize=13, ha="center", va="center", zorder=2)
        else:
            color = SYM_COLOR.get(val, "#111827")
            ax.text(cell_center(c), y + 0.5, val, color=color, fontsize=18,
                    fontweight="bold", ha="center", va="center", zorder=2)

# --- title + subtitle above the table ---
fig.suptitle(TITLE, x=0.012, y=0.995, ha="left", fontsize=18, fontweight="bold")
fig.text(0.012, 0.93, SUBTITLE, ha="left", fontsize=13, color="#6b7280")

plt.subplots_adjust(left=0.005, right=0.995, top=0.85, bottom=0.01)
out = __file__.rsplit("/", 1)[0] + "/comparison_table.png"
fig.savefig(out, dpi=200, bbox_inches="tight", facecolor="white")
print("wrote", out)
