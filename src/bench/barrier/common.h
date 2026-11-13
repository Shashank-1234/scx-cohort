/* Shared helpers for the P0.5 barrier benchmark, competitor and calibration.
 * Counters live in a mmap'd shared-memory file so the harness can read them
 * at window start and end even while a process is SIGSTOPped. */
#ifndef P05_COMMON_H
#define P05_COMMON_H
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define SHM_MAGIC 0x503035434f554e54ULL /* "P05COUNT" */
#define MAX_THREADS 16

struct shm_ctr {
    uint64_t magic;
    uint64_t rounds;               /* worker: completed barrier rounds (written by thread 0) */
    uint64_t iters[MAX_THREADS];   /* competitor: per-thread loop iterations */
    int32_t tids[MAX_THREADS];
    int32_t nthreads;
    int32_t ready;                 /* number of threads that reached their loop */
    int32_t stop;                  /* harness sets 1 to end the run */
    int32_t pad;
};

static inline uint64_t now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

static inline void cpu_relax(void) {
#if defined(__aarch64__)
    __asm__ __volatile__("yield" ::: "memory");
#elif defined(__x86_64__)
    __asm__ __volatile__("pause" ::: "memory");
#else
    __asm__ __volatile__("" ::: "memory");
#endif
}

/* Fixed useful work: a dependent LCG chain the compiler cannot shortcut. */
static inline uint64_t do_work(uint64_t n, uint64_t x) {
    for (uint64_t i = 0; i < n; i++) {
        x = x * 6364136223846793005ULL + 1442695040888963407ULL;
        __asm__ __volatile__("" : "+r"(x));
    }
    return x;
}

static struct shm_ctr *shm_open_ctr(const char *path) {
    int fd = open(path, O_RDWR | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) { perror("open shm"); exit(2); }
    if (ftruncate(fd, sizeof(struct shm_ctr)) != 0) { perror("ftruncate"); exit(2); }
    struct shm_ctr *s = mmap(NULL, sizeof(*s), PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (s == MAP_FAILED) { perror("mmap"); exit(2); }
    close(fd);
    memset(s, 0, sizeof(*s));
    s->magic = SHM_MAGIC;
    return s;
}

/* Parse "1-4" or "1,2,3,4" or "0" into a cpu_set_t. */
static void parse_cpus(const char *spec, cpu_set_t *set) {
    CPU_ZERO(set);
    char *dup = strdup(spec), *save = NULL;
    for (char *tok = strtok_r(dup, ",", &save); tok; tok = strtok_r(NULL, ",", &save)) {
        int a, b;
        if (sscanf(tok, "%d-%d", &a, &b) == 2) { for (int c = a; c <= b; c++) CPU_SET(c, set); }
        else CPU_SET(atoi(tok), set);
    }
    free(dup);
}

static void set_affinity_or_die(const char *spec) {
    cpu_set_t set;
    parse_cpus(spec, &set);
    if (sched_setaffinity(0, sizeof(set), &set) != 0) { perror("sched_setaffinity"); exit(2); }
}

static inline void store_relaxed64(uint64_t *p, uint64_t v) { __atomic_store_n(p, v, __ATOMIC_RELAXED); }
static inline int32_t load_i32(int32_t *p) { return __atomic_load_n(p, __ATOMIC_ACQUIRE); }

#endif
