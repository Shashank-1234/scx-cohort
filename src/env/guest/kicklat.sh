#!/usr/bin/env bash
# Cross-vCPU preemption latency on the VM (run as root in the guest: cd src/env && bash run.sh kicklat).
# Workload: spin-barrier pool (4 x p05w, 100 us work/round) + 4 CPU-bound competitors on CPUs 1-4.
# For scx_cohort tick-only (Ctick) and boundary+wake kick (Ckick), G = T = 10 ms, 4 s each:
#  1. raw IPI latency      ftrace ipi_raise (sender) -> ipi_entry (target), reschedule IPIs, trace_clock mono
#  2. kick -> switch       scx_cohort trace: boundary kick to CPU c -> next class change on c
#  3. edge -> switch       window start -> first task of the window's class running on each CPU
# Read-only apart from ftrace settings (restored on exit). No boot or kernel change.
set -euo pipefail
T=/sys/kernel/tracing
B=/opt/gangsched/bin/p05
S=/opt/gangsched/bin/scx_cohort
SECS=${SECS:-4}
OUT=/tmp/kicklat; rm -rf $OUT; mkdir -p $OUT
cleanup() {
  echo 0 > $T/tracing_on; echo > $T/set_event; echo local > $T/trace_clock
  pkill -f "$B/barrier --threads" 2>/dev/null || true; pkill -f "$B/competitor --threads" 2>/dev/null || true
  pkill -INT -x scx_cohort 2>/dev/null || true; rm -f /dev/shm/kl_w /dev/shm/kl_c
}
trap cleanup EXIT
echo "kernel $(uname -r) $(uname -m)  cmdline: $(cat /proc/cmdline)"
[[ $(cat /sys/kernel/sched_ext/state) == disabled ]] || { echo "sched_ext busy"; exit 1; }
IPU=$($B/barrier --calibrate --cpus 1 --cal-iters 20000000 --cal-reps 3 | python3 -c 'import json,sys;print(json.load(sys.stdin)["iters_per_us"])')
ITERS=$(python3 -c "print(int(0.94*$IPU*100))")   # ~100 us with 4 busy vCPUs
for arm in tick kick; do
  flags=(); [[ $arm == kick ]] && flags=(-b -k)
  $S -m 1-4 -n 4 -G 10000 -T 10000 -o $OUT/$arm.stats -t $OUT/$arm.csv "${flags[@]}" > $OUT/$arm.sched 2>&1 &
  for i in $(seq 50); do [[ $(cat /sys/kernel/sched_ext/state) == enabled ]] && break; sleep 0.1; done
  $B/competitor --threads 4 --cpus 1-4 --shm /dev/shm/kl_c > /dev/null 2>&1 &
  $B/barrier --threads 4 --cpus 1-4 --mode spin --iters $ITERS --shm /dev/shm/kl_w > /dev/null 2>&1 &
  sleep 1
  echo 0 > $T/tracing_on; echo > $T/trace; echo mono > $T/trace_clock; echo 16384 > $T/buffer_size_kb
  echo 'ipi:ipi_raise ipi:ipi_entry' > $T/set_event
  echo 1 > $T/tracing_on; sleep $SECS; echo 0 > $T/tracing_on
  cat $T/trace > $OUT/$arm.ipi
  pkill -f "$B/barrier --threads"; pkill -f "$B/competitor --threads"; sleep 0.3
  pkill -INT -x scx_cohort
  for i in $(seq 100); do [[ $(cat /sys/kernel/sched_ext/state) == disabled ]] && break; sleep 0.1; done
done
echo 1408 > $T/buffer_size_kb
python3 - "$OUT" <<'PY'
import csv, re, sys, statistics as st, collections
out = sys.argv[1]
G = P2 = 10_000_000; P = 20_000_000
q = lambda xs, p: sorted(xs)[min(len(xs) - 1, int(p * len(xs)))]
def show(name, xs):
    if not xs:
        print(f"  {name}: none"); return
    print(f"  {name}: n={len(xs)}  median {st.median(xs):.1f}  p90 {q(xs,.9):.1f}  p99 {q(xs,.99):.1f}  max {max(xs):.1f} us")
for arm in ("tick", "kick"):
    print(f"== {'Ctick (tick-only)' if arm == 'tick' else 'Ckick (-b -k)'}")
    # 1. raw reschedule IPIs: sender raise -> entry on each target CPU
    pend = collections.defaultdict(list); lat = []
    for line in open(f"{out}/{arm}.ipi"):
        m = re.search(r"\[(\d+)\].*?(\d+\.\d+): ipi_raise: target_mask=([0-9a-f,]+) \(([^)]*)\)", line)
        if m and "Rescheduling" in m.group(4):
            mask = int(m.group(3).replace(",", ""), 16); t = float(m.group(2))
            for c in range(64):
                if mask >> c & 1: pend[c].append(t)
            continue
        m = re.search(r"\[(\d+)\].*?(\d+\.\d+): ipi_entry: \(([^)]*)\)", line)
        if m and "Rescheduling" in m.group(3):
            c, t = int(m.group(1)), float(m.group(2))
            if pend[c]:
                t0 = pend[c].pop(0)
                if 0 <= t - t0 < 0.05: lat.append((t - t0) * 1e6)
    show("reschedule IPI, send -> handler entry on target", lat)
    # 2./3. from the scheduler's own trace (one clock: scx_bpf_now)
    ev = list(csv.DictReader(open(f"{out}/{arm}.csv")))
    for e in ev:
        for k in ("ts", "widx", "cpu", "type", "cls", "in_gang"): e[k] = int(e[k])
    runs = collections.defaultdict(list)
    for e in ev:
        if e["type"] == 1 and 1 <= e["cpu"] <= 4: runs[e["cpu"]].append(e)
    if arm == "kick":
        kl = []
        for e in ev:
            if e["type"] == 2:
                nxt = next((r for r in runs[e["cpu"]] if r["ts"] >= e["ts"]), None)
                if nxt and nxt["ts"] - e["ts"] < 2_000_000: kl.append((nxt["ts"] - e["ts"]) / 1000)
        show("boundary kick sent -> target CPU switches class", kl)
    edge = []
    for c, rs in runs.items():
        seen = set()
        for r in rs:
            w = r["widx"]; want = 1 if w % 2 == 0 else 2
            if r["cls"] == want and w not in seen:
                seen.add(w); start = (w // 2) * P + (w % 2) * G
                edge.append((r["ts"] - start) / 1000)
    show("window edge -> right class running, per CPU", edge)
PY
