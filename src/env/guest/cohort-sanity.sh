#!/usr/bin/env bash
# Load scx_cohort with debug printk on CPUs 1-4, run the barrier pool + competitors 10 s,
# show the callback's trace_pipe lines (progress-report screenshot), the stats, then unload.
# Run in guest as root after sync + cohort-build + p05 bench build (make -C /opt/gangsched/src/bench).
#
# Safe to rerun and to interrupt:
#  - leftovers of ours from an earlier run are swept first (only binaries under /opt/gangsched/bin);
#  - every exit path (normal end, error, Ctrl-C, hang-up, broken pipe) kills the workload and
#    unloads the scheduler;
#  - background jobs never hold the terminal, so the ssh session ends when the script ends.
set -euo pipefail
G=/opt/gangsched/bin; B=$G/p05; S=$G/scx_cohort
out=/opt/gangsched/results/cohort-sanity; mkdir -p "$out"
SPID=; TPID=; WPID=; CPID=

say() { echo "$@" 2>/dev/null || true; }          # never die on a closed terminal
state() { cat /sys/kernel/sched_ext/state; }

stop_all() {
  # 1. workload: ours only, SIGKILL (spinning threads, nothing to flush)
  local p
  for p in $WPID $CPID; do kill -KILL "$p" 2>/dev/null || true; done
  for p in $WPID $CPID; do wait "$p" 2>/dev/null || true; done   # reap quietly (no "Killed" notices)
  pkill -KILL -f "$G/(p05|gsf)/" 2>/dev/null || true
  # 2. scheduler: SIGINT lets the loader detach cleanly; SIGKILL after 5 s (the kernel unloads it anyway)
  if [[ -n $SPID ]]; then kill -INT "$SPID" 2>/dev/null || true; fi
  pkill -INT -f "$G/scx_" 2>/dev/null || true
  for _ in $(seq 1 20); do [[ $(state) == disabled ]] && break; sleep 0.25; done
  pkill -KILL -f "$G/scx_" 2>/dev/null || true
  # 3. trace reader (self-limited by timeout anyway) and shared-memory files
  if [[ -n $TPID ]]; then kill "$TPID" 2>/dev/null || true; fi
  rm -f /dev/shm/cs_comp /dev/shm/cs_work
}

cleanup() {
  trap - EXIT INT TERM HUP
  trap '' PIPE
  set +e
  stop_all
  say "== state after unload: $(state)"
  say "== kernel messages"; dmesg | grep -i sched_ext | tail -5 2>/dev/null
  left=$(pgrep -fc "$G/" || true)
  [[ ${left:-0} == 0 ]] && say "== clean: no processes of ours left" || say "WARN: $left process(es) of ours still running; run: make sweep"
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP PIPE

# Sweep leftovers from an earlier run, then require that no other sched_ext scheduler is active.
left=$(pgrep -fc "$G/" || true)
if [[ ${left:-0} != 0 ]]; then say "== sweeping $left leftover process(es) of ours from an earlier run"; stop_all; fi
[[ $(state) == disabled ]] || { say "STOP: another sched_ext scheduler is active (not ours)"; exit 21; }

mount | grep -q ' /sys/kernel/tracing ' || mount -t tracefs nodev /sys/kernel/tracing 2>/dev/null || true
TP=/sys/kernel/tracing/trace_pipe; [[ -r $TP ]] || TP=/sys/kernel/debug/tracing/trace_pipe
iters=$(python3 -c "import json;print(json.load(open('/opt/gangsched/results/cal.json'))['iters']['100'])" 2>/dev/null || echo 105000)

# Background jobs: stdin from /dev/null, output to files, so none of them holds the terminal.
timeout 15 cat "$TP" > "$out/trace_pipe.txt" 2>/dev/null < /dev/null & TPID=$!
"$S" -d -m 1-4 -G 10000 -T 10000 -b -k -o "$out/stats.jsonl" > "$out/loader.out" 2> "$out/loader.err" < /dev/null & SPID=$!
for _ in $(seq 1 40); do [[ $(state) == enabled ]] && break; sleep 0.25; done
say "== state: $(state)  ops: $(cat /sys/kernel/sched_ext/root/ops 2>/dev/null || cat /sys/kernel/sched_ext/root_ops)"
[[ $(state) == enabled ]] || { say "FAIL: not enabled"; cat "$out/loader.err"; exit 1; }
"$B/competitor" --threads 4 --cpus 1-4 --shm /dev/shm/cs_comp > "$out/competitor.out" 2>&1 < /dev/null & CPID=$!
"$B/barrier" --threads 4 --cpus 1-4 --mode spin --iters "$iters" --shm /dev/shm/cs_work > "$out/worker.out" 2>&1 < /dev/null & WPID=$!
sleep 10

say "== loader"; cat "$out/loader.err"
say "== last stats"; tail -1 "$out/stats.jsonl"
# Unload before reading the trace so the last lines are flushed; the rest is cleaned up on exit.
kill -INT "$SPID" 2>/dev/null || true; wait "$SPID" 2>/dev/null || true
sleep 1; kill "$TPID" 2>/dev/null || true
say "== scx_cohort callback messages (trace_pipe, first 8)"; { grep scx_cohort "$out/trace_pipe.txt" || true; } | sed -n 1,8p
if grep -q 'scx_cohort: running pool task' "$out/trace_pipe.txt"; then say "PASS: scx_cohort callback ran (see lines above)"; else say "WARN: no callback lines captured"; fi
