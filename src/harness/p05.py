#!/usr/bin/env python3
"""P0.5 gang-feasibility harness (runs as root inside the VM).

Subcommands (all write into --out):
  meta        machine / kernel / build metadata -> meta.json
  k3s-stop    stop k3s if active, record whether it was -> k3s.json
  k3s-restore start k3s again if it was active before
  calibrate   iterations/us on --cal-cpu -> calibration.json (work counts for each W)
  rep         one repetition: shuffled configs -> runs.jsonl (+ per-run raw files)

Arms: A0 workers alone, C0 competitors alone, E contended EEVDF, U10/U40 contended +
userspace gang windows (SIGSTOP/SIGCONT, G = T = 10 / 40 ms).
"""
import argparse
import json
import os
import random
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib  # noqa: E402

MODES = ["spin", "block", "adaptive"]
W_US = [10, 100, 1000]
ARMS = ["A0", "E", "U10", "U40"]
WINDOW_US = {"U10": 10000, "U40": 40000}


def configs(smoke):
    if smoke:
        return [("spin", 100, "A0"), ("spin", 100, "E"), ("spin", 100, "U10"),
                ("block", 100, "E"), ("adaptive", 100, "U40"), (None, None, "C0")]
    out = [(m, w, a) for m in MODES for w in W_US for a in ARMS]
    out.append((None, None, "C0"))
    return out


def cmd_meta(a):
    bins = {b: lib.sha256_file(os.path.join(a.bin, b)) for b in ("barrier", "competitor", "gangsim")
            if os.path.exists(os.path.join(a.bin, b))}
    rel = lib.sh("uname -r")
    meta = {
        "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "uname": lib.sh("uname -a"),
        "cmdline": open("/proc/cmdline").read().strip(),
        "config_hz": lib.sh(f"grep -E '^CONFIG_(HZ|HZ_[0-9]+|NO_HZ[A-Z_]*|PREEMPT[A-Z_]*)=' /boot/config-{rel} || true"),
        "nproc": lib.sh("nproc"),
        "lscpu": lib.sh("lscpu"),
        "sched_ext": lib.sched_ext_state(),
        "src_id": lib.sh("cat /opt/gangsched/src/SRC_ID 2>/dev/null || true"),
        "bin_sha256": bins,
        "gcc": lib.sh("gcc --version | head -1"),
        "python": sys.version.split()[0],
        "k3s_active": lib.sh("systemctl is-active k3s || true"),
        "loadavg": open("/proc/loadavg").read().strip(),
        "cpus": a.cpus, "ctl_cpu": a.ctl_cpu, "threads": a.threads,
    }
    os.makedirs(a.out, exist_ok=True)
    lib.write_json(os.path.join(a.out, "meta.json"), meta)
    print(json.dumps({"meta": "ok", "sched_ext": meta["sched_ext"], "hz": meta["config_hz"].splitlines()[:3]}))


def cmd_k3s_stop(a):
    was = lib.sh("systemctl is-active k3s || true")
    rec = {"was_active": was == "active", "before": was}
    if rec["was_active"]:
        lib.sh("systemctl stop k3s", check=True, timeout=120)
    rec["after"] = lib.sh("systemctl is-active k3s || true")
    # k3s KillMode=process can leave containers running; record, do not kill.
    rec["kubepods_procs_left"] = lib.sh("cat /sys/fs/cgroup/kubepods*/**/cgroup.procs 2>/dev/null | wc -l || true")
    os.makedirs(a.out, exist_ok=True)
    lib.write_json(os.path.join(a.out, "k3s.json"), rec)
    print(json.dumps(rec))


def cmd_k3s_restore(a):
    p = os.path.join(a.out, "k3s.json")
    rec = json.load(open(p)) if os.path.exists(p) else {"was_active": False}
    if rec.get("was_active"):
        lib.sh("systemctl start k3s", timeout=180)
    rec["restored_state"] = lib.sh("systemctl is-active k3s || true")
    lib.write_json(p, rec)
    print(json.dumps({"k3s_restored": rec["restored_state"], "was_active": rec.get("was_active")}))


def calib(bin_dir, cpu, n, reps):
    out = subprocess.run([os.path.join(bin_dir, "barrier"), "--calibrate", "--cpus", str(cpu),
                          "--cal-iters", str(n), "--cal-reps", str(reps)],
                         capture_output=True, text=True, check=True, timeout=120)
    return json.loads(out.stdout)


def cmd_calibrate(a):
    c = calib(a.bin, a.cal_cpu, 20_000_000, 5)
    ipu = c["iters_per_us"]
    cal = {"cal_cpu": a.cal_cpu, "raw": c, "iters_per_us": ipu,
           "iters": {str(w): max(1, int(round(ipu * w))) for w in W_US}}
    lib.write_json(os.path.join(a.out, "calibration.json"), cal)
    print(json.dumps(cal))


def speed_check(a):
    return {str(c): calib(a.bin, c, 5_000_000, 3) for c in expand_cpus(a.cpus)}


def expand_cpus(spec):
    out = []
    for tok in spec.split(","):
        if "-" in tok:
            lo, hi = tok.split("-")
            out += list(range(int(lo), int(hi) + 1))
        else:
            out.append(int(tok))
    return out


def snapshot(procs, shms):
    t = time.monotonic_ns()
    snap = {"t_ns": t, "stat": lib.proc_stat_cpus(), "sched_ext": lib.sched_ext_state()}
    for role, p in procs.items():
        snap[f"{role}_tasks"] = lib.task_stats(p.pid)
    if "worker" in shms:
        snap["rounds"] = shms["worker"].rounds()
    if "comp" in shms:
        snap["comp_iters"] = shms["comp"].iters()
    return snap


def run_one(a, cal, rundir, idx, mode, w, arm):
    tag = f"{idx:03d}_{arm}_{mode or 'na'}_{w or 0}"
    rd = os.path.join(rundir, tag)
    os.makedirs(rd, exist_ok=True)
    rec = {"idx": idx, "arm": arm, "mode": mode, "w_us": w, "tag": tag, "failed": False, "notes": []}
    procs, shms, files = {}, {}, []
    gs = None
    try:
        pre = lib.sched_ext_state()
        rec["sched_ext_pre"] = pre
        if pre["state"] is None:
            rec["notes"].append("no sched_ext sysfs (not the VM?)")
        elif pre["state"] != "disabled":
            rec["failed"] = True
            rec["notes"].append(f"sched_ext not disabled before run: {pre}")
            return rec
        if arm != "A0":
            path = f"/dev/shm/p05_comp_{os.getpid()}"
            fo = open(os.path.join(rd, "competitor.out"), "w"); files.append(fo)
            procs["comp"] = subprocess.Popen([os.path.join(a.bin, "competitor"), "--threads", str(a.threads),
                                              "--cpus", a.cpus, "--shm", path], stdout=fo, stderr=subprocess.STDOUT)
            shms["comp"] = lib.Shm(path)
        if arm != "C0":
            iters = cal["iters"][str(w)]
            rec["iters"] = iters
            path = f"/dev/shm/p05_work_{os.getpid()}"
            fo = open(os.path.join(rd, "worker.out"), "w"); files.append(fo)
            procs["worker"] = subprocess.Popen([os.path.join(a.bin, "barrier"), "--threads", str(a.threads),
                                                "--cpus", a.cpus, "--mode", mode, "--iters", str(iters),
                                                "--spin-us", str(a.spin_us), "--shm", path],
                                               stdout=fo, stderr=subprocess.STDOUT)
            shms["worker"] = lib.Shm(path)
        t_end = time.time() + 10
        for s in shms.values():
            while s.ready() < a.threads:
                if time.time() > t_end:
                    raise RuntimeError("threads not ready")
                time.sleep(0.005)
        if arm in WINDOW_US:
            win = WINDOW_US[arm]
            gs = subprocess.Popen([os.path.join(a.bin, "gangsim"), "--wpid", str(procs["worker"].pid),
                                   "--cpid", str(procs["comp"].pid), "--g-us", str(win), "--t-us", str(win),
                                   "--cpu", str(a.ctl_cpu), "--fifo", "50", "--log", os.path.join(rd, "gangsim.csv")],
                                  stdout=subprocess.DEVNULL, stderr=open(os.path.join(rd, "gangsim.err"), "w"))
            rec["window_us"] = win
        time.sleep(a.warmup)
        s0 = snapshot(procs, shms)
        if a.smoke and arm in WINDOW_US:
            samples = []
            for _ in range(40):
                samples.append({"w": "".join(sorted(lib.thread_states(procs["worker"].pid))),
                                "c": "".join(sorted(lib.thread_states(procs["comp"].pid)))})
                time.sleep(0.005)
            rec["state_samples"] = samples
        time.sleep(max(0.0, a.measure - (time.monotonic_ns() - s0["t_ns"]) / 1e9))
        s1 = snapshot(procs, shms)
        rec["s0"], rec["s1"] = s0, s1
        rec["measure_s"] = (s1["t_ns"] - s0["t_ns"]) / 1e9
        if s1["sched_ext"]["state"] not in ("disabled", None):
            rec["failed"] = True
            rec["notes"].append(f"sched_ext changed during run: {s1['sched_ext']}")
    except Exception as e:  # report, never hide
        rec["failed"] = True
        rec["notes"].append(f"exception: {e!r}")
    finally:
        if gs is not None:
            gs.send_signal(signal.SIGTERM)
            try:
                gs.wait(timeout=5)
            except subprocess.TimeoutExpired:
                gs.kill()
                rec["notes"].append("gangsim had to be killed")
        for s in shms.values():
            s.set_stop()
        for p in procs.values():
            try:
                os.kill(p.pid, signal.SIGCONT)
            except ProcessLookupError:
                pass
        for role, p in procs.items():
            try:
                p.wait(timeout=10)
                rec[f"{role}_rc"] = p.returncode
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
                rec["failed"] = True
                rec["notes"].append(f"{role} did not exit; killed")
        for s in shms.values():
            s.close()
        for fo in files:
            fo.close()
        for role in ("comp", "work"):
            try:
                os.unlink(f"/dev/shm/p05_{role}_{os.getpid()}")
            except OSError:
                pass
    return rec


def cmd_rep(a):
    os.sched_setaffinity(0, {a.ctl_cpu})
    cal = json.load(open(os.path.join(a.out, "calibration.json")))
    rundir = os.path.join(a.out, f"rep{a.rep}")
    os.makedirs(rundir, exist_ok=True)
    cfgs = configs(a.smoke)
    rng = random.Random(a.seed * 1000 + a.rep)
    rng.shuffle(cfgs)
    head = {"rep": a.rep, "seed": a.seed, "order": cfgs, "smoke": a.smoke, "warmup": a.warmup,
            "measure": a.measure, "speed_before": speed_check(a), "loadavg_before": open("/proc/loadavg").read().strip(),
            "utc_start": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    lib.write_json(os.path.join(rundir, "rep.json"), head)
    runs = os.path.join(a.out, "runs.jsonl")
    for i, (m, w, arm) in enumerate(cfgs):
        t0 = time.time()
        rec = run_one(a, cal, rundir, i, m, w, arm)
        rec["rep"] = a.rep
        lib.append_jsonl(runs, rec)
        print(f"rep{a.rep} {i + 1}/{len(cfgs)} {arm:4s} {m or '-':8s} W={w or 0:>5} "
              f"{'FAILED ' + ';'.join(rec['notes']) if rec['failed'] else 'ok'} ({time.time() - t0:.1f}s)", flush=True)
    head["speed_after"] = speed_check(a)
    head["loadavg_after"] = open("/proc/loadavg").read().strip()
    head["utc_end"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    lib.write_json(os.path.join(rundir, "rep.json"), head)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["meta", "k3s-stop", "k3s-restore", "calibrate", "rep"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--bin", default="/opt/gangsched/bin/p05")
    ap.add_argument("--cpus", default="1-4")
    ap.add_argument("--ctl-cpu", type=int, default=0)
    ap.add_argument("--cal-cpu", type=int, default=1)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--spin-us", type=int, default=50)
    ap.add_argument("--rep", type=int, default=0)
    ap.add_argument("--seed", type=int, default=506)
    ap.add_argument("--warmup", type=float, default=5.0)
    ap.add_argument("--measure", type=float, default=30.0)
    ap.add_argument("--smoke", action="store_true")
    a = ap.parse_args()
    {"meta": cmd_meta, "k3s-stop": cmd_k3s_stop, "k3s-restore": cmd_k3s_restore,
     "calibrate": cmd_calibrate, "rep": cmd_rep}[a.cmd](a)


if __name__ == "__main__":
    main()
