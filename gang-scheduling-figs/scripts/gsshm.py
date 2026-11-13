"""Reader for gsbarrier's shared memory: lib.Shm (struct shm_ctr, 224 bytes) plus the
per-worker slots gsbarrier.c places at offset 256 + 64 * id. Runs inside the VM."""
import mmap
import os
import struct
import sys
import time

HARNESS = os.environ.get("GANGSCHED_HARNESS", "/opt/gangsched/src/harness")
sys.path.insert(0, HARNESS)
import lib  # noqa: E402  (src/harness/lib.py)

SLOT_OFF, SLOT_SIZE, MAX_THREADS = 256, 64, 16
SLOT_FIELDS = ("rounds", "work_iters", "waits", "blocks", "futex_calls", "spin_ns_sum", "spin_ns_max")
TOTAL = SLOT_OFF + SLOT_SIZE * MAX_THREADS


class GsShm(lib.Shm):
    def __init__(self, path, wait_s=5.0):
        t_end = time.time() + wait_s
        while True:
            try:
                if os.path.getsize(path) >= TOTAL:
                    break
            except OSError:
                pass
            if time.time() > t_end:
                raise RuntimeError(f"shm {path} not created")
            time.sleep(0.01)
        super().__init__(path, wait_s)
        self.big = mmap.mmap(self.f.fileno(), TOTAL)

    def slots(self):
        out = []
        for i in range(self.nthreads()):
            vals = struct.unpack_from("<7Q", self.big, SLOT_OFF + SLOT_SIZE * i)
            out.append(dict(zip(SLOT_FIELDS, vals)))
        return out

    def close(self):
        self.big.close()
        super().close()


def calib_concurrent(cal_bin, cpus, n=20_000_000, reps=5):
    """Work rate with every listed CPU busy at once (one `barrier --calibrate` per CPU, started
    together). On this VM one busy vCPU runs ~6% faster than four busy ones (tests T6, 2026-11-05),
    and the workers always run with all four busy, so this is the rate used to convert us -> iterations."""
    import json
    import statistics
    import subprocess
    ps = [subprocess.Popen([cal_bin, "--calibrate", "--cpus", str(c), "--cal-iters", str(n), "--cal-reps", str(reps)],
                           stdout=subprocess.PIPE, text=True) for c in cpus]
    per = [json.loads(p.communicate(timeout=120)[0]) for p in ps]
    return {"iters_per_us": statistics.median(r["iters_per_us"] for r in per), "per_cpu": per}
