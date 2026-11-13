/* SPDX-License-Identifier: GPL-2.0 */
/*
 * Loader for scx_cohort. Structure follows scx_simple.c (Linux tools/sched_ext,
 * Copyright (c) 2022 Meta Platforms, Inc. and affiliates; Tejun Heo, David
 * Vernet); options, registration, stats and tracing are new.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <signal.h>
#include <libgen.h>
#include <time.h>
#include <bpf/bpf.h>
#include <bpf/libbpf.h>
#include <scx/common.h>
#include "scx_cohort.bpf.skel.h"

const char help_fmt[] =
"scx_cohort: gang-aware time windows for a barrier-synchronised pool.\n"
"\n"
"Usage: %s [options]\n"
"  -G US     gang window length in microseconds (default 10000)\n"
"  -T US     tenant window length in microseconds (default 10000)\n"
"  -w        windows off (plain shared-vtime, scx_simple-like; same counters)\n"
"  -b        boundary kick: first CPU to see a new window kicks the others\n"
"  -k        wake kick: a pool thread waking in its window kicks a tenant CPU\n"
"  -c NAME   pool = threads whose comm starts with NAME (default p05w; \"\" = none)\n"
"  -p TGID   also register every thread of process TGID (repeatable)\n"
"  -m LIST   CPUs where windows apply, e.g. 1-4 (default: all)\n"
"  -n N      pool size used for the co-run counter (default 4)\n"
"  -o FILE   append one JSON stats line per second to FILE (default stdout)\n"
"  -t FILE   trace: write per-event CSV (ts,widx,cpu,pid,type,cls,in_gang) to FILE\n"
"  -d        bpf_printk debug lines (trace_pipe)\n"
"  -v        libbpf debug output\n"
"  -h        help\n";

static bool verbose;
static volatile int exit_req;

static int libbpf_print_fn(enum libbpf_print_level level, const char *format, va_list args)
{
	if (level == LIBBPF_DEBUG && !verbose)
		return 0;
	return vfprintf(stderr, format, args);
}

static void sigint_handler(int sig)
{
	exit_req = 1;
}

struct event {
	__u64 ts;
	__u64 widx;
	__u32 cpu;
	__u32 pid;
	__u8 type;
	__u8 cls;
	__u8 in_gang;
	__u8 pad[5];
};

static __u64 parse_cpulist(const char *s)
{
	__u64 m = 0;
	char *dup = strdup(s), *save = NULL;

	for (char *tok = strtok_r(dup, ",", &save); tok; tok = strtok_r(NULL, ",", &save)) {
		int a, b;

		if (sscanf(tok, "%d-%d", &a, &b) == 2) {
			for (int c = a; c <= b && c < 64; c++)
				m |= 1ULL << c;
		} else if ((a = atoi(tok)) >= 0 && a < 64) {
			m |= 1ULL << a;
		}
	}
	free(dup);
	return m;
}

static int handle_event(void *ctx, void *data, size_t len)
{
	FILE *f = ctx;
	const struct event *e = data;

	fprintf(f, "%llu,%llu,%u,%u,%u,%u,%u\n", (unsigned long long)e->ts,
		(unsigned long long)e->widx, e->cpu, e->pid, e->type, e->cls, e->in_gang);
	return 0;
}

static void print_hist(FILE *f, const char *name, const void *hp, int n)
{
	const unsigned long long *h = hp;

	fprintf(f, ",\"%s\":[", name);
	for (int i = 0; i < n; i++)
		fprintf(f, "%s%llu", i ? "," : "", h[i]);
	fprintf(f, "]");
}

static void print_stats(FILE *f, struct scx_cohort *skel, int final)
{
	struct scx_cohort__bss *b = skel->bss;
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	fprintf(f, "{\"final\":%d,\"mono_ns\":%llu,\"enq_pool\":%llu,\"enq_tenant\":%llu,\"direct\":%llu,"
		"\"disp_gang\":%llu,\"disp_tenant\":%llu,\"fallback\":%llu,\"kick_boundary\":%llu,"
		"\"kick_wake\":%llu,\"wake_no_target\":%llu,\"tick_preempt\":%llu,\"boundaries\":%llu,"
		"\"co_ns\":%llu,\"wake_lat_sum\":%llu,\"wake_lat_cnt\":%llu,\"wake_lat_max\":%llu,"
		"\"bnd_lat_sum\":%llu,\"bnd_lat_cnt\":%llu,\"bnd_lat_max\":%llu,"
		"\"tick_gap_cnt\":%llu,\"tick_gap_max\":%llu,\"ev_dropped\":%llu",
		final, (unsigned long long)(ts.tv_sec * 1000000000ULL + ts.tv_nsec),
		(unsigned long long)b->nr_enq_pool, (unsigned long long)b->nr_enq_tenant,
		(unsigned long long)b->nr_direct, (unsigned long long)b->nr_disp_gang,
		(unsigned long long)b->nr_disp_tenant, (unsigned long long)b->nr_fallback,
		(unsigned long long)b->nr_kick_boundary, (unsigned long long)b->nr_kick_wake,
		(unsigned long long)b->nr_wake_no_target, (unsigned long long)b->nr_tick_preempt,
		(unsigned long long)b->nr_boundaries, (unsigned long long)b->co_ns,
		(unsigned long long)b->wake_lat_sum, (unsigned long long)b->wake_lat_cnt,
		(unsigned long long)b->wake_lat_max, (unsigned long long)b->bnd_lat_sum,
		(unsigned long long)b->bnd_lat_cnt, (unsigned long long)b->bnd_lat_max,
		(unsigned long long)b->tick_gap_cnt, (unsigned long long)b->tick_gap_max,
		(unsigned long long)b->nr_ev_dropped);
	print_hist(f, "wake_hist_us_log2", b->wake_hist, sizeof(b->wake_hist) / sizeof(b->wake_hist[0]));
	print_hist(f, "bnd_hist_us_log2", b->bnd_hist, sizeof(b->bnd_hist) / sizeof(b->bnd_hist[0]));
	fprintf(f, "}\n");
	fflush(f);
}

int main(int argc, char **argv)
{
	struct scx_cohort *skel;
	struct bpf_link *link;
	struct ring_buffer *rb = NULL;
	FILE *out = stdout, *tf = NULL;
	const char *outp = NULL, *tracep = NULL, *comm = "p05w";
	__u32 tgids[64];
	int ntgid = 0, opt;
	__u64 g_us = 10000, t_us = 10000, ecode;
	bool windows = true, bkick = false, wkick = false, debug = false;
	__u32 pool_n = 4;
	__u64 mask = ~0ULL;

	libbpf_set_print(libbpf_print_fn);
	signal(SIGINT, sigint_handler);
	signal(SIGTERM, sigint_handler);

	while ((opt = getopt(argc, argv, "G:T:wbkc:p:m:n:o:t:dvh")) != -1) {
		switch (opt) {
		case 'G': g_us = strtoull(optarg, NULL, 10); break;
		case 'T': t_us = strtoull(optarg, NULL, 10); break;
		case 'w': windows = false; break;
		case 'b': bkick = true; break;
		case 'k': wkick = true; break;
		case 'c': comm = optarg; break;
		case 'p': if (ntgid < 64) tgids[ntgid++] = strtoul(optarg, NULL, 10); break;
		case 'm': mask = parse_cpulist(optarg); break;
		case 'n': pool_n = strtoul(optarg, NULL, 10); break;
		case 'o': outp = optarg; break;
		case 't': tracep = optarg; break;
		case 'd': debug = true; break;
		case 'v': verbose = true; break;
		default:
			fprintf(stderr, help_fmt, basename(argv[0]));
			return opt != 'h';
		}
	}
	if (!g_us || !t_us) {
		fprintf(stderr, "-G and -T must be > 0\n");
		return 2;
	}
	if (outp && !(out = fopen(outp, "a"))) {
		perror("open -o file");
		return 2;
	}
	if (tracep && !(tf = fopen(tracep, "w"))) {
		perror("open -t file");
		return 2;
	}

	skel = SCX_OPS_OPEN(cohort_ops, scx_cohort);
	skel->rodata->nr_cpu_ids = libbpf_num_possible_cpus();
	skel->rodata->windows_on = windows;
	skel->rodata->gang_ns = g_us * 1000ULL;
	skel->rodata->tenant_ns = t_us * 1000ULL;
	skel->rodata->boundary_kick = bkick;
	skel->rodata->wake_kick = wkick;
	skel->rodata->pool_size = pool_n;
	skel->rodata->debug_printk = debug;
	skel->rodata->trace = tf != NULL;
	skel->rodata->managed_mask = mask;
	{
		size_t n = strlen(comm);

		if (n > 15)
			n = 15;
		memset((void *)skel->rodata->pool_comm, 0, sizeof(skel->rodata->pool_comm));
		memcpy((void *)skel->rodata->pool_comm, comm, n);
		skel->rodata->pool_comm_len = n;
	}

	SCX_OPS_LOAD(skel, cohort_ops, scx_cohort, uei);

	for (int i = 0; i < ntgid; i++) {
		__u8 one = 1;

		if (bpf_map_update_elem(bpf_map__fd(skel->maps.pool_tgid), &tgids[i], &one, BPF_ANY))
			fprintf(stderr, "warning: could not register tgid %u\n", tgids[i]);
	}

	if (tf) {
		fprintf(tf, "ts,widx,cpu,pid,type,cls,in_gang\n");
		rb = ring_buffer__new(bpf_map__fd(skel->maps.events), handle_event, tf, NULL);
		if (!rb) {
			fprintf(stderr, "ring_buffer__new failed\n");
			return 1;
		}
	}

	link = SCX_OPS_ATTACH(skel, cohort_ops, scx_cohort);
	fprintf(stderr, "scx_cohort attached: windows=%d G=%lluus T=%lluus bkick=%d wkick=%d comm=\"%s\" tgids=%d pool=%u mask=0x%llx\n",
		windows, (unsigned long long)g_us, (unsigned long long)t_us, bkick, wkick, comm, ntgid, pool_n, (unsigned long long)mask);

	while (!exit_req && !UEI_EXITED(skel, uei)) {
		if (rb) {
			for (int i = 0; i < 10 && !exit_req; i++)
				ring_buffer__poll(rb, 100);
		} else {
			sleep(1);
		}
		print_stats(out, skel, 0);
	}

	print_stats(out, skel, 1);
	if (rb) {
		ring_buffer__consume(rb);
		ring_buffer__free(rb);
	}
	if (tf)
		fclose(tf);
	bpf_link__destroy(link);
	ecode = UEI_REPORT(skel, uei);
	scx_cohort__destroy(skel);
	if (out != stdout)
		fclose(out);

	if (UEI_ECODE_RESTART(ecode))
		fprintf(stderr, "scx_cohort: kernel asked for a restart; exiting instead\n");
	return 0;
}
