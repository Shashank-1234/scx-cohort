#!/usr/bin/env bash
# Copy a result directory out of the VM:  bash pull.sh /opt/gangsched/results/<...>/<stamp> <local parent dir>
set -euo pipefail
cd "$(dirname "$0")"
remote=${1:?remote dir}; dest=${2:?local parent dir}
mkdir -p "$dest"
bash guest.sh sudo tar -C "$(dirname "$remote")" -czf - "$(basename "$remote")" | tar -xzf - -C "$dest"
echo "pulled $remote -> $dest/$(basename "$remote")"
