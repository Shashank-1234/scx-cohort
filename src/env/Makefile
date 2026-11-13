# Dev VM (Lima ebpf-gang-sched) environment. Run from src/env/: make <target>.
# Source: target set taken from ~/Downloads/k8s-LinuxScheduler/Makefile @ 460d811 (minimal subset).
# Logs: ../../phases/P1_platform/runs/<UTC>_<stage>.log (override with RUNS_DIR=...).
# No target deletes the VM or builds a kernel.
SHELL := /bin/bash
.NOTPARALLEL:
.DEFAULT_GOAL := vm
.PHONY: help exp sweep vm stop-vm kernel-check sync deps simple-build simple-sanity scheduler-state scheduler-stop p05-smoke p05-run cohort-build cohort-sanity p10-smoke p10-run skewtick-on skewtick-off skewtick-status p10-skew-run

##@ Experiments (shared runner, src/harness/RUNNER.md)
exp:             ## run experiments/<dir>/spec.json: make exp E=experiments/<dir> [SMOKE=1]; results in <dir>/results/<UTC>/
	@test -n "$(E)" || { echo "usage: make exp E=experiments/<dir> [SMOKE=1]"; exit 2; }
	SMOKE=$(SMOKE) bash exp.sh $(E)
sweep:           ## kill leftover benchmark/scheduler/workload processes of ours in the VM after an interrupted run (not during an experiment)
	bash run.sh sweep

##@ VM, builds and checks
vm:              ## start (or create if absent) the VM
	bash vm.sh
stop-vm:         ## stop the VM (keeps its disk)
	limactl stop ebpf-gang-sched
kernel-check:    ## sched_ext, BPF, BTF, cgroup v2, kernel >= 6.12
	bash run.sh kernel-check
sync:            ## copy repo src/ into the VM at /opt/gangsched/src
	bash sync.sh
deps:            ## install pinned build packages (after sync)
	bash run.sh deps
simple-build:    ## build scx_simple from Linux v6.17 into /opt/gangsched/bin
	bash run.sh simple-build
simple-sanity:   ## load scx_simple 5 s, check, unload (after sync + simple-build)
	bash run.sh simple-sanity
scheduler-state:
	bash guest.sh sudo bash /opt/gangsched/src/env/guest/scheduler.sh state
scheduler-stop:
	bash guest.sh sudo bash /opt/gangsched/src/env/guest/scheduler.sh stop
cohort-build:    ## build scx_cohort in the v6.17 sched_ext tree (after sync + simple-build)
	bash sync.sh && bash run.sh cohort-build
cohort-sanity:   ## load scx_cohort 10 s with debug printk + pool workload; prints callback lines (screenshot)
	bash guest.sh sudo make -s -C /opt/gangsched/src/bench OUT=/opt/gangsched/bin/p05
	bash run.sh cohort-sanity
skewtick-on:     ## add skew_tick=1 to the VM's boot options (grub drop-in), restart the VM, verify tick phases
	bash run.sh skewtick on
	limactl stop ebpf-gang-sched && limactl start --tty=false ebpf-gang-sched
	bash run.sh skewtick status
skewtick-off:    ## remove skew_tick=1, restart the VM, verify ticks are aligned again
	bash run.sh skewtick off
	limactl stop ebpf-gang-sched && limactl start --tty=false ebpf-gang-sched
	bash run.sh skewtick status
skewtick-status: ## print /proc/cmdline and per-CPU tick phase (CPUs 1-4 kept busy so their ticks run)
	bash run.sh skewtick status
help:            ## list targets
	@awk '/^##@ / {printf "\n%s\n", substr($$0, 5)} /^[a-z0-9-]+: +##/ {sub(/: +## /, "\t"); print}' Makefile | expand -t 18

##@ Legacy (frozen): earlier experiments' runners, kept so their raw data stays reproducible
p05-smoke:       ## P0.5 feasibility: 1 short config per arm (about 1 min)
	bash p05.sh smoke
p05-run:         ## P0.5 feasibility: calibrate + REPS (default 3) reps, about 22 min each; results pulled into experiments/
	bash p05.sh run
p10-smoke:       ## scx_cohort evaluation: 1 short run per arm type (about 2 min)
	bash p10.sh smoke
p10-run:         ## scx_cohort evaluation: calibrate + REPS (default 3) reps of 57 configs (~37 min each); results pulled into experiments/
	bash p10.sh run
p10-skew-run:    ## P10-02: spin-only tick-vs-kick subset (17 configs, ~11 min per rep); run label follows the VM's skew_tick state
	SET=skew bash p10.sh run
