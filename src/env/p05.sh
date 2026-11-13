#!/usr/bin/env bash
# P0.5 driver, run on the Mac host from src/env/:  bash p05.sh smoke|run
# Syncs src/, builds the benchmark in the VM, stops k3s (restored on exit), calibrates,
# runs REPS reps, records host load around each rep, then pulls results into
# experiments/2026-11-04_P05-01_gang-feasibility/results/<stamp>/.
# Works with macOS bash 3.2. Keep the Mac awake: caffeinate -i make p05-run
set -euo pipefail
cd "$(dirname "$0")"
mode=${1:-run}
[[ $mode == smoke || $mode == run ]] || { echo "usage: p05.sh smoke|run" >&2; exit 2; }
reps=${REPS:-3}; seed=${SEED:-$(date +%s)}
[[ $mode == smoke ]] && reps=1
stamp=$(date -u +%Y%m%dT%H%M%SZ)_P05-01_$mode
remote=/opt/gangsched/results/P05-01/$stamp
local_dir=../../experiments/2026-11-04_P05-01_gang-feasibility/results
H=(sudo python3 /opt/gangsched/src/harness/p05.py)
extra=(); [[ $mode == smoke ]] && extra=(--smoke --warmup 1 --measure 5)

echo "== $stamp  reps=$reps seed=$seed"
bash sync.sh
bash guest.sh sudo make -s -C /opt/gangsched/src/bench OUT=/opt/gangsched/bin/p05
bash guest.sh "${H[@]}" meta --out "$remote"
hostlog() { echo "$(date -u +%FT%TZ) $1 host_loadavg=$(sysctl -n vm.loadavg) $(sysctl -n machdep.cpu.brand_string)" \
  | bash guest.sh sudo tee -a "$remote/host.txt" >/dev/null; }
hostlog start
bash guest.sh "${H[@]}" k3s-stop --out "$remote"
finish() {
  bash guest.sh "${H[@]}" k3s-restore --out "$remote" || echo "WARN: k3s restore failed; run: limactl shell ebpf-gang-sched sudo systemctl start k3s" >&2
  hostlog end || true
  bash pull.sh "$remote" "$local_dir" || echo "WARN: pull failed; rerun: bash pull.sh $remote $local_dir" >&2
}
trap finish EXIT
bash guest.sh "${H[@]}" calibrate --out "$remote"
for ((i = 0; i < reps; i++)); do
  hostlog "rep$i-before"
  bash guest.sh "${H[@]}" rep --out "$remote" --rep "$i" --seed "$seed" ${extra[@]+"${extra[@]}"}
  hostlog "rep$i-after"
done
echo "== done: $stamp"
