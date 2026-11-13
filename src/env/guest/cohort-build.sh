#!/usr/bin/env bash
# Build scx_cohort inside the same Linux v6.17 sched_ext tree as scx_simple (after sync + simple-build).
# Copies src/scheduler/scx_cohort/* into tools/sched_ext, adds the target, builds, installs to /opt/gangsched/bin.
set -euo pipefail
cd /opt/gangsched
t=linux-examples/tools/sched_ext
[[ -d $t ]] || { echo "STOP: run simple-build first (no $t)" >&2; exit 20; }
cp /opt/gangsched/src/scheduler/scx_cohort/scx_cohort.bpf.c /opt/gangsched/src/scheduler/scx_cohort/scx_cohort.c "$t/"
grep -q '^c-sched-targets = .*scx_cohort' "$t/Makefile" || sed -i 's/^c-sched-targets = /c-sched-targets = scx_cohort /' "$t/Makefile"
echo "== versions"; uname -r; clang --version | sed -n 1p; bpftool version | sed -n 1p
make -C "$t" -j"$(nproc)" BPFTOOL="$(command -v bpftool)" scx_cohort
install "$t/build/bin/scx_cohort" /opt/gangsched/bin/scx_cohort
echo "== installed"; sha256sum /opt/gangsched/bin/scx_cohort | tee evidence/scx_cohort.sha256
