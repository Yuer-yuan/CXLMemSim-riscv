#define _GNU_SOURCE

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sched.h>
#include <sys/mman.h>
#include <sys/mount.h>
#include <sys/reboot.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <time.h>
#include <unistd.h>

#define CACHE_LINE_BYTES 64U
#define STREAM_OFFSET (2U * 1024U * 1024U)
#define MAX_STREAM_BYTES (4U * 1024U * 1024U)
#define MAX_ITERATIONS 16384U
#define DEFAULT_ITERATIONS 256U
#define DEFAULT_STREAM_BYTES (1024U * 1024U)
#define DEFAULT_TIMEOUT_MS 120000U
#define BOOT_MAGIC UINT64_C(0x43584c4249415050)

#define TAG_BOOT UINT64_C(0x10)
#define TAG_READER_READY UINT64_C(0x20)
#define TAG_WRITER_READY UINT64_C(0x30)
#define TAG_LITMUS_GO UINT64_C(0x40)
#define TAG_LITMUS_GEN UINT64_C(0x50)
#define TAG_PING_REQUEST UINT64_C(0x60)
#define TAG_PING_ACK UINT64_C(0x70)
#define TAG_STREAM_GENERATION UINT64_C(0x80)
#define TAG_STREAM_DONE UINT64_C(0x90)
#define TAG_FINAL UINT64_C(0xa0)

enum role {
	ROLE_READER,
	ROLE_WRITER,
};

struct cache_line {
	volatile uint64_t word[8];
} __attribute__((aligned(CACHE_LINE_BYTES)));

struct shared_area {
	struct cache_line bootstrap;
	struct cache_line reader_ready;
	struct cache_line writer_ready;
	struct cache_line litmus_go;
	struct cache_line litmus_generation;
	struct cache_line payload;
	struct cache_line ping_request;
	struct cache_line ping_ack;
	struct cache_line stream_generation;
	struct cache_line stream_done;
	struct cache_line final_signal;
} __attribute__((aligned(CACHE_LINE_BYTES)));

_Static_assert(sizeof(struct cache_line) == CACHE_LINE_BYTES,
	       "cache line layout must remain 64 bytes");
_Static_assert(offsetof(struct shared_area, reader_ready) % CACHE_LINE_BYTES == 0,
	       "reader-ready line must be isolated");
_Static_assert(offsetof(struct shared_area, litmus_generation) % CACHE_LINE_BYTES == 0,
	       "litmus generation line must be isolated");
_Static_assert(offsetof(struct shared_area, payload) % CACHE_LINE_BYTES == 0,
	       "payload line must be isolated");
_Static_assert(offsetof(struct shared_area, ping_request) % CACHE_LINE_BYTES == 0,
	       "ping request line must be isolated");
_Static_assert(offsetof(struct shared_area, ping_ack) % CACHE_LINE_BYTES == 0,
	       "ping ACK line must be isolated");
_Static_assert(sizeof(((struct shared_area *)0)->payload) == CACHE_LINE_BYTES,
	       "litmus payload must occupy exactly one line");
_Static_assert(sizeof(struct shared_area) < STREAM_OFFSET,
	       "control area must not overlap stream payload");

struct options {
	enum role role;
	uint64_t iterations;
	uint64_t stream_bytes;
	uint64_t timeout_ms;
};

struct dax_device {
	char name[64];
	char path[96];
	uint64_t size;
	uint64_t alignment;
	unsigned int major;
	unsigned int minor;
};

static const char *active_role = "unknown";

static inline void full_fence(void)
{
	__asm__ __volatile__("fence rw,rw" ::: "memory");
}

static uint64_t monotonic_ns(void)
{
	struct timespec now;

	if (clock_gettime(CLOCK_MONOTONIC, &now) != 0)
		return 0;
	return (uint64_t)now.tv_sec * UINT64_C(1000000000) +
	       (uint64_t)now.tv_nsec;
}

static void pause_poll(void)
{
	__asm__ __volatile__("nop" ::: "memory");
}

static void power_off(int status)
{
	fflush(NULL);
	reboot(RB_POWER_OFF);
	_exit(status);
}

static void fatal(const char *phase, int error_number)
{
	printf("CXLBI_JSON {\"schema_version\":1,\"event\":\"fatal\","
	       "\"role\":\"%s\",\"phase\":\"%s\",\"errno\":%d}\n",
	       active_role, phase, error_number);
	power_off(1);
}

static void make_directory(const char *path)
{
	if (mkdir(path, 0755) != 0 && errno != EEXIST)
		fatal("mkdir", errno);
}

static void mount_one(const char *source, const char *target,
		      const char *filesystem, unsigned long flags)
{
	if (mount(source, target, filesystem, flags, NULL) != 0 && errno != EBUSY)
		fatal("mount", errno);
}

static ssize_t read_text_file(const char *path, char *buffer, size_t capacity)
{
	int descriptor;
	ssize_t length;

	if (capacity < 2)
		return -1;
	descriptor = open(path, O_RDONLY | O_CLOEXEC);
	if (descriptor < 0)
		return -1;
	length = read(descriptor, buffer, capacity - 1);
	close(descriptor);
	if (length < 0)
		return -1;
	buffer[length] = '\0';
	return length;
}

static bool option_value(const char *cmdline, const char *key,
			 char *value, size_t capacity)
{
	size_t key_length = strlen(key);
	const char *cursor = cmdline;

	while (*cursor) {
		const char *end;
		size_t length;

		while (*cursor == ' ' || *cursor == '\t' || *cursor == '\n')
			cursor++;
		if (!*cursor)
			break;
		end = cursor;
		while (*end && *end != ' ' && *end != '\t' && *end != '\n')
			end++;
		if ((size_t)(end - cursor) > key_length &&
		    memcmp(cursor, key, key_length) == 0) {
			length = (size_t)(end - cursor) - key_length;
			if (length >= capacity)
				fatal("cmdline-value-too-long", E2BIG);
			memcpy(value, cursor + key_length, length);
			value[length] = '\0';
			return true;
		}
		cursor = end;
	}
	return false;
}

static uint64_t parse_u64(const char *text, const char *phase)
{
	char *end = NULL;
	unsigned long long value;

	errno = 0;
	value = strtoull(text, &end, 0);
	if (errno != 0 || end == text)
		fatal(phase, errno ? errno : EINVAL);
	while (*end == '\n' || *end == '\r' || *end == ' ' || *end == '\t')
		end++;
	if (*end != '\0')
		fatal(phase, EINVAL);
	return (uint64_t)value;
}

static struct options parse_options(void)
{
	char cmdline[2048];
	char value[96];
	struct options options = {
		.role = ROLE_READER,
		.iterations = DEFAULT_ITERATIONS,
		.stream_bytes = DEFAULT_STREAM_BYTES,
		.timeout_ms = DEFAULT_TIMEOUT_MS,
	};

	if (read_text_file("/proc/cmdline", cmdline, sizeof(cmdline)) <= 0)
		fatal("read-cmdline", errno ? errno : EIO);
	if (!option_value(cmdline, "cxlbi.role=", value, sizeof(value)))
		fatal("missing-role", EINVAL);
	if (strcmp(value, "reader") == 0) {
		options.role = ROLE_READER;
		active_role = "reader";
	} else if (strcmp(value, "writer") == 0) {
		options.role = ROLE_WRITER;
		active_role = "writer";
	} else {
		fatal("invalid-role", EINVAL);
	}
	if (option_value(cmdline, "cxlbi.iterations=", value, sizeof(value)))
		options.iterations = parse_u64(value, "invalid-iterations");
	if (option_value(cmdline, "cxlbi.stream_bytes=", value, sizeof(value)))
		options.stream_bytes = parse_u64(value, "invalid-stream-bytes");
	if (option_value(cmdline, "cxlbi.timeout_ms=", value, sizeof(value)))
		options.timeout_ms = parse_u64(value, "invalid-timeout");
	if (options.iterations == 0 || options.iterations > MAX_ITERATIONS)
		fatal("invalid-iterations", EINVAL);
	if (options.stream_bytes == 0 ||
	    options.stream_bytes > MAX_STREAM_BYTES ||
	    options.stream_bytes % CACHE_LINE_BYTES != 0)
		fatal("invalid-stream-bytes", EINVAL);
	if (options.timeout_ms == 0 || options.timeout_ms > UINT64_C(600000))
		fatal("invalid-timeout", EINVAL);
	return options;
}

static bool find_dax_name(char *name, size_t capacity)
{
	DIR *directory = opendir("/sys/bus/dax/devices");
	struct dirent *entry;
	bool found = false;

	if (!directory)
		return false;
	while ((entry = readdir(directory)) != NULL) {
		if (strncmp(entry->d_name, "dax", 3) != 0 ||
		    strchr(entry->d_name, '.') == NULL)
			continue;
		if (!found || strcmp(entry->d_name, name) < 0) {
			if (strlen(entry->d_name) >= capacity) {
				closedir(directory);
				fatal("dax-name-too-long", ENAMETOOLONG);
			}
			strcpy(name, entry->d_name);
			found = true;
		}
	}
	closedir(directory);
	return found;
}

static uint64_t read_u64_sysfs(const char *path, const char *phase)
{
	char contents[96];

	if (read_text_file(path, contents, sizeof(contents)) <= 0)
		fatal(phase, errno ? errno : EIO);
	return parse_u64(contents, phase);
}

static struct dax_device discover_dax(uint64_t timeout_ms)
{
	struct dax_device device;
	uint64_t deadline = monotonic_ns() + timeout_ms * UINT64_C(1000000);
	char sysfs[192];
	char dev_number[96];
	char *separator;
	struct timespec delay = {.tv_sec = 0, .tv_nsec = 100000000};

	memset(&device, 0, sizeof(device));
	while (!find_dax_name(device.name, sizeof(device.name))) {
		if (monotonic_ns() >= deadline)
			fatal("dax-discovery-timeout", ETIMEDOUT);
		nanosleep(&delay, NULL);
	}

	snprintf(device.path, sizeof(device.path), "/dev/%s", device.name);
	snprintf(sysfs, sizeof(sysfs), "/sys/bus/dax/devices/%s/dev", device.name);
	if (read_text_file(sysfs, dev_number, sizeof(dev_number)) <= 0)
		fatal("read-dax-dev", errno ? errno : EIO);
	separator = strchr(dev_number, ':');
	if (!separator)
		fatal("parse-dax-dev", EINVAL);
	*separator = '\0';
	device.major = (unsigned int)parse_u64(dev_number, "parse-dax-major");
	device.minor = (unsigned int)parse_u64(separator + 1, "parse-dax-minor");

	snprintf(sysfs, sizeof(sysfs), "/sys/bus/dax/devices/%s/size", device.name);
	device.size = read_u64_sysfs(sysfs, "read-dax-size");
	snprintf(sysfs, sizeof(sysfs), "/sys/bus/dax/devices/%s/align", device.name);
	device.alignment = read_u64_sysfs(sysfs, "read-dax-align");
	if (device.size == 0 || device.alignment < 4096 ||
	    (device.alignment & (device.alignment - 1)) != 0)
		fatal("invalid-dax-geometry", EINVAL);

	if (mknod(device.path, S_IFCHR | 0600,
		  makedev(device.major, device.minor)) != 0 && errno != EEXIST)
		fatal("mknod-dax", errno);
	return device;
}

static uint64_t round_up(uint64_t value, uint64_t alignment)
{
	if (value > UINT64_MAX - alignment + 1)
		fatal("mapping-size-overflow", EOVERFLOW);
	return (value + alignment - 1) & ~(alignment - 1);
}

static uint64_t mix64(uint64_t value)
{
	value ^= value >> 30;
	value *= UINT64_C(0xbf58476d1ce4e5b9);
	value ^= value >> 27;
	value *= UINT64_C(0x94d049bb133111eb);
	return value ^ (value >> 31);
}

static uint64_t signal_checksum(uint64_t value, uint64_t tag)
{
	return mix64(value ^ tag ^ UINT64_C(0x9e3779b97f4a7c15));
}

static void publish_signal(struct cache_line *line, uint64_t value,
			   uint64_t tag)
{
	line->word[1] = signal_checksum(value, tag);
	full_fence();
	line->word[0] = value;
	full_fence();
}

static uint64_t load_signal_value(const struct cache_line *line)
{
	uint64_t value = line->word[0];

	full_fence();
	return value;
}

static uint64_t wait_signal(struct cache_line *line, uint64_t expected,
			    uint64_t tag, uint64_t timeout_ms,
			    const char *phase, uint64_t *errors)
{
	uint64_t start = monotonic_ns();
	uint64_t deadline = start + timeout_ms * UINT64_C(1000000);
	uint64_t polls = 0;

	for (;;) {
		uint64_t value = load_signal_value(line);

		if (value == expected) {
			uint64_t checksum = line->word[1];

			full_fence();
			if (checksum != signal_checksum(expected, tag))
				(*errors)++;
			return monotonic_ns() - start;
		}
		if (value > expected && expected < BOOT_MAGIC)
			fatal(phase, EPROTO);
		if (monotonic_ns() >= deadline)
			fatal(phase, ETIMEDOUT);
		polls++;
		if ((polls & UINT64_C(0xfff)) == 0)
			sched_yield();
		else
			pause_poll();
	}
}

static uint64_t payload_value(uint64_t generation, unsigned int index)
{
	return mix64(UINT64_C(0x5041594c4f414400) ^
		     generation ^ ((uint64_t)index * UINT64_C(0x9e3779b97f4a7c15)));
}

static uint64_t write_payload(struct cache_line *payload, uint64_t generation)
{
	uint64_t checksum = 0;
	unsigned int index;

	for (index = 0; index < 8; index++) {
		uint64_t value = payload_value(generation, index);

		payload->word[index] = value;
		checksum ^= mix64(value + index);
	}
	full_fence();
	return checksum;
}

static uint64_t verify_payload(const struct cache_line *payload,
			       uint64_t generation, uint64_t *errors)
{
	uint64_t checksum = 0;
	unsigned int index;

	full_fence();
	for (index = 0; index < 8; index++) {
		uint64_t observed = payload->word[index];
		uint64_t expected = payload_value(generation, index);

		if (observed != expected)
			(*errors)++;
		checksum ^= mix64(observed + index);
	}
	full_fence();
	return checksum;
}

static uint64_t stream_value(uint64_t generation, uint64_t word_index)
{
	return mix64(UINT64_C(0x53545245414d0000) ^ generation ^
		     (word_index * UINT64_C(0xd6e8feb86659fd93)));
}

static void write_stream(volatile uint64_t *stream, uint64_t bytes,
			 uint64_t generation)
{
	uint64_t words = bytes / sizeof(uint64_t);
	uint64_t index;

	for (index = 0; index < words; index++)
		stream[index] = stream_value(generation, index);
	full_fence();
}

static uint64_t verify_stream(const volatile uint64_t *stream, uint64_t bytes,
			      uint64_t generation)
{
	uint64_t words = bytes / sizeof(uint64_t);
	uint64_t errors = 0;
	uint64_t index;

	full_fence();
	for (index = 0; index < words; index++) {
		if (stream[index] != stream_value(generation, index))
			errors++;
	}
	full_fence();
	return errors;
}

static int compare_u64(const void *left, const void *right)
{
	uint64_t first = *(const uint64_t *)left;
	uint64_t second = *(const uint64_t *)right;

	return (first > second) - (first < second);
}

static uint64_t nearest_rank(const uint64_t *sorted, uint64_t count,
			     uint64_t numerator, uint64_t denominator)
{
	uint64_t rank = (count * numerator + denominator - 1) / denominator;

	if (rank == 0)
		rank = 1;
	return sorted[rank - 1];
}

static void emit_stream(const char *operation, unsigned int direction,
			uint64_t bytes, uint64_t elapsed_ns,
			uint64_t errors)
{
	printf("CXLBI_JSON {\"schema_version\":1,\"event\":\"stream\","
	       "\"role\":\"%s\",\"operation\":\"%s\",\"direction\":%u,"
	       "\"bytes\":%" PRIu64 ",\"elapsed_ns\":%" PRIu64 ","
	       "\"errors\":%" PRIu64 "}\n",
	       active_role, operation, direction, bytes, elapsed_ns, errors);
}

static uint64_t run_reader(struct shared_area *area,
			   volatile uint64_t *stream,
			   const struct options *options)
{
	uint64_t errors = 0;
	uint64_t litmus_start;
	uint64_t litmus_elapsed;
	uint64_t payload_checksum;
	uint64_t *samples;
	uint64_t sum = 0;
	uint64_t ping_start;
	uint64_t ping_elapsed;
	uint64_t index;
	uint64_t elapsed;

	for (index = 0; index < sizeof(*area) / sizeof(uint64_t); index++)
		((volatile uint64_t *)area)[index] = 0;
	full_fence();
	write_payload(&area->payload, 0);
	publish_signal(&area->bootstrap, BOOT_MAGIC, TAG_BOOT);
	publish_signal(&area->reader_ready, 1, TAG_READER_READY);
	printf("CXLBI_JSON {\"schema_version\":1,\"event\":\"ready\","
	       "\"role\":\"reader\",\"generation\":1}\n");

	wait_signal(&area->writer_ready, 1, TAG_WRITER_READY,
		    options->timeout_ms, "wait-writer-ready", &errors);
	payload_checksum = verify_payload(&area->payload, 0, &errors);
	litmus_start = monotonic_ns();
	publish_signal(&area->litmus_go, 1, TAG_LITMUS_GO);
	wait_signal(&area->litmus_generation, 1, TAG_LITMUS_GEN,
		    options->timeout_ms, "wait-litmus-generation", &errors);
	payload_checksum = verify_payload(&area->payload, 1, &errors);
	litmus_elapsed = monotonic_ns() - litmus_start;
	printf("CXLBI_JSON {\"schema_version\":1,\"event\":\"litmus\","
	       "\"role\":\"reader\",\"operation\":\"observe\","
	       "\"generation\":1,\"elapsed_ns\":%" PRIu64 ","
	       "\"payload_checksum\":%" PRIu64 ",\"errors\":%" PRIu64 "}\n",
	       litmus_elapsed, payload_checksum, errors);

	samples = calloc((size_t)options->iterations, sizeof(*samples));
	if (!samples)
		fatal("allocate-ping-samples", ENOMEM);
	ping_start = monotonic_ns();
	for (index = 1; index <= options->iterations; index++) {
		uint64_t one_start = monotonic_ns();

		publish_signal(&area->ping_request, index, TAG_PING_REQUEST);
		wait_signal(&area->ping_ack, index, TAG_PING_ACK,
			    options->timeout_ms, "wait-ping-ack", &errors);
		samples[index - 1] = monotonic_ns() - one_start;
		sum += samples[index - 1];
	}
	ping_elapsed = monotonic_ns() - ping_start;
	qsort(samples, (size_t)options->iterations, sizeof(*samples), compare_u64);
	printf("CXLBI_JSON {\"schema_version\":1,\"event\":\"pingpong\","
	       "\"role\":\"reader\",\"iterations\":%" PRIu64 ","
	       "\"elapsed_ns\":%" PRIu64 ",\"min_ns\":%" PRIu64 ","
	       "\"mean_ns\":%" PRIu64 ",\"p50_ns\":%" PRIu64 ","
	       "\"p95_ns\":%" PRIu64 ",\"p99_ns\":%" PRIu64 ","
	       "\"max_ns\":%" PRIu64 ",\"errors\":%" PRIu64 "}\n",
	       options->iterations, ping_elapsed, samples[0],
	       sum / options->iterations,
	       nearest_rank(samples, options->iterations, 50, 100),
	       nearest_rank(samples, options->iterations, 95, 100),
	       nearest_rank(samples, options->iterations, 99, 100),
	       samples[options->iterations - 1], errors);
	free(samples);

	wait_signal(&area->stream_generation, 1, TAG_STREAM_GENERATION,
		    options->timeout_ms, "wait-stream-generation-1", &errors);
	elapsed = monotonic_ns();
	{
		uint64_t stream_errors = verify_stream(stream, options->stream_bytes, 1);

		elapsed = monotonic_ns() - elapsed;
		errors += stream_errors;
		emit_stream("read_verify", 0, options->stream_bytes, elapsed,
			    stream_errors);
	}
	publish_signal(&area->stream_done, 1, TAG_STREAM_DONE);

	elapsed = monotonic_ns();
	write_stream(stream, options->stream_bytes, 2);
	elapsed = monotonic_ns() - elapsed;
	emit_stream("write", 1, options->stream_bytes, elapsed, 0);
	publish_signal(&area->stream_generation, 2, TAG_STREAM_GENERATION);
	wait_signal(&area->stream_done, 2, TAG_STREAM_DONE,
		    options->timeout_ms, "wait-stream-done-2", &errors);

	publish_signal(&area->final_signal, 1, TAG_FINAL);
	wait_signal(&area->final_signal, 2, TAG_FINAL,
		    options->timeout_ms, "wait-final-writer", &errors);
	publish_signal(&area->final_signal, 3, TAG_FINAL);
	return errors;
}

static uint64_t run_writer(struct shared_area *area,
			   volatile uint64_t *stream,
			   const struct options *options)
{
	uint64_t errors = 0;
	uint64_t old_checksum;
	uint64_t new_checksum;
	uint64_t start;
	uint64_t elapsed;
	uint64_t index;

	wait_signal(&area->bootstrap, BOOT_MAGIC, TAG_BOOT,
		    options->timeout_ms, "wait-bootstrap", &errors);
	wait_signal(&area->reader_ready, 1, TAG_READER_READY,
		    options->timeout_ms, "wait-reader-ready", &errors);
	old_checksum = verify_payload(&area->payload, 0, &errors);
	publish_signal(&area->writer_ready, 1, TAG_WRITER_READY);
	wait_signal(&area->litmus_go, 1, TAG_LITMUS_GO,
		    options->timeout_ms, "wait-litmus-go", &errors);
	start = monotonic_ns();
	new_checksum = write_payload(&area->payload, 1);
	publish_signal(&area->litmus_generation, 1, TAG_LITMUS_GEN);
	elapsed = monotonic_ns() - start;
	printf("CXLBI_JSON {\"schema_version\":1,\"event\":\"litmus\","
	       "\"role\":\"writer\",\"operation\":\"publish\","
	       "\"generation\":1,\"elapsed_ns\":%" PRIu64 ","
	       "\"old_payload_checksum\":%" PRIu64 ","
	       "\"payload_checksum\":%" PRIu64 ",\"errors\":%" PRIu64 "}\n",
	       elapsed, old_checksum, new_checksum, errors);

	start = monotonic_ns();
	for (index = 1; index <= options->iterations; index++) {
		wait_signal(&area->ping_request, index, TAG_PING_REQUEST,
			    options->timeout_ms, "wait-ping-request", &errors);
		publish_signal(&area->ping_ack, index, TAG_PING_ACK);
	}
	elapsed = monotonic_ns() - start;
	printf("CXLBI_JSON {\"schema_version\":1,\"event\":\"pingpong\","
	       "\"role\":\"writer\",\"iterations\":%" PRIu64 ","
	       "\"elapsed_ns\":%" PRIu64 ",\"errors\":%" PRIu64 "}\n",
	       options->iterations, elapsed, errors);

	elapsed = monotonic_ns();
	write_stream(stream, options->stream_bytes, 1);
	elapsed = monotonic_ns() - elapsed;
	emit_stream("write", 0, options->stream_bytes, elapsed, 0);
	publish_signal(&area->stream_generation, 1, TAG_STREAM_GENERATION);
	wait_signal(&area->stream_done, 1, TAG_STREAM_DONE,
		    options->timeout_ms, "wait-stream-done-1", &errors);
	wait_signal(&area->stream_generation, 2, TAG_STREAM_GENERATION,
		    options->timeout_ms, "wait-stream-generation-2", &errors);
	elapsed = monotonic_ns();
	{
		uint64_t stream_errors = verify_stream(stream, options->stream_bytes, 2);

		elapsed = monotonic_ns() - elapsed;
		errors += stream_errors;
		emit_stream("read_verify", 1, options->stream_bytes, elapsed,
			    stream_errors);
	}
	publish_signal(&area->stream_done, 2, TAG_STREAM_DONE);

	wait_signal(&area->final_signal, 1, TAG_FINAL,
		    options->timeout_ms, "wait-final-reader", &errors);
	publish_signal(&area->final_signal, 2, TAG_FINAL);
	wait_signal(&area->final_signal, 3, TAG_FINAL,
		    options->timeout_ms, "wait-final-reader-ack", &errors);
	return errors;
}

int main(void)
{
	struct options options;
	struct dax_device dax;
	struct shared_area *area;
	volatile uint64_t *stream;
	uint64_t required_bytes;
	uint64_t mapping_bytes;
	uint64_t errors;
	int descriptor;

	setvbuf(stdout, NULL, _IONBF, 0);
	active_role = "boot";
	make_directory("/proc");
	make_directory("/sys");
	make_directory("/dev");
	make_directory("/tmp");
	mount_one("proc", "/proc", "proc", 0);
	mount_one("sysfs", "/sys", "sysfs", 0);
	mount_one("devtmpfs", "/dev", "devtmpfs", 0);
	options = parse_options();
	dax = discover_dax(options.timeout_ms);
	required_bytes = STREAM_OFFSET + options.stream_bytes;
	mapping_bytes = round_up(required_bytes, dax.alignment);
	if (mapping_bytes > dax.size)
		fatal("dax-device-too-small", ENOSPC);
	descriptor = open(dax.path, O_RDWR | O_CLOEXEC);
	if (descriptor < 0)
		fatal("open-dax", errno);
	area = mmap(NULL, (size_t)mapping_bytes, PROT_READ | PROT_WRITE,
		    MAP_SHARED, descriptor, 0);
	if (area == MAP_FAILED)
		fatal("mmap-dax", errno);
	stream = (volatile uint64_t *)((volatile unsigned char *)area +
					STREAM_OFFSET);
	printf("CXLBI_JSON {\"schema_version\":1,\"event\":\"mapped\","
	       "\"role\":\"%s\",\"endpoint_id\":%u,\"dax\":\"%s\","
	       "\"dax_size\":%" PRIu64 ",\"dax_align\":%" PRIu64 ","
	       "\"mapping_offset\":0,\"mapping_bytes\":%" PRIu64 ","
	       "\"litmus_generation_offset\":%zu,\"payload_offset\":%zu,"
	       "\"ping_request_offset\":%zu,\"ping_ack_offset\":%zu,"
	       "\"stream_offset\":%u}\n",
	       active_role, options.role == ROLE_READER ? 0U : 1U, dax.name,
	       dax.size, dax.alignment, mapping_bytes,
	       offsetof(struct shared_area, litmus_generation),
	       offsetof(struct shared_area, payload),
	       offsetof(struct shared_area, ping_request),
	       offsetof(struct shared_area, ping_ack), STREAM_OFFSET);

	if (options.role == ROLE_READER)
		errors = run_reader(area, stream, &options);
	else
		errors = run_writer(area, stream, &options);

	printf("CXLBI_JSON {\"schema_version\":1,\"event\":\"summary\","
	       "\"role\":\"%s\",\"status\":\"%s\","
	       "\"iterations\":%" PRIu64 ",\"stream_bytes\":%" PRIu64 ","
	       "\"errors\":%" PRIu64 ",\"explicit_flush_calls\":0}\n",
	       active_role, errors == 0 ? "pass" : "fail", options.iterations,
	       options.stream_bytes, errors);
	/* The host runner terminates both QEMU processes after seeing both summaries. */
	for (;;)
		pause();
}
