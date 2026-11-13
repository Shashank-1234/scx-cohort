/* SPDX-License-Identifier: GPL-2.0 */
/*
 * scx_cohort: gang-aware time windows for one barrier-synchronised pool.
 *
 * Derived from scx_simple (Linux tools/sched_ext, Copyright (c) 2022 Meta
 * Platforms, Inc. and affiliates; Tejun Heo, David Vernet). The weighted-vtime
 * accounting below is scx_simple's; everything about the pool, windows, kicks
 * and counters is new.
 *
 * Policy
 *  - Registered pool threads (comm prefix and/or tgid map) go to GANG_DSQ,
 *    everything else to TENANT_DSQ; both are weighted-vtime queues.
 *  - Shared windows: all CPUs read the same clock. With period P = G + T,
 *    the gang window is [kP, kP + G), the tenant window [kP + G, (k+1)P).
 *  - dispatch(): take from the window's queue first, else from the other one
 *    (work-conserving fallback).
 *  - running(): cap the slice at the end of the current window, so every task
 *    stops at the next window edge (the "tick bound": expiry happens on the
 *    first tick after the edge, <= 1 tick late).
 *  - tick(): if the running task is the wrong class for the window and the
 *    right class is waiting, end its slice now.
 *  - boundary_kick: the first CPU that observes a new window kicks every CPU
 *    running the wrong class (SCX_KICK_PREEMPT), instead of waiting for its tick.
 *  - wake_kick: a pool thread that wakes inside the gang window with no idle
 *    CPU kicks one CPU that is running a tenant.
 *  - Kicks are bounded: one outstanding kick per CPU, never to a CPU running a
 *    pool thread, never to an idle CPU.
 *  - windows_on = false gives plain scx_simple behaviour (one shared queue)
 *    with the same instrumentation.
 */
#include <scx/common.bpf.h>

char _license[] SEC("license") = "GPL";

#define GANG_DSQ	1
#define TENANT_DSQ	2
#define MAX_CPUS	64
#define MIN_SLICE_NS	50000ULL	/* never hand out a slice shorter than 50 us */
#define TICK_GAP_NS	2000000ULL	/* > 2 ticks between ticks of one run = host stole the vCPU */
#define NR_HIST		20

enum { CLS_NONE = 0, CLS_POOL = 1, CLS_TENANT = 2 };
enum { EV_RUN = 1, EV_BKICK = 2, EV_WKICK = 3 };

const volatile u32 nr_cpu_ids = 1;
const volatile bool windows_on = true;
const volatile u64 gang_ns = 10000000ULL;
const volatile u64 tenant_ns = 10000000ULL;
const volatile bool boundary_kick = false;
const volatile bool wake_kick = false;
const volatile u32 pool_size = 4;
const volatile char pool_comm[16] = "p05w";
const volatile u32 pool_comm_len = 4;
const volatile bool debug_printk = false;
const volatile bool trace = false;
const volatile u64 managed_mask = ~0ULL;	/* CPUs where windows apply (bit per CPU, < 64) */

static u64 vtime_now;
UEI_DEFINE(uei);

/* Optional registration by process (all threads of a tgid). */
struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, 64);
	__type(key, u32);
	__type(value, u8);
} pool_tgid SEC(".maps");

struct task_ctx {
	u64 wake_ts;
};

struct {
	__uint(type, BPF_MAP_TYPE_TASK_STORAGE);
	__uint(map_flags, BPF_F_NO_PREALLOC);
	__type(key, int);
	__type(value, struct task_ctx);
} task_ctx_stor SEC(".maps");

struct event {
	u64 ts;
	u64 widx;
	u32 cpu;
	u32 pid;
	u8 type;
	u8 cls;
	u8 in_gang;
	u8 pad[5];
};

struct {
	__uint(type, BPF_MAP_TYPE_RINGBUF);
	__uint(max_entries, 4 << 20);
} events SEC(".maps");

/* Per-CPU state in plain arrays, indexed by cpu & (MAX_CPUS - 1). */
u8 cpu_class[MAX_CPUS];
u8 cpu_kick_pending[MAX_CPUS];
u64 cpu_last_tick[MAX_CPUS];
u64 cpu_seen_widx[MAX_CPUS];

/* Counters, read by the loader from .bss. */
u64 nr_enq_pool, nr_enq_tenant, nr_direct;
u64 nr_disp_gang, nr_disp_tenant, nr_fallback;
u64 nr_kick_boundary, nr_kick_wake, nr_wake_no_target, nr_tick_preempt;
u64 nr_boundaries, nr_ev_dropped;
u64 co_ns, pool_run_ns;
s64 nrun;
u64 co_start;
u64 wake_lat_sum, wake_lat_cnt, wake_lat_max;
u64 wake_hist[NR_HIST];		/* log2 buckets of microseconds */
u64 bnd_lat_sum, bnd_lat_cnt, bnd_lat_max;
u64 bnd_hist[NR_HIST];
u64 tick_gap_cnt, tick_gap_max;
u64 last_widx;
u64 nr_debug;

static __always_inline u32 cidx(s32 cpu)
{
	return (u32)cpu & (MAX_CPUS - 1);
}

static __always_inline bool managed(s32 cpu)
{
	return cpu >= 0 && cpu < MAX_CPUS && ((managed_mask >> cidx(cpu)) & 1);
}

static __always_inline u64 period(void)
{
	return gang_ns + tenant_ns;
}

static __always_inline bool in_gang(u64 now)
{
	return (now % period()) < gang_ns;
}

/* Index of the current half-window: even = gang, odd = tenant. */
static __always_inline u64 win_idx(u64 now)
{
	u64 p = period();

	return (now / p) * 2 + ((now % p) >= gang_ns);
}

static __always_inline u64 win_start(u64 now)
{
	u64 p = period(), base = now - now % p;

	return (now % p) < gang_ns ? base : base + gang_ns;
}

static __always_inline u64 win_end(u64 now)
{
	u64 p = period(), base = now - now % p;

	return (now % p) < gang_ns ? base + gang_ns : base + p;
}

static __always_inline void hist_add(u64 *hist, u64 ns)
{
	u64 us = ns / 1000;
	u32 b = 0;

	bpf_for(b, 0, NR_HIST - 1) {
		if (us < (1ULL << b))
			break;
	}
	if (b < NR_HIST)
		__sync_fetch_and_add(&hist[b], 1);
}

static __always_inline bool is_pool(struct task_struct *p)
{
	u32 tgid = p->tgid;
	u32 n = pool_comm_len;

	if (bpf_map_lookup_elem(&pool_tgid, &tgid))
		return true;
	if (!n)
		return false;
	/*
	 * Fully unrolled so every p->comm[] read has a constant offset: the
	 * verifier rejects variable offsets into task_struct.
	 */
#pragma clang loop unroll(full)
	for (int i = 0; i < 16; i++) {
		if (i >= n)
			return true;
		if (p->comm[i] != pool_comm[i])
			return false;
	}
	return true;
}

static __always_inline void emit(u8 type, s32 cpu, struct task_struct *p, u8 cls, u64 now)
{
	struct event *e;

	if (!trace)
		return;
	e = bpf_ringbuf_reserve(&events, sizeof(*e), 0);
	if (!e) {
		__sync_fetch_and_add(&nr_ev_dropped, 1);
		return;
	}
	e->ts = now;
	e->widx = win_idx(now);
	e->cpu = cpu;
	e->pid = p ? p->pid : 0;
	e->type = type;
	e->cls = cls;
	e->in_gang = in_gang(now);
	bpf_ringbuf_submit(e, 0);
}

static __always_inline void kick(s32 cpu, bool boundary, u64 now)
{
	u32 c = cidx(cpu);

	if (cpu_kick_pending[c])
		return;
	cpu_kick_pending[c] = 1;
	scx_bpf_kick_cpu(cpu, SCX_KICK_PREEMPT);
	if (boundary)
		__sync_fetch_and_add(&nr_kick_boundary, 1);
	else
		__sync_fetch_and_add(&nr_kick_wake, 1);
	emit(boundary ? EV_BKICK : EV_WKICK, cpu, NULL, 0, now);
}

/* The first CPU to observe a new window tells the others. */
static __always_inline void boundary_check(s32 self, u64 now)
{
	u64 w = win_idx(now), old = last_widx;
	bool g;
	s32 cpu;

	if (!boundary_kick || w == old)
		return;
	if (__sync_val_compare_and_swap(&last_widx, old, w) != old)
		return;
	__sync_fetch_and_add(&nr_boundaries, 1);
	g = in_gang(now);
	if (!scx_bpf_dsq_nr_queued(g ? GANG_DSQ : TENANT_DSQ))
		return;
	bpf_for(cpu, 0, nr_cpu_ids) {
		u8 cls;

		if (cpu >= MAX_CPUS)
			break;
		if (cpu == self || !managed(cpu))
			continue;
		cls = cpu_class[cidx(cpu)];
		if (cls == CLS_NONE)
			continue;
		if ((cls == CLS_POOL) != g)
			kick(cpu, true, now);
	}
}

static __always_inline u64 capped_slice(u64 now, u64 slice)
{
	u64 rem;

	if (!windows_on)
		return slice;
	rem = win_end(now) - now;
	if (rem < MIN_SLICE_NS)
		rem = MIN_SLICE_NS;
	return slice < rem ? slice : rem;
}

s32 BPF_STRUCT_OPS(cohort_select_cpu, struct task_struct *p, s32 prev_cpu, u64 wake_flags)
{
	bool is_idle = false;
	s32 cpu;

	if (is_pool(p)) {
		struct task_ctx *tctx;

		tctx = bpf_task_storage_get(&task_ctx_stor, p, 0, BPF_LOCAL_STORAGE_GET_F_CREATE);
		if (tctx)
			tctx->wake_ts = scx_bpf_now();
	}

	cpu = scx_bpf_select_cpu_dfl(p, prev_cpu, wake_flags, &is_idle);
	if (is_idle) {
		__sync_fetch_and_add(&nr_direct, 1);
		scx_bpf_dsq_insert(p, SCX_DSQ_LOCAL,
				   managed(cpu) ? capped_slice(scx_bpf_now(), SCX_SLICE_DFL) : SCX_SLICE_DFL, 0);
	}
	return cpu;
}

static __always_inline void wake_kick_one(struct task_struct *p, u64 now)
{
	s32 cpu;

	bpf_for(cpu, 0, nr_cpu_ids) {
		u32 c;

		if (cpu >= MAX_CPUS)
			break;
		if (!managed(cpu) || !bpf_cpumask_test_cpu(cpu, p->cpus_ptr))
			continue;
		c = cidx(cpu);
		if (cpu_class[c] == CLS_TENANT && !cpu_kick_pending[c]) {
			kick(cpu, false, now);
			return;
		}
	}
	__sync_fetch_and_add(&nr_wake_no_target, 1);
}

void BPF_STRUCT_OPS(cohort_enqueue, struct task_struct *p, u64 enq_flags)
{
	bool pool = windows_on && is_pool(p);
	u64 vtime = p->scx.dsq_vtime;
	u64 now = scx_bpf_now();

	if (time_before(vtime, vtime_now - SCX_SLICE_DFL))
		vtime = vtime_now - SCX_SLICE_DFL;

	if (pool) {
		__sync_fetch_and_add(&nr_enq_pool, 1);
		scx_bpf_dsq_insert_vtime(p, GANG_DSQ, SCX_SLICE_DFL, vtime, enq_flags);
		if (wake_kick && (enq_flags & SCX_ENQ_WAKEUP) && in_gang(now))
			wake_kick_one(p, now);
	} else {
		__sync_fetch_and_add(&nr_enq_tenant, 1);
		scx_bpf_dsq_insert_vtime(p, TENANT_DSQ, SCX_SLICE_DFL, vtime, enq_flags);
	}
}

void BPF_STRUCT_OPS(cohort_dispatch, s32 cpu, struct task_struct *prev)
{
	u64 now;

	if (!windows_on) {
		if (scx_bpf_dsq_move_to_local(TENANT_DSQ))
			__sync_fetch_and_add(&nr_disp_tenant, 1);
		return;
	}

	if (!managed(cpu)) {
		if (scx_bpf_dsq_move_to_local(TENANT_DSQ))
			__sync_fetch_and_add(&nr_disp_tenant, 1);
		else if (scx_bpf_dsq_move_to_local(GANG_DSQ))
			__sync_fetch_and_add(&nr_disp_gang, 1);
		return;
	}

	now = scx_bpf_now();
	boundary_check(cpu, now);
	if (in_gang(now)) {
		if (scx_bpf_dsq_move_to_local(GANG_DSQ)) {
			__sync_fetch_and_add(&nr_disp_gang, 1);
		} else if (scx_bpf_dsq_move_to_local(TENANT_DSQ)) {
			__sync_fetch_and_add(&nr_disp_tenant, 1);
			__sync_fetch_and_add(&nr_fallback, 1);
		}
	} else {
		if (scx_bpf_dsq_move_to_local(TENANT_DSQ)) {
			__sync_fetch_and_add(&nr_disp_tenant, 1);
		} else if (scx_bpf_dsq_move_to_local(GANG_DSQ)) {
			__sync_fetch_and_add(&nr_disp_gang, 1);
			__sync_fetch_and_add(&nr_fallback, 1);
		}
	}
}

void BPF_STRUCT_OPS(cohort_tick, struct task_struct *p)
{
	s32 cpu = scx_bpf_task_cpu(p);
	u32 c = cidx(cpu);
	u64 now = scx_bpf_now(), last = cpu_last_tick[c];
	bool g, pool;

	if (last && now > last && now - last > TICK_GAP_NS) {
		__sync_fetch_and_add(&tick_gap_cnt, 1);
		if (now - last > tick_gap_max)
			tick_gap_max = now - last;
	}
	cpu_last_tick[c] = now;

	if (!windows_on || !managed(cpu))
		return;
	boundary_check(cpu, now);
	g = in_gang(now);
	pool = is_pool(p);
	if (pool != g && scx_bpf_dsq_nr_queued(g ? GANG_DSQ : TENANT_DSQ)) {
		p->scx.slice = 0;
		__sync_fetch_and_add(&nr_tick_preempt, 1);
	}
}

void BPF_STRUCT_OPS(cohort_running, struct task_struct *p)
{
	s32 cpu = scx_bpf_task_cpu(p);
	u32 c = cidx(cpu);
	u64 now = scx_bpf_now();
	bool pool = is_pool(p);
	u8 cls = pool ? CLS_POOL : CLS_TENANT;

	if (time_before(vtime_now, p->scx.dsq_vtime))
		vtime_now = p->scx.dsq_vtime;

	if (cpu_class[c] != cls)
		emit(EV_RUN, cpu, p, cls, now);
	cpu_class[c] = cls;
	cpu_kick_pending[c] = 0;
	cpu_last_tick[c] = now;

	if (windows_on && managed(cpu))
		p->scx.slice = capped_slice(now, p->scx.slice);

	if (pool) {
		struct task_ctx *tctx;
		s64 n = __sync_fetch_and_add(&nrun, 1) + 1;

		if (n == pool_size)
			co_start = now;

		tctx = bpf_task_storage_get(&task_ctx_stor, p, 0, 0);
		if (tctx && tctx->wake_ts && now > tctx->wake_ts) {
			u64 lat = now - tctx->wake_ts;

			__sync_fetch_and_add(&wake_lat_sum, lat);
			__sync_fetch_and_add(&wake_lat_cnt, 1);
			if (lat > wake_lat_max)
				wake_lat_max = lat;
			hist_add(wake_hist, lat);
			tctx->wake_ts = 0;
		}

		if (debug_printk && (__sync_fetch_and_add(&nr_debug, 1) % 200) == 0)
			bpf_printk("scx_cohort: running pool task pid=%d cpu=%d gang_window=%d co_run_ms=%llu",
				   p->pid, cpu, in_gang(now), co_ns / 1000000);
	}

	/* Boundary latency: first task of the window's own class on this CPU. */
	if (windows_on && managed(cpu) && (cls == CLS_POOL) == in_gang(now)) {
		u64 w = win_idx(now);

		if (cpu_seen_widx[c] != w) {
			u64 lat = now - win_start(now);

			cpu_seen_widx[c] = w;
			__sync_fetch_and_add(&bnd_lat_sum, lat);
			__sync_fetch_and_add(&bnd_lat_cnt, 1);
			if (lat > bnd_lat_max)
				bnd_lat_max = lat;
			hist_add(bnd_hist, lat);
		}
	}
}

void BPF_STRUCT_OPS(cohort_stopping, struct task_struct *p, bool runnable)
{
	s32 cpu = scx_bpf_task_cpu(p);
	u64 now = scx_bpf_now();

	p->scx.dsq_vtime += (SCX_SLICE_DFL - p->scx.slice) * 100 / p->scx.weight;

	if (cpu_class[cidx(cpu)] == CLS_POOL) {
		if (nrun == pool_size && now > co_start)
			__sync_fetch_and_add(&co_ns, now - co_start);
		if (nrun > 0)
			__sync_fetch_and_sub(&nrun, 1);
	}
	cpu_class[cidx(cpu)] = CLS_NONE;
}

void BPF_STRUCT_OPS(cohort_enable, struct task_struct *p)
{
	p->scx.dsq_vtime = vtime_now;
}

s32 BPF_STRUCT_OPS_SLEEPABLE(cohort_init)
{
	s32 ret;

	ret = scx_bpf_create_dsq(GANG_DSQ, -1);
	if (ret)
		return ret;
	ret = scx_bpf_create_dsq(TENANT_DSQ, -1);
	if (ret)
		return ret;
	if (debug_printk)
		bpf_printk("scx_cohort: init windows=%d G=%llu T=%llu bkick=%d wkick=%d",
			   windows_on, gang_ns, tenant_ns, boundary_kick, wake_kick);
	return 0;
}

void BPF_STRUCT_OPS(cohort_exit, struct scx_exit_info *ei)
{
	UEI_RECORD(uei, ei);
}

SCX_OPS_DEFINE(cohort_ops,
	       .select_cpu		= (void *)cohort_select_cpu,
	       .enqueue			= (void *)cohort_enqueue,
	       .dispatch		= (void *)cohort_dispatch,
	       .tick			= (void *)cohort_tick,
	       .running			= (void *)cohort_running,
	       .stopping		= (void *)cohort_stopping,
	       .enable			= (void *)cohort_enable,
	       .init			= (void *)cohort_init,
	       .exit			= (void *)cohort_exit,
	       .name			= "cohort");
