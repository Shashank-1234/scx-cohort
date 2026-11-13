#!/usr/bin/env python3
"""P10-01 scx_cohort evaluation harness (runs as root inside the VM).

Same workload, counters and normalisation as P0.5 (p05.py), plus sched_ext arms:
  A0 workers alone (EEVDF)          C0 competitors alone (EEVDF)
  E  contended, EEVDF                S  contended, unmodified scx_simple
  Coff  scx_cohort, windows off      Ctick scx_cohort, windows, tick bound only
  Cwake + wake kick                  Ckick + wake kick + boundary kick
  U10 userspace gang windows (gangsim, 10 ms), reference only
  G sweep (spin only): Ctick/Ckick with G = T = 5 ms and 20 ms
Subcommands: meta, k3s-stop, k3s-restore, calibrate (reuse p05), preflight, rep.
preflight kills stray benchmark/scheduler processes (ours only) and refuses to go on unless
the experiment CPUs are idle; it also runs at the start of every rep.
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
import p05  # noqa: E402

MODES = ["spin", "adaptive", "block"]
W_US = [10, 100]
MAIN_ARMS = ["A0", "E", "S", "Coff", "Ctick", "Cwake", "Ckick", "U10"]
COHORT = {  # arm -> (extra loader flags, G=T in us)
    "Coff": (["-w"], 10000), "Ctick": ([], 10000), "Cwake": (["-k"], 10000), "Ckick": (["-b", "-k"], 10000),
    "Ctick5": ([], 5000), "Ctick20": ([], 20000), "Ckick5": (["-b", "-k"], 5000), "Ckick20": (["-b", "-k"], 20000),
}
GSWEEP = ["Ctick5", "Ctick20", "Ckick5", "Ckick20"]


SKEW_ARMS = ["A0", "E", "Ctick", "Ckick", "Ctick5", "Ckick5", "Ctick20", "Ckick20"]


def configs(smoke, cset="main"):
    if cset == "skew":  # P10-02: spin only, tick-only vs boundary kick at G = 5/10/20 ms
        return [("spin", w, arm) for w in W_US for arm in SKEW_ARMS] + [(None, None, "C0")]
    if smoke:
        return [("spin", 100, a) for a in ["A0", "E", "S", "Coff", "Ctick", "Ckick"]] + \
               [("block", 100, "Cwake"), (None, None, "C0")]
    out = [(m, w, a) for m in MODES for w in W_US for a in MAIN_ARMS]
    out += [("spin", w, a) for w in W_US for a in GSWEEP]
    out.append((None, None, "C0"))
    return out


def sched_cmd(a, arm, rd):
    if arm == "S":
        return [os.path.join(a.sched_bin, "scx_simple")], "simple"
    if arm in COHORT:
        flags, g = COHORT[arm]
        return ([os.path.join(a.sched_bin, "scx_cohort"), "-m", a.cpus, "-n", str(a.threads),
                 "-G", str(g), "-T", str(g), "-o", os.path.join(rd, "cohort_stats.jsonl")] + flags), "cohort"
    return None, None


def wait_state(want, ops=None, timeout=10.0):
    t_end = time.time() + timeout
    while time.time() < t_end:
        st = lib.sched_ext_state()
        if st["state"] == want and (ops is None or st["ops"] == ops):
            return True
        time.sleep(0.1)
    return False


def run_one(a, cal, rundir, idx, mode, w, arm):
    tag = f"{idx:03d}_{arm}_{mode or 'na'}_{w or 0}"
    rd = os.path.join(rundir, tag)
    os.makedirs(rd, exist_ok=True)
    rec = {"idx": idx, "arm": arm, "mode": mode, "w_us": w, "tag": tag, "failed": False, "notes": []}
    procs, shms, files = {}, {}, []
    gs = sched = None
    try:
        pre = lib.sched_ext_state()
        rec["sched_ext_pre"] = pre
        if pre["state"] not in ("disabled", None):
            rec["failed"] = True
            rec["notes"].append(f"sched_ext not disabled before run: {pre}")
            return rec
        cmd, ops = sched_cmd(a, arm, rd)
        if cmd:
            rec["sched_cmd"] = cmd
            fo = open(os.path.join(rd, "sched.out"), "w"); files.append(fo)
            sched = subprocess.Popen(cmd, stdout=fo, stderr=subprocess.STDOUT)
            if not wait_state("enabled", ops):
                rec["failed"] = True
                rec["notes"].append(f"scheduler did not attach: {lib.sched_ext_state()}")
                return rec
            rec["sched_ops"] = ops
        if arm != "A0":
            path = f"/dev/shm/p10_comp_{os.getpid()}"
            fo = open(os.path.join(rd, "competitor.out"), "w"); files.append(fo)
            procs["comp"] = subprocess.Popen([os.path.join(a.bin, "competitor"), "--threads", str(a.threads),
                                              "--cpus", a.cpus, "--shm", path], stdout=fo, stderr=subprocess.STDOUT)
            shms["comp"] = lib.Shm(path)
        if arm != "C0":
            iters = cal["iters"][str(w)]
            rec["iters"] = iters
            path = f"/dev/shm/p10_work_{os.getpid()}"
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
        if arm == "U10":
            gs = subprocess.Popen([os.path.join(a.bin, "gangsim"), "--wpid", str(procs["worker"].pid),
                                   "--cpid", str(procs["comp"].pid), "--g-us", "10000", "--t-us", "10000",
                                   "--cpu", str(a.ctl_cpu), "--fifo", "50", "--log", os.path.join(rd, "gangsim.csv")],
                                  stdout=subprocess.DEVNULL, stderr=open(os.path.join(rd, "gangsim.err"), "w"))
        time.sleep(a.warmup)
        s0 = p05.snapshot(procs, shms)
        time.sleep(max(0.0, a.measure - (time.monotonic_ns() - s0["t_ns"]) / 1e9))
        s1 = p05.snapshot(procs, shms)
        rec["s0"], rec["s1"] = s0, s1
        rec["measure_s"] = (s1["t_ns"] - s0["t_ns"]) / 1e9
        want = "enabled" if sched else "disabled"
        if s1["sched_ext"]["state"] not in (want, None) or (sched and s1["sched_ext"]["ops"] != ops):
            rec["failed"] = True
            rec["notes"].append(f"scheduler state changed during run: {s1['sched_ext']}")
        if sched and sched.poll() is not None:
            rec["failed"] = True
            rec["notes"].append(f"scheduler exited early rc={sched.returncode}")
    except Exception as e:
        rec["failed"] = True
        rec["notes"].append(f"exception: {e!r}")
    finally:
        if gs is not None:
            gs.send_signal(signal.SIGTERM)
            try:
                gs.wait(timeout=5)
            except subprocess.TimeoutExpired:
                gs.kill()
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
                p.kill(); p.wait()
                rec["failed"] = True
                rec["notes"].append(f"{role} did not exit; killed")
        if sched is not None:
            if sched.poll() is None:
                sched.send_signal(signal.SIGINT)
                try:
                    sched.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    sched.kill(); sched.wait()
                    rec["notes"].append("scheduler killed after SIGINT timeout")
            rec["sched_rc"] = sched.returncode
            if not wait_state("disabled", timeout=15):
                rec["failed"] = True
                rec["notes"].append(f"sched_ext not disabled after unload: {lib.sched_ext_state()}")
        for s in shms.values():
            s.close()
        for fo in files:
            fo.close()
        for role in ("comp", "work"):
            try:
                os.unlink(f"/dev/shm/p10_{role}_{os.getpid()}")
            except OSError:
                pass
        try:  # scheduler exit reason (UEI) and kernel messages, for ejections
            rec["dmesg_tail"] = lib.sh("dmesg | grep -i sched_ext | tail -3")
        except Exception:
            pass
    return rec


IDLE_MAX = 0.10  # max non-idle fraction per experiment CPU before a rep may start


def preflight(a, out=None):
    exact = tuple(os.path.join(a.sched_bin, b) for b in ("scx_cohort", "scx_simple"))
    killed = lib.kill_strays([a.bin], exact)
    ok_state = wait_state("disabled", timeout=15)
    busy = lib.cpu_busy(p05.expand_cpus(a.cpus), 2.0)
    res = {"preflight": "ok", "killed": killed, "sched_ext": lib.sched_ext_state(), "busy": busy,
           "loadavg": open("/proc/loadavg").read().strip()}
    if not ok_state or max(busy.values()) > IDLE_MAX:
        res["preflight"] = "FAIL"
        res["top"] = lib.sh("ps -eo pid,etime,pcpu,psr,comm --sort=-pcpu | head -12")
    if out:
        lib.append_jsonl(os.path.join(out, "preflight.jsonl"), res)
    return res


def cmd_preflight(a):
    os.sched_setaffinity(0, {a.ctl_cpu})
    res = preflight(a, a.out)
    print(json.dumps(res), flush=True)
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
    cfgs = configs(a.smoke, a.set)
    random.Random(a.seed * 1000 + a.rep).shuffle(cfgs)
    head = {"rep": a.rep, "seed": a.seed, "order": cfgs, "smoke": a.smoke, "set": a.set, "cmdline": open("/proc/cmdline").read().strip(), "warmup": a.warmup,
            "measure": a.measure, "preflight": pf, "speed_before": p05.speed_check(a),
            "loadavg_before": open("/proc/loadavg").read().strip(),
            "utc_start": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "sched_sha256": {b: lib.sha256_file(os.path.join(a.sched_bin, b))
                             for b in ("scx_simple", "scx_cohort") if os.path.exists(os.path.join(a.sched_bin, b))}}
    lib.write_json(os.path.join(rundir, "rep.json"), head)
    runs = os.path.join(a.out, "runs.jsonl")
    for i, (m, w, arm) in enumerate(cfgs):
        t0 = time.time()
        rec = run_one(a, cal, rundir, i, m, w, arm)
        rec["rep"] = a.rep
        lib.append_jsonl(runs, rec)
        print(f"rep{a.rep} {i + 1}/{len(cfgs)} {arm:7s} {m or '-':8s} W={w or 0:>5} "
              f"{'FAILED ' + ';'.join(rec['notes']) if rec['failed'] else 'ok'} ({time.time() - t0:.1f}s)", flush=True)
    head["speed_after"] = p05.speed_check(a)
    head["loadavg_after"] = open("/proc/loadavg").read().strip()
    head["utc_end"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    lib.write_json(os.path.join(rundir, "rep.json"), head)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["meta", "k3s-stop", "k3s-restore", "calibrate", "preflight", "rep"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--bin", default="/opt/gangsched/bin/p05")
    ap.add_argument("--sched-bin", default="/opt/gangsched/bin")
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
    ap.add_argument("--set", choices=["main", "skew"], default="main")
    a = ap.parse_args()
    # Ctrl-C on the Mac reaches us as SIGHUP/SIGTERM (ssh session closes): turn it into SystemExit
    # so run_one's finally block stops the workload and unloads the scheduler (no orphans).
    for sig in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(sig, lambda n, f: sys.exit(128 + n))
    if a.cmd == "rep":
        cmd_rep(a)
    elif a.cmd == "preflight":
        cmd_preflight(a)
    else:
        {"meta": p05.cmd_meta, "k3s-stop": p05.cmd_k3s_stop, "k3s-restore": p05.cmd_k3s_restore,
         "calibrate": p05.cmd_calibrate}[a.cmd](a)


if __name__ == "__main__":
    main()
