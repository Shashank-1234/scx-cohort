#!/usr/bin/env python3
"""Correctness tests for gsbarrier (run inside the VM, sched_ext disabled, EEVDF).

  python3 test_gsbarrier.py --out RESULT.json [--gsbarrier BIN] [--cal-bin BARRIER]

Each test records its measurements and PASS/FAIL into RESULT.json. Thresholds are fixed here
before the first run. Tests:
  T1 rng          rand(V) per worker: mean V/2, s.d. V/sqrt(48), in [0, V), workers uncorrelated
  T2 stop         stop flag ends the run; exit 0; every worker counted the same rounds
  T3 spin_budget  adaptive: spin phase ~50 us then futex; no spin phase far beyond the budget
  T4 blocking     adaptive/blocked threads use less CPU than spin mode; threads seen in state S
  T5 liveness     8 threads on 2 CPUs + a CPU hog, adaptive and block: rounds advance every 0.5 s
                  (a lost wakeup would stall a round forever), then clean stop
  T6 calibration  alone, interval 100 / 1000 us: achieved round time vs requested; barrier cost at 0
  T7 two_apps     A and B run together; thread names p05w* and gsbB*; both progress
"""
import argparse
import json
import math
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import gsshm  # noqa: E402
import lib  # noqa: E402

R = {}


def check(name, ok, **data):
    R[name] = {"pass": bool(ok), **data}
    print(f"{'PASS' if ok else 'FAIL'} {name} {json.dumps(data)[:300]}", flush=True)


def ipu(cal_bin):
    """Same conversion as the runner: all four experiment CPUs calibrating at once."""
    return gsshm.calib_concurrent(cal_bin, [1, 2, 3, 4])["iters_per_us"]


class App:
    def __init__(self, a, tag, interval_it, var_it, cpus="1-4", threads=4, mode="adaptive", name="p05w", app=0):
        self.path = f"/dev/shm/gsf_test_{tag}_{os.getpid()}"
        self.p = subprocess.Popen([a.gsbarrier, "--threads", str(threads), "--cpus", cpus, "--mode", mode,
                                   "--interval-iters", str(interval_it), "--variance-iters", str(var_it),
                                   "--spin-us", "50", "--shm", self.path, "--name", name, "--seed", "7",
                                   "--app", str(app)], stdout=subprocess.PIPE, text=True)
        self.s = gsshm.GsShm(self.path)
        t_end = time.time() + 10
        while self.s.ready() < threads:
            if time.time() > t_end:
                raise RuntimeError("not ready")
            time.sleep(0.005)

    def stop(self, timeout=10):
        self.s.set_stop()
        t0 = time.time()
        try:
            out, _ = self.p.communicate(timeout=timeout)
            res = json.loads(out.strip().splitlines()[-1])
            res["rc"], res["stop_s"] = self.p.returncode, round(time.time() - t0, 3)
        except subprocess.TimeoutExpired:
            self.p.kill(); self.p.wait()
            res = {"rc": None, "hung": True}
        self.s.close()
        os.unlink(self.path)
        return res


def cpu_frac(res, wall):
    return sum(w["cpu_s"] for w in res["workers"]) / (len(res["workers"]) * res["wall_s"])


def t1_rng(a):
    rows = []
    ok = True
    for v in (0, 1083, 108300, 10830000):
        out = subprocess.run([a.gsbarrier, "--rng-selftest", "200000", "--variance-iters", str(v), "--threads", "4",
                              "--seed", "7"], capture_output=True, text=True, check=True)
        r = json.loads(out.stdout)
        rows.append(r)
        for w in r["workers"]:
            if v == 0:
                ok &= w["max"] == 0
                continue
            ok &= 0 <= w["min"] and w["max"] < v
            ok &= abs(w["mean"] - v / 2) <= 0.01 * v
            ok &= abs(w["sd"] - v / math.sqrt(48)) <= 0.02 * v / math.sqrt(48)
        ok &= abs(r["corr_w0_w1"]) < 0.01
        ok &= len({w["seed"] for w in r["workers"]}) == 4
    check("T1_rng", ok, results=rows,
          rule="mean within 1% of V/2, sd within 2% of V/sqrt(48), all draws in [0,V), |corr(w0,w1)|<0.01, distinct seeds")


def t2_stop(a, k):
    out = []
    ok = True
    for mode, it, vi in (("adaptive", 0, 0), ("adaptive", 0, int(2000 * k)), ("block", int(100 * k), 0), ("spin", 0, 0)):
        app = App(a, "t2", it, vi, mode=mode)
        time.sleep(1.5)
        res = app.stop()
        good = res.get("rc") == 0 and res.get("rounds_consistent") is True and res.get("rounds", 0) > 0 \
            and all(w["rounds"] == res["rounds"] for w in res["workers"])
        ok &= good
        out.append({"mode": mode, "interval_iters": it, "variance_iters": vi, "rc": res.get("rc"),
                    "rounds": res.get("rounds"), "stop_s": res.get("stop_s"),
                    "per_worker_rounds": [w["rounds"] for w in res.get("workers", [])]})
    check("T2_stop", ok, runs=out, rule="rc 0, rounds > 0, every worker's round count == shared count")


def t3_spin_budget(a, k):
    # variance 10 ms at interval 0: almost every non-last arriver waits >> 50 us
    app = App(a, "t3", 0, int(10000 * k))
    time.sleep(3)
    res = app.stop()
    W = res["workers"]
    waits, blocks = sum(w["waits"] for w in W), sum(w["blocks"] for w in W)
    mean_spin_us = sum(w["spin_ns_sum"] for w in W) / waits / 1000
    max_spin_us = max(w["spin_ns_max"] for w in W) / 1000
    ok = res["rc"] == 0 and blocks >= 0.9 * waits and 45 <= mean_spin_us <= 60 and max_spin_us < 1000
    check("T3_spin_budget", ok, waits=waits, blocks=blocks, mean_spin_us=round(mean_spin_us, 2),
          max_spin_us=round(max_spin_us, 1), futex_calls=sum(w["futex_calls"] for w in W),
          rule="blocks >= 90% of waits, mean spin phase 45-60 us, max spin phase < 1 ms (descheduling can stretch one)")


def t4_blocking(a, k):
    vi = int(10000 * k)
    out = {}
    for mode in ("adaptive", "spin"):
        app = App(a, "t4" + mode, 0, vi, mode=mode)
        states = []
        for _ in range(40):
            states += lib.thread_states(app.p.pid)
            time.sleep(0.05)
        ts = lib.task_stats(app.p.pid)
        res = app.stop()
        out[mode] = {"cpu_frac": round(cpu_frac(res, None), 3), "state_S": states.count("S"),
                     "state_R": states.count("R"), "vcsw": sum(t["vcsw"] or 0 for t in ts.values())}
    ok = out["adaptive"]["cpu_frac"] < 0.9 and out["spin"]["cpu_frac"] > 0.97 and out["adaptive"]["state_S"] > 0
    check("T4_blocking", ok, **out,
          rule="adaptive CPU fraction < 0.9, spin > 0.97, adaptive worker threads observed sleeping (S)")


def t5_liveness(a, k):
    out = []
    ok = True
    hog = subprocess.Popen(["taskset", "-c", "1-2", "sh", "-c", "while :; do :; done"])
    try:
        for mode in ("adaptive", "block"):
            for vi in (0, int(50 * k)):
                apps = [App(a, f"t5{i}", 0, vi, cpus="1-2", mode=mode, name=("p05w" if i == 0 else "gsbB"), app=i)
                        for i in range(2)]
                samples, stalls = [], 0
                last = [x.s.rounds() for x in apps]
                for _ in range(8):
                    time.sleep(0.5)
                    cur = [x.s.rounds() for x in apps]
                    stalls += sum(c == l for c, l in zip(cur, last))
                    samples.append([c - l for c, l in zip(cur, last)])
                    last = cur
                res = [x.stop() for x in apps]
                good = stalls == 0 and all(r.get("rc") == 0 and r.get("rounds_consistent") for r in res)
                ok &= good
                out.append({"mode": mode, "variance_iters": vi, "stalled_intervals": stalls,
                            "rounds_per_0.5s": samples, "rc": [r.get("rc") for r in res]})
    finally:
        hog.kill(); hog.wait()
    check("T5_liveness", ok, runs=out, rule="no 0.5 s interval without progress, both apps exit 0 with consistent counts")


def t6_calibration(a, k):
    out = {}
    for us in (0, 1, 10, 100, 1000):
        app = App(a, "t6", int(round(us * k)), 0)
        time.sleep(0.5)
        r0, t0 = app.s.rounds(), time.monotonic()
        time.sleep(3)
        r1, t1 = app.s.rounds(), time.monotonic()
        app.stop()
        out[str(us)] = {"rounds_per_s": round((r1 - r0) / (t1 - t0), 1),
                        "round_us": round((t1 - t0) / (r1 - r0) * 1e6, 3)}
    base = out["0"]["round_us"]
    err = {us: round(out[us]["round_us"] - base - int(us), 3) for us in ("100", "1000")}
    ok = all(abs(e) <= 0.03 * int(us) for us, e in err.items())
    check("T6_calibration", ok, iters_per_us=k, by_interval=out, barrier_cost_us=base,
          residual_us=err, rule="round time - barrier-only round time within 3% of the requested 100 / 1000 us")


def t7_two_apps(a, k):
    A = App(a, "t7a", int(100 * k), 0, name="p05w", app=0)
    B = App(a, "t7b", 0, 0, name="gsbB", app=1)
    time.sleep(1)
    names = {}
    for app in (A, B):
        for tdir in os.listdir(f"/proc/{app.p.pid}/task"):
            names[tdir] = open(f"/proc/{app.p.pid}/task/{tdir}/comm").read().strip()
    rb = B.stop(); ra = A.stop()
    wn = sorted(v for v in names.values() if v.startswith(("p05w", "gsbB")))
    ok = wn == [f"gsbB{i}" for i in range(4)] + [f"p05w{i}" for i in range(4)] and ra["rc"] == rb["rc"] == 0 \
        and ra["rounds"] > 0 and rb["rounds"] > 0
    check("T7_two_apps", ok, worker_names=wn, rounds_A=ra["rounds"], rounds_B=rb["rounds"],
          rule="4 threads p05w0-3 (pool) + 4 gsbB0-3 (tenant), both progress, both exit 0")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--gsbarrier", default="/opt/gangsched/bin/gsf/gsbarrier")
    ap.add_argument("--cal-bin", default="/opt/gangsched/bin/p05/barrier")
    a = ap.parse_args()
    st = lib.sched_ext_state()
    if st["state"] not in ("disabled", None):
        sys.exit(f"sched_ext must be disabled for the tests: {st}")
    k = ipu(a.cal_bin)
    meta = {"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "kernel": lib.sh("uname -r"),
            "sched_ext": st, "iters_per_us": k, "gsbarrier_sha256": lib.sha256_file(a.gsbarrier),
            "loadavg": open("/proc/loadavg").read().strip()}
    t1_rng(a); t2_stop(a, k); t3_spin_budget(a, k); t4_blocking(a, k); t5_liveness(a, k)
    t6_calibration(a, k); t7_two_apps(a, k)
    meta["all_pass"] = all(v["pass"] for v in R.values())
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    lib.write_json(a.out, {"meta": meta, "tests": R})
    print("ALL PASS" if meta["all_pass"] else "SOME TESTS FAILED")
    sys.exit(0 if meta["all_pass"] else 1)


if __name__ == "__main__":
    main()
