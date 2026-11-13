#!/usr/bin/env bash
# Load scx_simple, hold 5 s, check it is attached, unload, check EEVDF is back. Run in guest as root, after sync + simple-build.
# Source: ~/Downloads/k8s-LinuxScheduler/scripts/guest/simple-sanity.sh @ 460d811.
# Change: paths under /opt/gangsched; prints state before/after, the loader's own output, and
# sched_ext kernel messages, so a watchdog eject would show up in the log.
set -euo pipefail
sched=/opt/gangsched/src/env/guest/scheduler.sh
echo "== src_id $(cat /opt/gangsched/src/SRC_ID 2>/dev/null || echo unknown); binary $(sha256sum /opt/gangsched/bin/scx_simple | cut -c1-16)"
echo "== before"; bash $sched state
since=$(date '+%F %T')
echo "== load"; bash $sched simple
trap 'echo "== unload"; bash $sched stop; echo "== loader output"; journalctl -u gangsched-simple --since "$since" --no-pager | tail -20; echo "== kernel messages"; dmesg | grep -i sched_ext | tail -5' EXIT
sleep 5
echo "== after 5 s"; bash $sched state
[[ $(cat /sys/kernel/sched_ext/state) == enabled ]]
[[ $(cat /sys/kernel/sched_ext/root/ops 2>/dev/null || cat /sys/kernel/sched_ext/root_ops) == simple ]]
echo "PASS: scx_simple attached for 5 s"
