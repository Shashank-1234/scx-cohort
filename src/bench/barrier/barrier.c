/* P0.5 barrier benchmark: N worker threads, fixed work per round, then a barrier.
 *
 *   barrier --threads 4 --cpus 1-4 --mode spin|block|adaptive --iters N [--spin-us 50] --shm FILE
 *   barrier --calibrate --cpus C [--cal-iters N] [--cal-reps 5]
 *
 * Modes: spin = sense-reversing counter, cpu_relax loop; block = futex wait on the sense
 * word (no spin); adaptive = spin up to --spin-us, then futex.  The last arriver of a round
 * increments shm->rounds and publishes the stop decision, so all threads leave together. */
#include "common.h"
#include <limits.h>
#include <linux/futex.h>
#include <pthread.h>
#include <sys/syscall.h>

enum mode { M_SPIN, M_BLOCK, M_ADAPTIVE };

struct barrier {
    uint32_t count __attribute__((aligned(64)));
    uint32_t sense __attribute__((aligned(64)));
    uint32_t waiters __attribute__((aligned(64)));
    uint32_t done;
    uint32_t n;
};

static struct barrier B;
static struct shm_ctr *S;
static enum mode MODE = M_SPIN;
static uint64_t ITERS = 1000, SPIN_NS = 50000;
static int NT = 4;
static volatile int start_flag = 0;
static double thread_cpu_s[MAX_THREADS];

static long futex(uint32_t *addr, int op, uint32_t val) {
    return syscall(SYS_futex, addr, op, val, NULL, NULL, 0);
}

/* Returns 1 if the run should end (decided by the last arriver of this round). */
static int barrier_wait(uint32_t *local_sense) {
    uint32_t ls = !*local_sense;
    *local_sense = ls;
    if (__atomic_fetch_add(&B.count, 1, __ATOMIC_ACQ_REL) == B.n - 1) {
        __atomic_store_n(&B.count, 0, __ATOMIC_RELAXED);
        __atomic_fetch_add(&S->rounds, 1, __ATOMIC_RELAXED);
        __atomic_store_n(&B.done, (uint32_t)load_i32(&S->stop), __ATOMIC_RELAXED);
        /* seq_cst store of sense + seq_cst load of waiters: the store-buffering pattern;
         * with both seq_cst a sleeping waiter is never missed. */
        __atomic_store_n(&B.sense, ls, __ATOMIC_SEQ_CST);
        if (MODE != M_SPIN && __atomic_load_n(&B.waiters, __ATOMIC_SEQ_CST))
            futex(&B.sense, FUTEX_WAKE_PRIVATE, INT_MAX);
    } else {
        if (MODE == M_SPIN) {
            while (__atomic_load_n(&B.sense, __ATOMIC_ACQUIRE) != ls) cpu_relax();
        } else {
            if (MODE == M_ADAPTIVE) {
                uint64_t t0 = now_ns();
                unsigned k = 0;
                while (__atomic_load_n(&B.sense, __ATOMIC_ACQUIRE) != ls) {
                    cpu_relax();
                    if ((++k & 63) == 0 && now_ns() - t0 > SPIN_NS) break;
                }
            }
            while (__atomic_load_n(&B.sense, __ATOMIC_ACQUIRE) != ls) {
                __atomic_fetch_add(&B.waiters, 1, __ATOMIC_SEQ_CST);
                if (__atomic_load_n(&B.sense, __ATOMIC_SEQ_CST) != ls)
                    futex(&B.sense, FUTEX_WAIT_PRIVATE, !ls);
                __atomic_fetch_sub(&B.waiters, 1, __ATOMIC_SEQ_CST);
            }
        }
    }
    return (int)__atomic_load_n(&B.done, __ATOMIC_RELAXED);
}

static void *worker(void *arg) {
    int id = (int)(intptr_t)arg;
    S->tids[id] = (int32_t)syscall(SYS_gettid);
    char nm[16]; snprintf(nm, sizeof(nm), "p05w%d", id); pthread_setname_np(pthread_self(), nm);
    __atomic_fetch_add(&S->ready, 1, __ATOMIC_ACQ_REL);
    while (!__atomic_load_n(&start_flag, __ATOMIC_ACQUIRE)) cpu_relax();
    uint32_t local_sense = 0;
    uint64_t x = (uint64_t)id + 1;
    for (;;) {
        x = do_work(ITERS, x);
        if (barrier_wait(&local_sense)) break;
    }
    struct timespec ts;
    clock_gettime(CLOCK_THREAD_CPUTIME_ID, &ts);
    thread_cpu_s[id] = ts.tv_sec + ts.tv_nsec / 1e9;
    return (void *)(uintptr_t)x;
}

static int cmp_d(const void *a, const void *b) {
    double x = *(const double *)a, y = *(const double *)b;
    return (x > y) - (x < y);
}

static int calibrate(const char *cpus, uint64_t n, int reps) {
    set_affinity_or_die(cpus);
    double r[64];
    if (reps > 64) reps = 64;
    uint64_t x = 1;
    x = do_work(n / 4, x); /* warm-up */
    for (int i = 0; i < reps; i++) {
        uint64_t t0 = now_ns();
        x = do_work(n, x);
        uint64_t t1 = now_ns();
        r[i] = (double)n / ((t1 - t0) / 1000.0);
    }
    qsort(r, reps, sizeof(double), cmp_d);
    printf("{\"cpu\":\"%s\",\"iters_per_us\":%.4f,\"min\":%.4f,\"max\":%.4f,\"reps\":%d,\"n\":%llu,\"sink\":%llu}\n",
           cpus, r[reps / 2], r[0], r[reps - 1], reps, (unsigned long long)n, (unsigned long long)(x & 1));
    return 0;
}

int main(int argc, char **argv) {
    const char *cpus = "1-4", *shm = NULL, *mode = "spin";
    int cal = 0, cal_reps = 5;
    uint64_t cal_iters = 20000000ULL;
    for (int i = 1; i < argc; i++) {
        const char *a = argv[i];
        const char *v = (i + 1 < argc) ? argv[i + 1] : NULL;
        if (!strcmp(a, "--threads") && v) { NT = atoi(v); i++; }
        else if (!strcmp(a, "--cpus") && v) { cpus = v; i++; }
        else if (!strcmp(a, "--mode") && v) { mode = v; i++; }
        else if (!strcmp(a, "--iters") && v) { ITERS = strtoull(v, NULL, 10); i++; }
        else if (!strcmp(a, "--spin-us") && v) { SPIN_NS = strtoull(v, NULL, 10) * 1000ULL; i++; }
        else if (!strcmp(a, "--shm") && v) { shm = v; i++; }
        else if (!strcmp(a, "--calibrate")) { cal = 1; }
        else if (!strcmp(a, "--cal-iters") && v) { cal_iters = strtoull(v, NULL, 10); i++; }
        else if (!strcmp(a, "--cal-reps") && v) { cal_reps = atoi(v); i++; }
        else { fprintf(stderr, "unknown arg %s\n", a); return 2; }
    }
    if (cal) return calibrate(cpus, cal_iters, cal_reps);
    if (!shm || NT < 1 || NT > MAX_THREADS) { fprintf(stderr, "need --shm and 1..%d threads\n", MAX_THREADS); return 2; }
    if (!strcmp(mode, "spin")) MODE = M_SPIN;
    else if (!strcmp(mode, "block")) MODE = M_BLOCK;
    else if (!strcmp(mode, "adaptive")) MODE = M_ADAPTIVE;
    else { fprintf(stderr, "bad mode\n"); return 2; }

    set_affinity_or_die(cpus); /* threads inherit the 4-CPU set (set, not 1:1) */
    S = shm_open_ctr(shm);
    S->nthreads = NT;
    B.n = (uint32_t)NT;
    pthread_t th[MAX_THREADS];
    uint64_t t0 = now_ns();
    for (int i = 0; i < NT; i++) pthread_create(&th[i], NULL, worker, (void *)(intptr_t)i);
    while (load_i32(&S->ready) < NT) usleep(1000);
    __atomic_store_n(&start_flag, 1, __ATOMIC_RELEASE);
    for (int i = 0; i < NT; i++) pthread_join(th[i], NULL);
    uint64_t t1 = now_ns();
    printf("{\"role\":\"worker\",\"mode\":\"%s\",\"iters\":%llu,\"threads\":%d,\"rounds\":%llu,\"wall_s\":%.3f,\"thread_cpu_s\":[",
           mode, (unsigned long long)ITERS, NT, (unsigned long long)S->rounds, (t1 - t0) / 1e9);
    for (int i = 0; i < NT; i++) printf("%s%.4f", i ? "," : "", thread_cpu_s[i]);
    printf("]}\n");
    return 0;
}
