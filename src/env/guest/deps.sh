#!/usr/bin/env bash
# Install the pinned packages needed to build scx_simple. Run in guest as root, after sync.
# Source: ~/Downloads/k8s-LinuxScheduler/scripts/guest/deps.sh @ 460d811.
# Change: reads src/env/apt-build.lock (scx_simple subset of the lab's lock, same versions);
# dropped the tracefs mount (not needed for scx_simple); evidence goes to /opt/gangsched/evidence.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
lock=/opt/gangsched/src/env/apt-build.lock
[[ -s $lock ]] || { echo 'STOP: run make sync first.' >&2; exit 1; }
apt-get update
mapfile -t packages < "$lock"
apt-get install -y "${packages[@]}"
mkdir -p /opt/gangsched/evidence
dpkg-query -W > /opt/gangsched/evidence/packages.tsv
