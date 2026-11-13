#!/usr/bin/env bash
# Copy this repo's src/ into the VM at /opt/gangsched/src (the VM has no host mounts).
# Source: idea and tar flags from ~/Downloads/k8s-LinuxScheduler/scripts/sync.sh @ 460d811.
# Change: syncs this repo's src/ only (the original synced a third-party scheduler tree and lab assets
# to the lab's own /opt directory). The repo has no git, so a sha256 manifest stands in for a revision.
set -euo pipefail
cd "$(dirname "$0")/.."            # repo src/
manifest=$(find . -type f ! -name '.DS_Store' ! -name '._*' -print0 | sort -z | xargs -0 shasum -a 256)
src_id=$(printf '%s\n' "$manifest" | shasum -a 256 | cut -c1-16)
bash env/guest.sh sudo mkdir -p /opt/gangsched/src
bash env/guest.sh sudo rm -rf /opt/gangsched/src.new
bash env/guest.sh sudo mkdir -p /opt/gangsched/src.new
COPYFILE_DISABLE=1 tar --no-xattrs --no-mac-metadata --exclude='._*' --exclude=.DS_Store -czf - . \
  | bash env/guest.sh sudo tar -xzf - -C /opt/gangsched/src.new
printf '%s\n' "$manifest" | bash env/guest.sh sudo tee /opt/gangsched/src.new/MANIFEST.sha256 >/dev/null
echo "$src_id" | bash env/guest.sh sudo tee /opt/gangsched/src.new/SRC_ID >/dev/null
bash env/guest.sh sudo bash -c 'rm -rf /opt/gangsched/src && mv /opt/gangsched/src.new /opt/gangsched/src'
echo "synced src/ -> /opt/gangsched/src  src_id=$src_id  files=$(printf '%s\n' "$manifest" | wc -l | tr -d ' ')"
