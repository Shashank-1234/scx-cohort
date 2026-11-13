#!/usr/bin/env bash
# Shared experiment runner, guest side: one batch (workload + knob) of an experiment spec.
#   sudo bash /opt/gangsched/src/harness/runner.sh SPEC.json BATCH OUTDIR run|smoke
# Generalised from experiments/2026-11-12_EXPLORE_real-workloads/explore.sh (arms.sh). Design: src/harness/RUNNER.md.
# Per batch: sweep leftovers of ours, require sched_ext disabled, record meta, stop k3s, one unrecorded
# warm-up (sets the timeout), then REPS reps of the spec's arms in a per-rep shuffled order. Per arm: own dir,
# one line in arms.jsonl (raw facts only: status, monotonic timestamps, competitor counters; parsing is
# summarize.py's job). On every exit path (end, error, SIGINT/TERM/HUP, broken pipe): kill the workload and
# competitor, unload the scheduler, restore k3s, report leftovers. Never prints paths outside /opt/gangsched.
set -euo pipefail
spec=${1:?spec} batch=${2:?batch} out=${3:?outdir} mode=${4:?run|smoke}
[[ $mode == run || $mode == smoke ]] || { echo "mode must be run or smoke" >&2; exit 2; }
H=/opt/gangsched/src/harness; G=/opt/gangsched/bin; W=/opt/gangsched/workloads
OURS="/opt/gangsched/(bin|workloads)/"
SHM=/dev/shm/exp_comp
SPID=; CPID=; WPID=; K3S_WAS=unknown; CUR_ARM=; CUR_REP=
say() { echo "$@" 2>/dev/null || true; }          # never die on a closed terminal
state() { cat /sys/kernel/sched_ext/state; }
mono() { python3 -I -c 'import time; print(time.monotonic_ns())'; }

# ---- spec -> shell variables (validated; no free-form strings reach the shell)
specvars=$(python3 -I - "$spec" "$batch" "$mode" <<'PY'
import json, re, shlex, sys
spec, bname, mode = json.load(open(sys.argv[1])), sys.argv[2], sys.argv[3]
ok = lambda pat, v: re.fullmatch(pat, str(v)) or sys.exit(f"spec: bad value {v!r}")
bl = [b for b in spec["batches"] if b["name"] == bname] or sys.exit(f"spec: no batch {bname}")
b = bl[0]; bidx = spec["batches"].index(b)
params = dict(b.get("params", {}))
reps = spec["reps"]
if mode == "smoke":
    sm = spec.get("smoke", {}); reps = sm.get("reps", 1)
    params.update(sm.get("params", {}).get(b["workload"], {}))
t = spec["timeout"]
q = shlex.quote
print(f"REPS={int(reps)}; SEED={int(spec['seed'])}; BIDX={bidx}")
print(f"WORKLOAD={q(ok(r'[a-z0-9-]+', b['workload'])[0])}; KNOB={q(ok(r'[a-z0-9_-]+', b['knob'])[0])}")
print(f"CPUS={q(ok(r'[0-9,-]+', spec['cpus'])[0])}")
print(f"COMP_BIN={q(ok(r'[a-z0-9_/]+', spec['competitor']['bin'])[0])}; COMP_THREADS={int(spec['competitor']['threads'])}")
print(f"COMP_ALONE_S={float(spec['comp_alone_s'])}; TF={float(t['factor'])}; TMIN={float(t['min_s'])}; WCAP={float(t.get('warmup_cap_s', 600))}")
for k, v in params.items():
    ok(r'[a-z_]+', k); ok(r'[A-Za-z0-9._-]+', v); print(f"export P_{k}={q(str(v))}")
print("ARMS=(" + " ".join(ok(r'[a-z_]+', a["name"])[0] for a in spec["arms"]) + ")")
print("declare -gA A_SCHED A_COMP A_WL")
for a in spec["arms"]:
    s = a.get("sched") or ""
    if s:
        ok(r'scx_[a-z0-9_]+', s.split()[0])
        for tok in s.split()[1:]: ok(r'[A-Za-z0-9._,{}-]+', tok)
    print(f"A_SCHED[{a['name']}]={q(s)}; A_COMP[{a['name']}]={int(bool(a.get('competitor')))}; A_WL[{a['name']}]={int(a.get('workload', True))}")
PY
)
eval "$specvars"

# shellcheck source=/dev/null
source "$H/workloads/$WORKLOAD.sh"
wl_init "$KNOB"
mkdir -p "$out"

stop_all() {
  local p
  for p in $WPID $CPID; do kill -KILL "$p" 2>/dev/null || true; done
  for p in $WPID $CPID; do wait "$p" 2>/dev/null || true; done
  pkill -KILL -f "$W/" 2>/dev/null || true
  pkill -KILL -f "$G/(p05|gsf)/" 2>/dev/null || true
  # scheduler: SIGINT lets the loader detach cleanly; SIGKILL after 5 s (the kernel unloads it anyway)
  if [[ -n $SPID ]]; then kill -INT "$SPID" 2>/dev/null || true; fi
  pkill -INT -f "$G/scx_" 2>/dev/null || true
  for _ in $(seq 1 20); do [[ $(state) == disabled ]] && break; sleep 0.25; done
  pkill -KILL -f "$G/scx_" 2>/dev/null || true
  if [[ -n $SPID ]]; then wait "$SPID" 2>/dev/null || true; fi
  rm -f "$SHM"
  SPID=; CPID=; WPID=
}
cleanup() {
  local rc=$?
  trap - EXIT INT TERM HUP
  trap '' PIPE
  set +e
  stop_all
  if [[ -n $CUR_ARM ]]; then   # interrupted mid-arm: a failed rep, recorded, never dropped
    echo "{\"arm\":\"$CUR_ARM\",\"rep\":$CUR_REP,\"status\":\"interrupted\",\"exit\":$rc,\"dir\":\"r${CUR_REP}_$CUR_ARM\"}" >> "$out/arms.jsonl"
  fi
  if [[ $K3S_WAS == active ]]; then systemctl start k3s; fi
  echo "k3s_restored=$(systemctl is-active k3s) exit=$rc" >> "$out/meta.txt"
  say "== $batch end (exit $rc): sched_ext $(state); k3s $(systemctl is-active k3s) (was $K3S_WAS)"
  local left; left=$(pgrep -fc "$OURS" || true)
  [[ ${left:-0} == 0 ]] && say "== clean: no processes of ours left" || say "WARN: $left process(es) of ours left; run: make sweep"
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP PIPE

preflight() {   # sweep leftovers of ours, require sched_ext disabled
  local left; left=$(pgrep -fc "$OURS" || true)
  if [[ ${left:-0} != 0 ]]; then say "== sweeping $left leftover process(es) of ours"; echo "swept=$left" >> "$out/preflight.txt"; stop_all; fi
  [[ $(state) == disabled ]] || { say "STOP: sched_ext is $(state), not loaded by us"; exit 21; }
}
shm() {  # read | ready | stop     struct shm_ctr: iters[16] at 16, ready at 212, stop at 216 (src/bench/barrier/common.h)
  python3 -I - "$SHM" "$1" <<'PY'
import mmap, struct, sys, time
f = open(sys.argv[1], "r+b"); m = mmap.mmap(f.fileno(), 224)
if sys.argv[2] == "read":
    print(sum(struct.unpack_from("<16Q", m, 16)), time.monotonic_ns())
elif sys.argv[2] == "ready":
    print(struct.unpack_from("<i", m, 212)[0])
else:
    struct.pack_into("<i", m, 216, 1)
PY
}
busy() {  # % busy of the experiment CPUs over 1 s
  python3 -I - "$CPUS" <<'PY'
import sys, time
cpus = []
for part in sys.argv[1].split(","):
    a, _, b = part.partition("-"); cpus += range(int(a), int(b or a) + 1)
def rd():
    d = {}
    for l in open("/proc/stat"):
        f = l.split()
        if f[0][3:].isdigit(): v = list(map(int, f[1:])); d[int(f[0][3:])] = (v[3] + v[4], sum(v))
    return d
a = rd(); time.sleep(1); b = rd()
print(" ".join("cpu%d=%.0f" % (c, 100 * (1 - (b[c][0] - a[c][0]) / max(1, b[c][1] - a[c][1]))) for c in cpus))
PY
}

# ---- metadata
{
  echo "batch=$batch workload=$WORKLOAD knob=$KNOB mode=$mode reps=$REPS seed=$SEED batch_index=$BIDX cpus=$CPUS"
  { env | grep '^P_' || true; } | sort | sed 's/^/param /'
  echo "date_utc=$(date -u +%FT%TZ)"; echo "kernel=$(uname -r)"; echo "hz=$(grep -m1 '^CONFIG_HZ=' /boot/config-$(uname -r) 2>/dev/null || echo unknown)"
  echo "sched_ext=$(state)"
  echo "src_id=$(cat /opt/gangsched/src/SRC_ID 2>/dev/null || echo unknown)"
  echo "--- sha256 (schedulers, competitor, workload)"
  sha256sum $G/scx_cohort $G/scx_simple $G/$COMP_BIN "${WL_HASH[@]}"
  echo "--- workload"; wl_meta
  echo "--- tools"; echo "gcc=$(gcc --version | sed -n 1p)"; echo "python3=$(python3 --version 2>&1)"; echo "bash=$BASH_VERSION"
  echo "--- lscpu"; lscpu
  echo "--- smt"; cat /sys/devices/system/cpu/smt/control 2>/dev/null || echo "no smt control"
  for c in /sys/devices/system/cpu/cpu[0-9]*; do echo "$(basename $c) siblings=$(cat $c/topology/thread_siblings_list 2>/dev/null) gov=$(cat $c/cpufreq/scaling_governor 2>/dev/null || echo n/a) khz=$(cat $c/cpufreq/scaling_cur_freq 2>/dev/null || echo n/a)"; done
  echo "--- cmdline"; cat /proc/cmdline
} > "$out/meta.txt" 2>&1

preflight
K3S_WAS=$(systemctl is-active k3s || true)
if [[ $K3S_WAS == active ]]; then systemctl stop k3s; fi
echo "k3s_was=$K3S_WAS k3s_during=$(systemctl is-active k3s || true) k3s_mainpid=$(systemctl show -p MainPID --value k3s) kubepods_procs_left=$(cat /sys/fs/cgroup/kubepods*/**/cgroup.procs 2>/dev/null | wc -l)" >> "$out/meta.txt"
say "== $batch ($WORKLOAD $KNOB, $mode): k3s was $K3S_WAS, now $(systemctl is-active k3s || true)"

load_sched() {  # $1 = arm, $2 = arm dir; scheduler command from the spec, {comm} and {stats} filled in
  local spec_cmd=${A_SCHED[$1]} d=$2 w; local -a cmd=()
  [[ -z $spec_cmd ]] && return 0
  for w in $spec_cmd; do w=${w//\{comm\}/$WL_COMM}; w=${w//\{stats\}/$d/stats.jsonl}; cmd+=("$w"); done
  cmd[0]=$G/${cmd[0]}
  echo "sched_cmd=${cmd[*]}" >> "$d/arm.txt"
  "${cmd[@]}" > "$d/sched.out" 2> "$d/sched.err" < /dev/null & SPID=$!
  for _ in $(seq 1 40); do [[ $(state) == enabled ]] && break; sleep 0.25; done
  [[ $(state) == enabled ]] || return 1
  echo "sched_t0_mono=$(mono) ops=$(cat /sys/kernel/sched_ext/root/ops 2>/dev/null)" >> "$d/arm.txt"
}

run_arm() {  # $1 = arm, $2 = rep
  local arm=$1 rep=$2 d="$out/r$2_$1" rc=0 st=none c0=0 t0=0 c1=0 t1=0 w0 w1 s0=0
  CUR_ARM=$arm; CUR_REP=$rep
  mkdir -p "$d"; preflight
  echo "busy_before=$(busy)" > "$d/arm.txt"
  if ! load_sched "$arm" "$d"; then
    stop_all
    echo "{\"arm\":\"$arm\",\"rep\":$rep,\"status\":\"sched_load_fail\",\"dir\":\"r${rep}_$arm\"}" >> "$out/arms.jsonl"
    CUR_ARM=; say "   rep $rep $arm: scheduler did not load"; return 0
  fi
  [[ -n $SPID ]] && s0=$(sed -n 's/^sched_t0_mono=\([0-9]*\).*/\1/p' "$d/arm.txt")
  if [[ ${A_COMP[$arm]} == 1 ]]; then
    $G/$COMP_BIN --threads "$COMP_THREADS" --cpus "$CPUS" --shm "$SHM" > "$d/competitor.json" 2> "$d/competitor.err" < /dev/null & CPID=$!
    for _ in $(seq 1 50); do [[ -e $SHM && $(shm ready 2>/dev/null || echo 0) == "$COMP_THREADS" ]] && break; sleep 0.1; done
    sleep 0.5
    read -r c0 t0 < <(shm read)
  fi
  w0=$(mono)
  if [[ ${A_WL[$arm]} == 1 ]]; then
    wl "$TMO" > "$d/workload.out" 2> "$d/workload.err" < /dev/null & WPID=$!
    wait "$WPID" || rc=$?
    WPID=
  else
    sleep "$COMP_ALONE_S"
  fi
  w1=$(mono)
  if [[ ${A_COMP[$arm]} == 1 ]]; then
    read -r c1 t1 < <(shm read); shm stop
    for _ in $(seq 1 50); do kill -0 "$CPID" 2>/dev/null || break; sleep 0.1; done
    wait "$CPID" 2>/dev/null || true; CPID=
  fi
  if [[ -n $SPID ]]; then
    st=$(state); kill -0 "$SPID" 2>/dev/null || st="loader_exited_$st"
    kill -INT "$SPID" 2>/dev/null || true; wait "$SPID" 2>/dev/null || true; SPID=
    for _ in $(seq 1 20); do [[ $(state) == disabled ]] && break; sleep 0.25; done
    dmesg 2>/dev/null | grep -i sched_ext | tail -3 > "$d/dmesg.txt" || true
  fi
  local status=ok
  [[ $rc == 124 ]] && status=timeout
  [[ $rc != 0 && $rc != 124 ]] && status=fail
  [[ -n ${A_SCHED[$arm]} && $st != enabled ]] && status=detached
  echo "busy_after=$(busy) sched_state_at_end=$st" >> "$d/arm.txt"
  printf '{"arm":"%s","rep":%d,"status":"%s","rc":%d,"dir":"%s","timeout_s":%s,"w0_mono":%s,"w1_mono":%s,"sched_t0_mono":%s,"sched_state_at_end":"%s","comp_c0":%s,"comp_t0":%s,"comp_c1":%s,"comp_t1":%s}\n' \
    "$arm" "$rep" "$status" "$rc" "r${rep}_$arm" "$TMO" "$w0" "$w1" "${s0:-0}" "$st" "$c0" "$t0" "$c1" "$t1" >> "$out/arms.jsonl"
  CUR_ARM=
  say "   rep $rep $arm: $status rc=$rc wall=$(( (w1 - w0) / 1000000 )) ms"
}

# ---- warm-up (not recorded in arms.jsonl): sets this batch's timeout = max(TMIN, TF x warm-up wall)
preflight
mkdir -p "$out/warmup"
TMO=$WCAP
w0=$(mono); wl "$WCAP" > "$out/warmup/workload.out" 2> "$out/warmup/workload.err" < /dev/null & WPID=$!
wrc=0; wait "$WPID" || wrc=$?; WPID=; w1=$(mono)
TMO=$(python3 -I -c "import math,sys; print(max(float(sys.argv[1]), math.ceil(float(sys.argv[2]) * (int(sys.argv[4]) - int(sys.argv[3])) / 1e9)))" "$TMIN" "$TF" "$w0" "$w1")
echo "warmup_rc=$wrc warmup_wall_ms=$(( (w1 - w0) / 1000000 )) timeout_s=$TMO" >> "$out/meta.txt"
say "== warm-up rc=$wrc wall=$(( (w1 - w0) / 1000000 )) ms -> timeout ${TMO} s"
[[ $wrc == 0 ]] || { say "STOP: warm-up failed (rc $wrc); batch not run"; exit 22; }

for rep in $(seq 1 "$REPS"); do
  order=$(python3 -I -c "import random,sys; a=sys.argv[2].split(); random.Random(sys.argv[1]).shuffle(a); print(' '.join(a))" "$SEED:$batch:$rep" "${ARMS[*]}")
  say "== rep $rep order: $order"; echo "rep $rep order: $order" >> "$out/meta.txt"
  for arm in $order; do run_arm "$arm" "$rep"; done
done
say "== $batch done: $(grep -c . "$out/arms.jsonl") arm records"
