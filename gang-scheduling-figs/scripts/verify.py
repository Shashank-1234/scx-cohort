#!/usr/bin/env python3
"""Independent checks of the derived data and figures (stdlib only). Writes data/derived/verify.json.

Recomputes rates straight from raw runs.jsonl with separate code (not analyze.py's functions), compares
with summary.csv, checks completeness, positivity, legends vs. scheduler commands, kernels, and that the
raw files still match the hashes recorded in data/manifest.json.
"""
import csv
import glob
import hashlib
import json
import os
import random
import statistics

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D = os.path.join(ROOT, "data", "derived")


def main():
    res = {}
    summ = list(csv.DictReader(open(os.path.join(D, "summary.csv"))))
    runs = list(csv.DictReader(open(os.path.join(D, "runs.csv"))))
    man = json.load(open(os.path.join(ROOT, "data", "manifest.json")))
    raw = {}
    for src in man["figure2_sources"]:
        p = os.path.join(ROOT, src["run_dir"], "runs.jsonl")
        h = hashlib.sha256(open(p, "rb").read()).hexdigest()
        res.setdefault("raw_hash_matches_manifest", []).append({src["run_dir"]: h == src["runs_jsonl_sha256"]})
        for line in open(p):
            r = json.loads(line)
            raw[(r["exp"], r["arm"], r["x_us"], r["rep"])] = r

    # 1. independent recompute of rounds/s mean, min, max for every (exp, arm, x)
    groups = {}
    for (e, arm, x, rep), r in raw.items():
        if r["failed"] or arm == "B0":
            continue
        dt_s = (r["s1"]["t_ns"] - r["s0"]["t_ns"]) * 1e-9
        assert abs(dt_s - r["measure_s"]) < 1e-9
        groups.setdefault((e, arm, x), []).append((r["s1"]["A_rounds"] - r["s0"]["A_rounds"]) / dt_s)
    worst = 0.0
    for s in summ:
        if s["arm"] == "B0":
            continue
        v = groups[(s["experiment"], s["arm"], int(float(s["x_us"])))]
        for name, f in (("mean", statistics.fmean), ("min", min), ("max", max), ("median", statistics.median)):
            rel = abs(f(v) - float(s[f"rounds_per_second_{name}"])) / f(v)
            worst = max(worst, rel)
        assert int(s["n_ok"]) == len(v)
    res["recompute_rps_worst_rel_err"] = worst
    sample = random.Random(1).sample(sorted(groups), 5)
    res["recompute_examples"] = [{"point": list(k), "n": len(groups[k]), "mean": statistics.fmean(groups[k]),
                                  "min": min(groups[k]), "max": max(groups[k])} for k in sample]

    # 2. completeness: every (exp, arm, x) has the same set of reps; no duplicates
    reps = sorted({r for (_, _, _, r) in raw})
    keys = {(e, a, x) for (e, a, x, _) in raw}
    res["repetitions"] = reps
    res["missing"] = [[e, a, x, r] for (e, a, x) in keys for r in reps if (e, a, x, r) not in raw]
    res["duplicates_in_runs_csv"] = len(runs) - len({(r["experiment"], r["arm"], r["x_us"], r["repetition"]) for r in runs})
    res["failed_runs"] = [k for k, r in raw.items() if r["failed"]]

    # 3. positivity for the log axes
    res["nonpositive_plotted"] = [s for s in summ if s["arm"] != "B0" and not float(s["rounds_per_second_min"]) > 0]

    # 4. legend vs command: arms -> distinct scheduler commands actually run
    cmds = {}
    for (e, arm, x, rep), r in raw.items():
        c = " ".join([os.path.basename(r["sched_cmd"][0])] + [t for t in r["sched_cmd"][1:] if not t.endswith(".jsonl")]) \
            if r.get("sched_cmd") else "none (EEVDF)"
        cmds.setdefault(arm, set()).add(c)
    res["commands_by_arm"] = {k: sorted(v) for k, v in cmds.items()}
    res["commands_consistent"] = all(len(v) == 1 for v in cmds.values())
    res["kicks_by_arm"] = {arm: {"boundary_max": max((float(r["kicks_boundary_per_second"]) for r in runs if r["arm"] == arm
                                                      and r["kicks_boundary_per_second"]), default=None),
                                 "wake_max": max((float(r["kicks_wake_per_second"]) for r in runs if r["arm"] == arm
                                                  and r["kicks_wake_per_second"]), default=None)} for arm in ("Ctick", "Ckick")}

    # 5. kernels and run ids traceable
    res["kernels"] = sorted({r["kernel"] for r in runs})
    res["source_dirs_exist"] = all(os.path.isdir(os.path.join(ROOT, "data", "raw", r["source_run_id"])) for r in runs)
    res["figures"] = sorted(os.path.basename(p) for p in glob.glob(os.path.join(ROOT, "figures", "*")))
    res["ok"] = (worst < 1e-9 and not res["missing"] and not res["duplicates_in_runs_csv"] and not res["nonpositive_plotted"]
                 and res["commands_consistent"] and len(res["kernels"]) == 1 and res["source_dirs_exist"]
                 and all(list(d.values())[0] for d in res["raw_hash_matches_manifest"]))
    json.dump(res, open(os.path.join(D, "verify.json"), "w"), indent=1, default=str)
    print(json.dumps({k: v for k, v in res.items() if k not in ("recompute_examples",)}, default=str, indent=1))


if __name__ == "__main__":
    main()
