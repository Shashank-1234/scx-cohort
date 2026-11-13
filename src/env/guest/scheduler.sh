#!/usr/bin/env bash
# Load, unload or show the sched_ext scheduler. Run in guest as root.
# Source: ~/Downloads/k8s-LinuxScheduler/scripts/guest/scheduler.sh @ 460d811.
# Change: scx_simple only, from /opt/gangsched/bin, under systemd unit gangsched-simple.
# Stop only stops our unit; if something else owns sched_ext it reports and fails.
set -euo pipefail
mode=${1:?simple|stop|state}
unit=gangsched-simple
state() {
  printf 'state: '; cat /sys/kernel/sched_ext/state
  # Kernel ABI varies: older versions use root/ops, later ones root_ops.
  for f in /sys/kernel/sched_ext/root_ops /sys/kernel/sched_ext/root/ops; do
    [[ ! -r $f ]] || { printf '%s: ' "$f"; cat "$f"; }
  done
}
case $mode in
 state) state; exit ;;
 stop)
  systemctl stop "$unit.service" 2>/dev/null || true
  for i in $(seq 1 30); do [[ $(cat /sys/kernel/sched_ext/state) != enabled ]] && break; sleep 1; done
  [[ $(cat /sys/kernel/sched_ext/state) == disabled ]] || { echo 'Scheduler still active and not ours; inspect Helm or other owner.' >&2; exit 1; }
  state; exit ;;
 simple) ;;
 *) exit 2 ;;
esac
[[ $(cat /sys/kernel/sched_ext/state) == disabled ]] || { echo 'Refusing to replace active scheduler' >&2; exit 1; }
systemctl reset-failed "$unit.service" 2>/dev/null || true
systemd-run --unit="$unit" --collect /opt/gangsched/bin/scx_simple
for i in $(seq 1 30); do [[ $(cat /sys/kernel/sched_ext/state) == enabled ]] && break; sleep 1; done
state
if [[ $(cat /sys/kernel/sched_ext/state) != enabled ]]; then journalctl -u "$unit" -n100 --no-pager; exit 1; fi
ops=$(cat /sys/kernel/sched_ext/root_ops /sys/kernel/sched_ext/root/ops 2>/dev/null || true)
[[ $ops == simple ]]
