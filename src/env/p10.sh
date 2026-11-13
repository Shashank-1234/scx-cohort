#!/usr/bin/env bash
# P10-01 (scx_cohort evaluation) driver, run on the Mac host from src/env/:  bash p10.sh smoke|run
# Syncs src/, builds the benchmark in the VM, stops k3s (restored on exit), calibrates,
# runs REPS reps, records host load around each rep, then pulls results into
# experiments/2026-11-04_P10-01_cohort-eval/results/<stamp>/.
# Works with macOS bash 3.2. Keep the Mac awake: caffeinate -i make p10-run
set -euo pipefail
cd "$(dirname "$0")"
mode=${1:-run}
[[ $mode == smoke || $mode == run ]] || { echo "usage: p10.sh smoke|run" >&2; exit 2; }
reps=${REPS:-3}; seed=${SEED:-$(date +%s)}
[[ $mode == smoke ]] && reps=1
set_=${SET:-main}
if [[ $set_ == skew ]]; then
  # P10-02: label the run by the guest's actual boot option, so on/off runs can't be confused
  st=0; bash guest.sh grep -qw skew_tick=1 /proc/cmdline && st=1
  stamp=$(date -u +%Y%m%dT%H%M%SZ)_P10-02_skew${st}_$mode
  remote=/opt/gangsched/results/P10-02/$stamp
  local_dir=../../experiments/2026-11-04_P10-02_skew-tick/results
else
  stamp=$(date -u +%Y%m%dT%H%M%SZ)_P10-01_$mode
  remote=/opt/gangsched/results/P10-01/$stamp
  local_dir=../../experiments/2026-11-04_P10-01_cohort-eval/results
fi
H=(sudo python3 /opt/gangsched/src/harness/p10.py)
extra=(--set "$set_"); [[ $mode == smoke ]] && extra+=(--smoke --warmup 1 --measure 5)

echo "== $stamp  reps=$reps seed=$seed"
bash sync.sh
bash guest.sh sudo make -s -C /opt/gangsched/src/bench OUT=/opt/gangsched/bin/p05
bash guest.sh sudo test -x /opt/gangsched/bin/scx_cohort -a -x /opt/gangsched/bin/scx_simple || { echo "STOP: build scx_simple and scx_cohort first (make simple-build cohort-build)" >&2; exit 20; }
bash guest.sh "${H[@]}" meta --out "$remote"
hostlog() { echo "$(date -u +%FT%TZ) $1 host_loadavg=$(sysctl -n vm.loadavg) $(sysctl -n machdep.cpu.brand_string)" \
  | bash guest.sh sudo tee -a "$remote/host.txt" >/dev/null; }
hostlog start
bash guest.sh "${H[@]}" k3s-stop --out "$remote"
finish() {
  bash guest.sh "${H[@]}" preflight --out "$remote" >/dev/null 2>&1 || true  # sweep strays on any exit
  bash guest.sh "${H[@]}" k3s-restore --out "$remote" || echo "WARN: k3s restore failed; run: limactl shell ebpf-gang-sched sudo systemctl start k3s" >&2
  hostlog end || true
  bash pull.sh "$remote" "$local_dir" || echo "WARN: pull failed; rerun: bash pull.sh $remote $local_dir" >&2
}
trap finish EXIT
bash guest.sh "${H[@]}" preflight --out "$remote" || { echo "STOP: VM CPUs 1-4 busy after sweeping strays (see preflight output)" >&2; exit 30; }
bash guest.sh "${H[@]}" calibrate --out "$remote"
for ((i = 0; i < reps; i++)); do
  hostlog "rep$i-before"
  bash guest.sh "${H[@]}" rep --out "$remote" --rep "$i" --seed "$seed" "${extra[@]}"
  hostlog "rep$i-after"
done
echo "== done: $stamp"
