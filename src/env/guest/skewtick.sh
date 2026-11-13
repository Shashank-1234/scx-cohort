#!/usr/bin/env bash
# P10-02: turn the kernel boot option skew_tick=1 on/off (grub drop-in) and check per-CPU tick phases.
# skew_tick=1 offsets CPU k's tick by k * (TICK_NSEC/2) / nr_possible_cpus (kernel/time/tick-sched.c);
# with HZ=1000 and 6 vCPUs that is 83.3 us per CPU, so CPUs 1-4 sit at +83/+167/+250/+333 us.
# Boot option only: no kernel build, no package change. Takes effect after a VM restart.
set -euo pipefail
F=/etc/default/grub.d/99-gangsched-skewtick.cfg
case "${1:-status}" in
on)
  echo 'GRUB_CMDLINE_LINUX_DEFAULT="$GRUB_CMDLINE_LINUX_DEFAULT skew_tick=1"' > "$F"
  update-grub 2>&1 | tail -2
  grep -q 'skew_tick=1' /boot/grub/grub.cfg && echo "skew_tick=1 written to grub.cfg; restart the VM" || { echo "FAIL: not in grub.cfg"; exit 1; }
  ;;
off)
  rm -f "$F"
  update-grub 2>&1 | tail -2
  if grep -q 'skew_tick' /boot/grub/grub.cfg; then echo "FAIL: skew_tick still in grub.cfg"; exit 1; fi
  echo "skew_tick removed from grub.cfg; restart the VM"
  ;;
status)
  echo "cmdline: $(cat /proc/cmdline)"
  if grep -qw 'skew_tick=1' /proc/cmdline; then echo "skew_tick: ON"; else echo "skew_tick: off"; fi
  echo "sched_ext: $(cat /sys/kernel/sched_ext/state)   k3s: $(systemctl is-active k3s || true)"
  C=/opt/gangsched/bin/p05/competitor
  [[ -x $C ]] || { echo "no competitor binary; skip phase check"; exit 0; }
  # keep CPUs 1-4 busy so their ticks run (an idle NO_HZ CPU stops its tick)
  $C --threads 4 --cpus 1-4 --shm /dev/shm/skewchk </dev/null >/dev/null 2>&1 & P=$!
  trap 'kill $P 2>/dev/null || true; rm -f /dev/shm/skewchk' EXIT
  sleep 1
  python3 - <<'PY'
import re, time
for rep in range(3):
    cpu, want, ph = None, False, {}
    for line in open("/proc/timer_list"):
        m = re.match(r"cpu: (\d+)", line)
        if m:
            cpu, want = int(m.group(1)), False
            continue
        if re.search(r"tick_(sched_timer|nohz_handler)", line):
            want = True
            continue
        if want:
            m = re.search(r"expires at (\d+)-", line)
            if m and cpu is not None and cpu not in ph:
                ph[cpu] = int(m.group(1)) % 1_000_000
            want = False
    busy = {c: ph[c] for c in (1, 2, 3, 4) if c in ph}
    if len(busy) < 2:
        print("tick timers not found for CPUs 1-4:", ph); break
    ref = busy[min(busy)]
    rel = {c: ((v - ref) % 1_000_000) / 1000 for c, v in sorted(busy.items())}
    rel = {c: (r if r < 500 else r - 1000) for c, r in rel.items()}
    print(f"sample {rep}: tick phase of CPUs 1-4 relative to CPU {min(busy)} (us): "
          + ", ".join(f"cpu{c} {r:+.1f}" for c, r in rel.items())
          + f"   spread {max(rel.values()) - min(rel.values()):.1f} us")
    time.sleep(0.3)
PY
  ;;
*) echo "usage: skewtick.sh on|off|status" >&2; exit 2 ;;
esac
