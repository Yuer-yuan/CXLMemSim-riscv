#define _GNU_SOURCE

#include "cxl_arena.h"

#include <errno.h>
#include <inttypes.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define PROBE_MAGIC UINT64_C(0x32433153434f4845) /* "2C1SCOHE" */
#define PROBE_VERSION 2
#define CONTROL_BYTES 4096UL
#define WAIT_TIMEOUT_MS 60000
#define CHECK_MAGIC UINT64_C(0xd6e8feb86659fd93)
#define REPLY_MAGIC UINT64_C(0xa5a55a5af00dcafe)
#define MIXED_PASSES 4U

typedef struct __attribute__((aligned(64))) {
    uint64_t value;
    unsigned char padding[56];
} cacheline_u64_t;

typedef struct __attribute__((aligned(64))) {
    cacheline_u64_t ready;
    cacheline_u64_t ack;
    struct __attribute__((aligned(64))) {
        uint64_t seq;
        uint64_t value;
        uint64_t checksum;
        uint64_t reply;
        uint64_t pid;
        uint64_t cpu;
        uint64_t errors;
        unsigned char padding[8];
    } packet;
    cacheline_u64_t handshake_done;
    cacheline_u64_t bulk_done;
    cacheline_u64_t bulk_ack;
    cacheline_u64_t verify_done;
    cacheline_u64_t mixed_done;
    cacheline_u64_t mixed_elapsed_ns;
    cacheline_u64_t mixed_checksum;
    cacheline_u64_t seqlock_ready;
    cacheline_u64_t seqlock_done;
    cacheline_u64_t seqlock_accepted;
    cacheline_u64_t seqlock_retries;
    cacheline_u64_t seqlock_errors;
} lane_t;

typedef struct __attribute__((aligned(64))) {
    uint64_t seq;
    uint64_t word[7];
} shared_snapshot_t;

typedef struct __attribute__((aligned(64))) {
    struct __attribute__((aligned(64))) {
        uint64_t magic;
        uint64_t version;
        uint64_t tensor_bytes;
        uint64_t payload_bytes;
        uint64_t iterations;
        uint64_t seqlock_updates;
        uint64_t numa_node;
        unsigned char padding[8];
    } metadata;
    cacheline_u64_t initialized;
    cacheline_u64_t clients_mask;
    cacheline_u64_t start;
    cacheline_u64_t bulk_start;
    cacheline_u64_t mixed_phase;
    cacheline_u64_t seqlock_start;
    cacheline_u64_t seqlock_stop;
    shared_snapshot_t snapshot;
    lane_t lane[2];
} probe_control_t;

_Static_assert(sizeof(probe_control_t) <= CONTROL_BYTES,
               "probe control structure exceeds its reserved page");

static uint64_t load_acquire(const uint64_t *p)
{
    return __atomic_load_n(p, __ATOMIC_ACQUIRE);
}

static void store_release(uint64_t *p, uint64_t value)
{
    __atomic_store_n(p, value, __ATOMIC_RELEASE);
}

static uint64_t load_relaxed(const uint64_t *p)
{
    return __atomic_load_n(p, __ATOMIC_RELAXED);
}

static void store_relaxed(uint64_t *p, uint64_t value)
{
    __atomic_store_n(p, value, __ATOMIC_RELAXED);
}

static uint64_t now_ns(void)
{
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC_RAW, &ts) != 0) {
        perror("clock_gettime");
        exit(2);
    }
    return (uint64_t)ts.tv_sec * UINT64_C(1000000000) + (uint64_t)ts.tv_nsec;
}

static void cpu_relax(unsigned long spins)
{
#if defined(__x86_64__) || defined(__i386__)
    __asm__ __volatile__("pause" ::: "memory");
#else
    __asm__ __volatile__("" ::: "memory");
#endif
    if ((spins & 0xfffffUL) == 0)
        sched_yield();
}

static int wait_equal(const uint64_t *p, uint64_t expected, const char *what)
{
    const uint64_t deadline = now_ns() + (uint64_t)WAIT_TIMEOUT_MS * UINT64_C(1000000);
    unsigned long spins = 0;

    while (load_acquire(p) != expected) {
        cpu_relax(++spins);
        if ((spins & 0xfffffUL) == 0 && now_ns() >= deadline) {
            fprintf(stderr, "timeout waiting for %s (wanted=%" PRIu64 ", saw=%" PRIu64 ")\n",
                    what, expected, load_acquire(p));
            return -1;
        }
    }
    return 0;
}

static int wait_mask(const uint64_t *p, uint64_t mask, const char *what)
{
    const uint64_t deadline = now_ns() + (uint64_t)WAIT_TIMEOUT_MS * UINT64_C(1000000);
    unsigned long spins = 0;

    while ((load_acquire(p) & mask) != mask) {
        cpu_relax(++spins);
        if ((spins & 0xfffffUL) == 0 && now_ns() >= deadline) {
            fprintf(stderr, "timeout waiting for %s (mask=%" PRIu64 ", saw=%" PRIu64 ")\n",
                    what, mask, load_acquire(p));
            return -1;
        }
    }
    return 0;
}

static uint64_t packet_value(unsigned client, uint64_t seq)
{
    return (seq * UINT64_C(0x9e3779b97f4a7c15)) ^
           ((uint64_t)(client + 1) * UINT64_C(0x94d049bb133111eb));
}

static uint64_t packet_checksum(unsigned client, uint64_t seq, uint64_t value)
{
    return CHECK_MAGIC ^ value ^ (seq << 17) ^ ((uint64_t)client << 61);
}

static uint64_t packet_reply(unsigned client, uint64_t seq, uint64_t value)
{
    return REPLY_MAGIC ^ value ^ (seq << 1) ^ (uint64_t)client;
}

static uint64_t payload_value(unsigned client, uint64_t word)
{
    return UINT64_C(0x243f6a8885a308d3) ^
           ((uint64_t)(client + 1) * UINT64_C(0x9e3779b97f4a7c15)) ^
           (word * UINT64_C(0xbf58476d1ce4e5b9));
}

static uint64_t mixed_write_value(unsigned phase, unsigned client,
                                  unsigned pass, uint64_t word)
{
    return payload_value(client, word) ^
           ((uint64_t)phase * UINT64_C(0xd1342543de82ef95)) ^
           ((uint64_t)(pass + 1) * UINT64_C(0x94d049bb133111eb));
}

static uint64_t mixed_read_value(unsigned phase, unsigned client, uint64_t word)
{
    if (phase == 1)
        return ~payload_value(client, word);
    return mixed_write_value(1, client, MIXED_PASSES - 1, word);
}

static uint64_t checksum_step(uint64_t checksum, uint64_t value,
                              unsigned pass, uint64_t word)
{
    /* An additive reduction can be vectorized by gcc.  A hash chain with a
     * loop-carried dependency would benchmark integer latency, not reads. */
    return checksum +
           (value ^ (word * UINT64_C(0x9e3779b97f4a7c15)) ^
            ((uint64_t)(pass + 1) * UINT64_C(0xd6e8feb86659fd93)));
}

static uint64_t expected_mixed_checksum(unsigned phase, unsigned client,
                                        uint64_t words)
{
    uint64_t checksum = UINT64_C(0xcbf29ce484222325);

    for (unsigned pass = 0; pass < MIXED_PASSES; ++pass)
        for (uint64_t word = 0; word < words; ++word)
            checksum = checksum_step(checksum,
                                     mixed_read_value(phase, client, word),
                                     pass, word);
    return checksum;
}

static uint64_t snapshot_value(uint64_t version, unsigned word)
{
    return UINT64_C(0x6a09e667f3bcc909) ^
           (version * UINT64_C(0x9e3779b97f4a7c15)) ^
           ((uint64_t)(word + 1) * UINT64_C(0xbf58476d1ce4e5b9));
}

static void *probe_tensor(cxl_arena_t *arena, size_t *bytes)
{
    const cxl_tensor_desc_t *desc = cxl_arena_tensors(arena);
    uint32_t count = __atomic_load_n(&arena->hdr->n_tensors, __ATOMIC_ACQUIRE);

    for (uint32_t i = 0; i < count; ++i) {
        if (strcmp(desc[i].name, "coherence_2c1s") == 0) {
            *bytes = (size_t)desc[i].nbytes;
            return cxl_arena_ptr(arena, desc[i].offset);
        }
    }
    return NULL;
}

static int parse_u64(const char *text, uint64_t min, uint64_t max,
                     const char *name, uint64_t *result)
{
    char *end = NULL;
    errno = 0;
    unsigned long long value = strtoull(text, &end, 10);
    if (errno || !end || *end != '\0' || value < min || value > max) {
        fprintf(stderr, "invalid %s: %s\n", name, text);
        return -1;
    }
    *result = (uint64_t)value;
    return 0;
}

static double gib_per_second(uint64_t bytes, uint64_t elapsed_ns)
{
    if (!elapsed_ns)
        return 0.0;
    return ((double)bytes / (double)(UINT64_C(1) << 30)) /
           ((double)elapsed_ns / 1.0e9);
}

static int run_server(const char *arena_name, int numa_node, uint64_t iterations,
                      uint64_t seqlock_updates, size_t payload_bytes,
                      unsigned hold_ms)
{
    cxl_arena_t arena;
    const size_t tensor_bytes = CONTROL_BYTES + 2 * payload_bytes;
    int64_t shape[1] = {(int64_t)tensor_bytes};
    int rc = 1;
    int numa_percent = -1;

    (void)cxl_arena_unlink(arena_name);
    if (cxl_arena_create(&arena, arena_name, tensor_bytes, 4096, numa_node) != 0)
        return 1;

    unsigned char *tensor = cxl_arena_add_tensor(&arena, "coherence_2c1s",
                                                  CXL_DT_U8, 1, shape);
    if (!tensor) {
        fprintf(stderr, "failed to allocate the probe tensor\n");
        goto out;
    }

    /* cxl_arena_create() applies mbind before faulting.  Touch every payload
     * page here, in the creator, so later client stores reuse node-1 pages. */
    memset(tensor, 0, tensor_bytes);
    probe_control_t *control = (probe_control_t *)tensor;
    control->metadata.magic = PROBE_MAGIC;
    control->metadata.version = PROBE_VERSION;
    control->metadata.tensor_bytes = tensor_bytes;
    control->metadata.payload_bytes = payload_bytes;
    control->metadata.iterations = iterations;
    control->metadata.seqlock_updates = seqlock_updates;
    control->metadata.numa_node = (uint64_t)numa_node;

    numa_percent = cxl_arena_verify_numa(&arena, numa_node, 4096);
    store_release(&control->initialized.value, 1);
    printf("SERVER_READY pid=%ld cpu=%d arena=%s numa_node=%d numa_percent=%d\n",
           (long)getpid(), sched_getcpu(), arena_name, numa_node, numa_percent);

    if (wait_mask(&control->clients_mask.value, 3, "both client registrations") != 0)
        goto out;

    printf("INSPECT_READY server=%ld client0=%" PRIu64 " client1=%" PRIu64
           " server_cpu=%d client0_cpu=%" PRIu64 " client1_cpu=%" PRIu64 "\n",
           (long)getpid(), control->lane[0].packet.pid, control->lane[1].packet.pid,
           sched_getcpu(), control->lane[0].packet.cpu, control->lane[1].packet.cpu);
    fflush(stdout);
    usleep((useconds_t)hold_ms * 1000U);

    uint64_t server_errors = 0;
    uint64_t start_ns = now_ns();
    store_release(&control->start.value, 1);
    for (uint64_t seq = 1; seq <= iterations; ++seq) {
        for (unsigned client = 0; client < 2; ++client) {
            lane_t *lane = &control->lane[client];
            if (wait_equal(&lane->ready.value, seq, "client publication") != 0)
                goto out;
            uint64_t value = lane->packet.value;
            if (lane->packet.seq != seq ||
                lane->packet.checksum != packet_checksum(client, seq, value) ||
                value != packet_value(client, seq))
                ++server_errors;
            lane->packet.reply = packet_reply(client, seq, value);
            store_release(&lane->ack.value, seq);
        }
    }
    if (wait_equal(&control->lane[0].handshake_done.value, 1,
                   "client0 handshake completion") != 0 ||
        wait_equal(&control->lane[1].handshake_done.value, 1,
                   "client1 handshake completion") != 0)
        goto out;
    uint64_t handshake_ns = now_ns() - start_ns;

    uint64_t bulk_begin_ns = now_ns();
    store_release(&control->bulk_start.value, 1);
    if (wait_equal(&control->lane[0].bulk_done.value, 1, "client0 bulk write") != 0 ||
        wait_equal(&control->lane[1].bulk_done.value, 1, "client1 bulk write") != 0)
        goto out;
    uint64_t bulk_write_ns = now_ns() - bulk_begin_ns;

    const uint64_t words = payload_bytes / sizeof(uint64_t);
    uint64_t verify_begin_ns = now_ns();
    for (unsigned client = 0; client < 2; ++client) {
        const uint64_t *payload = (const uint64_t *)(tensor + CONTROL_BYTES +
                                                     client * payload_bytes);
        for (uint64_t word = 0; word < words; ++word)
            if (payload[word] != payload_value(client, word))
                ++server_errors;
    }
    uint64_t server_read_ns = now_ns() - verify_begin_ns;

    uint64_t writeback_begin_ns = now_ns();
    for (unsigned client = 0; client < 2; ++client) {
        uint64_t *payload = (uint64_t *)(tensor + CONTROL_BYTES + client * payload_bytes);
        for (uint64_t word = 0; word < words; ++word)
            payload[word] = ~payload_value(client, word);
    }
    uint64_t server_write_ns = now_ns() - writeback_begin_ns;

    uint64_t client_read_begin_ns = now_ns();
    store_release(&control->lane[0].bulk_ack.value, 1);
    store_release(&control->lane[1].bulk_ack.value, 1);
    if (wait_equal(&control->lane[0].verify_done.value, 1, "client0 readback") != 0 ||
        wait_equal(&control->lane[1].verify_done.value, 1, "client1 readback") != 0)
        goto out;
    uint64_t client_read_ns = now_ns() - client_read_begin_ns;

    /* Concurrent read/write to disjoint payloads.  Swap the roles in the
     * second phase so the result is not an accident of one CCX or direction. */
    uint64_t mixed_wall_ns[2] = {0, 0};
    uint64_t mixed_writer_ns[2] = {0, 0};
    uint64_t mixed_reader_ns[2] = {0, 0};
    for (unsigned phase = 1; phase <= 2; ++phase) {
        const unsigned writer = phase - 1;
        const unsigned reader = 1 - writer;
        uint64_t mixed_begin_ns = now_ns();
        store_release(&control->mixed_phase.value, phase);
        if (wait_equal(&control->lane[0].mixed_done.value, phase,
                       "client0 mixed read/write") != 0 ||
            wait_equal(&control->lane[1].mixed_done.value, phase,
                       "client1 mixed read/write") != 0)
            goto out;
        mixed_wall_ns[phase - 1] = now_ns() - mixed_begin_ns;
        mixed_writer_ns[phase - 1] =
            load_acquire(&control->lane[writer].mixed_elapsed_ns.value);
        mixed_reader_ns[phase - 1] =
            load_acquire(&control->lane[reader].mixed_elapsed_ns.value);

        const uint64_t *writer_payload =
            (const uint64_t *)(tensor + CONTROL_BYTES + writer * payload_bytes);
        for (uint64_t word = 0; word < words; ++word)
            if (writer_payload[word] !=
                mixed_write_value(phase, writer, MIXED_PASSES - 1, word))
                ++server_errors;
        if (load_acquire(&control->lane[reader].mixed_checksum.value) !=
            expected_mixed_checksum(phase, reader, words))
            ++server_errors;
    }

    /* Same-cacheline concurrent read/write.  The server is the sole writer;
     * both clients take lock-free snapshots using an atomic sequence counter. */
    store_relaxed(&control->snapshot.seq, 0);
    for (unsigned word = 0; word < 7; ++word)
        store_relaxed(&control->snapshot.word[word], snapshot_value(0, word));
    if (wait_equal(&control->lane[0].seqlock_ready.value, 1,
                   "client0 seqlock readiness") != 0 ||
        wait_equal(&control->lane[1].seqlock_ready.value, 1,
                   "client1 seqlock readiness") != 0)
        goto out;

    uint64_t seqlock_begin_ns = now_ns();
    store_release(&control->seqlock_start.value, 1);
    for (uint64_t version = 1; version <= seqlock_updates; ++version) {
        __atomic_store_n(&control->snapshot.seq, 2 * version - 1,
                         __ATOMIC_SEQ_CST);
        for (unsigned word = 0; word < 7; ++word)
            store_relaxed(&control->snapshot.word[word],
                          snapshot_value(version, word));
        store_release(&control->snapshot.seq, 2 * version);
    }
    uint64_t seqlock_write_ns = now_ns() - seqlock_begin_ns;
    store_release(&control->seqlock_stop.value, 1);
    if (wait_equal(&control->lane[0].seqlock_done.value, 1,
                   "client0 seqlock completion") != 0 ||
        wait_equal(&control->lane[1].seqlock_done.value, 1,
                   "client1 seqlock completion") != 0)
        goto out;

    if (load_acquire(&control->snapshot.seq) != 2 * seqlock_updates)
        ++server_errors;
    for (unsigned word = 0; word < 7; ++word)
        if (load_relaxed(&control->snapshot.word[word]) !=
            snapshot_value(seqlock_updates, word))
            ++server_errors;
    for (unsigned client = 0; client < 2; ++client)
        if (load_acquire(&control->lane[client].seqlock_accepted.value) == 0)
            ++server_errors;

    uint64_t client_errors = control->lane[0].packet.errors +
                             control->lane[1].packet.errors;
    uint64_t total_errors = server_errors + client_errors;
    const uint64_t aggregate_bytes = 2 * payload_bytes;
    double handshakes_mops = ((double)(2 * iterations) / 1.0e6) /
                             ((double)handshake_ns / 1.0e9);

    printf("METRIC handshake rounds=%" PRIu64 " publications=%" PRIu64
           " elapsed_ms=%.3f aggregate_Mops=%.3f round_ns=%.1f\n",
           iterations, 2 * iterations, (double)handshake_ns / 1.0e6,
           handshakes_mops, (double)handshake_ns / (double)iterations);
    printf("METRIC clients_sequential_write bytes=%" PRIu64
           " elapsed_ms=%.3f aggregate_GiB_s=%.3f\n",
           aggregate_bytes, (double)bulk_write_ns / 1.0e6,
           gib_per_second(aggregate_bytes, bulk_write_ns));
    printf("METRIC server_sequential_read_verify bytes=%" PRIu64
           " elapsed_ms=%.3f GiB_s=%.3f\n",
           aggregate_bytes, (double)server_read_ns / 1.0e6,
           gib_per_second(aggregate_bytes, server_read_ns));
    printf("METRIC server_sequential_writeback bytes=%" PRIu64
           " elapsed_ms=%.3f GiB_s=%.3f\n",
           aggregate_bytes, (double)server_write_ns / 1.0e6,
           gib_per_second(aggregate_bytes, server_write_ns));
    printf("METRIC clients_sequential_readback bytes=%" PRIu64
           " elapsed_ms=%.3f aggregate_GiB_s=%.3f\n",
           aggregate_bytes, (double)client_read_ns / 1.0e6,
           gib_per_second(aggregate_bytes, client_read_ns));
    const uint64_t mixed_bytes_per_role =
        (uint64_t)payload_bytes * MIXED_PASSES;
    for (unsigned phase = 1; phase <= 2; ++phase) {
        const unsigned writer = phase - 1;
        const unsigned reader = 1 - writer;
        printf("METRIC mixed_rw phase=%u writer=client%u reader=client%u"
               " bytes_per_role=%" PRIu64 " writer_GiB_s=%.3f"
               " reader_GiB_s=%.3f wall_aggregate_GiB_s=%.3f\n",
               phase, writer, reader, mixed_bytes_per_role,
               gib_per_second(mixed_bytes_per_role,
                              mixed_writer_ns[phase - 1]),
               gib_per_second(mixed_bytes_per_role,
                              mixed_reader_ns[phase - 1]),
               gib_per_second(2 * mixed_bytes_per_role,
                              mixed_wall_ns[phase - 1]));
    }
    printf("METRIC shared_seqlock writer=server updates=%" PRIu64
           " elapsed_ms=%.3f update_Mops=%.3f cacheline_bytes=64\n",
           seqlock_updates, (double)seqlock_write_ns / 1.0e6,
           ((double)seqlock_updates / 1.0e6) /
               ((double)seqlock_write_ns / 1.0e9));
    for (unsigned client = 0; client < 2; ++client) {
        uint64_t accepted =
            load_acquire(&control->lane[client].seqlock_accepted.value);
        uint64_t retries =
            load_acquire(&control->lane[client].seqlock_retries.value);
        uint64_t snapshot_errors =
            load_acquire(&control->lane[client].seqlock_errors.value);
        double retry_percent = accepted + retries
            ? 100.0 * (double)retries / (double)(accepted + retries)
            : 0.0;
        printf("METRIC shared_seqlock_reader role=client%u accepted=%" PRIu64
               " retries=%" PRIu64 " retry_percent=%.3f errors=%" PRIu64 "\n",
               client, accepted, retries, retry_percent, snapshot_errors);
    }
    printf("RESULT %s errors=%" PRIu64 " server_errors=%" PRIu64
           " client_errors=%" PRIu64 " numa_percent=%d ordering=release-acquire"
           " mixed_rw=yes shared_seqlock=yes\n",
           (total_errors == 0 && (numa_percent < 0 || numa_percent >= 95)) ? "PASS" : "FAIL",
           total_errors, server_errors, client_errors, numa_percent);
    rc = (total_errors == 0 && (numa_percent < 0 || numa_percent >= 95)) ? 0 : 1;

out:
    cxl_arena_detach(&arena);
    (void)cxl_arena_unlink(arena_name);
    return rc;
}

static int snapshot_try_read(const shared_snapshot_t *snapshot,
                             uint64_t *retries, uint64_t *errors)
{
    uint64_t value[7];
    uint64_t first = load_acquire(&snapshot->seq);
    if (first & 1) {
        ++*retries;
        return 0;
    }

    for (unsigned word = 0; word < 7; ++word)
        value[word] = load_relaxed(&snapshot->word[word]);
    __atomic_thread_fence(__ATOMIC_ACQUIRE);
    uint64_t second = load_acquire(&snapshot->seq);
    if (first != second || (second & 1)) {
        ++*retries;
        return 0;
    }

    uint64_t version = second / 2;
    for (unsigned word = 0; word < 7; ++word)
        if (value[word] != snapshot_value(version, word))
            ++*errors;
    return 1;
}

static int run_client(const char *arena_name, unsigned client)
{
    cxl_arena_t arena;
    size_t tensor_bytes = 0;
    uint64_t errors = 0;
    lane_t *lane = NULL;

    if (cxl_arena_attach(&arena, arena_name) != 0)
        return 1;
    unsigned char *tensor = probe_tensor(&arena, &tensor_bytes);
    if (!tensor) {
        fprintf(stderr, "client%u: probe tensor not found\n", client);
        cxl_arena_detach(&arena);
        return 1;
    }

    probe_control_t *control = (probe_control_t *)tensor;
    if (wait_equal(&control->initialized.value, 1, "server initialization") != 0)
        goto fail;
    if (control->metadata.magic != PROBE_MAGIC ||
        control->metadata.version != PROBE_VERSION ||
        control->metadata.tensor_bytes != tensor_bytes ||
        control->metadata.payload_bytes < sizeof(uint64_t) ||
        control->metadata.tensor_bytes != CONTROL_BYTES +
                                          2 * control->metadata.payload_bytes) {
        fprintf(stderr, "client%u: incompatible control metadata\n", client);
        goto fail;
    }

    lane = &control->lane[client];
    lane->packet.pid = (uint64_t)getpid();
    lane->packet.cpu = (uint64_t)sched_getcpu();
    __atomic_fetch_or(&control->clients_mask.value, UINT64_C(1) << client,
                      __ATOMIC_RELEASE);
    printf("CLIENT_READY role=client%u pid=%ld cpu=%d arena=%s\n",
           client, (long)getpid(), sched_getcpu(), arena_name);

    if (wait_equal(&control->start.value, 1, "handshake start") != 0)
        goto fail;
    for (uint64_t seq = 1; seq <= control->metadata.iterations; ++seq) {
        uint64_t value = packet_value(client, seq);
        lane->packet.seq = seq;
        lane->packet.value = value;
        lane->packet.checksum = packet_checksum(client, seq, value);
        store_release(&lane->ready.value, seq);
        if (wait_equal(&lane->ack.value, seq, "server acknowledgement") != 0)
            goto fail;
        if (lane->packet.reply != packet_reply(client, seq, value))
            ++errors;
    }
    store_release(&lane->handshake_done.value, 1);

    if (wait_equal(&control->bulk_start.value, 1, "bulk phase") != 0)
        goto fail;
    size_t payload_bytes = (size_t)control->metadata.payload_bytes;
    uint64_t words = payload_bytes / sizeof(uint64_t);
    uint64_t *payload = (uint64_t *)(tensor + CONTROL_BYTES + client * payload_bytes);
    for (uint64_t word = 0; word < words; ++word)
        payload[word] = payload_value(client, word);
    store_release(&lane->bulk_done.value, 1);

    if (wait_equal(&lane->bulk_ack.value, 1, "server writeback") != 0)
        goto fail;
    for (uint64_t word = 0; word < words; ++word)
        if (payload[word] != ~payload_value(client, word))
            ++errors;
    store_release(&lane->verify_done.value, 1);

    for (unsigned phase = 1; phase <= 2; ++phase) {
        if (wait_equal(&control->mixed_phase.value, phase,
                       "mixed read/write phase") != 0)
            goto fail;
        const unsigned writer = phase - 1;
        uint64_t checksum = UINT64_C(0xcbf29ce484222325);
        uint64_t mixed_begin_ns = now_ns();
        if (client == writer) {
            for (unsigned pass = 0; pass < MIXED_PASSES; ++pass)
                for (uint64_t word = 0; word < words; ++word)
                    payload[word] = mixed_write_value(phase, client, pass, word);
        } else {
            for (unsigned pass = 0; pass < MIXED_PASSES; ++pass)
                for (uint64_t word = 0; word < words; ++word)
                    checksum = checksum_step(checksum, payload[word], pass, word);
        }
        store_relaxed(&lane->mixed_elapsed_ns.value, now_ns() - mixed_begin_ns);
        store_relaxed(&lane->mixed_checksum.value, checksum);
        store_release(&lane->mixed_done.value, phase);
    }

    uint64_t accepted = 0;
    uint64_t retries = 0;
    uint64_t snapshot_errors = 0;
    store_release(&lane->seqlock_ready.value, 1);
    if (wait_equal(&control->seqlock_start.value, 1,
                   "shared seqlock start") != 0)
        goto fail;
    while (!load_acquire(&control->seqlock_stop.value)) {
        if (snapshot_try_read(&control->snapshot, &retries, &snapshot_errors))
            ++accepted;
    }
    while (!snapshot_try_read(&control->snapshot, &retries, &snapshot_errors))
        cpu_relax((unsigned long)retries);
    ++accepted;

    errors += snapshot_errors;
    store_relaxed(&lane->seqlock_accepted.value, accepted);
    store_relaxed(&lane->seqlock_retries.value, retries);
    store_relaxed(&lane->seqlock_errors.value, snapshot_errors);
    lane->packet.errors = errors;
    store_release(&lane->seqlock_done.value, 1);
    printf("CLIENT_DONE role=client%u pid=%ld errors=%" PRIu64 "\n",
           client, (long)getpid(), errors);
    cxl_arena_detach(&arena);
    return errors ? 1 : 0;

fail:
    if (lane)
        lane->packet.errors = errors + 1;
    cxl_arena_detach(&arena);
    return 1;
}

static void usage(const char *program)
{
    fprintf(stderr,
            "usage:\n"
            "  %s server ARENA NUMA_NODE ITERATIONS PAYLOAD_MIB HOLD_MS SEQCLOCK_UPDATES\n"
            "  %s client ARENA CLIENT_ID\n",
            program, program);
}

int main(int argc, char **argv)
{
    setvbuf(stdout, NULL, _IOLBF, 0);
    setvbuf(stderr, NULL, _IOLBF, 0);

    if (argc >= 2 && strcmp(argv[1], "server") == 0) {
        uint64_t node, iterations, payload_mib, hold_ms, seqlock_updates;
        if (argc != 8 ||
            parse_u64(argv[3], 0, 1023, "NUMA_NODE", &node) != 0 ||
            parse_u64(argv[4], 1, UINT64_C(1000000000), "ITERATIONS", &iterations) != 0 ||
            parse_u64(argv[5], 1, 1024, "PAYLOAD_MIB", &payload_mib) != 0 ||
            parse_u64(argv[6], 0, 60000, "HOLD_MS", &hold_ms) != 0 ||
            parse_u64(argv[7], 1, UINT64_C(1000000000),
                      "SEQCLOCK_UPDATES", &seqlock_updates) != 0) {
            usage(argv[0]);
            return 2;
        }
        return run_server(argv[2], (int)node, iterations, seqlock_updates,
                          (size_t)payload_mib << 20, (unsigned)hold_ms);
    }
    if (argc >= 2 && strcmp(argv[1], "client") == 0) {
        uint64_t client;
        if (argc != 4 || parse_u64(argv[3], 0, 1, "CLIENT_ID", &client) != 0) {
            usage(argv[0]);
            return 2;
        }
        return run_client(argv[2], (unsigned)client);
    }

    usage(argv[0]);
    return 2;
}
