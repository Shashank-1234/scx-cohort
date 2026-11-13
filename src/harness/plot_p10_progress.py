#!/usr/bin/env python3
"""Second P10 figure: worker progress + competitor progress (stacked = combined), per barrier mode and W.
Efficiency alone hides who got the CPU; this shows the split. Reads analysis/summary.json from analyze_p10.py."""
import json, os, sys
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt

run = sys.argv[1]
cells = json.load(open(os.path.join(run, "analysis", "summary.json")))["cells"]
arms = ["E", "S", "Coff", "Ctick", "Cwake", "Ckick", "U10"]
fig, axes = plt.subplots(2, 3, figsize=(13, 6.5), sharey=True)
for j, mode in enumerate(["spin", "adaptive", "block"]):
    for i, w in enumerate([10, 100]):
        ax = axes[i][j]
        wp = [cells.get(f"{a}/{mode}/{w}", {}).get("w_progress", [0])[0] for a in arms]
        cp = [cells.get(f"{a}/{mode}/{w}", {}).get("c_progress", [0])[0] for a in arms]
        ax.bar(arms, wp, color="#3a6ea5", label="worker progress")
        ax.bar(arms, cp, bottom=wp, color="#c8c8c8", label="competitor progress")
        for k, (a, b) in enumerate(zip(wp, cp)):
            ax.text(k, a / 2, f"{a:.2f}", ha="center", va="center", fontsize=7, color="white")
            ax.text(k, a + b + 0.02, f"{a + b:.2f}", ha="center", fontsize=7)
        ax.axhline(1.0, ls=":", c="k", lw=0.8)
        ax.set_title(f"{mode} barrier, W={w} µs", fontsize=10)
        ax.tick_params(axis="x", labelsize=8)
        if j == 0:
            ax.set_ylabel("progress (1.0 = as fast as alone)")
axes[0][0].legend(fontsize=8, loc="upper left")
fig.suptitle("Who gets the CPU: worker (blue) vs competitor (grey); bar top = combined (1.0 = ideal time-sharing)", fontsize=10)
fig.tight_layout(); fig.savefig(os.path.join(run, "analysis", "progress.png"), dpi=150)
print("wrote", os.path.join(run, "analysis", "progress.png"))
