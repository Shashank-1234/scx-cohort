#!/usr/bin/env python3
"""Analyse a P10-01 result dir (scx_cohort evaluation). Writes RESULT_DIR/analysis/{table.md,summary.json,*.png}.
Normalisation within a rep (A0 / C0 of that rep), medians [min-max] over reps. Gates from the PREREG."""
import argparse
import glob
import json
import os
import statistics as st
from collections import defaultdict

import analyze_p05 as a5

MODES = ["spin", "adaptive", "block"]
W_US = [10, 100]
ARMS = ["E", "S", "Coff", "Ctick", "Cwake", "Ckick", "U10", "Ctick5", "Ctick20", "Ckick5", "Ckick20"]
COHORT = {"Coff", "Ctick", "Cwake", "Ckick", "Ctick5", "Ctick20", "Ckick5", "Ckick20"}


def cohort_window(rd, t0, t1):
    p = os.path.join(rd, "cohort_stats.jsonl")
    if not os.path.exists(p):
        return None
    rows = []
    for line in open(p):
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    before = [r for r in rows if r["mono_ns"] <= t0]
    upto = [r for r in rows if r["mono_ns"] <= t1]
    if not before or not upto:
        return None
    b, e = before[-1], upto[-1]
    dt = (e["mono_ns"] - b["mono_ns"]) / 1e9
    if dt <= 0:
        return None
    d = lambda k: e[k] - b[k]
    out = {"dt": dt, "co_frac": d("co_ns") / 1e9 / dt,
           "kicks_boundary_s": d("kick_boundary") / dt, "kicks_wake_s": d("kick_wake") / dt,
           "fallback_s": d("fallback") / dt, "tick_preempt_s": d("tick_preempt") / dt,
           "wake_no_target_s": d("wake_no_target") / dt, "tick_gaps": d("tick_gap_cnt"),
           "ev_dropped": d("ev_dropped")}
    out["wake_lat_us"] = d("wake_lat_sum") / d("wake_lat_cnt") / 1000 if d("wake_lat_cnt") else None
    out["bnd_lat_us"] = d("bnd_lat_sum") / d("bnd_lat_cnt") / 1000 if d("bnd_lat_cnt") else None
    out["bnd_lat_max_us"] = e["bnd_lat_max"] / 1000
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("result_dir")
    a = ap.parse_args()
    R = a.result_dir
    meta = json.load(open(os.path.join(R, "meta.json")))
    nthr = int(meta.get("threads", 4))
    runs = [json.loads(l) for l in open(os.path.join(R, "runs.jsonl"))]
    failed = [r for r in runs if r.get("failed")]
    per = {}
    for r in runs:
        if r.get("failed"):
            continue
        s0, s1, dt = r["s0"], r["s1"], r["measure_s"]
        m = {}
        if "rounds" in s0:
            m["rps"] = (s1["rounds"] - s0["rounds"]) / dt
            m["w_cpu"] = a5.cpu_s(s0, s1, "worker_tasks")
            m["w_share"] = m["w_cpu"] / (nthr * dt)
        if "comp_iters" in s0:
            m["c_ips"] = (sum(s1["comp_iters"]) - sum(s0["comp_iters"])) / dt
        if r["arm"] in COHORT:
            m["cohort"] = cohort_window(os.path.join(R, f"rep{r['rep']}", r["tag"]), s0["t_ns"], s1["t_ns"])
        per[(r["rep"], r["arm"], r["mode"], r["w_us"])] = m

    D = defaultdict(lambda: defaultdict(list))
    for (rep, arm, mode, w), m in per.items():
        if arm in ("A0", "C0"):
            continue
        a0, c0 = per.get((rep, "A0", mode, w)), per.get((rep, "C0", None, None))
        d = D[(arm, mode, w)]
        if a0 and a0.get("rps") and m.get("w_cpu"):
            wp = m["rps"] / a0["rps"]
            d["w_progress"].append(wp)
            d["eff"].append(wp / (m["w_cpu"] / a0["w_cpu"]))
            d["w_share"].append(m["w_share"])
        if c0 and c0.get("c_ips") and m.get("c_ips") is not None:
            d["c_progress"].append(m["c_ips"] / c0["c_ips"])
            if d["w_progress"]:
                d["combined"].append(d["w_progress"][-1] + d["c_progress"][-1])
        c = m.get("cohort")
        if c:
            for k in ("co_frac", "kicks_boundary_s", "kicks_wake_s", "fallback_s", "tick_preempt_s",
                      "wake_lat_us", "bnd_lat_us", "bnd_lat_max_us", "tick_gaps"):
                if c.get(k) is not None:
                    d[k].append(c[k])
    C = {f"{k[0]}/{k[1]}/{k[2]}": {m: a5.med(v) for m, v in d.items()} for k, d in D.items()}
    g = lambda arm, mode, w, k="eff": (C.get(f"{arm}/{mode}/{w}", {}).get(k) or (None,))[0]

    L = [f"# P10-01 analysis: {os.path.basename(os.path.abspath(R))}", "",
         f"Runs ok: {len(runs) - len(failed)}, failed: {len(failed)}. Cells: median [min–max] over reps.", "",
         "| Mode | W µs | Arm | Efficiency | Worker progress | CPU share | Competitor progress | Combined | Co-run frac | Wake→run µs | Edge→run µs (mean/max) | Kicks/s (edge, wake) | Fallback/s | Tick gaps |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for mode in MODES:
        for w in W_US:
            for arm in ARMS:
                c = C.get(f"{arm}/{mode}/{w}")
                if not c:
                    continue
                f = lambda k, n=2: a5.fmt(c.get(k, (None,) * 4), n)
                kick = f"{a5.fmt(c.get('kicks_boundary_s', (None,)*4), 0)}, {a5.fmt(c.get('kicks_wake_s', (None,)*4), 0)}"
                L.append(f"| {mode} | {w} | {arm} | {f('eff')} | {f('w_progress')} | {f('w_share')} | {f('c_progress')} | "
                         f"{f('combined')} | {f('co_frac', 3)} | {f('wake_lat_us', 0)} | {f('bnd_lat_us', 0)} / {f('bnd_lat_max_us', 0)} | "
                         f"{kick} | {f('fallback_s', 0)} | {f('tick_gaps', 0)} |")

    # Gates
    V = {}
    best = lambda mode, w, arms: max(((g(x, mode, w) or 0), x) for x in arms)
    h1 = []
    for w in W_US:
        ce, cx = best("spin", w, ["Ctick", "Cwake", "Ckick"])
        base = max(g("E", "spin", w) or 0, g("S", "spin", w) or 0)
        comp_ok = (g(cx, "spin", w, "c_progress") or 0) >= 0.9 * (g("E", "spin", w, "c_progress") or 1)
        co_ok = (g(cx, "spin", w, "co_frac") or 0) >= 2 * (g("Coff", "spin", w, "co_frac") or 1e9)
        ok = base > 0 and ce >= 1.5 * base and comp_ok and co_ok
        h1.append(ok)
        u = g("U10", "spin", w)
        V[f"H1 W={w}"] = (f"best {cx} eff {ce:.2f} vs max(E,S) {base:.2f} (×{ce / base if base else float('nan'):.2f}); "
                          f"competitor ok={comp_ok}; co-run {g(cx, 'spin', w, 'co_frac')} vs Coff {g('Coff', 'spin', w, 'co_frac')} ok={co_ok}; "
                          f"H1b vs U10 {ce / u if u else float('nan'):.2f}")
    V["H1"] = "supported" if any(h1) else "not supported"
    t10, k10 = g("Ctick", "spin", 10), g("Ckick", "spin", 10)
    def h2w(w):
        t, k = g("Ctick", "spin", w), g("Ckick", "spin", w)
        t20, k20 = g("Ctick20", "spin", w), g("Ckick20", "spin", w)
        t5, k5 = g("Ctick5", "spin", w), g("Ckick5", "spin", w)
        if None in (t, k, t20, k20, t5, k5):
            return None, "missing"
        close = abs(t - k) <= 0.1 * k and abs(t20 - k20) <= 0.1 * k20
        short = k5 >= 1.1 * t5
        return close and short, f"G10 tick {t:.2f} kick {k:.2f}; G20 {t20:.2f}/{k20:.2f}; G5 {t5:.2f}/{k5:.2f}"
    h2 = [h2w(w) for w in W_US]
    V["H2"] = ("supported" if any(x[0] for x in h2) else "not supported") + " — " + " | ".join(f"W={w}: {x[1]}" for w, x in zip(W_US, h2))
    h3 = []
    for w in W_US:
        ct, cw = g("Ctick", "adaptive", w), g("Cwake", "adaptive", w)
        lt, lw = g("Ctick", "adaptive", w, "wake_lat_us"), g("Cwake", "adaptive", w, "wake_lat_us")
        ok = (ct and cw and cw >= 1.1 * ct) or (lt and lw and lw <= 0.5 * lt)
        h3.append(bool(ok))
        V[f"H3 W={w}"] = f"eff tick {ct} wake {cw}; wake→run µs tick {lt} wake {lw}"
    V["H3"] = "supported" if any(h3) else "not supported"
    h4 = True
    for w in W_US:
        e, ec = g("E", "block", w), g("E", "block", w, "combined")
        for x in ("Ctick", "Cwake", "Ckick"):
            v, vc = g(x, "block", w), g(x, "block", w, "combined")
            if None in (e, v, ec, vc) or not (0.9 * e <= v <= 1.1 * e) or vc < 0.9 * ec:
                h4 = False
    V["H4"] = "supported" if h4 else "not supported"
    L += ["", "## Gates (PREREG)", ""] + [f"- **{k}**: {v}" for k, v in V.items()]
    L += ["", f"## Failed runs ({len(failed)})", ""] + [f"- rep{r['rep']} {r['tag']}: {'; '.join(r['notes'])}" for r in failed]
    L += ["", "## Context", "", f"- Kernel: {meta.get('uname')}", f"- cmdline: {meta.get('cmdline')}"]
    out = os.path.join(R, "analysis")
    os.makedirs(out, exist_ok=True)
    open(os.path.join(out, "table.md"), "w").write("\n".join(L) + "\n")
    json.dump({"cells": C, "gates": V, "failed": [r["tag"] for r in failed]}, open(os.path.join(out, "summary.json"), "w"),
              indent=1, default=str)
    print("\n".join(L))
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, axes = plt.subplots(1, 3, figsize=(13, 3.8), sharey=True)
        show = ["E", "S", "Coff", "Ctick", "Cwake", "Ckick", "U10"]
        for ax, mode in zip(axes, MODES):
            for i, arm in enumerate(show):
                xs, ys, lo, hi = [], [], [], []
                for j, w in enumerate(W_US):
                    c = C.get(f"{arm}/{mode}/{w}", {}).get("eff")
                    if c and c[0] is not None:
                        xs.append(j + (i - 3) * 0.1); ys.append(c[0]); lo.append(c[0] - c[1]); hi.append(c[2] - c[0])
                if xs:
                    ax.errorbar(xs, ys, yerr=[lo, hi], fmt="o", capsize=2, label=arm)
            ax.axhline(1.0, color="gray", lw=0.8, ls=":")
            ax.set_xticks(range(len(W_US))); ax.set_xticklabels([f"{w} µs" for w in W_US])
            ax.set_title(f"{mode} barrier"); ax.set_xlabel("work per round")
        axes[0].set_ylabel("efficiency (1.0 = as alone)")
        axes[0].legend(fontsize=7, ncol=2)
        fig.tight_layout(); fig.savefig(os.path.join(out, "efficiency.png"), dpi=150)
    except Exception as e:
        print(f"figure skipped: {e!r}")


if __name__ == "__main__":
    main()
