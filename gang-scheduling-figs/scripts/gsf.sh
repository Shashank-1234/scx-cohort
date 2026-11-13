#!/usr/bin/env bash
# Mac-side driver for gang-scheduling-figs (pattern of src/env/p10.sh).
#   bash gang-scheduling-figs/scripts/gsf.sh test|pilot|run     (from anywhere; macOS bash 3.2 ok)
# test : sync + build, run tests/test_gsbarrier.py in the VM -> data/raw/tests/<stamp>.json
# pilot: sync + build, calibrate, 1 rep of the pilot set (17 runs)      -> data/raw/<stamp>_GSF_pilot/
# run  : sync + build, calibrate, REPS (default 10) reps of the main set -> data/raw/<stamp>_GSF_run/
# Stops k3s for pilot/run and restores it on exit. Keep the Mac awake: caffeinate -i bash gsf.sh run
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)            # gang-scheduling-figs/
repo=$(cd "$here/.." && pwd)
mode=${1:-}
[[ $mode == test || $mode == pilot || $mode == run ]] || { echo "usage: gsf.sh test|pilot|run" >&2; exit 2; }
G() { bash "$repo/src/env/guest.sh" "$@"; }
reps=${REPS:-10}; seed=${SEED:-2011}; first=${FIRST_REP:-0}
[[ $mode == pilot ]] && { reps=1; first=0; }

# 1. sync src/ (harness libs, existing benchmark) and gang-scheduling-figs/{bench,scripts,tests}
bash "$repo/src/env/sync.sh"
cd "$here"
manifest=$(find bench scripts tests -type f ! -name '.DS_Store' ! -name '._*' ! -name '*.pyc' -print0 | sort -z | xargs -0 shasum -a 256)
gsf_id=$(printf '%s\n' "$manifest" | shasum -a 256 | cut -c1-16)
G sudo rm -rf /opt/gangsched/gsf.new
G sudo mkdir -p /opt/gangsched/gsf.new
COPYFILE_DISABLE=1 tar --no-xattrs --no-mac-metadata --exclude='._*' --exclude=.DS_Store --exclude='__pycache__' -czf - bench scripts tests \
  | G sudo tar -xzf - -C /opt/gangsched/gsf.new
printf '%s\n' "$manifest" | G sudo tee /opt/gangsched/gsf.new/MANIFEST.sha256 >/dev/null
echo "$gsf_id" | G sudo tee /opt/gangsched/gsf.new/GSF_ID >/dev/null
G sudo bash -c 'rm -rf /opt/gangsched/gsf && mv /opt/gangsched/gsf.new /opt/gangsched/gsf'
echo "synced gang-scheduling-figs -> /opt/gangsched/gsf  gsf_id=$gsf_id"

# 2. build: existing benchmark (calibration binary) + gsbarrier (includes ../../src/bench/barrier/common.h)
G sudo make -s -C /opt/gangsched/src/bench OUT=/opt/gangsched/bin/p05
G sudo mkdir -p /opt/gangsched/bin/gsf
G sudo gcc -O2 -g -Wall -Wextra -std=gnu11 -pthread -o /opt/gangsched/bin/gsf/gsbarrier /opt/gangsched/gsf/bench/gsbarrier.c -lm
G sudo test -x /opt/gangsched/bin/scx_cohort || { echo "STOP: build scx_cohort first (cd src/env && make cohort-build)" >&2; exit 20; }

stamp=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$here/data/raw/tests"
if [[ $mode == test ]]; then
  remote=/opt/gangsched/results/GSF/tests_$stamp.json
  rc=0
  G sudo python3 /opt/gangsched/gsf/tests/test_gsbarrier.py --out "$remote" || rc=$?
  G sudo cat "$remote" > "$here/data/raw/tests/${stamp}_tests.json"
  echo "tests rc=$rc -> data/raw/tests/${stamp}_tests.json"
  exit $rc
fi

stamp=${stamp}_GSF_$mode
remote=/opt/gangsched/results/GSF/$stamp
H=(sudo python3 /opt/gangsched/gsf/scripts/run_experiments.py)
extra=(--set "$( [[ $mode == pilot ]] && echo pilot || echo main )" --seed "$seed")
echo "== $stamp reps=$reps first=$first seed=$seed"
G "${H[@]}" meta --out "$remote" --seed "$seed"
hostlog() { echo "$(date -u +%FT%TZ) $1 host_loadavg=$(sysctl -n vm.loadavg) $(sysctl -n machdep.cpu.brand_string)" \
  | G sudo tee -a "$remote/host.txt" >/dev/null; }
hostlog start
G "${H[@]}" k3s-stop --out "$remote"
finish() {
  G "${H[@]}" preflight --out "$remote" >/dev/null 2>&1 || true
  G "${H[@]}" k3s-restore --out "$remote" || echo "WARN: k3s restore failed; run: limactl shell ebpf-gang-sched sudo systemctl start k3s" >&2
  hostlog end || true
  bash "$repo/src/env/pull.sh" "$remote" "$here/data/raw" || echo "WARN: pull failed; rerun: bash src/env/pull.sh $remote $here/data/raw" >&2
}
trap finish EXIT
G "${H[@]}" preflight --out "$remote" || { echo "STOP: VM CPUs 1-4 busy (see preflight output)" >&2; exit 30; }
G "${H[@]}" calibrate --out "$remote"
for ((i = first; i < first + reps; i++)); do
  hostlog "rep$i-before"
  G "${H[@]}" rep --out "$remote" --rep "$i" "${extra[@]}"
  hostlog "rep$i-after"
done
echo "== done: $stamp"
