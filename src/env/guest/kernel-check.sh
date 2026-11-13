#!/usr/bin/env bash
set -euo pipefail
# Execute in guest as root. Save the entire output as course evidence.
uname -a
version=$(uname -r)
if [[ -r /boot/config-$version ]]; then
  config=$(cat "/boot/config-$version")
elif [[ -r /proc/config.gz ]]; then
  config=$(gzip -dc /proc/config.gz)
else
  echo 'STOP: cannot verify stock kernel config.' >&2; exit 20
fi
printf '%s\n' "$config" | grep -E '^(CONFIG_(SCHED_CLASS_EXT|BPF|BPF_SYSCALL|BPF_JIT|DEBUG_INFO_BTF|CGROUPS|CGROUP_SCHED|FAIR_GROUP_SCHED)=|# CONFIG_SCHED_CLASS_EXT)' || true
if ! printf '%s\n' "$config" | grep -x 'CONFIG_SCHED_CLASS_EXT=y' >/dev/null; then
  echo 'STOP: stock kernel lacks sched_ext. Options: another distro stock kernel, a distro-supported kernel package, or request approval for a custom kernel. No kernel build performed.' >&2
  exit 21
fi
if [[ $(printf '%s\n' 6.12 "${version%%-*}" | sort -V | sed -n 1p) != 6.12 ]]; then
  echo 'STOP: kernel < 6.12' >&2; exit 22
fi
[[ $(uname -m) == aarch64 ]]
[[ $(nproc) == 6 ]]
[[ -r /sys/kernel/btf/vmlinux ]]
[[ $(stat -fc %t /sys/fs/cgroup) == 63677270 ]]
cat /sys/kernel/sched_ext/state
cat /sys/kernel/sched_ext/root_ops 2>/dev/null || true
