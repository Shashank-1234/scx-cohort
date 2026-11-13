#!/usr/bin/env bash
# Shared experiment runner, Mac side:  make exp E=experiments/<dir>      (SMOKE=1 for the spec's smoke variant)
# Syncs src/ to the VM, builds the competitor, runs every batch of <dir>/spec.json with the guest runner
# (src/harness/runner.sh), pulls the raw results to <dir>/results/<UTC>[_smoke]/ and writes summary.{md,csv}
# there (src/harness/summarize.py). Design: src/harness/RUNNER.md. Works with macOS bash 3.2.
# Interrupt (Ctrl-C / SIGINT / SIGTERM / SIGHUP): the guest runner is sent SIGINT and cleans up itself
# (workload, competitor and scheduler stopped, k3s restored); then a sweep, a k3s check, the pull and the
# summary still run, so a partial run is never lost or left half-cleaned.
# Extra env: TAG=<word> appends _<word> to the results dir name (e.g. TAG=interrupt-test).
set -euo pipefail
cd "$(dirname "$0")"                                   # src/env
root=$(cd ../.. && pwd)
E=${1:?usage: exp.sh experiments/<dir>}; E=${E%/}
[[ -f $root/$E/spec.json ]] || { echo "no spec: $E/spec.json" >&2; exit 2; }
if [[ -z ${EXP_CAFFEINATED:-} ]]; then export EXP_CAFFEINATED=1; exec caffeinate -i bash "$PWD/exp.sh" "$@"; fi

mode=run; [[ ${SMOKE:-0} == 1 ]] && mode=smoke
stamp=$(date -u +%Y%m%dT%H%M%SZ); [[ $mode == smoke ]] && stamp+=_smoke; [[ -n ${TAG:-} ]] && stamp+=_$TAG
name=$(basename "$E")
remote=/opt/gangsched/results/$name/$stamp
ldir=$root/$E/results/$stamp
mkdir -p "$ldir"
exec > >(tee -a "$ldir/exp.log") 2>&1
G() { bash guest.sh "$@"; }
hostlog() { echo "$(date -u +%FT%TZ) $1 loadavg=$(sysctl -n vm.loadavg | tr -d '{}') power=$(pmset -g batt 2>/dev/null | sed -n "1s/.*'\(.*\)'.*/\1/p" | tr ' ' _) lowpower=$(pmset -g 2>/dev/null | awk '/lowpowermode/ {print $2}')" >> "$ldir/host.txt"; }

batches=$(python3 -I -c "import json,sys; print(' '.join(b['name'] for b in json.load(open(sys.argv[1]))['batches']))" "$root/$E/spec.json")
echo "== $name $stamp mode=$mode batches: $batches"
echo "== results: $E/results/$stamp"
bash sync.sh
G sudo make -s -C /opt/gangsched/src/bench OUT=/opt/gangsched/bin/p05
G sudo test -x /opt/gangsched/bin/scx_cohort -a -x /opt/gangsched/bin/scx_simple || { echo "STOP: build scx_simple and scx_cohort first (make simple-build cohort-build)" >&2; exit 20; }
G sudo test -f /opt/gangsched/workloads/SHA256SUMS || { echo "STOP: no /opt/gangsched/workloads (run: bash run.sh workloads-setup)" >&2; exit 20; }
G sudo mkdir -p "$remote"
G sudo tee "$remote/spec.json" < "$root/$E/spec.json" > /dev/null
G sudo cp /opt/gangsched/workloads/SHA256SUMS "$remote/workloads.SHA256SUMS"
k3s_at_start=$(G systemctl is-active k3s || true)
hostlog "start k3s=$k3s_at_start"

gpid=; interrupted=0; failed=
stop_guest() {   # SIGINT the guest runner (only the root bash running runner.sh), wait up to 60 s for its cleanup
  G sudo pkill -INT -f '^bash /opt/gangsched/src/harness/runner.sh' 2>/dev/null || true
  for _ in $(seq 1 60); do G pgrep -f '^bash /opt/gangsched/src/harness/runner.sh' > /dev/null 2>&1 || break; sleep 1; done
}
on_signal() { trap '' INT TERM HUP; interrupted=1; echo "== interrupted: stopping the guest runner"; stop_guest; exit 130; }
finish() {
  local rc=$?
  trap - EXIT; trap '' INT TERM HUP
  set +e
  if [[ -n $gpid ]] && kill -0 "$gpid" 2>/dev/null; then stop_guest; fi
  G sudo bash /opt/gangsched/src/env/guest/sweep.sh > "$ldir/sweep.txt" 2>&1; echo "== sweep: $(tail -1 "$ldir/sweep.txt")"
  local k; k=$(G systemctl is-active k3s)
  if [[ $k3s_at_start == active && $k != active ]]; then echo "== k3s was active at start, now $k: starting it"; G sudo systemctl start k3s; fi
  hostlog "end rc=$rc k3s=$(G systemctl is-active k3s) sched_ext=$(G cat /sys/kernel/sched_ext/state)"
  bash pull.sh "$remote" "$root/$E/results" > /dev/null && echo "== pulled raw results" || echo "WARN: pull failed; rerun: bash src/env/pull.sh $remote $E/results"
  python3 -I "$root/src/harness/summarize.py" "$ldir" || echo "WARN: summary failed"
  echo "== end: rc=$rc interrupted=$interrupted failed_batches=${failed:-none}; results in $E/results/$stamp"
}
trap finish EXIT
trap on_signal INT TERM HUP

for b in $batches; do
  hostlog "batch $b start"
  # background + wait, so a signal reaches our trap at once instead of after the batch
  G sudo bash /opt/gangsched/src/harness/runner.sh "$remote/spec.json" "$b" "$remote/$b" "$mode" < /dev/null & gpid=$!
  brc=0; wait "$gpid" || brc=$?; gpid=
  hostlog "batch $b end rc=$brc"
  if [[ $brc == 130 ]]; then interrupted=1; echo "== batch $b interrupted in the guest; stopping"; exit 130; fi
  [[ $brc == 0 ]] || { failed+="$b(rc$brc) "; echo "WARN: batch $b ended with rc $brc (reported; continuing)"; }
done
