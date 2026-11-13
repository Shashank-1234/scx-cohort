# scx_cohort

A gang-aware Linux CPU scheduler written in eBPF with `sched_ext`. All CPUs share one clock-based schedule of time windows: one window gives priority to a barrier-synchronised thread pool, the next to everything else. Each job keeps its CPU share, but the pool's threads now run together instead of spinning on descheduled teammates.

Two variants:

| Variant | How a CPU switches at a window edge |
|---|---|
| Ctick (default) | at its own next scheduler tick |
| Ckick (`-b -k`) | the first CPU to see the edge sends reschedule IPIs to the others (`-b`); waking pool threads may preempt (`-k`) |

## Layout

| Path | Contents |
|---|---|
| `src/scheduler/scx_cohort/` | BPF policy (`scx_cohort.bpf.c`) and loader (`scx_cohort.c`) |
| `src/bench/` | barrier benchmark (spin / adaptive / block), CPU-bound competitor, user-space gang controller |
| `src/harness/` | experiment runners, workload adapters (llama.cpp, NPB CG) and analysis scripts |
| `src/env/` | host-side `make` targets; `guest/` scripts run inside the VM; `lima/` VM definition |
| `gang-scheduling-figs/` | two-application barrier benchmark and sweep scripts after Peter et al. (2011), Fig. 2 |

## Run

Host: macOS with a Lima VM (Ubuntu 25.10, Linux 6.17, sched_ext enabled), defined in `src/env/lima/`.

```
cd src/env
make help                        # list every target
make vm                          # start (or create) the VM
make simple-build cohort-build   # build scx_simple, then build and install scx_cohort in the VM
make cohort-sanity               # load scx_cohort for 10 s and print callback activity
make exp E=<dir> [SMOKE=1]       # run the experiment described by <dir>/spec.json
```

Loader options: `/opt/gangsched/bin/scx_cohort -h` (window lengths `-G/-T`, CPUs `-m`, pool by thread name `-c` or process `-p`, stats `-o`, trace `-t`).
