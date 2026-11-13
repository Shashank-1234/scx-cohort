#!/usr/bin/env python3
"""Raw runs -> derived tables (stdlib only; runs on the Mac). Never writes into data/raw.

  python3 scripts/analyze.py                 # main runs: data/raw/*_GSF_run  -> data/derived/
  python3 scripts/analyze.py --pilot         # pilot:     data/raw/*_GSF_pilot -> data/derived/pilot/
  python3 scripts/analyze.py --raw DIR ...   # explicit run dirs

Writes  runs.csv       one row per (experiment, scheduler, point, repetition), failures included
        summary.csv    per (experiment, scheduler, point): n, mean / median / min / max per metric
        checks.json    completeness, duplicates, failures, positivity, kernels
        preemption_runs.csv / preemption_summary.csv   from P10-02 raw data (optional figure)
        ../manifest.json (main only)   sources of every derived file
Metric definitions: EXPERIMENT_SPEC.md. Unavailable metrics are empty cells (null), never 0.
"""
import argparse
import csv
import glob
import hashlib
import json
import os
import statistics as st
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))          # gang-scheduling-figs/
REPO = os.path.dirname(ROOT)
P1002 = os.path.join(REPO, "experiments", "2026-11-04_P10-02_skew-tick", "results")
PREEMPT_SRC = {"aligned": "20261104T212905Z_P10-02_skew0_run", "skewed": "20261104T205834Z_P10-02_skew1_run"}

SCHED_LABEL = {"A0": "A alone (EEVDF)", "B0": "B alone (EEVDF)", "EEVDF": "EEVDF",
               "Ctick": "scx_cohort Ctick", "Ckick": "scx_cohort Ckick (boundary+wake kick)"}
COLS = ["experiment", "scheduler", "arm", "interval_us", "variance_us", "x_us", "repetition", "rounds_per_second",
        "b_rounds_per_second", "worker_cpu_share", "cpu_efficiency", "worker_progress", "competitor_progress",
        "combined_progress", "co_run_fraction", "kicks_per_second", "kicks_boundary_per_second",
        "kicks_wake_per_second", "fallback_per_second", "wake_to_run_us", "boundary_latency_us", "block_fraction",
        "mean_spin_us", "tick_gaps", "speed_min_iters_per_us", "speed_max_iters_per_us", "measure_s",
        "cohort_window_s", "failure_status", "notes", "source_run_id", "kernel", "sched_cmd"]
METRICS = ["rounds_per_second", "b_rounds_per_second", "worker_cpu_share", "cpu_efficiency", "worker_progress",
           "competitor_progress", "combined_progress", "co_run_fraction", "kicks_per_second",
           "kicks_boundary_per_second", "kicks_wake_per_second", "fallback_per_second", "wake_to_run_us",
           "boundary_latency_us", "block_fraction", "mean_spin_us", "tick_gaps"]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def cpu_s(s0, s1, key):
    """Thread CPU seconds between snapshots (schedstat run_ns), threads present in both."""
    a, b = s0.get(key, {}), s1.get(key, {})
    return sum(b[t]["run_ns"] - a[t]["run_ns"] for t in b if t in a) / 1e9


def cohort_window(rd, t0, t1):
    """scx_cohort counter deltas over the measured window. Same rule as src/harness/analyze_p10.py:
    last stats line at or before t0 to last line at or before t1 (lines are 1 s apart)."""
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
    return {"dt": dt, "co_frac": d("co_ns") / 1e9 / dt, "kb": d("kick_boundary") / dt, "kw": d("kick_wake") / dt,
            "fallback": d("fallback") / dt, "tick_gaps": d("tick_gap_cnt"),
            "wake_lat_us": d("wake_lat_sum") / d("wake_lat_cnt") / 1000 if d("wake_lat_cnt") else None,
            "bnd_lat_us": d("bnd_lat_sum") / d("bnd_lat_cnt") / 1000 if d("bnd_lat_cnt") else None}


def slot_delta(s0, s1, key, field):
    a, b = s0.get(key) or [], s1.get(key) or []
    return sum(y[field] - x[field] for x, y in zip(a, b))


def base_rows(rundir):
    meta = json.load(open(os.path.join(rundir, "meta.json")))
    rid = os.path.basename(os.path.normpath(rundir))
    rows = []
    for line in open(os.path.join(rundir, "runs.jsonl")):
        r = json.loads(line)
        row = {c: None for c in COLS}
        row.update(experiment=r["exp"], arm=r["arm"], scheduler=SCHED_LABEL[r["arm"]], interval_us=r["interval_us"],
                   variance_us=r["variance_us"], x_us=r["x_us"], repetition=r["rep"],
                   source_run_id=f"{rid}/rep{r['rep']}/{r['tag']}", kernel=meta["kernel"],
                   sched_cmd=" ".join(os.path.basename(c) if i == 0 else c for i, c in enumerate(r.get("sched_cmd") or []))
                   or "none (EEVDF)", notes="; ".join(r.get("notes", [])) or None)
        row["failure_status"] = "failed" if r.get("failed") else "ok"
        sp = [v for v in (r.get("speed_pre") or {}).values() if isinstance(v, (int, float))]
        if sp:
            row["speed_min_iters_per_us"], row["speed_max_iters_per_us"] = min(sp), max(sp)
        if r.get("failed") or "s0" not in r:
            rows.append(row)
            continue
        s0, s1, dt = r["s0"], r["s1"], r["measure_s"]
        row["measure_s"] = dt
        if "A_rounds" in s0:
            row["rounds_per_second"] = (s1["A_rounds"] - s0["A_rounds"]) / dt
            row["_a_cpu"] = cpu_s(s0, s1, "A_tasks")
            row["worker_cpu_share"] = row["_a_cpu"] / (4 * dt)
            w, bl = slot_delta(s0, s1, "A_slots", "waits"), slot_delta(s0, s1, "A_slots", "blocks")
            row["block_fraction"] = bl / w if w else None
            row["mean_spin_us"] = slot_delta(s0, s1, "A_slots", "spin_ns_sum") / w / 1000 if w else None
        if "B_rounds" in s0:
            row["b_rounds_per_second"] = (s1["B_rounds"] - s0["B_rounds"]) / dt
        if r["arm"] in ("Ctick", "Ckick"):
            c = cohort_window(os.path.join(rundir, f"rep{r['rep']}", r["tag"]), s0["t_ns"], s1["t_ns"])
            if c:
                row.update(co_run_fraction=c["co_frac"], kicks_per_second=c["kb"] + c["kw"],
                           kicks_boundary_per_second=c["kb"], kicks_wake_per_second=c["kw"],
                           fallback_per_second=c["fallback"], wake_to_run_us=c["wake_lat_us"],
                           boundary_latency_us=c["bnd_lat_us"], tick_gaps=c["tick_gaps"], cohort_window_s=c["dt"])
        rows.append(row)
    return rows, meta


def normalise(rows):
    ok = lambda r: r["failure_status"] == "ok"
    a0 = {(r["source_run_id"].split("/")[0], r["repetition"], r["experiment"], r["x_us"]): r
          for r in rows if r["arm"] == "A0" and ok(r)}
    b0 = {(r["source_run_id"].split("/")[0], r["repetition"]): r for r in rows if r["arm"] == "B0" and ok(r)}
    for r in rows:
        if not ok(r):
            continue
        rid = r["source_run_id"].split("/")[0]
        ref = a0.get((rid, r["repetition"], r["experiment"], r["x_us"]))
        if r["rounds_per_second"] is not None and ref and ref["rounds_per_second"]:
            r["worker_progress"] = r["rounds_per_second"] / ref["rounds_per_second"]
            if r.get("_a_cpu") and ref.get("_a_cpu"):
                r["cpu_efficiency"] = r["worker_progress"] / (r["_a_cpu"] / ref["_a_cpu"])
        bref = b0.get((rid, r["repetition"]))
        if r["b_rounds_per_second"] is not None and bref and bref["b_rounds_per_second"]:
            r["competitor_progress"] = r["b_rounds_per_second"] / bref["b_rounds_per_second"]
            if r["worker_progress"] is not None:
                r["combined_progress"] = r["worker_progress"] + r["competitor_progress"]


def summarise(rows, keys=("experiment", "arm", "x_us"), metrics=METRICS):
    g = defaultdict(list)
    for r in rows:
        g[tuple(r[k] for k in keys)].append(r)
    out = []
    for k, rs in sorted(g.items(), key=lambda kv: tuple(str(x) for x in kv[0][:2]) + tuple(x or 0 for x in kv[0][2:])):
        good = [r for r in rs if r["failure_status"] == "ok"]
        row = dict(zip(keys, k))
        row["scheduler"] = rs[0].get("scheduler")
        row["n_ok"], row["n_failed"] = len(good), len(rs) - len(good)
        row["repetitions"] = " ".join(str(r["repetition"]) for r in sorted(good, key=lambda r: r["repetition"]))
        for m in metrics:
            v = [r[m] for r in good if r.get(m) is not None]
            row[f"{m}_n"] = len(v)
            for name, f in (("mean", st.mean), ("median", st.median), ("min", min), ("max", max)):
                row[f"{m}_{name}"] = f(v) if v else None
        out.append(row)
    return out


def write_csv(path, rows, cols=None):
    cols = cols or list(rows[0].keys())
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: ("" if r.get(c) is None else r.get(c)) for c in cols})


def checks(rows, rundirs):
    expected = defaultdict(int)
    for d in rundirs:
        rid = os.path.basename(os.path.normpath(d))
        for rj in sorted(glob.glob(os.path.join(d, "rep*", "rep.json"))):
            h = json.load(open(rj))
            for e, x, arm in h["order"]:
                expected[(rid, h["rep"], e, x, arm)] += 1
    seen = defaultdict(int)
    for r in rows:
        seen[(r["source_run_id"].split("/")[0], r["repetition"], r["experiment"], r["x_us"], r["arm"])] += 1
    missing = [list(k) for k in expected if seen[k] == 0]
    dup = [list(k) for k, n in seen.items() if n > 1]
    unexpected = [list(k) for k in seen if k not in expected]
    nonpos = [r["source_run_id"] for r in rows if r["failure_status"] == "ok" and r["arm"] != "B0"
              and not (r["rounds_per_second"] or 0) > 0]
    per_point = defaultdict(set)
    for r in rows:
        if r["arm"] != "B0":
            per_point[(r["experiment"], r["arm"], r["x_us"])].add(r["repetition"])
    reps = sorted({r["repetition"] for r in rows})
    return {"runs": len(rows), "ok": sum(r["failure_status"] == "ok" for r in rows),
            "failed": [{"run": r["source_run_id"], "notes": r["notes"]} for r in rows if r["failure_status"] != "ok"],
            "notes": [{"run": r["source_run_id"], "notes": r["notes"]} for r in rows if r["notes"]],
            "missing_vs_rep_order": missing, "duplicates": dup, "unexpected": unexpected,
            "nonpositive_rps": nonpos, "repetitions": reps,
            "points_with_incomplete_reps": [list(k) for k, v in per_point.items() if sorted(v) != reps],
            "kernels": sorted({r["kernel"] for r in rows})}


def paired(rows):
    """EXPLORATORY (not pre-registered; added after seeing the rep-9 host slowdown and EEVDF's bimodality):
    per rep, rounds/s of Cx divided by rounds/s of EEVDF at the same point. Host speed drift between reps
    cancels; drift within a rep does not."""
    idx = {(r["source_run_id"].split("/")[0], r["repetition"], r["experiment"], r["x_us"], r["arm"]): r
           for r in rows if r["failure_status"] == "ok" and r["rounds_per_second"]}
    out = []
    for (rid, rep, e, x, arm), r in idx.items():
        base = idx.get((rid, rep, e, x, "EEVDF"))
        if arm in ("Ctick", "Ckick") and base:
            out.append({"experiment": e, "arm": arm, "x_us": x, "repetition": rep,
                        "ratio_vs_eevdf": r["rounds_per_second"] / base["rounds_per_second"],
                        "competitor_ratio_vs_eevdf": (r["b_rounds_per_second"] / base["b_rounds_per_second"])
                        if base["b_rounds_per_second"] else None,
                        "source_run_id": r["source_run_id"], "eevdf_source_run_id": base["source_run_id"],
                        "failure_status": "ok"})
    out.sort(key=lambda r: (r["experiment"], r["arm"], r["x_us"], r["repetition"]))
    return out


def preemption():
    """P10-02 (spin barrier, CPU-bound competitor): efficiency per (ticks, arm, W, rep)."""
    out = []
    for ticks, name in PREEMPT_SRC.items():
        d = os.path.join(P1002, name)
        if not os.path.exists(os.path.join(d, "runs.jsonl")):
            return None, None
        kernel = json.load(open(os.path.join(d, "meta.json")))["uname"].split()[2]
        recs = [json.loads(l) for l in open(os.path.join(d, "runs.jsonl"))]
        a0 = {}
        for r in recs:
            if r["arm"] == "A0" and not r["failed"]:
                a0[(r["rep"], r["w_us"])] = ((r["s1"]["rounds"] - r["s0"]["rounds"]) / r["measure_s"],
                                             cpu_s(r["s0"], r["s1"], "worker_tasks"))
        for r in recs:
            if r["arm"] in ("A0", "C0", "E"):
                continue
            arm = r["arm"]
            kind = "Ckick" if arm.startswith("Ckick") else "Ctick"
            g = {"5": 5, "20": 20}.get(arm[5:], 10)
            row = {"ticks": ticks, "arm": arm, "variant": kind, "G_ms": g, "W_us": r["w_us"], "repetition": r["rep"],
                   "failure_status": "failed" if r["failed"] else "ok", "kernel": kernel,
                   "source_run_id": f"experiments/2026-11-04_P10-02_skew-tick/results/{name}/rep{r['rep']}/{r['tag']}",
                   "sched_cmd": " ".join([os.path.basename(r["sched_cmd"][0])] + r["sched_cmd"][1:]) if r.get("sched_cmd") else None}
            if not r["failed"]:
                rps = (r["s1"]["rounds"] - r["s0"]["rounds"]) / r["measure_s"]
                ref = a0.get((r["rep"], r["w_us"]))
                if ref:
                    row["efficiency"] = (rps / ref[0]) / (cpu_s(r["s0"], r["s1"], "worker_tasks") / ref[1])
                c = cohort_window(os.path.join(d, f"rep{r['rep']}", r["tag"]), r["s0"]["t_ns"], r["s1"]["t_ns"])
                if c:
                    row.update(kicks_boundary_per_second=c["kb"], kicks_wake_per_second=c["kw"],
                               fallback_per_second=c["fallback"], co_run_fraction=c["co_frac"],
                               boundary_latency_us=c["bnd_lat_us"])
            out.append(row)
    pm = ["efficiency", "kicks_boundary_per_second", "kicks_wake_per_second", "fallback_per_second",
          "co_run_fraction", "boundary_latency_us"]
    return out, summarise(out, keys=("ticks", "variant", "G_ms", "W_us"), metrics=pm)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", nargs="*")
    ap.add_argument("--pilot", action="store_true")
    a = ap.parse_args()
    pat = "*_GSF_pilot" if a.pilot else "*_GSF_run"
    rundirs = a.raw or sorted(glob.glob(os.path.join(ROOT, "data", "raw", pat)))
    if not rundirs:
        raise SystemExit(f"no raw run dirs matching {pat}")
    out = os.path.join(ROOT, "data", "derived", "pilot" if a.pilot else "")
    os.makedirs(out, exist_ok=True)
    rows, metas = [], {}
    for d in rundirs:
        rr, m = base_rows(d)
        rows += rr
        metas[os.path.basename(os.path.normpath(d))] = m
    normalise(rows)
    rows.sort(key=lambda r: (r["experiment"], r["arm"], r["x_us"] or 0, r["source_run_id"]))
    write_csv(os.path.join(out, "runs.csv"), rows, COLS)
    summ = summarise(rows)
    write_csv(os.path.join(out, "summary.csv"), summ)
    chk = checks(rows, rundirs)
    json.dump(chk, open(os.path.join(out, "checks.json"), "w"), indent=1)
    print(json.dumps({k: (v if not isinstance(v, list) or len(v) < 6 else f"{len(v)} items") for k, v in chk.items()}))
    pr_ = paired(rows)
    write_csv(os.path.join(out, "paired_runs.csv"), pr_)
    write_csv(os.path.join(out, "paired_summary.csv"),
              summarise(pr_, metrics=["ratio_vs_eevdf", "competitor_ratio_vs_eevdf"]))
    files = {"runs.csv": "one row per run", "summary.csv": "per point and arm", "checks.json": "validation",
             "paired_runs.csv": "EXPLORATORY: per-rep Cx / EEVDF ratios", "paired_summary.csv": "EXPLORATORY: per point"}
    if not a.pilot:
        pr, ps = preemption()
        if pr:
            write_csv(os.path.join(out, "preemption_runs.csv"), pr)
            write_csv(os.path.join(out, "preemption_summary.csv"), ps)
            files.update({"preemption_runs.csv": "P10-02 per run", "preemption_summary.csv": "P10-02 per cell"})
        man = {"generated_by": "scripts/analyze.py", "derived_dir": "data/derived", "derived_files": files,
               "figure2_sources": [{"run_dir": f"data/raw/{rid}", "kernel": m["kernel"], "src_id": m.get("src_id"),
                                    "gsf_id": m.get("gsf_id"), "utc": m["utc"],
                                    "runs_jsonl_sha256": sha256(os.path.join(ROOT, "data", "raw", rid, "runs.jsonl")),
                                    "runs": sum(1 for r in rows if r["source_run_id"].startswith(rid + "/"))}
                                   for rid, m in metas.items()],
               "preemption_sources": [{"run_dir": f"experiments/2026-11-04_P10-02_skew-tick/results/{n}", "ticks": t,
                                       "runs_jsonl_sha256": sha256(os.path.join(P1002, n, "runs.jsonl"))}
                                      for t, n in PREEMPT_SRC.items()] if pr else [],
               "tests": sorted(os.path.relpath(p, ROOT) for p in glob.glob(os.path.join(ROOT, "data", "raw", "tests", "*.json"))),
               "pilot": sorted(os.path.relpath(p, ROOT) for p in glob.glob(os.path.join(ROOT, "data", "raw", "*_GSF_pilot"))),
               "row_key": "every derived row carries source_run_id = <run dir>/rep<k>/<run tag> (a raw directory)"}
        json.dump(man, open(os.path.join(ROOT, "data", "manifest.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
