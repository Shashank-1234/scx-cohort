/* P0.5 competitor: N independent CPU-bound threads counting loop iterations.
 *   competitor --threads 4 --cpus 1-4 --shm FILE
 * Each thread adds CHUNK to shm->iters[i] after every chunk of the same LCG work
 * the barrier workers do, so "progress" is in the same units. */
#include "common.h"
#include <pthread.h>
#include <sys/syscall.h>

#define CHUNK 4096ULL
static struct shm_ctr *S;
static int NT = 4;
static double thread_cpu_s[MAX_THREADS];

static void *spin(void *arg) {
    int id = (int)(intptr_t)arg;
    S->tids[id] = (int32_t)syscall(SYS_gettid);
    char nm[16]; snprintf(nm, sizeof(nm), "p05c%d", id); pthread_setname_np(pthread_self(), nm);
    __atomic_fetch_add(&S->ready, 1, __ATOMIC_ACQ_REL);
    uint64_t x = (uint64_t)id + 7, n = 0;
    while (!load_i32(&S->stop)) {
        x = do_work(CHUNK, x);
        n += CHUNK;
        store_relaxed64(&S->iters[id], n);
    }
    struct timespec ts;
    clock_gettime(CLOCK_THREAD_CPUTIME_ID, &ts);
    thread_cpu_s[id] = ts.tv_sec + ts.tv_nsec / 1e9;
    return (void *)(uintptr_t)x;
}

int main(int argc, char **argv) {
    const char *cpus = "1-4", *shm = NULL;
    for (int i = 1; i < argc; i++) {
        const char *v = (i + 1 < argc) ? argv[i + 1] : NULL;
        if (!strcmp(argv[i], "--threads") && v) { NT = atoi(v); i++; }
        else if (!strcmp(argv[i], "--cpus") && v) { cpus = v; i++; }
        else if (!strcmp(argv[i], "--shm") && v) { shm = v; i++; }
        else { fprintf(stderr, "unknown arg %s\n", argv[i]); return 2; }
    }
    if (!shm || NT < 1 || NT > MAX_THREADS) { fprintf(stderr, "need --shm\n"); return 2; }
    set_affinity_or_die(cpus);
    S = shm_open_ctr(shm);
    S->nthreads = NT;
    pthread_t th[MAX_THREADS];
    for (int i = 0; i < NT; i++) pthread_create(&th[i], NULL, spin, (void *)(intptr_t)i);
    for (int i = 0; i < NT; i++) pthread_join(th[i], NULL);
    unsigned long long tot = 0;
    for (int i = 0; i < NT; i++) tot += S->iters[i];
    printf("{\"role\":\"competitor\",\"threads\":%d,\"iters\":%llu,\"thread_cpu_s\":[", NT, tot);
    for (int i = 0; i < NT; i++) printf("%s%.4f", i ? "," : "", thread_cpu_s[i]);
    printf("]}\n");
    return 0;
}
