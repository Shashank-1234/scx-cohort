"""Shared helpers for the run harness (P0.5 onward). Stdlib only; runs as root in the VM."""
import glob
import hashlib
import json
import mmap
import os
import struct
import subprocess
import signal
import time

# Layout of struct shm_ctr in src/bench/barrier/common.h
SHM_SIZE = 224
OFF_MAGIC, OFF_ROUNDS, OFF_ITERS, OFF_TIDS = 0, 8, 16, 144
OFF_NTHREADS, OFF_READY, OFF_STOP = 208, 212, 216
SHM_MAGIC = 0x503035434F554E54


class Shm:
    def __init__(self, path, wait_s=5.0):
        t_end = time.time() + wait_s
        while True:
            try:
                if os.path.getsize(path) >= SHM_SIZE:
                    break
            except OSError:
                pass
            if time.time() > t_end:
                raise RuntimeError(f"shm {path} not created")
            time.sleep(0.01)
        self.f = open(path, "r+b")
        self.m = mmap.mmap(self.f.fileno(), SHM_SIZE)

    def _u64(self, off):
        # read until two consecutive reads agree (avoids a torn read of a moving counter)
        a = struct.unpack_from("<Q", self.m, off)[0]
        for _ in range(4):
            b = struct.unpack_from("<Q", self.m, off)[0]
            if a == b:
                return a
            a = b
        return a

    def i32(self, off):
        return struct.unpack_from("<i", self.m, off)[0]

    def magic_ok(self):
        return self._u64(OFF_MAGIC) == SHM_MAGIC

    def rounds(self):
        return self._u64(OFF_ROUNDS)

    def iters(self):
        n = self.i32(OFF_NTHREADS)
        return [self._u64(OFF_ITERS + 8 * i) for i in range(n)]

    def ready(self):
        return self.i32(OFF_READY)

    def nthreads(self):
        return self.i32(OFF_NTHREADS)

    def set_stop(self):
        struct.pack_into("<i", self.m, OFF_STOP, 1)
        self.m.flush()

    def close(self):
        self.m.close()
        self.f.close()


def task_stats(pid):
    """Per-thread schedstat (runtime_ns, wait_ns, slices) and context switches."""
    out = {}
    for tdir in glob.glob(f"/proc/{pid}/task/*"):
        tid = os.path.basename(tdir)
        try:
            with open(f"{tdir}/schedstat") as f:
                run_ns, wait_ns, slices = (int(x) for x in f.read().split())
            vol = nonvol = None
            with open(f"{tdir}/status") as f:
                for line in f:
                    if line.startswith("voluntary_ctxt_switches"):
                        vol = int(line.split()[1])
                    elif line.startswith("nonvoluntary_ctxt_switches"):
                        nonvol = int(line.split()[1])
            out[tid] = {"run_ns": run_ns, "wait_ns": wait_ns, "slices": slices, "vcsw": vol, "ivcsw": nonvol}
        except (OSError, ValueError):
            pass
    return out


def thread_states(pid):
    st = []
    for tdir in glob.glob(f"/proc/{pid}/task/*"):
        try:
            with open(f"{tdir}/stat") as f:
                s = f.read()
            st.append(s[s.rindex(")") + 2])
        except (OSError, ValueError):
            pass
    return st


def proc_stat_cpus():
    rows = {}
    with open("/proc/stat") as f:
        for line in f:
            if line.startswith("cpu"):
                p = line.split()
                rows[p[0]] = [int(x) for x in p[1:]]
    return rows  # fields: user nice system idle iowait irq softirq steal guest guest_nice


def sched_ext_state():
    def rd(p):
        try:
            with open(p) as f:
                return f.read().strip()
        except OSError:
            return None
    ops = rd("/sys/kernel/sched_ext/root/ops") or rd("/sys/kernel/sched_ext/root_ops")
    return {"state": rd("/sys/kernel/sched_ext/state"), "ops": ops}


def sh(cmd, check=False, timeout=60):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"{cmd}: rc={r.returncode} {r.stderr.strip()}")
    return r.stdout.strip()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def append_jsonl(path, obj):
    with open(path, "a") as f:
        f.write(json.dumps(obj, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())


def kill_strays(prefixes, exact=(), grace=3.0):
    """SIGINT then SIGKILL every process whose executable is under one of `prefixes`
    or equals one of `exact` (our benchmark and scheduler binaries only). Returns what it found."""
    found = []
    me = os.getpid()
    for d in os.listdir("/proc"):
        if not d.isdigit() or int(d) == me:
            continue
        try:
            exe = os.readlink(f"/proc/{d}/exe").replace(" (deleted)", "")
        except OSError:
            continue
        if exe in exact or any(exe.startswith(pf.rstrip("/") + "/") for pf in prefixes):
            found.append({"pid": int(d), "exe": exe})
    for f in found:
        try:
            os.kill(f["pid"], signal.SIGCONT)
            os.kill(f["pid"], signal.SIGINT)
        except ProcessLookupError:
            pass
    t_end = time.time() + grace
    while found and time.time() < t_end and any(os.path.exists(f"/proc/{f['pid']}") for f in found):
        time.sleep(0.1)
    for f in found:
        try:
            os.kill(f["pid"], signal.SIGKILL)
            f["sigkill"] = True
        except ProcessLookupError:
            pass
    return found


def cpu_busy(cpus, secs=2.0):
    """Fraction of time each listed CPU was non-idle over `secs` (from /proc/stat)."""
    a = proc_stat_cpus()
    time.sleep(secs)
    b = proc_stat_cpus()
    out = {}
    for c in cpus:
        x, y = a[f"cpu{c}"], b[f"cpu{c}"]
        tot = sum(y[:8]) - sum(x[:8])
        idle = (y[3] + y[4]) - (x[3] + x[4])
        out[c] = round(1.0 - idle / tot, 4) if tot > 0 else 0.0
    return out
