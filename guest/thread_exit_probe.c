#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <string.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define THREADS_PER_MODE 32
#define WAIT_STEPS 4000

static atomic_int worker_tid;

static void pause_step(void)
{
    struct timespec delay = {0, 1000000};
    while (nanosleep(&delay, &delay) != 0 && errno == EINTR) {}
}

static void *worker(void *unused)
{
    (void)unused;
    atomic_store_explicit(&worker_tid, (int)syscall(SYS_gettid), memory_order_release);
    return NULL;
}

static int wait_for_retirement(void)
{
    int tid = 0;
    for (unsigned step = 0; step < WAIT_STEPS; ++step) {
        tid = atomic_load_explicit(&worker_tid, memory_order_acquire);
        if (tid != 0) break;
        pause_step();
    }
    if (tid <= 0) return 20;
    char path[80];
    snprintf(path, sizeof(path), "/proc/self/task/%d", tid);
    for (unsigned step = 0; step < WAIT_STEPS; ++step) {
        int fd = open(path, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
        if (fd < 0) return errno == ENOENT ? 0 : 21;
        close(fd);
        pause_step();
    }
    // A flag written before return does not establish actual thread exit.
    return 22;
}

static int exercise_threads(void)
{
    for (int detached = 0; detached <= 1; ++detached) {
        pthread_attr_t attr;
        if (pthread_attr_init(&attr) != 0) return 10;
        if (pthread_attr_setdetachstate(&attr, detached ? PTHREAD_CREATE_DETACHED
                                                       : PTHREAD_CREATE_JOINABLE) != 0)
            return 11;
        for (unsigned i = 0; i < THREADS_PER_MODE; ++i) {
            pthread_t thread;
            atomic_store_explicit(&worker_tid, 0, memory_order_relaxed);
            if (pthread_create(&thread, &attr, worker, NULL) != 0) return 12;
            if (!detached && pthread_join(thread, NULL) != 0) return 13;
            int rc = wait_for_retirement();
            if (rc != 0) return rc;
        }
        pthread_attr_destroy(&attr);
    }
    return 0;
}

int main(int argc, char **argv)
{
    if (argc != 2 || (strcmp(argv[1], "control") != 0 &&
                      strcmp(argv[1], "preload") != 0)) return 64;
    // Supervise in a separate process: a concurrent exit_group(0) in the
    // workload must not conceal the detached worker's fatal signal.
    pid_t child = fork();
    if (child < 0) return 65;
    if (child == 0) _exit(exercise_threads());
    int status = 0;
    pid_t waited;
    do { waited = waitpid(child, &status, 0); } while (waited < 0 && errno == EINTR);
    if (waited != child) return 66;
    int passed = WIFEXITED(status) && WEXITSTATUS(status) == 0;
    printf("LEGOFS_THREAD_EXIT_PROBE label=%s raw_status=%d exited=%d exit_code=%d "
           "signaled=%d term_signal=%d joinable=%d detached=%d passed=%d\n",
           argv[1], status, WIFEXITED(status),
           WIFEXITED(status) ? WEXITSTATUS(status) : -1,
           WIFSIGNALED(status), WIFSIGNALED(status) ? WTERMSIG(status) : -1,
           passed ? THREADS_PER_MODE : 0, passed ? THREADS_PER_MODE : 0, passed);
    return passed ? 0 : 1;
}
