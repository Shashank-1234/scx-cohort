#!/usr/bin/env bash
# Start the dev VM, creating it from lima/ebpf-gang-sched.yaml only if it does not exist.
# Source: ~/Downloads/k8s-LinuxScheduler/scripts/vm.sh @ 460d811 (Codex lab).
# Change: path to the Lima file is relative to src/env/. Never deletes a VM.
set -euo pipefail
cd "$(dirname "$0")"
name=ebpf-gang-sched
if limactl list --format '{{.Name}}' | grep -qx "$name"; then
  limactl start --tty=false "$name"
else
  limactl start --tty=false --name="$name" lima/ebpf-gang-sched.yaml
fi
