#!/usr/bin/env bash
# Run as root in the VM: bash src/env/run.sh tickphase   (or: guest.sh sudo bash -s < guest/tickphase.sh)
# Measure ACTUAL scheduler-tick entry times per CPU (not the programmed expiry).
# ftrace timer:hrtimer_expire_entry for the tick hrtimer, trace_clock=mono, CPUs 1-4 kept busy.
set -euo pipefail
T=/sys/kernel/tracing
[[ -d $T/events ]] || mount -t tracefs nodev $T
echo "cmdline: $(cat /proc/cmdline)"
echo "sched_ext: $(cat /sys/kernel/sched_ext/state)  k3s: $(systemctl is-active k3s || true)"
grep -E "tick_(nohz_handler|sched_timer)" /proc/kallsyms | sed -n 1,2p
C=/opt/gangsched/bin/p05/competitor
$C --threads 4 --cpus 1-4 --shm /dev/shm/tickchk </dev/null >/dev/null 2>&1 & P=$!
trap 'kill $P 2>/dev/null || true; rm -f /dev/shm/tickchk; echo 0 > $T/tracing_on; echo > $T/set_event; echo local > $T/trace_clock; echo $(printf %x $(( (1 << $(nproc)) - 1 ))) > $T/tracing_cpumask; echo 1408 > $T/buffer_size_kb; true' EXIT
sleep 1
echo 0 > $T/tracing_on
echo > $T/trace
echo mono > $T/trace_clock
echo 8192 > $T/buffer_size_kb
echo 1e > $T/tracing_cpumask          # CPUs 1-4
echo hrtimer_expire_entry > $T/set_event
echo 1 > $T/tracing_on
sleep ${SECS:-3}
echo 0 > $T/tracing_on
cat $T/trace > /tmp/tick_trace.txt
python3 - <<'PY'
import re, collections, statistics as st
rows = []
for line in open("/tmp/tick_trace.txt"):
    m = re.search(r"\[(\d+)\].*hrtimer_expire_entry: .*function=(\S+) now=(\d+)", line)
    if m and m.group(2).startswith("tick_"):
        rows.append((int(m.group(1)), int(m.group(3))))
# The tick grid has a fixed offset inside each 1 ms; take it from the earliest-arriving 10 % of entries.
phase = sorted(now % 1_000_000 for _, now in rows)
grid = phase[len(phase) // 10] if phase else 0
by = collections.defaultdict(dict)
late = collections.defaultdict(list)
for cpu, now in rows:
    k = (now - grid + 200_000) // 1_000_000   # period index; entries up to 800 us late stay in their period
    by[k][cpu] = now
    late[cpu].append(((now - grid + 200_000) % 1_000_000 - 200_000) / 1000)   # us after the grid point
spreads = sorted((max(d.values()) - min(d.values())) / 1000 for d in by.values() if len(d) == 4)
q = lambda xs, p: xs[min(len(xs) - 1, int(p * len(xs)))]
print(f"tick events on CPUs 1-4: {len(rows)}; periods with all 4 CPUs: {len(spreads)} of {len(by)}")
for cpu in sorted(late):
    xs = sorted(late[cpu])
    print(f"  cpu{cpu}: n={len(xs)}  entry after grid point (us): median {st.median(xs):.1f}  p90 {q(xs,.9):.1f}  p99 {q(xs,.99):.1f}  max {xs[-1]:.1f}")
if spreads:
    print(f"cross-CPU spread of actual tick entry within one tick (us): median {st.median(spreads):.1f}  "
          f"p90 {q(spreads,.9):.1f}  p99 {q(spreads,.99):.1f}  max {spreads[-1]:.1f}")
    print(f"  share of ticks with spread < 10 us: {sum(s < 10 for s in spreads)/len(spreads):.3f}; "
          f"< 50 us: {sum(s < 50 for s in spreads)/len(spreads):.3f}; > 250 us: {sum(s > 250 for s in spreads)/len(spreads):.3f}")
PY
