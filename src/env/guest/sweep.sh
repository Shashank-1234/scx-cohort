#!/usr/bin/env bash
# Kill leftover benchmark and scheduler processes of ours in the VM (after an interrupted run)
# and make sure sched_ext is back to disabled. Touches only binaries under /opt/gangsched/bin and the
# shared runner's workload builds under /opt/gangsched/workloads.
# Do not run while an experiment is in progress: it would kill that experiment's processes.
set -uo pipefail
G=/opt/gangsched/bin; W=/opt/gangsched/workloads
OURS="/opt/gangsched/(bin|workloads)/"
state() { cat /sys/kernel/sched_ext/state; }

echo "== before: sched_ext $(state); processes of ours: $(pgrep -fc "$OURS" || true)"
pgrep -fa "$OURS" | sed -n 1,20p
pkill -KILL -f "$G/(p05|gsf)/"                       # workload: barrier, competitor, gangsim, gsbarrier
pkill -KILL -f "$W/"                                 # shared runner workloads (llama-bench, NPB)
pkill -INT -f "$G/scx_"                              # scheduler loaders: detach cleanly
for _ in $(seq 1 20); do [[ $(state) == disabled ]] && break; sleep 0.25; done
pkill -KILL -f "$G/scx_"                             # still there after 5 s: kill (the kernel unloads it)
sleep 0.5
rm -f /dev/shm/cs_comp /dev/shm/cs_work /dev/shm/exp_comp
left=$(pgrep -fc "$OURS" || true)
echo "== after:  sched_ext $(state); processes of ours: ${left:-0}"
[[ $(state) == disabled ]] || echo "NOTE: sched_ext still active, but not by a binary of ours (check: cat /sys/kernel/sched_ext/root/ops)"
[[ ${left:-0} == 0 ]]
