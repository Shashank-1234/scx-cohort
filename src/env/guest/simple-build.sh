#!/usr/bin/env bash
# Build scx_simple from the Linux v6.17 tree (matches the VM's 6.17 kernel). Run in guest as root.
# Source: ~/Downloads/k8s-LinuxScheduler/scripts/guest/simple-build.sh @ 460d811.
# Change: works under /opt/gangsched and installs to /opt/gangsched/bin (the original used
# the lab's own /opt directory and /usr/local/bin, which still hold the Codex lab's copies); logs tool versions.
set -euo pipefail
mkdir -p /opt/gangsched/evidence /opt/gangsched/bin
cd /opt/gangsched
# Only build userspace + BPF examples. Never compile a Linux kernel.
if [[ ! -d linux-examples/.git ]]; then
  git clone --filter=blob:none --depth=1 --branch v6.17 --sparse https://github.com/torvalds/linux.git linux-examples
fi
git -C linux-examples sparse-checkout set tools include scripts arch/arm64/include
[[ $(git -C linux-examples rev-parse HEAD) == e5f0a698b34ed76002dc5cff3804a61c80233a7a ]]
git -C linux-examples rev-parse HEAD > evidence/linux-examples.commit
echo "== versions"; uname -r; clang --version | sed -n 1p; bpftool version | sed -n 1p; make --version | sed -n 1p
echo "== linux tree: $(cat evidence/linux-examples.commit) (tag v6.17)"
make -C linux-examples/tools/sched_ext -j"$(nproc)" BPFTOOL="$(command -v bpftool)" scx_simple
install linux-examples/tools/sched_ext/build/bin/scx_simple /opt/gangsched/bin/scx_simple
echo "== installed"; sha256sum /opt/gangsched/bin/scx_simple | tee evidence/scx_simple.sha256
