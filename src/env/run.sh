#!/usr/bin/env bash
# Run one guest stage (guest/<stage>.sh) as root inside the VM; tee output to a log.
# Source: ~/Downloads/k8s-LinuxScheduler/scripts/run.sh @ 460d811 (Codex lab).
# Change: log goes to $RUNS_DIR/<UTC>_<stage>.log (default phases/P1_platform/runs/)
# instead of artifacts/<stage>.log, so logs are never overwritten.
set -euo pipefail
cd "$(dirname "$0")"
stage=${1:?guest script stage required}; shift
[[ $stage =~ ^[a-z-]+$ ]]
runs=${RUNS_DIR:-../../phases/P1_platform/runs}
mkdir -p "$runs"
log="$runs/$(date -u +%Y%m%dT%H%M%SZ)_$stage.log"
echo "# stage=$stage host_utc=$(date -u +%FT%TZ) log=$log" | tee "$log"
bash guest.sh sudo bash -s -- "$@" < "guest/$stage.sh" 2>&1 | tee -a "$log"
