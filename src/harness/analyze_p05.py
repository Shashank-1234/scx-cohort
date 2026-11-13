#!/usr/bin/env python3
"""Analyse a P0.5 result dir: per-config medians, efficiency, progress, gate verdict.

  analyze_p05.py RESULT_DIR [--fig out.png]

Reads RESULT_DIR/{meta,calibration}.json, runs.jsonl, rep*/rep.json, rep*/*/gangsim.csv.
Writes RESULT_DIR/analysis/{table.md,summary.json} (+ figure). Never edits raw files.
Normalisation is within a rep (A0 and C0 of the same rep), then medians over reps.
"""
import argparse
import csv
import glob
import json
import os
import statistics as st
from collections import defaultdict

MODES = ["spin", "block", "adaptive"]
W_US = [10, 100, 1000]
ARMS = ["A0", "E", "U10", "U40"]


def cpu_s(s0, s1, key):
    a, b = s0.get(key, {}), s1.get(key, {})
    return sum(b[t]["run_ns"] - a[t]["run_ns"] for t in b if t in a) / 1e9


def steal_frac(s0, s1, cpus):
    num = den = 0
    for c in cpus:
        k = f"cpu{c}"
        if k in s0["stat"] and k in s1["stat"]:
            d = [y - x for x, y in zip(s0["stat"][k], s1["stat"][k])]
            num += d[7] if len(d) > 7 else 0
            den += sum(d[:8])
    return num / den if den else None


def gangsim_stats(path):
    if not os.path.exists(path):
        return None
    late, kill = [], []
    with open(path) as f:
        rows = [r for r in csv.reader(l for l in f if not l.startswith("#"))][1:]
    for r in rows:
        tgt, wake, after = int(r[2]), int(r[3]), int(r[4])
        late.append((wake - tgt) / 1000.0)
        kill.append((after - wake) / 1000.0)
    if not late:
        return None
    q = lambda xs, p: sorted(xs)[min(len(xs) - 1, int(p * len(xs)))]
    return {"n": len(late), "late_us_p50": q(late, 0.5), "late_us_p99": q(late, 0.99),
            "kill_us_p50": q(kill, 0.5), "kill_us_p99": q(kill, 0.99)}


def med(xs):
    xs = [x for x in xs if x is not None]
    return (st.median(xs), min(xs), max(xs), len(xs)) if xs else (None, None, None, 0)


def fmt(t, d=2):
    m, lo, hi, n = t
    return "n/a" if m is None else f"{m:.{d}f} [{lo:.{d}f}–{hi:.{d}f}]"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("result_dir")
    ap.add_argument("--fig", default=None)
    a = ap.parse_args()
    R = a.result_dir
    meta = json.load(open(os.path.join(R, "meta.json")))
    nthr = int(meta.get("threads", 4))
    cpus = []
    for tok in meta.get("cpus", "1-4").split(","):
        lo, _, hi = tok.partition("-")
        cpus += list(range(int(lo), int(hi or lo) + 1))
    runs = [json.loads(l) for l in open(os.path.join(R, "runs.jsonl"))]
    failed = [r for r in runs if r.get("failed")]
    ok = [r for r in runs if not r.get("failed")]

    per = {}  # (rep, arm, mode, w) -> metrics
    for r in ok:
        s0, s1, dt = r["s0"], r["s1"], r["measure_s"]
        m = {"dt": dt, "steal": steal_frac(s0, s1, cpus)}
        if "rounds" in s0:
            m["rps"] = (s1["rounds"] - s0["rounds"]) / dt
            m["w_cpu"] = cpu_s(s0, s1, "worker_tasks")
            m["w_share"] = m["w_cpu"] / (nthr * dt)
        if "comp_iters" in s0:
            m["c_ips"] = (sum(s1["comp_iters"]) - sum(s0["comp_iters"])) / dt
            m["c_cpu"] = cpu_s(s0, s1, "comp_tasks")
        if r["arm"] in ("U10", "U40"):
            m["gangsim"] = gangsim_stats(os.path.join(R, f"rep{r['rep']}", r["tag"], "gangsim.csv"))
        per[(r["rep"], r["arm"], r["mode"], r["w_us"])] = m

    reps = sorted({k[0] for k in per})
    derived = defaultdict(lambda: defaultdict(list))  # (arm,mode,w) -> metric -> [per rep]
    for (rep, arm, mode, w), m in per.items():
        if arm in ("A0", "C0"):
            continue
        a0 = per.get((rep, "A0", mode, w))
        c0 = per.get((rep, "C0", None, None))
        d = derived[(arm, mode, w)]
        d["rps"].append(m.get("rps"))
        d["w_share"].append(m.get("w_share"))
        d["steal"].append(m.get("steal"))
        if a0 and a0.get("rps") and m.get("w_cpu"):
            wp = m["rps"] / a0["rps"]
            d["w_progress"].append(wp)
            d["eff"].append(wp / (m["w_cpu"] / a0["w_cpu"]))
        if c0 and c0.get("c_ips") and m.get("c_ips") is not None:
            cp = m["c_ips"] / c0["c_ips"]
            d["c_progress"].append(cp)
            if d["w_progress"]:
                d["combined"].append(d["w_progress"][-1] + cp)
        if m.get("gangsim"):
            d["late_p99"].append(m["gangsim"]["late_us_p99"])
            d["kill_p99"].append(m["gangsim"]["kill_us_p99"])
    a0_rps = defaultdict(list)
    for (rep, arm, mode, w), m in per.items():
        if arm == "A0":
            a0_rps[(mode, w)].append(m.get("rps"))

    lines = [f"# P0.5 analysis: {os.path.basename(os.path.abspath(R))}", "",
             f"Reps: {reps}. Runs ok: {len(ok)}, failed: {len(failed)}. Cells: median [min–max] over reps.", "",
             "| Mode | W µs | A0 rounds/s | Arm | Efficiency | Worker progress | Worker CPU share | Competitor progress | Combined | Steal | gangsim late p99 µs |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    summary = {"cells": {}}
    for mode in MODES:
        for w in W_US:
            a0 = med(a0_rps[(mode, w)])
            for arm in ARMS[1:]:
                d = derived.get((arm, mode, w))
                if not d:
                    continue
                cell = {k: med(v) for k, v in d.items()}
                summary["cells"][f"{arm}/{mode}/{w}"] = {k: v[0] for k, v in cell.items()} | {
                    "eff_min": cell["eff"][1] if "eff" in cell else None,
                    "eff_max": cell["eff"][2] if "eff" in cell else None}
                lines.append(f"| {mode} | {w} | {fmt(a0, 0)} | {arm} | {fmt(cell.get('eff', (None,)*4))} | "
                             f"{fmt(cell.get('w_progress', (None,)*4))} | {fmt(cell.get('w_share', (None,)*4))} | "
                             f"{fmt(cell.get('c_progress', (None,)*4))} | {fmt(cell.get('combined', (None,)*4))} | "
                             f"{fmt(cell.get('steal', (None,)*4), 3)} | {fmt(cell.get('late_p99', (None,)*4), 0)} |")

    # Gate (fixed in PREREG.md before the first measured run)
    C = summary["cells"]
    g = lambda arm, mode, w, k="eff": (C.get(f"{arm}/{mode}/{w}") or {}).get(k)
    verdict, why = None, []
    e_spin = {w: g("E", "spin", w) for w in W_US}
    if all(v is not None and v >= 0.8 for v in e_spin.values()):
        verdict = "NO PROBLEM ON VM"
        why.append(f"E-spin efficiency >= 0.8 at every W: {e_spin}")
    else:
        collapse = [w for w in (10, 100) if e_spin.get(w) is not None and e_spin[w] <= 0.5]
        go_w = []
        for w in collapse:
            e = e_spin[w]
            best = max((arm for arm in ("U10", "U40") if g(arm, "spin", w) is not None),
                       key=lambda arm: g(arm, "spin", w), default=None)
            if best is None:
                continue
            u = g(best, "spin", w)
            ucomb, ecomb = g(best, "spin", w, "combined"), g("E", "spin", w, "combined")
            overlap = (C[f"{best}/spin/{w}"]["eff_min"] or 0) <= (C[f"E/spin/{w}"]["eff_max"] or 0)
            share_mis = None
            us, es = g(best, "spin", w, "w_share"), g("E", "spin", w, "w_share")
            if us and es:
                share_mis = abs(us - es) / es
            ok_w = u >= 1.5 * e and ucomb is not None and ecomb is not None and ucomb >= ecomb - 0.05
            why.append(f"W={w}: E eff {e:.2f}, best U ({best}) eff {u:.2f} (ratio {u / e:.2f}), "
                       f"combined U {ucomb} vs E {ecomb}, rep ranges overlap={overlap}, "
                       f"worker-share mismatch={share_mis if share_mis is None else round(share_mis, 3)}")
            if ok_w and not overlap:
                go_w.append(w)
        if go_w:
            narrowed = all((g("E", m, w) or 0) >= 0.8 for m in ("block", "adaptive") for w in go_w)
            verdict = "GO, NARROWED (spinning barriers)" if narrowed else "GO"
        elif collapse:
            verdict = "INCONCLUSIVE (collapse present, userspace windows did not clear the bar; not a no-go)"
        else:
            verdict = "WEAK PROBLEM (E-spin efficiency between 0.5 and 0.8; not a GO by the pre-registered rule)"
            why.append(f"E-spin efficiency: {e_spin}")
    summary["verdict"], summary["why"] = verdict, why
    summary["failed_runs"] = [{"rep": r["rep"], "tag": r["tag"], "notes": r["notes"]} for r in failed]

    # Platform context
    speeds = []
    for rj in sorted(glob.glob(os.path.join(R, "rep*", "rep.json"))):
        h = json.load(open(rj))
        for k in ("speed_before", "speed_after"):
            if k in h:
                v = [x["iters_per_us"] for x in h[k].values()]
                speeds.append((os.path.basename(os.path.dirname(rj)), k, min(v), max(v), max(v) / min(v) - 1))
    summary["speed_spread"] = speeds
    lines += ["", f"## Verdict: **{verdict}**", ""] + [f"- {x}" for x in why]
    lines += ["", "## Context", "", f"- Kernel: {meta.get('uname')}", f"- HZ/NO_HZ: {meta.get('config_hz')}",
              f"- cmdline: {meta.get('cmdline')}", f"- Failed runs: {len(failed)} {summary['failed_runs'] if failed else ''}"]
    lines += [f"- vCPU speed spread {r} {k}: {lo:.0f}–{hi:.0f} iters/µs ({sp * 100:.1f} %)" for r, k, lo, hi, sp in speeds]
    lines += ["", "Userspace windows are strict and non-work-conserving (CPUs idle in G when workers block). "
              "A userspace loss is inconclusive, not evidence against gang scheduling."]
    out = os.path.join(R, "analysis")
    os.makedirs(out, exist_ok=True)
    open(os.path.join(out, "table.md"), "w").write("\n".join(lines) + "\n")
    json.dump(summary, open(os.path.join(out, "summary.json"), "w"), indent=1, default=str)
    print("\n".join(lines))

    fig = a.fig or os.path.join(out, "efficiency.png")
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        f, axes = plt.subplots(1, 3, figsize=(12, 3.6), sharey=True)
        for ax, mode in zip(axes, MODES):
            for i, arm in enumerate(ARMS[1:]):
                ys, lo, hi, xs = [], [], [], []
                for j, w in enumerate(W_US):
                    c = C.get(f"{arm}/{mode}/{w}")
                    if c and c.get("eff") is not None:
                        xs.append(j + (i - 1) * 0.22); ys.append(c["eff"])
                        lo.append(c["eff"] - c["eff_min"]); hi.append(c["eff_max"] - c["eff"])
                if xs:
                    ax.errorbar(xs, ys, yerr=[lo, hi], fmt="o", capsize=3, label=arm)
            ax.axhline(1.0, color="gray", lw=0.8, ls=":")
            ax.set_xticks(range(len(W_US))); ax.set_xticklabels([f"{w} µs" for w in W_US])
            ax.set_title(f"{mode} barrier"); ax.set_xlabel("work per round")
        axes[0].set_ylabel("efficiency (1.0 = as alone)")
        axes[0].legend(fontsize=8)
        f.tight_layout(); f.savefig(fig, dpi=150)
        print(f"figure: {fig}")
    except Exception as e:  # figure is optional
        print(f"figure skipped: {e!r}")


if __name__ == "__main__":
    main()
