#!/usr/bin/env python3
"""P10-02: compare tick-only (Ctick*) vs boundary-kick (Ckick*) windows with aligned ticks vs skew_tick=1.
Usage: analyze_p10_skew.py ALIGNED_RUN_DIR SKEW_RUN_DIR [--out DIR]
Runs analyze_p10.py on each dir if needed, then writes OUT/{compare.md,compare.json,skew.png}.
Gates as in experiments/2026-11-04_P10-02_skew-tick/PREREG.md."""
import argparse, json, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
G = {5: ("Ctick5", "Ckick5"), 10: ("Ctick", "Ckick"), 20: ("Ctick20", "Ckick20")}
WS = [10, 100]
KEYS = ["eff", "w_progress", "c_progress", "co_frac", "bnd_lat_us", "kicks_boundary_s", "fallback_s"]


def cells(run):
    p = os.path.join(run, "analysis", "summary.json")
    if not os.path.exists(p):
        subprocess.run([sys.executable, os.path.join(HERE, "analyze_p10.py"), run], check=True, stdout=subprocess.DEVNULL)
    return json.load(open(p))["cells"]


def med(c, arm, w, k):
    v = c.get(f"{arm}/spin/{w}", {}).get(k)
    return v[0] if v and v[0] is not None else None


def cmdline(run):
    try:
        return json.load(open(os.path.join(run, "meta.json"))).get("cmdline", "")
    except OSError:
        return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("aligned"); ap.add_argument("skew"); ap.add_argument("--out")
    a = ap.parse_args()
    out = a.out or os.path.join(a.skew, "analysis")
    os.makedirs(out, exist_ok=True)
    ca, cs = cells(a.aligned), cells(a.skew)
    warn = []
    if "skew_tick=1" in cmdline(a.aligned):
        warn.append("ALIGNED run has skew_tick=1 in its cmdline")
    if "skew_tick=1" not in cmdline(a.skew):
        warn.append("SKEW run lacks skew_tick=1 in its cmdline")
    rows, res = [], {"aligned": a.aligned, "skew": a.skew, "warnings": warn, "g": {}}
    for w in WS:
        for g, (t, k) in G.items():
            r = {}
            for tag, c in (("al", ca), ("sk", cs)):
                for arm, nm in ((t, "tick"), (k, "kick")):
                    for key in KEYS:
                        r[f"{nm}_{key}_{tag}"] = med(c, arm, w, key)
            f = lambda x, y: (x / y) if (x is not None and y) else None
            r["tick_skew_over_al"] = f(r["tick_eff_sk"], r["tick_eff_al"])
            r["kick_over_tick_al"] = f(r["kick_eff_al"], r["tick_eff_al"])
            r["kick_over_tick_sk"] = f(r["kick_eff_sk"], r["tick_eff_sk"])
            res["g"][f"{w}/{g}"] = r
            rows.append((w, g, r))
    g5 = [res["g"][f"{w}/5"]["tick_skew_over_al"] for w in WS]
    k1 = all(x is not None and x <= 0.97 for x in g5)
    k2v = [(r["kick_over_tick_sk"] - r["kick_over_tick_al"]) if None not in (r["kick_over_tick_sk"], r["kick_over_tick_al"]) else None
           for _, _, r in rows]
    k2 = all(x is not None and x >= 0.05 for x in k2v)
    k3v = [(r["kick_fallback_s_sk"] / r["kick_fallback_s_al"]) if r["kick_fallback_s_sk"] is not None and r["kick_fallback_s_al"] else None
           for _, _, r in rows]
    e1 = all(x is not None and x <= 0.5 for x in k3v)
    res["gates"] = {"K1": k1, "K2": k2, "E1": e1, "K1_vals": g5, "K2_vals": k2v, "E1_vals": k3v}
    json.dump(res, open(os.path.join(out, "compare.json"), "w"), indent=1)

    fmt = lambda x, n=2: "n/a" if x is None else f"{x:.{n}f}"
    L = [f"# P10-02: aligned ticks vs skew_tick=1 (spin barrier)", "",
         f"aligned: `{a.aligned}`  ", f"skew: `{a.skew}`", ""]
    L += [f"**WARNING: {x}**" for x in warn]
    L += ["| W µs | G ms | eff tick al→sk | eff kick al→sk | tick sk/al | kick÷tick al→sk | co-run tick al→sk | edge→run µs tick al→sk | edge→run µs kick al→sk | kick fallback/s al→sk |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for w, g, r in rows:
        L.append(f"| {w} | {g} | {fmt(r['tick_eff_al'])}→{fmt(r['tick_eff_sk'])} | {fmt(r['kick_eff_al'])}→{fmt(r['kick_eff_sk'])} | "
                 f"{fmt(r['tick_skew_over_al'], 3)} | {fmt(r['kick_over_tick_al'])}→{fmt(r['kick_over_tick_sk'])} | "
                 f"{fmt(r['tick_co_frac_al'], 3)}→{fmt(r['tick_co_frac_sk'], 3)} | {fmt(r['tick_bnd_lat_us_al'], 0)}→{fmt(r['tick_bnd_lat_us_sk'], 0)} | "
                 f"{fmt(r['kick_bnd_lat_us_al'], 0)}→{fmt(r['kick_bnd_lat_us_sk'], 0)} | {fmt(r['kick_fallback_s_al'], 0)}→{fmt(r['kick_fallback_s_sk'], 0)} |")
    L += ["", "## Gates (PREREG)", "",
          f"- **K1** skew hurts tick-only at G=5 (Ctick5 skew/aligned ≤ 0.97 at both W): {'supported' if k1 else 'not supported'} — {[fmt(x, 3) for x in g5]}",
          f"- **K2** kick gains ≥ 0.05 (kick÷tick) under skew vs aligned at every G and W: {'supported' if k2 else 'not supported'} — {[fmt(x, 3) for x in k2v]}",
          f"- **E1** (exploratory) Ckick fallbacks/s under skew ≤ 0.5 × aligned: {'supported' if e1 else 'not supported'} — {[fmt(x, 2) for x in k3v]}"]
    open(os.path.join(out, "compare.md"), "w").write("\n".join(L) + "\n")

    import matplotlib; matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axs = plt.subplots(1, 2, figsize=(10, 3.8), sharey=True)
    for ax, w in zip(axs, WS):
        gs = sorted(G)
        for nm, col in (("tick", "#2f6db5"), ("kick", "#e08a1e")):
            for tag, ls, lab in (("al", "-", "aligned ticks"), ("sk", "--", "skew_tick=1")):
                ys = [res["g"][f"{w}/{g}"][f"{nm}_eff_{tag}"] for g in gs]
                ax.plot(gs, [y if y is not None else float("nan") for y in ys], ls, marker="o", color=col,
                        label=f"C{nm} ({lab})")
        ax.axhline(1.0, ls=":", c="k", lw=0.8)
        ax.set_xticks(sorted(G)); ax.set_xlabel("window G = T (ms)"); ax.set_title(f"spin barrier, W={w} µs")
    axs[0].set_ylabel("efficiency (1.0 = as alone)"); axs[0].legend(fontsize=8)
    fig.tight_layout(); fig.savefig(os.path.join(out, "skew.png"), dpi=150)
    print("\n".join(L))


if __name__ == "__main__":
    main()
