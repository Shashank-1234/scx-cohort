#!/usr/bin/env python3
"""Figure 2-inspired sweeps (gang-scheduling-figs). Runs as root inside the VM.

Two BARRIER applications (gsbarrier, adaptive 50 us spin-then-block) share CPUs 1-4:
  A = target, 4 threads named p05w0..3 -> scx_cohort's registered pool (gang class)
  B = competitor, 4 threads named gsbB0..3, interval 0, variance 0 -> scx_cohort tenant class
Experiment "interval": A interval = x, variance 0.   Experiment "variance": A interval 0, variance = x.
Arms: A0 (A alone, EEVDF, reference), EEVDF, Ctick, Ckick; plus B0 (B alone, EEVDF) once per rep.

Subcommands (all write into --out): meta, k3s-stop, k3s-restore, calibrate, preflight, rep.
Reuses src/harness (lib.py, p05.py, p10.py) from the synced tree at /opt/gangsched/src/harness.
The frozen design is in ../EXPERIMENT_SPEC.md; change it there first, never here alone.
"""
import argparse
import json
import os
import random
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gsshm  # noqa: E402  (also puts src/harness on sys.path)
import lib  # noqa: E402
import p05  # noqa: E402
import p10  # noqa: E402

POINTS_US = [1, 3, 10, 30, 50, 100, 300, 1000, 3000, 10000]
ARMS = ["A0", "EEVDF", "Ctick", "Ckick"]
COHORT_FLAGS = {"Ctick": [], "Ckick": ["-b", "-k"]}  # G = T = 10 ms in both
G_US = 10000
A_NAME, B_NAME = "p05w", "gsbB"  # p05w = scx_cohort's default pool comm prefix
PILOT = [("interval", 1), ("interval", 10000), ("variance", 1), ("variance", 10000)]


def configs(set_):
    pts = PILOT if set_ == "pilot" else [(e, x) for e in ("interval", "variance") for x in POINTS_US]
    return [(e, x, arm) for e, x in pts for arm in ARMS] + [("B0", None, "B0")]


def a_params(exp, x):
    if exp == "interval":
        return x, 0
    if exp == "variance":
        return 0, x
    return None, None


def sched_cmd(a, arm, rd):
    if arm not in COHORT_FLAGS:
        return None
    return [os.path.join(a.sched_bin, "scx_cohort"), "-m", a.cpus, "-n", str(a.threads), "-G", str(G_US),
            "-T", str(G_US), "-o", os.path.join(rd, "cohort_stats.jsonl")] + COHORT_FLAGS[arm]


def speed(a, cpus, n=1_000_000, reps=3):
    """Work rate per vCPU (iterations/us): the clock proxy, since the guest has no cpufreq."""
    out = {}
    for c in cpus:
        try:
            out[str(c)] = p05.calib(a.p05_bin, c, n, reps)["iters_per_us"]
        except Exception as e:  # recorded, not fatal
            out[str(c)] = f"error: {e!r}"
    return out


def snapshot(procs, shms):
    t = time.monotonic_ns()
    snap = {"t_ns": t}
    for role, s in shms.items():  # counters first, closest to t_ns
        snap[f"{role}_rounds"] = s.rounds()
    for role, s in shms.items():
        snap[f"{role}_slots"] = s.slots()
    snap["stat"] = lib.proc_stat_cpus()
    snap["sched_ext"] = lib.sched_ext_state()
    for role, p in procs.items():
        snap[f"{role}_tasks"] = lib.task_stats(p.pid)
    return snap


def start_app(a, role, interval_us, variance_us, seed, rd, files, ipu):
    it, vi = int(round(ipu * interval_us)), int(round(ipu * variance_us))
    path = f"/dev/shm/gsf_{role}_{os.getpid()}"
    name, app = (A_NAME, 0) if role == "A" else (B_NAME, 1)
    cmd = [a.gsbarrier, "--threads", str(a.threads), "--cpus", a.cpus, "--mode", "adaptive",
           "--interval-iters", str(it), "--variance-iters", str(vi), "--spin-us", str(a.spin_us),
           "--shm", path, "--name", name, "--seed", str(seed), "--app", str(app)]
    fo = open(os.path.join(rd, f"{role}.out"), "w"); files.append(fo)
    p = subprocess.Popen(cmd, stdout=fo, stderr=subprocess.STDOUT)
    return p, gsshm.GsShm(path), {"cmd": cmd, "interval_us": interval_us, "variance_us": variance_us,
                                  "interval_iters": it, "variance_iters": vi, "seed": seed, "name": name}


def run_one(a, cal, rundir, idx, exp, x, arm, seed):
    tag = f"{idx:03d}_{arm}_{exp}_{x if x is not None else 0}"
    rd = os.path.join(rundir, tag)
    os.makedirs(rd, exist_ok=True)
    iv, vv = a_params(exp, x)
    rec = {"idx": idx, "exp": exp, "x_us": x, "arm": arm, "tag": tag, "interval_us": iv, "variance_us": vv,
           "failed": False, "notes": [], "seed": seed}
    procs, shms, files = {}, {}, []
    sched = None
    ipu = cal["iters_per_us"]
    cpus = p05.expand_cpus(a.cpus)
    try:
        rec["speed_pre"] = speed(a, cpus)
        pre = lib.sched_ext_state()
        rec["sched_ext_pre"] = pre
        if pre["state"] not in ("disabled", None):
            rec["failed"] = True
            rec["notes"].append(f"sched_ext not disabled before run: {pre}")
            return rec
        cmd = sched_cmd(a, arm, rd)
        if cmd:
            rec["sched_cmd"] = cmd
            fo = open(os.path.join(rd, "sched.out"), "w"); files.append(fo)
            sched = subprocess.Popen(cmd, stdout=fo, stderr=subprocess.STDOUT)
            if not p10.wait_state("enabled", "cohort"):
                rec["failed"] = True
                rec["notes"].append(f"scheduler did not attach: {lib.sched_ext_state()}")
                return rec
            rec["sched_ops"] = "cohort"
        if arm != "A0":  # competitor B: interval 0, variance 0
            procs["B"], shms["B"], rec["B"] = start_app(a, "B", 0, 0, seed, rd, files, ipu)
        if arm != "B0":
            procs["A"], shms["A"], rec["A"] = start_app(a, "A", iv, vv, seed, rd, files, ipu)
        t_end = time.time() + 10
        for s in shms.values():
            while s.ready() < a.threads:
                if time.time() > t_end:
                    raise RuntimeError("threads not ready")
                time.sleep(0.005)
        time.sleep(a.warmup)
        s0 = snapshot(procs, shms)
        time.sleep(max(0.0, a.measure - (time.monotonic_ns() - s0["t_ns"]) / 1e9))
        s1 = snapshot(procs, shms)
        rec["s0"], rec["s1"] = s0, s1
        rec["measure_s"] = (s1["t_ns"] - s0["t_ns"]) / 1e9
        want = "enabled" if sched else "disabled"
        if s1["sched_ext"]["state"] not in (want, None) or (sched and s1["sched_ext"]["ops"] != "cohort"):
            rec["failed"] = True
            rec["notes"].append(f"scheduler state changed during run: {s1['sched_ext']}")
        if sched and sched.poll() is not None:
            rec["failed"] = True
            rec["notes"].append(f"scheduler exited early rc={sched.returncode}")
        for role in shms:
            if s1[f"{role}_rounds"] == s0[f"{role}_rounds"]:
                rec["notes"].append(f"{role}: zero rounds in the measured window")
    except Exception as e:
        rec["failed"] = True
        rec["notes"].append(f"exception: {e!r}")
    finally:
        for s in shms.values():
            s.set_stop()
        for role, p in procs.items():
            try:
                p.wait(timeout=15)
                rec[f"{role}_rc"] = p.returncode
                if p.returncode != 0:
                    rec["failed"] = True
                    rec["notes"].append(f"{role} exited rc={p.returncode} (3 = per-worker round counts disagree)")
            except subprocess.TimeoutExpired:
                p.kill(); p.wait()
                rec["failed"] = True
                rec["notes"].append(f"{role} did not exit after stop; killed")
        if sched is not None:
            if sched.poll() is None:
                sched.send_signal(signal.SIGINT)
                try:
                    sched.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    sched.kill(); sched.wait()
                    rec["notes"].append("scheduler killed after SIGINT timeout")
            rec["sched_rc"] = sched.returncode
            if not p10.wait_state("disabled", timeout=15):
                rec["failed"] = True
                rec["notes"].append(f"sched_ext not disabled after unload: {lib.sched_ext_state()}")
        for s in shms.values():
            s.close()
        for fo in files:
            fo.close()
        for role in ("A", "B"):
            try:
                os.unlink(f"/dev/shm/gsf_{role}_{os.getpid()}")
            except OSError:
                pass
        for role in ("A", "B"):  # program summaries (per-worker rounds, seeds, spin stats)
            pth = os.path.join(rd, f"{role}.out")
            if os.path.exists(pth):
                try:
                    rec[f"{role}_summary"] = json.loads(open(pth).read().strip().splitlines()[-1])
                except (ValueError, IndexError):
                    rec["notes"].append(f"{role}.out not parseable")
        try:
            rec["dmesg_tail"] = lib.sh("dmesg | grep -i sched_ext | tail -3")
        except Exception:
            pass
    return rec


IDLE_MAX = 0.10


def preflight(a, out=None):
    exact = tuple(os.path.join(a.sched_bin, b) for b in ("scx_cohort", "scx_simple"))
    killed = lib.kill_strays([a.p05_bin, os.path.dirname(a.gsbarrier)], exact)
    ok_state = p10.wait_state("disabled", timeout=15)
    busy = lib.cpu_busy(p05.expand_cpus(a.cpus), 2.0)
    res = {"preflight": "ok", "killed": killed, "sched_ext": lib.sched_ext_state(), "busy": busy,
           "loadavg": open("/proc/loadavg").read().strip(), "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if not ok_state or max(busy.values()) > IDLE_MAX:
        res["preflight"] = "FAIL"
        res["top"] = lib.sh("ps -eo pid,etime,pcpu,psr,comm --sort=-pcpu | head -12")
    if out:
        lib.append_jsonl(os.path.join(out, "preflight.jsonl"), res)
    return res


def cmd_meta(a):
    rel = lib.sh("uname -r")
    bins = {p: lib.sha256_file(p) for p in (a.gsbarrier, os.path.join(a.p05_bin, "barrier"),
                                             os.path.join(a.sched_bin, "scx_cohort")) if os.path.exists(p)}
    meta = {
        "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "uname": lib.sh("uname -a"), "kernel": rel,
        "cmdline": open("/proc/cmdline").read().strip(),
        "config": lib.sh(f"grep -E '^CONFIG_(HZ|HZ_[0-9]+|NO_HZ[A-Z_]*|PREEMPT[A-Z_]*|SCHED_CLASS_EXT)=' /boot/config-{rel} || true"),
        "nproc": lib.sh("nproc"), "lscpu": lib.sh("lscpu"), "cpu_map": lib.sh("lscpu -e"),
        "smt": lib.sh("cat /sys/devices/system/cpu/smt/control 2>/dev/null || echo absent"),
        "cpufreq": lib.sh("ls /sys/devices/system/cpu/cpu1/cpufreq 2>/dev/null || echo absent "
                          "(no cpufreq in the guest; per-run speed checks stand in for MHz)"),
        "sched_ext": lib.sched_ext_state(),
        "src_id": lib.sh("cat /opt/gangsched/src/SRC_ID 2>/dev/null || true"),
        "gsf_id": lib.sh(f"cat {a.gsf_root}/GSF_ID 2>/dev/null || true"),
        "bin_sha256": bins, "gcc": lib.sh("gcc --version | head -1"), "python": sys.version.split()[0],
        "k3s_active": lib.sh("systemctl is-active k3s || true"),
        "loadavg": open("/proc/loadavg").read().strip(),
        "cpus": a.cpus, "ctl_cpu": a.ctl_cpu, "threads": a.threads, "spin_us": a.spin_us,
        "warmup_s": a.warmup, "measure_s": a.measure, "points_us": POINTS_US, "arms": ARMS,
        "cohort_flags": COHORT_FLAGS, "G_us": G_US, "T_us": G_US, "names": {"A": A_NAME, "B": B_NAME},
    }
    os.makedirs(a.out, exist_ok=True)
    lib.write_json(os.path.join(a.out, "meta.json"), meta)
    print(json.dumps({"meta": "ok", "kernel": rel, "sched_ext": meta["sched_ext"]}))


def cmd_calibrate(a):
    single = p05.calib(a.p05_bin, a.cal_cpu, 20_000_000, 5)  # existing convention, kept for reference
    conc = gsshm.calib_concurrent(os.path.join(a.p05_bin, "barrier"), p05.expand_cpus(a.cpus))
    ipu = conc["iters_per_us"]  # used for every conversion (see EXPERIMENT_SPEC.md, calibration)
    cal = {"method": "concurrent: median over CPUs 1-4 calibrating at the same time", "iters_per_us": ipu,
           "concurrent": conc, "single_cpu": single, "single_over_concurrent": single["iters_per_us"] / ipu,
           "iters": {str(x): int(round(ipu * x)) for x in POINTS_US}}
    lib.write_json(os.path.join(a.out, "calibration.json"), cal)
    print(json.dumps(cal))


def cmd_preflight(a):
    os.sched_setaffinity(0, {a.ctl_cpu})
    res = preflight(a, a.out)
    print(json.dumps({k: res[k] for k in ("preflight", "busy", "killed")}), flush=True)
    if res["preflight"] != "ok":
        sys.exit(30)


def cmd_rep(a):
    os.sched_setaffinity(0, {a.ctl_cpu})
    pf = preflight(a, a.out)
    print(json.dumps({k: pf[k] for k in ("preflight", "busy", "killed")}), flush=True)
    if pf["preflight"] != "ok":
        print("STOP: experiment CPUs not idle before rep; see preflight.jsonl", file=sys.stderr)
        sys.exit(30)
    cal = json.load(open(os.path.join(a.out, "calibration.json")))
    rundir = os.path.join(a.out, f"rep{a.rep}")
    os.makedirs(rundir, exist_ok=True)
    cfgs = configs(a.set)
    random.Random(a.seed * 1000 + a.rep).shuffle(cfgs)
    head = {"rep": a.rep, "seed": a.seed, "set": a.set, "order": cfgs, "warmup": a.warmup, "measure": a.measure,
            "cmdline": open("/proc/cmdline").read().strip(), "preflight": pf,
            "speed_before": p05.speed_check(argparse.Namespace(bin=a.p05_bin, cpus=a.cpus)),
            "loadavg_before": open("/proc/loadavg").read().strip(),
            "utc_start": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "bin_sha256": {p: lib.sha256_file(p) for p in (a.gsbarrier, os.path.join(a.sched_bin, "scx_cohort"))}}
    lib.write_json(os.path.join(rundir, "rep.json"), head)
    runs = os.path.join(a.out, "runs.jsonl")
    for i, (e, x, arm) in enumerate(cfgs):
        t0 = time.time()
        rec = run_one(a, cal, rundir, i, e, x, arm, a.seed * 100000 + a.rep * 1000 + i)
        rec["rep"] = a.rep
        lib.append_jsonl(runs, rec)
        print(f"rep{a.rep} {i + 1}/{len(cfgs)} {arm:6s} {e:8s} x={x or 0:>5} "
              f"{'FAILED ' + ';'.join(rec['notes']) if rec['failed'] else 'ok'}"
              f"{' NOTE ' + ';'.join(rec['notes']) if rec['notes'] and not rec['failed'] else ''} ({time.time() - t0:.1f}s)",
              flush=True)
    head["speed_after"] = p05.speed_check(argparse.Namespace(bin=a.p05_bin, cpus=a.cpus))
    head["loadavg_after"] = open("/proc/loadavg").read().strip()
    head["utc_end"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    lib.write_json(os.path.join(rundir, "rep.json"), head)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["meta", "k3s-stop", "k3s-restore", "calibrate", "preflight", "rep"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--gsf-root", default="/opt/gangsched/gsf")
    ap.add_argument("--gsbarrier", default="/opt/gangsched/bin/gsf/gsbarrier")
    ap.add_argument("--p05-bin", default="/opt/gangsched/bin/p05")
    ap.add_argument("--sched-bin", default="/opt/gangsched/bin")
    ap.add_argument("--cpus", default="1-4")
    ap.add_argument("--ctl-cpu", type=int, default=0)
    ap.add_argument("--cal-cpu", type=int, default=1)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--spin-us", type=int, default=50)
    ap.add_argument("--rep", type=int, default=0)
    ap.add_argument("--seed", type=int, default=2011)
    ap.add_argument("--warmup", type=float, default=3.0)
    ap.add_argument("--measure", type=float, default=10.0)
    ap.add_argument("--set", choices=["main", "pilot"], default="main")
    a = ap.parse_args()
    a.bin = a.p05_bin  # p05.cmd_k3s_* only use a.out
    for sig in (signal.SIGHUP, signal.SIGTERM):  # Ctrl-C on the Mac closes ssh: clean up like p10.py
        signal.signal(sig, lambda n, f: sys.exit(128 + n))
    {"meta": cmd_meta, "k3s-stop": p05.cmd_k3s_stop, "k3s-restore": p05.cmd_k3s_restore,
     "calibrate": cmd_calibrate, "preflight": cmd_preflight, "rep": cmd_rep}[a.cmd](a)


if __name__ == "__main__":
    main()
