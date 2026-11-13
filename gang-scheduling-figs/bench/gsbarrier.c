/* gsbarrier: the BARRIER benchmark of Peter et al. 2011 (Figure 2), adapted.
 *
 *   for (;;) { work(interval + rand(variance)); barrier(); rounds++; }
 *
 * Minimal adaptation of src/bench/barrier/barrier.c (same work loop, same
 * sense-reversing barrier, same spin-then-futex code). Added:
 *   --interval-iters I   fixed work per round, in calibrated do_work() iterations
 *   --variance-iters V   extra work per round, per worker, drawn from rand(V) (below)
 *   --name PREFIX        thread names PREFIX0..PREFIX(n-1); scx_cohort's pool is "p05w*"
 *   --seed S --app N     per-worker RNG seeds = f(S, N, worker id)
 *   per-worker counters in shared memory (rounds seen, work done, waits, spins, blocks)
 *   --rng-selftest N     print statistics of N draws per worker and exit (no threads run)
 *
 * rand(V): bounded, approximately normal, on [0, V): the mean of four independent
 * uniform draws (Irwin-Hall, n = 4), scaled by V. Mean V/2, s.d. V/sqrt(48) ~ 0.144 V.
 * The paper says only "normally distributed within [0, a]"; this is our choice.
 * Each worker owns its splitmix64 state (no shared RNG, no locks, no libm).
 *
 * Usage:
 *   gsbarrier --threads 4 --cpus 1-4 --mode adaptive --interval-iters I --variance-iters V
 *             --spin-us 50 --shm FILE [--name p05w] [--seed 1] [--app 0]
 *   gsbarrier --rng-selftest 100000 --variance-iters V [--threads 4] [--seed 1] [--app 0]
 *
 * Calibration (iterations per microsecond) uses the existing `barrier --calibrate`:
 * the same do_work() from common.h, so the units are identical. */
#include "../../src/bench/barrier/common.h"
#include <limits.h>
#include <linux/futex.h>
#include <math.h>
#include <pthread.h>
#include <sys/syscall.h>

enum mode { M_SPIN, M_BLOCK, M_ADAPTIVE };

/* Shared-memory layout: struct shm_ctr (224 bytes, read by src/harness/lib.py) at
 * offset 0, then one 64-byte slot per worker at offset 256 + 64 * id. Each worker writes
 * only its own slot (own cache line). */
#define SLOT_OFF 256
struct slot {
    uint64_t rounds;       /* barrier rounds this worker completed */
    uint64_t work_iters;   /* do_work() iterations executed (fixed + random part) */
    uint64_t waits;        /* barrier arrivals that had to wait (not the last arriver) */
    uint64_t blocks;       /* waits that went past the spin budget into the futex phase */
    uint64_t futex_calls;  /* FUTEX_WAIT syscalls issued */
    uint64_t spin_ns_sum;  /* time spent in the spin phase (adaptive only) */
    uint64_t spin_ns_max;  /* longest spin phase (adaptive only) */
    uint64_t pad;
};
#define SHM_TOTAL (SLOT_OFF + MAX_THREADS * sizeof(struct slot))

struct barrier {
    uint32_t count __attribute__((aligned(64)));
    uint32_t sense __attribute__((aligned(64)));
    uint32_t waiters __attribute__((aligned(64)));
    uint32_t done;
    uint32_t n;
};

static struct barrier B;
static struct shm_ctr *S;
static struct slot *SL;
static enum mode MODE = M_ADAPTIVE;
static uint64_t ITERS = 0, VAR = 0, SPIN_NS = 50000, SEED = 1, APP = 0;
static int NT = 4;
static const char *NAME = "p05w";
static volatile int start_flag = 0;
static double thread_cpu_s[MAX_THREADS];

static long futex(uint32_t *addr, int op, uint32_t val) {
    return syscall(SYS_futex, addr, op, val, NULL, NULL, 0);
}

static inline uint64_t splitmix64(uint64_t *s) {
    uint64_t z = (*s += 0x9e3779b97f4a7c15ULL);
    z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ULL;
    z = (z ^ (z >> 27)) * 0x94d049bb133111ebULL;
    return z ^ (z >> 31);
}

static uint64_t seed_for(int id) {
    uint64_t s = SEED * 0x100000001b3ULL ^ (APP << 40) ^ ((uint64_t)id << 20) ^ 0x6a09e667f3bcc909ULL;
    (void)splitmix64(&s);
    return s;
}

/* rand(V): Irwin-Hall n = 4 on [0, V). */
static inline uint64_t rand_bounded(uint64_t *s, uint64_t v) {
    if (!v)
        return 0;
    uint64_t a = splitmix64(s), b = splitmix64(s);
    uint64_t sum = (a & 0xffffffffULL) + (a >> 32) + (b & 0xffffffffULL) + (b >> 32); /* < 2^34 */
    return (uint64_t)(((unsigned __int128)sum * v) >> 34);
}

/* Returns 1 if the run should end (decided by the last arriver of this round). */
static int barrier_wait(uint32_t *local_sense, struct slot *my) {
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
        uint64_t waits = my->waits + 1;
        __atomic_store_n(&my->waits, waits, __ATOMIC_RELAXED);
        if (MODE == M_SPIN) {
            while (__atomic_load_n(&B.sense, __ATOMIC_ACQUIRE) != ls) cpu_relax();
        } else {
            if (MODE == M_ADAPTIVE) {
                uint64_t t0 = now_ns(), t1;
                unsigned k = 0;
                while (__atomic_load_n(&B.sense, __ATOMIC_ACQUIRE) != ls) {
                    cpu_relax();
                    if ((++k & 63) == 0 && now_ns() - t0 > SPIN_NS) break;
                }
                t1 = now_ns() - t0;
                __atomic_store_n(&my->spin_ns_sum, my->spin_ns_sum + t1, __ATOMIC_RELAXED);
                if (t1 > my->spin_ns_max) __atomic_store_n(&my->spin_ns_max, t1, __ATOMIC_RELAXED);
            }
            if (__atomic_load_n(&B.sense, __ATOMIC_ACQUIRE) != ls)
                __atomic_store_n(&my->blocks, my->blocks + 1, __ATOMIC_RELAXED);
            while (__atomic_load_n(&B.sense, __ATOMIC_ACQUIRE) != ls) {
                __atomic_fetch_add(&B.waiters, 1, __ATOMIC_SEQ_CST);
                if (__atomic_load_n(&B.sense, __ATOMIC_SEQ_CST) != ls) {
                    __atomic_store_n(&my->futex_calls, my->futex_calls + 1, __ATOMIC_RELAXED);
                    futex(&B.sense, FUTEX_WAIT_PRIVATE, !ls);
                }
                __atomic_fetch_sub(&B.waiters, 1, __ATOMIC_SEQ_CST);
            }
        }
    }
    return (int)__atomic_load_n(&B.done, __ATOMIC_RELAXED);
}

static void *worker(void *arg) {
    int id = (int)(intptr_t)arg;
    struct slot *my = &SL[id];
    S->tids[id] = (int32_t)syscall(SYS_gettid);
    char nm[16]; snprintf(nm, sizeof(nm), "%s%d", NAME, id); pthread_setname_np(pthread_self(), nm);
    uint64_t rng = seed_for(id);
    __atomic_fetch_add(&S->ready, 1, __ATOMIC_ACQ_REL);
    while (!__atomic_load_n(&start_flag, __ATOMIC_ACQUIRE)) cpu_relax();
    uint32_t local_sense = 0;
    uint64_t x = (uint64_t)id + 1 + APP * 101;
    for (;;) {
        uint64_t n = ITERS + rand_bounded(&rng, VAR);
        x = do_work(n, x);
        __atomic_store_n(&my->work_iters, my->work_iters + n, __ATOMIC_RELAXED);
        int stop = barrier_wait(&local_sense, my);
        __atomic_store_n(&my->rounds, my->rounds + 1, __ATOMIC_RELAXED);
        if (stop) break;
    }
    struct timespec ts;
    clock_gettime(CLOCK_THREAD_CPUTIME_ID, &ts);
    thread_cpu_s[id] = ts.tv_sec + ts.tv_nsec / 1e9;
    return (void *)(uintptr_t)x;
}

static struct shm_ctr *shm_open_ext(const char *path) {
    int fd = open(path, O_RDWR | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) { perror("open shm"); exit(2); }
    if (ftruncate(fd, SHM_TOTAL) != 0) { perror("ftruncate"); exit(2); }
    void *m = mmap(NULL, SHM_TOTAL, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (m == MAP_FAILED) { perror("mmap"); exit(2); }
    close(fd);
    memset(m, 0, SHM_TOTAL);
    struct shm_ctr *s = m;
    s->magic = SHM_MAGIC;
    SL = (struct slot *)((char *)m + SLOT_OFF);
    return s;
}

/* Per-worker statistics of N draws, plus the correlation of workers 0 and 1. */
static int rng_selftest(uint64_t n) {
    printf("{\"variance_iters\":%llu,\"draws\":%llu,\"workers\":[", (unsigned long long)VAR, (unsigned long long)n);
    double *d0 = calloc(n, sizeof(double)), *d1 = calloc(n, sizeof(double));
    for (int id = 0; id < NT; id++) {
        uint64_t s = seed_for(id), mn = UINT64_MAX, mx = 0;
        double sum = 0, sq = 0;
        for (uint64_t i = 0; i < n; i++) {
            uint64_t v = rand_bounded(&s, VAR);
            if (v < mn) mn = v;
            if (v > mx) mx = v;
            sum += v; sq += (double)v * v;
            if (id == 0) d0[i] = v;
            if (id == 1) d1[i] = v;
        }
        double mean = sum / n, sd = sqrt(sq / n - mean * mean);
        printf("%s{\"id\":%d,\"seed\":%llu,\"mean\":%.3f,\"sd\":%.3f,\"min\":%llu,\"max\":%llu}", id ? "," : "", id,
               (unsigned long long)seed_for(id), mean, sd, (unsigned long long)mn, (unsigned long long)mx);
    }
    double corr = 0;
    if (NT >= 2) {
        double m0 = 0, m1 = 0, c = 0, v0 = 0, v1 = 0;
        for (uint64_t i = 0; i < n; i++) { m0 += d0[i]; m1 += d1[i]; }
        m0 /= n; m1 /= n;
        for (uint64_t i = 0; i < n; i++) {
            c += (d0[i] - m0) * (d1[i] - m1); v0 += (d0[i] - m0) * (d0[i] - m0); v1 += (d1[i] - m1) * (d1[i] - m1);
        }
        corr = (v0 > 0 && v1 > 0) ? c / sqrt(v0 * v1) : 0;
    }
    printf("],\"corr_w0_w1\":%.5f}\n", corr);
    free(d0); free(d1);
    return 0;
}

int main(int argc, char **argv) {
    const char *cpus = "1-4", *shm = NULL, *mode = "adaptive";
    uint64_t selftest = 0;
    for (int i = 1; i < argc; i++) {
        const char *a = argv[i];
        const char *v = (i + 1 < argc) ? argv[i + 1] : NULL;
        if (!strcmp(a, "--threads") && v) { NT = atoi(v); i++; }
        else if (!strcmp(a, "--cpus") && v) { cpus = v; i++; }
        else if (!strcmp(a, "--mode") && v) { mode = v; i++; }
        else if (!strcmp(a, "--interval-iters") && v) { ITERS = strtoull(v, NULL, 10); i++; }
        else if (!strcmp(a, "--variance-iters") && v) { VAR = strtoull(v, NULL, 10); i++; }
        else if (!strcmp(a, "--spin-us") && v) { SPIN_NS = strtoull(v, NULL, 10) * 1000ULL; i++; }
        else if (!strcmp(a, "--shm") && v) { shm = v; i++; }
        else if (!strcmp(a, "--name") && v) { NAME = v; i++; }
        else if (!strcmp(a, "--seed") && v) { SEED = strtoull(v, NULL, 10); i++; }
        else if (!strcmp(a, "--app") && v) { APP = strtoull(v, NULL, 10); i++; }
        else if (!strcmp(a, "--rng-selftest") && v) { selftest = strtoull(v, NULL, 10); i++; }
        else { fprintf(stderr, "unknown arg %s\n", a); return 2; }
    }
    if (NT < 1 || NT > MAX_THREADS) { fprintf(stderr, "need 1..%d threads\n", MAX_THREADS); return 2; }
    if (selftest) return rng_selftest(selftest);
    if (!shm) { fprintf(stderr, "need --shm\n"); return 2; }
    if (strlen(NAME) > 12) { fprintf(stderr, "--name too long (max 12)\n"); return 2; }
    if (!strcmp(mode, "spin")) MODE = M_SPIN;
    else if (!strcmp(mode, "block")) MODE = M_BLOCK;
    else if (!strcmp(mode, "adaptive")) MODE = M_ADAPTIVE;
    else { fprintf(stderr, "bad mode\n"); return 2; }

    set_affinity_or_die(cpus); /* threads inherit the 4-CPU set (set, not 1:1), as barrier.c */
    S = shm_open_ext(shm);
    S->nthreads = NT;
    B.n = (uint32_t)NT;
    pthread_t th[MAX_THREADS];
    uint64_t t0 = now_ns();
    for (int i = 0; i < NT; i++) pthread_create(&th[i], NULL, worker, (void *)(intptr_t)i);
    while (load_i32(&S->ready) < NT) usleep(1000);
    __atomic_store_n(&start_flag, 1, __ATOMIC_RELEASE);
    for (int i = 0; i < NT; i++) pthread_join(th[i], NULL);
    uint64_t t1 = now_ns();
    int consistent = 1;
    for (int i = 0; i < NT; i++)
        if (SL[i].rounds != S->rounds) consistent = 0;
    printf("{\"role\":\"gsbarrier\",\"name\":\"%s\",\"mode\":\"%s\",\"interval_iters\":%llu,\"variance_iters\":%llu,"
           "\"spin_ns\":%llu,\"seed\":%llu,\"app\":%llu,\"threads\":%d,\"rounds\":%llu,\"rounds_consistent\":%s,"
           "\"wall_s\":%.3f,\"workers\":[",
           NAME, mode, (unsigned long long)ITERS, (unsigned long long)VAR, (unsigned long long)SPIN_NS,
           (unsigned long long)SEED, (unsigned long long)APP, NT, (unsigned long long)S->rounds,
           consistent ? "true" : "false", (t1 - t0) / 1e9);
    for (int i = 0; i < NT; i++)
        printf("%s{\"id\":%d,\"seed\":%llu,\"cpu_s\":%.4f,\"rounds\":%llu,\"work_iters\":%llu,\"waits\":%llu,"
               "\"blocks\":%llu,\"futex_calls\":%llu,\"spin_ns_sum\":%llu,\"spin_ns_max\":%llu}",
               i ? "," : "", i, (unsigned long long)seed_for(i), thread_cpu_s[i],
               (unsigned long long)SL[i].rounds, (unsigned long long)SL[i].work_iters,
               (unsigned long long)SL[i].waits, (unsigned long long)SL[i].blocks,
               (unsigned long long)SL[i].futex_calls, (unsigned long long)SL[i].spin_ns_sum,
               (unsigned long long)SL[i].spin_ns_max);
    printf("]}\n");
    return consistent ? 0 : 3;
}
