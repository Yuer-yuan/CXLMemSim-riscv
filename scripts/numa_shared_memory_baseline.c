#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

/* Ordinary cached MAP_SHARED loads/stores, with disjoint worker ranges.
 * Software bytes are useful work, not CXL link bytes or persistence evidence.
 * Run under numactl --membind; every resident page is checked before and after.
 */
struct worker { int cpu; uint64_t *data; size_t words; uint64_t bytes, sum; };
static pthread_barrier_t barrier;
static double deadline;
static const char *mode;

static void fail(const char *what) { perror(what); exit(2); }
static double now(void) {
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t)) fail("clock_gettime");
    return (double)t.tv_sec + (double)t.tv_nsec / 1e9;
}
static void sync_workers(void) {
    int rc = pthread_barrier_wait(&barrier);
    if (rc && rc != PTHREAD_BARRIER_SERIAL_THREAD) { errno = rc; fail("barrier"); }
}
static void *work(void *arg) {
    struct worker *w = arg;
    cpu_set_t cpus;
    CPU_ZERO(&cpus); CPU_SET(w->cpu, &cpus);
    int rc = pthread_setaffinity_np(pthread_self(), sizeof(cpus), &cpus);
    if (rc) { errno = rc; fail("setaffinity"); }
    for (size_t i = 0; i < w->words; ++i) w->data[i] = i + 1;
    sync_workers(); /* All first touch is complete. Main checks every page. */
    sync_workers(); /* Publish the common deadline after the placement check. */
    uint64_t sum = 0, passes = 0;
    do {
        __asm__ volatile("" : : "r"(w->data) : "memory");
        if (!strcmp(mode, "read")) {
            for (size_t i = 0; i < w->words; ++i) sum += w->data[i];
            w->bytes += w->words * sizeof(uint64_t);
        } else if (!strcmp(mode, "write")) {
            for (size_t i = 0; i < w->words; ++i) w->data[i] = i + passes;
            w->bytes += w->words * sizeof(uint64_t);
        } else {
            /* One read and one write per word; ordinary write allocation. */
            for (size_t i = 0; i < w->words; ++i) {
                uint64_t value = w->data[i];
                sum += value;
                w->data[i] = value + 1;
            }
            w->bytes += 2 * w->words * sizeof(uint64_t);
        }
        __asm__ volatile("" : : "r"(w->data) : "memory");
        ++passes;
    } while (now() < deadline);
    w->sum = sum ^ w->data[w->words - 1];
    return NULL;
}
static size_t check_pages(void *mapping, size_t bytes, int expected_node) {
    long page = sysconf(_SC_PAGESIZE);
    void *addresses[4096];
    int status[4096];
    size_t checked = 0, count = bytes / (size_t)page;
    while (checked < count) {
        size_t n = count - checked;
        if (n > 4096) n = 4096;
        for (size_t i = 0; i < n; ++i)
            addresses[i] = (char *)mapping + (checked + i) * (size_t)page;
        long rc = syscall(SYS_move_pages, 0, n, addresses, NULL, status, 0);
        if (rc) fail("move_pages query");
        for (size_t i = 0; i < n; ++i) {
            if (status[i] != expected_node) {
                fprintf(stderr, "page=%zu node=%d expected=%d\n", checked + i, status[i], expected_node);
                exit(2);
            }
        }
        checked += n;
    }
    return checked;
}
static unsigned long number(const char *text) {
    char *end;
    errno = 0;
    unsigned long value = strtoul(text, &end, 10);
    if (errno || !*text || *end || *text == '-') { fprintf(stderr, "invalid number: %s\n", text); exit(2); }
    return value;
}
int main(int argc, char **argv) {
    if (argc != 7) {
        fprintf(stderr, "Usage: %s FILE CPU_CSV MIB_PER_WORKER SECONDS read|write|mixed EXPECTED_NODE\n", argv[0]);
        return 2;
    }
    mode = argv[5];
    if (strcmp(mode, "read") && strcmp(mode, "write") && strcmp(mode, "mixed")) return 2;
    unsigned long mib = number(argv[3]), seconds = number(argv[4]), node = number(argv[6]);
    if (!mib || mib > 32768 || !seconds || seconds > 300 || node > 1024) return 2;
    struct worker workers[CPU_SETSIZE] = {0};
    pthread_t threads[CPU_SETSIZE];
    unsigned n = 0;
    for (char *token = strtok(argv[2], ","); token; token = strtok(NULL, ",")) {
        unsigned long cpu = number(token);
        if (cpu >= CPU_SETSIZE || n == CPU_SETSIZE) return 2;
        for (unsigned i = 0; i < n; ++i) if (workers[i].cpu == (int)cpu) return 2;
        workers[n++].cpu = (int)cpu;
    }
    if (!n || (uint64_t)mib * n > 32768) return 2;
    size_t each = (size_t)mib * 1024 * 1024, bytes = each * n;
    int fd = open(argv[1], O_RDWR | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
    if (fd < 0) fail("exclusive create");
    struct stat st;
    if (fstat(fd, &st) || !S_ISREG(st.st_mode)) fail("regular file required");
    if (ftruncate(fd, (off_t)bytes)) fail("ftruncate");
    void *mapping = mmap(NULL, bytes, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (mapping == MAP_FAILED) fail("mmap");
    int rc = pthread_barrier_init(&barrier, NULL, n + 1);
    if (rc) { errno = rc; fail("barrier_init"); }
    for (unsigned i = 0; i < n; ++i) {
        workers[i].data = (uint64_t *)((char *)mapping + each * i);
        workers[i].words = each / sizeof(uint64_t);
        rc = pthread_create(&threads[i], NULL, work, &workers[i]);
        if (rc) { errno = rc; fail("pthread_create"); }
    }
    sync_workers();
    size_t before = check_pages(mapping, bytes, (int)node);
    double started = now();
    deadline = started + (double)seconds;
    printf("{\"event\":\"start\",\"monotonic_seconds\":%.9f,\"pid\":%d,\"mode\":\"%s\",\"workers\":%u,\"resident_pages\":%zu,\"node\":%lu}\n", started, getpid(), mode, n, before, node);
    fflush(stdout);
    sync_workers();
    uint64_t useful = 0, sum = 0;
    for (unsigned i = 0; i < n; ++i) {
        rc = pthread_join(threads[i], NULL);
        if (rc) { errno = rc; fail("pthread_join"); }
        useful += workers[i].bytes; sum ^= workers[i].sum;
    }
    double elapsed = now() - started;
    size_t after = check_pages(mapping, bytes, (int)node);
    printf("{\"event\":\"result\",\"mode\":\"%s\",\"workers\":%u,\"mapped_bytes\":%zu,\"useful_bytes\":%llu,\"seconds\":%.9f,\"gib_per_second\":%.6f,\"checksum\":%llu,\"resident_pages_before\":%zu,\"resident_pages_after\":%zu,\"node\":%lu}\n", mode, n, bytes, (unsigned long long)useful, elapsed, (double)useful / elapsed / 1073741824.0, (unsigned long long)sum, before, after, node);
    if (munmap(mapping, bytes) || close(fd)) fail("close mapping");
    pthread_barrier_destroy(&barrier);
    return 0;
}
