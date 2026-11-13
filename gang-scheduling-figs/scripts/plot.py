#!/usr/bin/env python3
"""Figures from data/derived/*.csv (written by analyze.py from raw data). No measured value is typed here.

  uv run --offline --no-project --with matplotlib python scripts/plot.py [--derived DIR] [--out DIR] [--tag pilot]

Outputs (pdf/png/svg): figure2_combined, figure2a_interval, figure2b_variance,
figure2_supp_progress (normalised progress, separate from the primary figure),\nfigure2_supp_paired (EXPLORATORY within-rep ratios), preemption_ablation.
Points: mean over successful repetitions; bars: min-max (as in the paper's Figure 2).
"""
import argparse
import csv
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Categorical slots 1-3 of the dataviz reference palette (validated all-pairs, light mode) + gray reference.
STYLE = {
    "EEVDF": dict(color="#2a78d6", marker="o", label="EEVDF (Linux default)"),
    "Ctick": dict(color="#eb6834", marker="s", label="scx_cohort Ctick (tick-only windows)"),
    "Ckick": dict(color="#1baf7a", marker="^", label="scx_cohort Ckick (+ boundary & wake kicks)"),
    "A0": dict(color="#8a8984", marker=None, label="A alone (EEVDF), reference", ls=(0, (4, 2)), lw=1.2),
}
ORDER = ["A0", "EEVDF", "Ctick", "Ckick"]
DODGE = {"EEVDF": 0.93, "Ctick": 1.0, "Ckick": 1.075, "A0": 1.0}
XLAB = {"interval": "Synchronization interval (µs of computation per round)",
        "variance": "Maximum synchronization variance (µs)"}
TITLE = {"interval": "(a) Interval sweep (variance 0)", "variance": "(b) Variance sweep (interval 0)"}

plt.rcParams.update({"font.size": 8, "axes.titlesize": 8, "axes.labelsize": 8, "legend.fontsize": 7,
                     "xtick.labelsize": 7, "ytick.labelsize": 7, "font.family": "DejaVu Sans",
                     "axes.spines.top": False, "axes.spines.right": False, "axes.edgecolor": "#52514e",
                     "axes.linewidth": 0.6, "xtick.color": "#52514e", "ytick.color": "#52514e",
                     "grid.color": "#e4e3df", "grid.linewidth": 0.5, "svg.fonttype": "none", "pdf.fonttype": 42})


def load(path):
    with open(path) as f:
        return list(csv.DictReader(f))


def fl(v):
    return float(v) if v not in ("", None) else None


def series(summ, exp, arm, metric):
    pts = []
    for r in summ:
        if r["experiment"] == exp and r["arm"] == arm and fl(r[f"{metric}_mean"]) is not None:
            pts.append((float(r["x_us"]), fl(r[f"{metric}_mean"]), fl(r[f"{metric}_min"]), fl(r[f"{metric}_max"]),
                        int(r["n_ok"])))
    return sorted(pts)


def panel(ax, summ, exp, metric="rounds_per_second", log_y=True, arms=ORDER, ylabel="Loop iterations per second"):
    nmin = None
    for arm in arms:
        pts = series(summ, exp, arm, metric)
        if not pts:
            continue
        if log_y:
            assert all(p[1] > 0 and p[2] > 0 for p in pts), f"non-positive value on a log axis: {exp} {arm}"
        s = STYLE[arm]
        xs = [p[0] * DODGE[arm] for p in pts]
        ys = [p[1] for p in pts]
        nmin = min([nmin or 99] + [p[4] for p in pts])
        if arm == "A0":
            ax.plot(xs, ys, color=s["color"], ls=s["ls"], lw=s["lw"], zorder=1)
            continue
        ax.errorbar(xs, ys, yerr=[[p[1] - p[2] for p in pts], [p[3] - p[1] for p in pts]], color=s["color"],
                    marker=s["marker"], ms=4.5, lw=1.4, elinewidth=0.9, capsize=2, capthick=0.9,
                    markeredgecolor="white", markeredgewidth=0.6, zorder=3)
    ax.set_xscale("log")
    if log_y:
        ax.set_yscale("log")
    ax.grid(True, which="major")
    ax.set_xlabel(XLAB[exp])
    ax.set_ylabel(ylabel)
    return nmin


def handles(arms=ORDER):
    out = []
    for arm in arms:
        s = STYLE[arm]
        out.append(Line2D([], [], color=s["color"], marker=s["marker"], ls=s.get("ls", "-"), lw=s.get("lw", 1.4),
                          ms=4.5, markeredgecolor="white", label=s["label"]))
    return out


def save(fig, out, name):
    for ext in ("pdf", "png", "svg"):
        fig.savefig(os.path.join(out, f"{name}.{ext}"), dpi=300 if ext == "png" else None, bbox_inches="tight")
    plt.close(fig)


def fig2(summ, out, tag):
    n = []
    fig, axes = plt.subplots(2, 1, figsize=(3.45, 5.0))
    for ax, exp in zip(axes, ("interval", "variance")):
        n.append(panel(ax, summ, exp))
        ax.set_title(TITLE[exp], loc="left")
    fig.legend(handles=handles(), loc="lower center", ncol=1, frameon=False, bbox_to_anchor=(0.55, -0.02))
    fig.tight_layout(rect=(0, 0.13, 1, 1), h_pad=1.2)
    save(fig, out, f"figure2_combined{tag}")
    for exp, name in (("interval", "figure2a_interval"), ("variance", "figure2b_variance")):
        fig, ax = plt.subplots(figsize=(3.45, 2.9))
        panel(ax, summ, exp)
        ax.set_title(TITLE[exp], loc="left")
        ax.legend(handles=handles(), loc="upper center", bbox_to_anchor=(0.5, -0.27), frameon=False, ncol=1)
        fig.tight_layout()
        save(fig, out, f"{name}{tag}")
    return min(x for x in n if x is not None)


def supp(summ, out, tag):
    fig, axes = plt.subplots(2, 2, figsize=(7.0, 4.4), sharey=True)
    for i, exp in enumerate(("interval", "variance")):
        for j, (metric, yl) in enumerate((("worker_progress", "Target A progress\n(÷ A alone, same rep)"),
                                          ("competitor_progress", "Competitor B progress\n(÷ B alone, same rep)"))):
            ax = axes[i][j]
            panel(ax, summ, exp, metric, log_y=False, arms=["EEVDF", "Ctick", "Ckick"], ylabel=yl if j == 0 else "")
            ax.axhline(0.5, color="#8a8984", lw=0.8, ls=(0, (4, 2)), zorder=0)
            ax.set_ylim(-0.03, 1.08)
            ax.set_title(f"{TITLE[exp]}: {'target A' if j == 0 else 'competitor B'}", loc="left")
            if j == 1:
                ax.set_ylabel(yl)
                ax.yaxis.set_tick_params(labelleft=True)
    fig.legend(handles=handles(["EEVDF", "Ctick", "Ckick"]), loc="lower center", ncol=3, frameon=False,
               bbox_to_anchor=(0.5, -0.01))
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    save(fig, out, f"figure2_supp_progress{tag}")


def paired_fig(pp, out, tag):
    """EXPLORATORY: within-rep ratio Cx / EEVDF (median, min-max over reps)."""
    fig, axes = plt.subplots(1, 2, figsize=(7.0, 2.5), sharey=True)
    for ax, exp in zip(axes, ("interval", "variance")):
        for arm in ("Ctick", "Ckick"):
            pts = sorted((float(r["x_us"]), fl(r["ratio_vs_eevdf_median"]), fl(r["ratio_vs_eevdf_min"]),
                          fl(r["ratio_vs_eevdf_max"])) for r in pp if r["experiment"] == exp and r["arm"] == arm)
            s = STYLE[arm]
            ax.errorbar([p[0] * DODGE[arm] for p in pts], [p[1] for p in pts],
                        yerr=[[p[1] - p[2] for p in pts], [p[3] - p[1] for p in pts]], color=s["color"],
                        marker=s["marker"], ms=4.5, lw=1.4, elinewidth=0.9, capsize=2,
                        markeredgecolor="white", markeredgewidth=0.6)
        ax.axhline(1.0, color=STYLE["EEVDF"]["color"], lw=0.9, ls=(0, (4, 2)), zorder=0)
        ax.set_xscale("log"); ax.set_yscale("log")
        ax.grid(True, which="major")
        ax.set_xlabel(XLAB[exp])
        ax.set_title(TITLE[exp], loc="left")
    axes[0].set_ylabel("Target A rounds/s ÷ EEVDF\n(same rep; median, min–max)")
    h = handles(["Ctick", "Ckick"]) + [Line2D([], [], color=STYLE["EEVDF"]["color"], ls=(0, (4, 2)), label="EEVDF = 1")]
    fig.legend(handles=h, loc="lower center", ncol=3, frameon=False, bbox_to_anchor=(0.5, -0.06))
    fig.tight_layout(rect=(0, 0.08, 1, 1))
    save(fig, out, f"figure2_supp_paired{tag}")


def preemption(ps, out):
    sty = {("Ctick", "aligned"): dict(color=STYLE["Ctick"]["color"], marker="s", ls="-", mfc=STYLE["Ctick"]["color"]),
           ("Ctick", "skewed"): dict(color=STYLE["Ctick"]["color"], marker="s", ls=(0, (3, 1.5)), mfc="white"),
           ("Ckick", "aligned"): dict(color=STYLE["Ckick"]["color"], marker="^", ls="-", mfc=STYLE["Ckick"]["color"]),
           ("Ckick", "skewed"): dict(color=STYLE["Ckick"]["color"], marker="^", ls=(0, (3, 1.5)), mfc="white")}
    lab = {("Ctick", "aligned"): "Ctick, aligned ticks", ("Ctick", "skewed"): "Ctick, skew_tick=1",
           ("Ckick", "aligned"): "Ckick, aligned ticks", ("Ckick", "skewed"): "Ckick, skew_tick=1"}
    off = {("Ctick", "aligned"): -0.15, ("Ctick", "skewed"): -0.05, ("Ckick", "aligned"): 0.05, ("Ckick", "skewed"): 0.15}
    gs = [5, 10, 20]
    fig, axes = plt.subplots(1, 2, figsize=(7.0, 2.4), sharey=True)
    for ax, w in zip(axes, (10, 100)):
        for key, s in sty.items():
            pts = []
            for g in gs:
                r = [x for x in ps if x["variant"] == key[0] and x["ticks"] == key[1] and int(x["G_ms"]) == g
                     and int(x["W_us"]) == w]
                if r and fl(r[0]["efficiency_mean"]) is not None:
                    pts.append((gs.index(g) + off[key], fl(r[0]["efficiency_mean"]), fl(r[0]["efficiency_min"]),
                                fl(r[0]["efficiency_max"])))
            ax.errorbar([p[0] for p in pts], [p[1] for p in pts], yerr=[[p[1] - p[2] for p in pts], [p[3] - p[1] for p in pts]],
                        color=s["color"], marker=s["marker"], ls=s["ls"], mfc=s["mfc"], ms=5, lw=1.3,
                        elinewidth=0.9, capsize=2, label=lab[key])
        ax.axhline(1.0, color="#8a8984", lw=0.8, ls=(0, (4, 2)), zorder=0)
        ax.set_xticks(range(3)); ax.set_xticklabels([f"{g} ms" for g in gs])
        ax.set_xlabel("Window length G = T")
        ax.set_title(f"Spin barrier, {w} µs work per round", loc="left")
        ax.grid(True, axis="y")
    axes[0].set_ylabel("Worker efficiency\n(progress ÷ CPU share, 1 = as alone)")
    fig.legend(*axes[0].get_legend_handles_labels(), loc="lower center", ncol=4, frameon=False, bbox_to_anchor=(0.5, -0.04))
    fig.tight_layout(rect=(0, 0.08, 1, 1))
    save(fig, out, "preemption_ablation")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--derived", default=os.path.join(ROOT, "data", "derived"))
    ap.add_argument("--out", default=os.path.join(ROOT, "figures"))
    ap.add_argument("--tag", default="")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    tag = f"_{a.tag}" if a.tag else ""
    summ = load(os.path.join(a.derived, "summary.csv"))
    nmin = fig2(summ, a.out, tag)
    supp(summ, a.out, tag)
    pq = os.path.join(a.derived, "paired_summary.csv")
    if os.path.exists(pq):
        paired_fig(load(pq), a.out, tag)
    pp = os.path.join(a.derived, "preemption_summary.csv")
    if not a.tag and os.path.exists(pp):
        preemption(load(pp), a.out)
    print(f"figures -> {a.out} (min reps per plotted point: {nmin})")


if __name__ == "__main__":
    main()
