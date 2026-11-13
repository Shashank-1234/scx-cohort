/* P0.5 userspace gang-window simulator.
 *   gangsim --wpid PID --cpid PID --g-us 10000 --t-us 10000 [--cpu 0] [--fifo 50] --log FILE
 * Alternates windows until SIGTERM/SIGINT:
 *   gang window   (G): SIGSTOP competitor, then SIGCONT workers
 *   tenant window (T): SIGSTOP workers,    then SIGCONT competitor
 * Deadlines are absolute (clock_nanosleep TIMER_ABSTIME), so jitter does not accumulate.
 * On exit both processes are SIGCONTed. Log: one CSV line per transition:
 *   idx,phase(G|T),target_ns,wake_ns,after_kill_ns   (CLOCK_MONOTONIC) */
#define _GNU_SOURCE
#include <errno.h>
#include <sched.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static volatile sig_atomic_t quit = 0;
static void on_term(int s) { (void)s; quit = 1; }

static uint64_t now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

struct rec { uint64_t target, wake, after; char phase; };

int main(int argc, char **argv) {
    pid_t wpid = 0, cpid = 0;
    uint64_t g_ns = 10000000ULL, t_ns = 10000000ULL;
    int cpu = 0, fifo = 50;
    const char *log = NULL;
    for (int i = 1; i < argc; i++) {
        const char *v = (i + 1 < argc) ? argv[i + 1] : NULL;
        if (!strcmp(argv[i], "--wpid") && v) { wpid = atoi(v); i++; }
        else if (!strcmp(argv[i], "--cpid") && v) { cpid = atoi(v); i++; }
        else if (!strcmp(argv[i], "--g-us") && v) { g_ns = strtoull(v, NULL, 10) * 1000ULL; i++; }
        else if (!strcmp(argv[i], "--t-us") && v) { t_ns = strtoull(v, NULL, 10) * 1000ULL; i++; }
        else if (!strcmp(argv[i], "--cpu") && v) { cpu = atoi(v); i++; }
        else if (!strcmp(argv[i], "--fifo") && v) { fifo = atoi(v); i++; }
        else if (!strcmp(argv[i], "--log") && v) { log = v; i++; }
        else { fprintf(stderr, "unknown arg %s\n", argv[i]); return 2; }
    }
    if (!wpid || !cpid || !log) { fprintf(stderr, "need --wpid --cpid --log\n"); return 2; }

    cpu_set_t set; CPU_ZERO(&set); CPU_SET(cpu, &set);
    if (sched_setaffinity(0, sizeof(set), &set) != 0) perror("affinity (continuing)");
    int fifo_ok = 0;
    if (fifo > 0) {
        struct sched_param sp = { .sched_priority = fifo };
        fifo_ok = sched_setscheduler(0, SCHED_FIFO, &sp) == 0;
        if (!fifo_ok) perror("SCHED_FIFO (continuing at normal priority)");
    }
    struct sigaction sa; memset(&sa, 0, sizeof(sa));
    sa.sa_handler = on_term;
    sigaction(SIGTERM, &sa, NULL);
    sigaction(SIGINT, &sa, NULL);

    size_t cap = 1 << 16, n = 0;
    struct rec *r = calloc(cap, sizeof(*r));
    uint64_t next = now_ns();
    char phase = 'G';
    while (!quit) {
        struct timespec ts = { .tv_sec = (time_t)(next / 1000000000ULL), .tv_nsec = (long)(next % 1000000000ULL) };
        int rc;
        do { rc = clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &ts, NULL); } while (rc == EINTR && !quit);
        if (quit) break;
        uint64_t wake = now_ns();
        if (phase == 'G') { kill(cpid, SIGSTOP); kill(wpid, SIGCONT); }
        else              { kill(wpid, SIGSTOP); kill(cpid, SIGCONT); }
        uint64_t after = now_ns();
        if (n < cap) r[n++] = (struct rec){ next, wake, after, phase };
        next += (phase == 'G') ? g_ns : t_ns;
        phase = (phase == 'G') ? 'T' : 'G';
    }
    kill(wpid, SIGCONT);
    kill(cpid, SIGCONT);
    FILE *f = fopen(log, "w");
    if (f) {
        fprintf(f, "# gangsim wpid=%d cpid=%d g_ns=%llu t_ns=%llu cpu=%d fifo=%d fifo_ok=%d\n",
                wpid, cpid, (unsigned long long)g_ns, (unsigned long long)t_ns, cpu, fifo, fifo_ok);
        fprintf(f, "idx,phase,target_ns,wake_ns,after_kill_ns\n");
        for (size_t i = 0; i < n; i++)
            fprintf(f, "%zu,%c,%llu,%llu,%llu\n", i, r[i].phase, (unsigned long long)r[i].target,
                    (unsigned long long)r[i].wake, (unsigned long long)r[i].after);
        fclose(f);
    }
    return 0;
}
