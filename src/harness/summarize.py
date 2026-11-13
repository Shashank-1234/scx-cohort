#!/usr/bin/env python3
"""Shared runner summary:  python3 -I src/harness/summarize.py RESULTS_DIR

Reads RESULTS_DIR/spec.json and, per batch, RESULTS_DIR/<batch>/{meta.txt,arms.jsonl,r<rep>_<arm>/...}
(raw, never modified). Writes RESULTS_DIR/summary.csv (one row per arm record) and summary.md.
Generalised from the `summ` function of experiments/2026-11-12_EXPLORE_real-workloads/explore.sh.

Per rep: workload relative = arm / same rep's baseline arm; within-rep ratio = arm / same rep's reference
arm; competitor relative = competitor rate over exactly the workload's run / same rep's comp_alone rate;
combined = the two summed. Timed-out arms enter at the spec's bound (relative < timeout_bound_rel) and
are marked. Co-run fraction = delta co_ns / delta t over the compute window (llama-bench: the last
sum(samples_ns) of the run; npb: whole run), interpolated linearly between the 1-s stats lines
(monotonic clock; the scheduler's load time counts as a zero sample). Counter deltas over the whole run.
Host flag: a rep whose comp_alone rate < frac x the batch median is flagged; a sensitivity line without
flagged reps is printed. Decision labels when the spec has "decision_rules".
"""
import csv
import json
import os
import re
import statistics as st
import sys

COUNTERS = ["co_ns", "kick_boundary", "kick_wake", "fallback", "tick_gap_cnt"]
HARNESS_OK = {"ok", "timeout"}


def read_jsonl(p):
    out = []
    if os.path.exists(p):
        for line in open(p):
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    return out


def parse_workload(parser, path):
    """-> dict(thr, unit, win_ns, verified) or {} if unparsable."""
    if not os.path.exists(path):
        return {}
    txt = open(path, errors="replace").read()
    if parser == "llama-bench":
        try:
            tg = [t for t in json.loads(txt) if t.get("n_gen", 0) > 0]
        except ValueError:
            return {}
        if not tg:
            return {}
        return {"thr": float(tg[-1]["avg_ts"]), "unit": "gen tok/s", "win_ns": int(sum(tg[-1].get("samples_ns", [])))}
    if parser == "npb":
        m = re.search(r"Mop/s total\s*=\s*([\d.]+)", txt)
        v = re.search(r"Verification\s*=\s*(\w+)", txt)
        if not m:
            return {}
        return {"thr": float(m.group(1)), "unit": "Mop/s", "win_ns": None, "verified": v.group(1) if v else None}
    raise SystemExit(f"unknown parser {parser}")


def interp(samples, t, key):
    """Linear interpolation of a cumulative counter at monotonic time t; None outside the samples."""
    if not samples or t < samples[0][0] or t > samples[-1][0]:
        return None
    for (ta, a), (tb, b) in zip(samples, samples[1:]):
        if ta <= t <= tb:
            va, vb = a.get(key, 0) or 0, b.get(key, 0) or 0
            return va if tb == ta else va + (vb - va) * (t - ta) / (tb - ta)
    return samples[-1][1].get(key)


def stats_samples(armdir, sched_t0):
    rows = read_jsonl(os.path.join(armdir, "stats.jsonl"))
    s = [(r["mono_ns"], r) for r in rows if "mono_ns" in r]
    if sched_t0:
        s.insert(0, (sched_t0, {k: 0 for k in COUNTERS}))
    return sorted(s, key=lambda x: x[0])


def med(vals):
    """vals: list of (v, kind) with kind in '=', '<', '>'. -> (median, min, max, n, n_bound) or None."""
    vs = [v for v, _ in vals if v is not None]
    if not vs:
        return None
    return st.median(vs), min(vs), max(vs), len(vs), sum(1 for v, k in vals if v is not None and k != "=")


def fmt_med(m, p=2):
    if not m:
        return "—"
    s = f"{m[0]:.{p}f} [{m[1]:.{p}f}–{m[2]:.{p}f}]"
    if m[4]:
        s += f" ({m[4]} bound)"
    return s


def meta_kv(path):
    kv = {}
    if os.path.exists(path):
        for line in open(path, errors="replace"):
            for tok in line.split():
                if "=" in tok and not tok.startswith("--"):
                    k, _, v = tok.partition("=")
                    kv.setdefault(k, v)
    return kv


def meta_params(path):
    """The parameters the runner actually used (spec params plus smoke overrides): 'param P_<k>=<v>' lines."""
    out = {}
    if os.path.exists(path):
        for line in open(path, errors="replace"):
            if line.startswith("param P_") and "=" in line:
                k, _, v = line[len("param P_"):].strip().partition("=")
                out[k] = v
    return out


def meta_sha(path):
    sha, on = {}, False
    if os.path.exists(path):
        for line in open(path, errors="replace"):
            if line.startswith("--- sha256"):
                on = True
                continue
            if on and line.startswith("---"):
                break
            if on and len(line.split()) == 2:
                h, f = line.split()
                sha[os.path.basename(f)] = h[:16]
    return sha


def analyse_batch(R, spec, b):
    an = spec.get("analysis", {})
    base, ref, cbase = an.get("baseline_arm", "alone"), an.get("within_rep_ref", "eevdf"), an.get("comp_baseline_arm", "comp_alone")
    bound = float(an.get("timeout_bound_rel", 1 / float(spec["timeout"]["factor"])))
    parser = {"llama-bench": "llama-bench", "npb-cg": "npb"}[b["workload"]]
    bd = os.path.join(R, b["name"])
    recs = read_jsonl(os.path.join(bd, "arms.jsonl"))
    arms = [a["name"] for a in spec["arms"]]
    sched = {a["name"]: (a.get("sched") or "") for a in spec["arms"]}
    wl_arm = {a["name"]: a.get("workload", True) for a in spec["arms"]}
    unit = None
    for r in recs:
        d = os.path.join(bd, r.get("dir", ""))
        r["batch"] = b["name"]
        if wl_arm.get(r["arm"], True) and r.get("status") in ("ok", "timeout"):
            w = parse_workload(parser, os.path.join(d, "workload.out"))
            r.update({k: v for k, v in w.items() if k != "unit"})
            unit = w.get("unit", unit)
            if r["status"] == "ok" and "thr" not in w:
                r["status"] = "parse_fail"
            if r["status"] == "ok" and parser == "npb" and w.get("verified") != "SUCCESSFUL":
                r["status"] = "verify_fail"
        if r.get("comp_t1", 0) > r.get("comp_t0", 0):
            r["comp_rate"] = (r["comp_c1"] - r["comp_c0"]) / ((r["comp_t1"] - r["comp_t0"]) / 1e9)
        if r.get("w1_mono") and r.get("w0_mono"):
            r["wall_s"] = (r["w1_mono"] - r["w0_mono"]) / 1e9
        smp = stats_samples(d, r.get("sched_t0_mono")) if sched.get(r["arm"], "").startswith("scx_cohort") else []
        if smp and r.get("w0_mono"):
            w0, w1 = r["w0_mono"], r["w1_mono"]
            # compute window: llama-bench = generation only (last sum(samples_ns)); npb = the whole run
            ws, r["co_window"] = w0, ("compute" if parser == "npb" and r.get("status") == "ok" else "run")
            if r.get("win_ns") and r.get("status") == "ok":
                ws, r["co_window"] = max(w0, w1 - r["win_ns"]), "compute"
            a, z = interp(smp, ws, "co_ns"), interp(smp, w1, "co_ns")
            if a is not None and z is not None and w1 > ws:
                r["co_frac"] = (z - a) / (w1 - ws)
            for k in COUNTERS[1:]:
                a, z = interp(smp, w0, k), interp(smp, w1, k)
                if a is not None and z is not None:
                    r["d_" + k] = round(z - a)
            r["tick_gap_max"] = smp[-1][1].get("tick_gap_max")
    # ---- per-rep derived values
    reps = sorted({r["rep"] for r in recs})
    by = {(r["arm"], r["rep"]): r for r in recs}
    for r in recs:
        rep = r["rep"]
        al, ca = by.get((base, rep)), by.get((cbase, rep))
        al_thr = al.get("thr") if al and al.get("status") == "ok" else None
        if r.get("status") == "ok" and "thr" in r and al_thr:
            r["rel"], r["rel_k"] = r["thr"] / al_thr, "="
        elif r.get("status") == "timeout":
            r["rel"], r["rel_k"] = bound, "<"
        if ca and ca.get("status") == "ok" and ca.get("comp_rate") and r.get("comp_rate") and r["arm"] != cbase:
            r["comp_rel"] = r["comp_rate"] / ca["comp_rate"]
        if r.get("rel") is not None and r.get("comp_rel") is not None:
            r["combined"], r["combined_k"] = r["rel"] + r["comp_rel"], r["rel_k"]
        e = by.get((ref, rep))
        if e and r["arm"] not in (ref, base, cbase):   # within-rep ratio only for contended arms
            ok_x, ok_e = r.get("status") == "ok" and "thr" in r, e.get("status") == "ok" and "thr" in e
            if ok_x and ok_e:
                r["within"], r["within_k"] = r["thr"] / e["thr"], "="
            elif ok_x and e.get("status") == "timeout" and r.get("rel_k") == "=":
                r["within"], r["within_k"] = r["rel"] / bound, ">"
            elif r.get("status") == "timeout" and ok_e and e.get("rel_k") == "=":
                r["within"], r["within_k"] = bound / e["rel"], "<"
    # ---- host flag
    hf = an.get("host_flag")
    flagged = set()
    if hf:
        ca_rates = {r["rep"]: r["comp_rate"] for r in recs if r["arm"] == hf["arm"] and r.get("status") == "ok" and r.get("comp_rate")}
        if ca_rates:
            m = st.median(ca_rates.values())
            flagged = {rep for rep, v in ca_rates.items() if v < float(hf["min_frac_of_batch_median"]) * m}
    for r in recs:
        r["host_flagged"] = r["rep"] in flagged
    return {"batch": b, "recs": recs, "arms": arms, "sched": sched, "reps": reps, "flagged": flagged,
            "unit": unit or "", "meta": meta_kv(os.path.join(bd, "meta.txt")), "params": meta_params(os.path.join(bd, "meta.txt")), "sha": meta_sha(os.path.join(bd, "meta.txt"))}


def arm_medians(res, arm, exclude=frozenset()):
    rs = [r for r in res["recs"] if r["arm"] == arm and r["rep"] not in exclude]
    g = lambda k: med([(r.get(k), r.get(k + "_k", "=")) for r in rs if r.get(k) is not None])
    return {"rel": g("rel"), "within": g("within"), "comp_rel": g("comp_rel"), "combined": g("combined"),
            "thr": med([(r["thr"], "=") for r in rs if r.get("status") == "ok" and "thr" in r]),
            "co_frac": med([(r["co_frac"], "=") for r in rs if r.get("co_frac") is not None]), "rs": rs}


def decide(res, rules, exclude=frozenset()):
    ref, test = rules["ref_arm"], rules["test_arm"]
    E, C = arm_medians(res, ref, exclude), arm_medians(res, test, exclude)
    lost = {}
    for a, s in res["sched"].items():
        if s.startswith(rules.get("harness_loss_arms_prefix", "scx_cohort")):
            lost[a] = sum(1 for r in res["recs"] if r["arm"] == a and r["rep"] not in exclude and r.get("status") not in HARNESS_OK)
    if any(v > rules["harness_loss_max"] for v in lost.values()):
        return "Not decided (harness losses)", f"lost reps per scx_cohort arm: {lost}"
    if not E["rel"] or not C["rel"]:
        return "Not decided (missing data)", ""
    e, c = E["rel"][0], C["rel"][0]
    ws = [r for r in C["rs"] if r.get("within") is not None]
    n_gt = sum(1 for r in ws if r["within"] > 1 and r["within_k"] in ("=", ">"))
    n_lt = sum(1 for r in ws if r["within"] < 1 and r["within_k"] in ("=", "<"))
    ec = E["comp_rel"][0] if E["comp_rel"] else None
    cc = C["comp_rel"][0] if C["comp_rel"] else None
    collapse = e <= rules["collapse_max"]
    comp_ok = ec is not None and cc is not None and cc >= ec - rules["comp_tolerance"]
    fixed = collapse and c >= rules["fixed_min"] and n_gt >= rules["within_rep_min_count"] and comp_ok
    neutral = (not collapse) and abs(c - e) <= rules["neutral_band"]
    hurts = c < e - rules["hurts_band"] and n_lt >= rules["within_rep_min_count"]
    if fixed:
        lab = "Collapse · Fixed"
    elif collapse:
        lab = "Collapse · Hurts" if hurts else "Collapse · not fixed"
    elif neutral:
        lab = "Neutral"
    else:
        lab = "Hurts" if hurts else "Not decided"
    why = (f"{ref} rel {e:.3f} (Collapse if ≤ {rules['collapse_max']}); {test} rel {c:.3f}; within-rep {test}/{ref} > 1 in "
           f"{n_gt}/{len(ws)} reps, < 1 in {n_lt}; competitor rel {test} {cc if cc is None else round(cc, 3)} vs {ref} {ec if ec is None else round(ec, 3)}")
    return lab, why


def main():
    R = sys.argv[1].rstrip("/")
    spec = json.load(open(os.path.join(R, "spec.json")))
    smoke = R.endswith("_smoke") or "_smoke" in os.path.basename(R)
    results = [analyse_batch(R, spec, b) for b in spec["batches"] if os.path.isdir(os.path.join(R, b["name"]))]
    rules = spec.get("decision_rules")
    # ---- csv
    cols = ["batch", "arm", "rep", "status", "rc", "host_flagged", "wall_s", "thr", "rel", "rel_k", "within", "within_k",
            "comp_rate", "comp_rel", "combined", "co_frac", "co_window", "d_kick_boundary", "d_kick_wake", "d_fallback",
            "d_tick_gap_cnt", "tick_gap_max", "verified", "timeout_s", "sched_state_at_end", "dir"]
    with open(os.path.join(R, "summary.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for res in results:
            for r in sorted(res["recs"], key=lambda r: (r["rep"], res["arms"].index(r["arm"]) if r["arm"] in res["arms"] else 99)):
                w.writerow([r.get(c, "") for c in cols])
    # ---- md
    L = [f"# Summary · {os.path.basename(R)}", ""]
    if smoke:
        L += ["**SMOKE RUN: checks the harness only. Not analysed, not reported; decision labels are meaningless here.**", ""]
    L += [f"Experiment `{spec.get('experiment')}`, pre-registration `{spec.get('prereg')}`, seed {spec['seed']}. "
          "Medians over reps [min–max]. *rel* = arm ÷ same rep's `alone`; *within* = arm ÷ same rep's `eevdf`; "
          "*comp* = competitor rate during the workload ÷ same rep's `comp_alone`; *combined* = rel + comp. "
          f"Timed-out arms enter at their bound (rel < {spec.get('analysis', {}).get('timeout_bound_rel')}), marked “bound”. "
          "Co-run = Δco_ns ÷ Δt over the compute window (llama-bench: generation only; NPB: whole run), 1-s resolution. "
          "Kicks / fallback / tick_gap = counter deltas over the run.", ""]
    # settings: fixed vs changed, with mismatch flags
    L += ["## Settings", "", "| Setting | Value | Fixed / changed |", "|---|---|---|"]
    def uniq(get):
        vals = sorted({get(res) for res in results})
        return vals
    for name, get in [("kernel", lambda r: r["meta"].get("kernel", "?")), ("cpus", lambda r: r["meta"].get("cpus", "?")),
                      ("src_id", lambda r: r["meta"].get("src_id", "?")), ("scx_cohort sha256", lambda r: r["sha"].get("scx_cohort", "?")),
                      ("scx_simple sha256", lambda r: r["sha"].get("scx_simple", "?")), ("competitor sha256", lambda r: r["sha"].get("competitor", "?")),
                      ("k3s during batch", lambda r: "stopped" if r["meta"].get("k3s_during") in ("inactive", "failed") and r["meta"].get("k3s_mainpid", "0") == "0" else r["meta"].get("k3s_during", "?"))]:
        v = uniq(get)
        L.append(f"| {name} | {', '.join(v)} | {'fixed' if len(v) == 1 else '**MISMATCH across batches**'} |")
    L.append("| workload / knob / params / timeout | per batch, below | changed (by design) |")
    L.append("| scheduler | per arm | changed (by design) |")
    L.append("")
    labels = {}
    for res in results:
        b = res["batch"]
        m = res["meta"]
        L += [f"## {b['name']} · {b['workload']} `{b['knob']}`", "",
              f"params used {json.dumps(res['params'])} · "
              f"warm-up {m.get('warmup_wall_ms', '?')} ms → timeout {m.get('timeout_s', '?')} s · reps {len(res['reps'])} · "
              f"k3s restored: {m.get('k3s_restored', '?')} · runner exit {m.get('exit', '?')} · "
              f"host-flagged reps: {sorted(res['flagged']) or 'none'}", ""]
        hdr = ["arm", "ok / total", f"throughput ({res['unit']})", "rel", "within vs eevdf", "comp", "combined", "co-run", "kicks bnd / wake", "fallback", "tick_gap Δ (max)"]
        L += ["| " + " | ".join(hdr) + " |", "|" + "---|" * len(hdr)]
        for a in res["arms"]:
            M = arm_medians(res, a)
            rs = M["rs"]
            if not rs:
                continue
            ok = sum(r.get("status") == "ok" for r in rs)
            other = {}
            for r in rs:
                if r.get("status") != "ok":
                    other[r.get("status")] = other.get(r.get("status"), 0) + 1
            cnt = f"{ok}/{len(rs)}" + (" (" + ", ".join(f"{k} {v}" for k, v in other.items()) + ")" if other else "")
            def cm(k):
                v = [r.get(k) for r in rs if r.get(k) is not None]
                return f"{st.median(v):.0f}" if v else "—"
            tg = [r.get("tick_gap_max") for r in rs if r.get("tick_gap_max") is not None]
            L.append("| " + " | ".join([a, cnt, fmt_med(M["thr"], 1), fmt_med(M["rel"]), fmt_med(M["within"]), fmt_med(M["comp_rel"]),
                                        fmt_med(M["combined"]), fmt_med(M["co_frac"], 3), f"{cm('d_kick_boundary')} / {cm('d_kick_wake')}",
                                        cm("d_fallback"), f"{cm('d_tick_gap_cnt')} ({max(tg) if tg else '—'})"]) + " |")
        L.append("")
        if rules:
            lab, why = decide(res, rules)
            labels[b["name"]] = lab
            L += [f"**Decision: {lab}.** {why}", ""]
            if res["flagged"]:
                slab, swhy = decide(res, rules, exclude=res["flagged"])
                L += [f"Sensitivity without host-flagged reps {sorted(res['flagged'])}: **{slab}**. {swhy}", ""]
        bad = [r for r in res["recs"] if r.get("status") != "ok"]
        if bad:
            L += ["Failed reps (reported, never rerun): " + "; ".join(f"{r['arm']} rep {r['rep']} {r.get('status')}" for r in bad), ""]
    if rules and labels:
        sup = all(l in ("Collapse · Fixed", "Neutral") for l in labels.values())
        L += ["## Claim", "", "| Batch | Label |", "|---|---|"] + [f"| {k} | {v} |" for k, v in labels.items()]
        L += ["", f"Claim (every Collapse batch Fixed, every other batch Neutral): **{'supported' if sup else 'not supported'}**"
              + (" (smoke: meaningless)" if smoke else ""), ""]
    hp = os.path.join(R, "host.txt")
    if os.path.exists(hp):
        L += ["## Host (Mac) log", "", "```"] + [l.rstrip() for l in open(hp)] + ["```", ""]
    open(os.path.join(R, "summary.md"), "w").write("\n".join(L) + "\n")
    print(f"wrote summary.md and summary.csv ({sum(len(r['recs']) for r in results)} arm records, {len(results)} batches)")


if __name__ == "__main__":
    main()
