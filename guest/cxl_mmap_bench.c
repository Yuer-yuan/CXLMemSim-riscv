#ifdef CXL_BENCH_FREESTANDING

#include <stddef.h>
#include <stdint.h>

#define AT_FDCWD (-100)
#define CLOCK_MONOTONIC_RAW 4
#define O_RDWR 2
#define O_SYNC 04010000
#define PROT_READ 1
#define PROT_WRITE 2
#define MAP_SHARED 1
#define SYS_OPENAT 56
#define SYS_CLOSE 57
#define SYS_WRITE 64
#define SYS_EXIT 93
#define SYS_CLOCK_GETTIME 113
#define SYS_MUNMAP 215
#define SYS_MMAP 222
#define BENCH_ITERATIONS 3
#define PAGE_SIZE 4096

struct kernel_timespec {
	long tv_sec;
	long tv_nsec;
};

struct bench_result {
	uint64_t write_ns[BENCH_ITERATIONS];
	uint64_t read_ns[BENCH_ITERATIONS];
	uint64_t random_ns_x1000;
	uint64_t checksum;
	int verified;
};

struct output {
	char data[1024];
	size_t length;
};

static long syscall6(long number, long arg0, long arg1, long arg2,
		     long arg3, long arg4, long arg5)
{
	register long a0 __asm__("a0") = arg0;
	register long a1 __asm__("a1") = arg1;
	register long a2 __asm__("a2") = arg2;
	register long a3 __asm__("a3") = arg3;
	register long a4 __asm__("a4") = arg4;
	register long a5 __asm__("a5") = arg5;
	register long a7 __asm__("a7") = number;

	__asm__ volatile("ecall"
			 : "+r"(a0)
			 : "r"(a1), "r"(a2), "r"(a3), "r"(a4), "r"(a5),
			   "r"(a7)
			 : "memory");
	return a0;
}

static long syscall3(long number, long arg0, long arg1, long arg2)
{
	return syscall6(number, arg0, arg1, arg2, 0, 0, 0);
}

static long syscall1(long number, long arg0)
{
	return syscall6(number, arg0, 0, 0, 0, 0, 0);
}

static size_t string_length(const char *text)
{
	size_t length = 0;

	while (text[length])
		length++;
	return length;
}

static void write_text(int fd, const char *text)
{
	size_t remaining = string_length(text);

	while (remaining) {
		long written = syscall3(SYS_WRITE, fd, (long)text, remaining);

		if (written <= 0)
			return;
		text += written;
		remaining -= (size_t)written;
	}
}

static void append_char(struct output *output, char value)
{
	if (output->length < sizeof(output->data))
		output->data[output->length++] = value;
}

static void append_text(struct output *output, const char *text)
{
	while (*text)
		append_char(output, *text++);
}

static void append_u64(struct output *output, uint64_t value)
{
	char reversed[24];
	size_t count = 0;

	do {
		reversed[count++] = '0' + value % 10;
		value /= 10;
	} while (value);
	while (count)
		append_char(output, reversed[--count]);
}

static void append_hex64(struct output *output, uint64_t value)
{
	static const char digits[] = "0123456789abcdef";
	int shift;

	append_text(output, "0x");
	for (shift = 60; shift >= 0; shift -= 4)
		append_char(output, digits[(value >> shift) & 0xf]);
}

static void append_seconds(struct output *output, uint64_t nanoseconds)
{
	uint64_t fraction = nanoseconds % UINT64_C(1000000000);
	uint64_t divisor;

	append_u64(output, nanoseconds / UINT64_C(1000000000));
	append_char(output, '.');
	for (divisor = UINT64_C(100000000); divisor; divisor /= 10)
		append_char(output, '0' + (fraction / divisor) % 10);
}

static void append_milli(struct output *output, uint64_t value_x1000)
{
	uint64_t fraction = value_x1000 % 1000;

	append_u64(output, value_x1000 / 1000);
	append_char(output, '.');
	append_char(output, '0' + fraction / 100);
	append_char(output, '0' + (fraction / 10) % 10);
	append_char(output, '0' + fraction % 10);
}

static uint64_t monotonic_nanoseconds(void)
{
	struct kernel_timespec time;

	if (syscall3(SYS_CLOCK_GETTIME, CLOCK_MONOTONIC_RAW,
		     (long)&time, 0) < 0)
		return 0;
	return (uint64_t)time.tv_sec * UINT64_C(1000000000) +
	       (uint64_t)time.tv_nsec;
}

static uint64_t pattern(size_t index)
{
	return UINT64_C(0x9e3779b97f4a7c15) ^
	       index * UINT64_C(0xbf58476d1ce4e5b9);
}

static uint64_t xorshift64(uint64_t *state)
{
	uint64_t value = *state;

	value ^= value << 13;
	value ^= value >> 7;
	value ^= value << 17;
	*state = value;
	return value;
}

static uint64_t median3(const uint64_t samples[BENCH_ITERATIONS])
{
	uint64_t first = samples[0];
	uint64_t second = samples[1];
	uint64_t third = samples[2];
	uint64_t temporary;

	if (first > second) {
		temporary = first;
		first = second;
		second = temporary;
	}
	if (second > third) {
		temporary = second;
		second = third;
		third = temporary;
	}
	if (first > second)
		second = first;
	return second;
}

static int parse_u64(const char *text, uint64_t *value)
{
	uint64_t parsed = 0;
	unsigned int base = 10;
	unsigned int digit;

	if (!text || !*text)
		return -1;
	if (text[0] == '0' && (text[1] == 'x' || text[1] == 'X')) {
		base = 16;
		text += 2;
		if (!*text)
			return -1;
	}
	while (*text) {
		if (*text >= '0' && *text <= '9')
			digit = *text - '0';
		else if (*text >= 'a' && *text <= 'f')
			digit = *text - 'a' + 10;
		else if (*text >= 'A' && *text <= 'F')
			digit = *text - 'A' + 10;
		else
			return -1;
		if (digit >= base ||
		    parsed > (UINT64_MAX - digit) / base)
			return -1;
		parsed = parsed * base + digit;
		text++;
	}
	*value = parsed;
	return 0;
}

static int run_benchmark(volatile uint64_t *words, size_t bytes,
			 uint64_t random_ops, struct bench_result *result)
{
	const size_t word_count = bytes / sizeof(*words);
	uint64_t random_state = UINT64_C(0x2545f4914f6cdd1d);
	uint64_t checksum = 0;
	uint64_t start;
	uint64_t end;
	size_t index;
	int iteration;

	result->verified = 0;
	for (iteration = 0; iteration < BENCH_ITERATIONS; iteration++) {
		start = monotonic_nanoseconds();
		for (index = 0; index < word_count; index++)
			words[index] = pattern(
				index + (size_t)iteration * word_count);
		end = monotonic_nanoseconds();
		if (!start || end <= start)
			return -1;
		result->write_ns[iteration] = end - start;
	}
	for (iteration = 0; iteration < BENCH_ITERATIONS; iteration++) {
		uint64_t pass_checksum = 0;

		start = monotonic_nanoseconds();
		for (index = 0; index < word_count; index++)
			pass_checksum ^= words[index] +
					 UINT64_C(0x9e3779b97f4a7c15);
		end = monotonic_nanoseconds();
		if (!start || end <= start)
			return -1;
		result->read_ns[iteration] = end - start;
		checksum ^= pass_checksum;
	}
	start = monotonic_nanoseconds();
	for (uint64_t operation = 0; operation < random_ops; operation++) {
		index = xorshift64(&random_state) % word_count;
		checksum ^= words[index];
	}
	end = monotonic_nanoseconds();
	if (!start || end <= start)
		return -1;
	result->random_ns_x1000 = (end - start) * 1000 / random_ops;

	for (index = 0; index < word_count; index++)
		words[index] = pattern(index);
	for (index = 0; index < word_count; index++) {
		if (words[index] != pattern(index))
			return -1;
	}
	result->checksum = checksum;
	result->verified = 1;
	return 0;
}

static void print_result(const struct bench_result *result, size_t bytes,
			 uint64_t random_ops)
{
	struct output output;
	uint64_t write_median = median3(result->write_ns);
	uint64_t read_median = median3(result->read_ns);
	uint64_t write_mib_s_x1000 =
		((bytes / 1024) * UINT64_C(976562500)) / write_median;
	uint64_t read_mib_s_x1000 =
		((bytes / 1024) * UINT64_C(976562500)) / read_median;
	int index;

	output.length = 0;
	append_text(&output, "CXL_BENCH_JSON {\"status\":\"pass\",\"bytes\":");
	append_u64(&output, bytes);
	append_text(&output, ",\"iterations\":3,\"write_seconds\":[");
	for (index = 0; index < BENCH_ITERATIONS; index++) {
		if (index)
			append_char(&output, ',');
		append_seconds(&output, result->write_ns[index]);
	}
	append_text(&output, "],\"read_seconds\":[");
	for (index = 0; index < BENCH_ITERATIONS; index++) {
		if (index)
			append_char(&output, ',');
		append_seconds(&output, result->read_ns[index]);
	}
	append_text(&output, "],\"write_mib_s_median\":");
	append_milli(&output, write_mib_s_x1000);
	append_text(&output, ",\"read_mib_s_median\":");
	append_milli(&output, read_mib_s_x1000);
	append_text(&output, ",\"random_ops\":");
	append_u64(&output, random_ops);
	append_text(&output, ",\"random_ns_per_load\":");
	append_milli(&output, result->random_ns_x1000);
	append_text(&output, ",\"checksum\":\"");
	append_hex64(&output, result->checksum);
	append_text(&output, "\",\"verified\":true}\n");
	syscall3(SYS_WRITE, 1, (long)output.data, output.length);
}

static __attribute__((used)) int cxl_bench_main(int argc, char **argv)
{
	struct bench_result result;
	volatile uint64_t *words;
	uint64_t physical_base;
	uint64_t bytes_u64;
	uint64_t iterations;
	uint64_t random_ops;
	long fd;
	long mapping;
	int ret = 1;

	if (argc != 5 ||
	    parse_u64(argv[1], &physical_base) ||
	    parse_u64(argv[2], &bytes_u64) ||
	    parse_u64(argv[3], &iterations) ||
	    parse_u64(argv[4], &random_ops) ||
	    !bytes_u64 || bytes_u64 > (uint64_t)SIZE_MAX ||
	    bytes_u64 % PAGE_SIZE || physical_base % PAGE_SIZE ||
	    bytes_u64 % sizeof(uint64_t) ||
	    iterations != BENCH_ITERATIONS || !random_ops) {
		write_text(2, "invalid benchmark arguments\n");
		return 1;
	}

	fd = syscall6(SYS_OPENAT, AT_FDCWD, (long)"/dev/mem",
		      O_RDWR | O_SYNC, 0, 0, 0);
	if (fd < 0) {
		write_text(2, "open /dev/mem failed\n");
		return 1;
	}
	mapping = syscall6(SYS_MMAP, 0, (long)bytes_u64,
			   PROT_READ | PROT_WRITE, MAP_SHARED, fd,
			   (long)physical_base);
	if ((unsigned long)mapping >= (unsigned long)-4095) {
		write_text(2, "mmap /dev/mem failed\n");
		goto out_close;
	}

	words = (volatile uint64_t *)mapping;
	if (run_benchmark(words, (size_t)bytes_u64, random_ops, &result)) {
		write_text(2, "benchmark or verification failed\n");
		goto out_unmap;
	}
	print_result(&result, (size_t)bytes_u64, random_ops);
	ret = 0;

out_unmap:
	syscall3(SYS_MUNMAP, mapping, (long)bytes_u64, 0);
out_close:
	syscall1(SYS_CLOSE, fd);
	return ret;
}

__asm__(
".global _start\n"
".type _start,@function\n"
"_start:\n"
"  ld a0, 0(sp)\n"
"  addi a1, sp, 8\n"
"  call cxl_bench_main\n"
"  li a7, 93\n"
"  ecall\n"
".size _start, .-_start\n");

#else

#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

#define BENCH_ITERATIONS 3
#define SELF_TEST_BYTES (8U * 1024U * 1024U)
#define SELF_TEST_RANDOM_OPS 100000U

struct bench_result {
	double write_seconds[BENCH_ITERATIONS];
	double read_seconds[BENCH_ITERATIONS];
	double random_ns_per_load;
	uint64_t checksum;
	bool verified;
};

static double monotonic_seconds(void)
{
	struct timespec ts;

	if (clock_gettime(CLOCK_MONOTONIC_RAW, &ts))
		return -1;
	return ts.tv_sec + ts.tv_nsec / 1000000000.0;
}

static uint64_t pattern(size_t index)
{
	return UINT64_C(0x9e3779b97f4a7c15) ^
	       index * UINT64_C(0xbf58476d1ce4e5b9);
}

static uint64_t xorshift64(uint64_t *state)
{
	uint64_t value = *state;

	value ^= value << 13;
	value ^= value >> 7;
	value ^= value << 17;
	*state = value;
	return value;
}

static double median3(const double samples[BENCH_ITERATIONS])
{
	double sorted[BENCH_ITERATIONS];
	double temporary;

	memcpy(sorted, samples, sizeof(sorted));
	if (sorted[0] > sorted[1]) {
		temporary = sorted[0];
		sorted[0] = sorted[1];
		sorted[1] = temporary;
	}
	if (sorted[1] > sorted[2]) {
		temporary = sorted[1];
		sorted[1] = sorted[2];
		sorted[2] = temporary;
	}
	if (sorted[0] > sorted[1]) {
		temporary = sorted[0];
		sorted[0] = sorted[1];
		sorted[1] = temporary;
	}
	return sorted[1];
}

static int run_benchmark(volatile uint64_t *words, size_t bytes,
			 uint64_t random_ops, struct bench_result *result)
{
	const size_t word_count = bytes / sizeof(*words);
	uint64_t random_state = UINT64_C(0x2545f4914f6cdd1d);
	uint64_t checksum = 0;
	double start;
	double end;
	size_t index;
	int iteration;

	memset(result, 0, sizeof(*result));
	for (iteration = 0; iteration < BENCH_ITERATIONS; iteration++) {
		start = monotonic_seconds();
		if (start < 0)
			return -1;
		for (index = 0; index < word_count; index++)
			words[index] = pattern(index +
					       (size_t)iteration * word_count);
		end = monotonic_seconds();
		if (end <= start)
			return -1;
		result->write_seconds[iteration] = end - start;
	}

	for (iteration = 0; iteration < BENCH_ITERATIONS; iteration++) {
		uint64_t pass_checksum = 0;

		start = monotonic_seconds();
		if (start < 0)
			return -1;
		for (index = 0; index < word_count; index++)
			pass_checksum ^= words[index] +
					 UINT64_C(0x9e3779b97f4a7c15);
		end = monotonic_seconds();
		if (end <= start)
			return -1;
		result->read_seconds[iteration] = end - start;
		checksum ^= pass_checksum;
	}

	start = monotonic_seconds();
	if (start < 0)
		return -1;
	for (uint64_t operation = 0; operation < random_ops; operation++) {
		index = xorshift64(&random_state) % word_count;
		checksum ^= words[index];
	}
	end = monotonic_seconds();
	if (end <= start)
		return -1;
	result->random_ns_per_load =
		(end - start) * 1000000000.0 / random_ops;

	for (index = 0; index < word_count; index++)
		words[index] = pattern(index);
	for (index = 0; index < word_count; index++) {
		if (words[index] != pattern(index)) {
			fprintf(stderr,
				"verification mismatch at word %zu: "
				"expected=%016" PRIx64 " actual=%016" PRIx64 "\n",
				index, pattern(index), words[index]);
			return -1;
		}
	}

	result->checksum = checksum;
	result->verified = true;
	return 0;
}

static int parse_u64(const char *text, uint64_t *value)
{
	char *end;
	unsigned long long parsed;

	errno = 0;
	parsed = strtoull(text, &end, 0);
	if (errno || !text[0] || *end)
		return -1;
	*value = parsed;
	return 0;
}

static void print_result(const struct bench_result *result, size_t bytes,
			 uint64_t random_ops)
{
	const double mib = bytes / (1024.0 * 1024.0);
	const double write_median = median3(result->write_seconds);
	const double read_median = median3(result->read_seconds);

	printf("{\"status\":\"pass\",\"bytes\":%zu,\"iterations\":3,"
	       "\"write_seconds\":[%.9f,%.9f,%.9f],"
	       "\"read_seconds\":[%.9f,%.9f,%.9f],"
	       "\"write_mib_s_median\":%.6f,"
	       "\"read_mib_s_median\":%.6f,"
	       "\"random_ops\":%" PRIu64 ","
	       "\"random_ns_per_load\":%.6f,"
	       "\"checksum\":\"0x%016" PRIx64 "\","
	       "\"verified\":%s}\n",
	       bytes,
	       result->write_seconds[0], result->write_seconds[1],
	       result->write_seconds[2],
	       result->read_seconds[0], result->read_seconds[1],
	       result->read_seconds[2],
	       mib / write_median, mib / read_median, random_ops,
	       result->random_ns_per_load, result->checksum,
	       result->verified ? "true" : "false");
}

int main(int argc, char **argv)
{
	struct bench_result result;
	volatile uint64_t *words;
	uint64_t physical_base = 0;
	uint64_t bytes_u64;
	uint64_t iterations;
	uint64_t random_ops;
	size_t bytes;
	long page_size;
	bool self_test;
	void *mapping;
	int fd = -1;
	int ret = EXIT_FAILURE;

	self_test = argc == 2 && !strcmp(argv[1], "--self-test");
	if (self_test) {
		bytes_u64 = SELF_TEST_BYTES;
		iterations = BENCH_ITERATIONS;
		random_ops = SELF_TEST_RANDOM_OPS;
	} else {
		if (argc != 5 ||
		    parse_u64(argv[1], &physical_base) ||
		    parse_u64(argv[2], &bytes_u64) ||
		    parse_u64(argv[3], &iterations) ||
		    parse_u64(argv[4], &random_ops)) {
			fprintf(stderr,
				"usage: %s --self-test | "
				"PHYS_BASE BYTES ITERATIONS RANDOM_OPS\n",
				argv[0]);
			return EXIT_FAILURE;
		}
	}

	page_size = sysconf(_SC_PAGESIZE);
	if (page_size <= 0 || !bytes_u64 || bytes_u64 > SIZE_MAX ||
	    bytes_u64 % (uint64_t)page_size ||
	    physical_base % (uint64_t)page_size ||
	    bytes_u64 % sizeof(uint64_t) ||
	    iterations != BENCH_ITERATIONS || !random_ops) {
		fprintf(stderr, "invalid benchmark arguments\n");
		return EXIT_FAILURE;
	}
	bytes = (size_t)bytes_u64;

	if (self_test) {
		mapping = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
			       MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	} else {
		fd = open("/dev/mem", O_RDWR | O_SYNC);
		if (fd < 0) {
			fprintf(stderr, "open /dev/mem: %s\n", strerror(errno));
			return EXIT_FAILURE;
		}
		mapping = mmap(NULL, bytes, PROT_READ | PROT_WRITE, MAP_SHARED,
			       fd, (off_t)physical_base);
	}
	if (mapping == MAP_FAILED) {
		fprintf(stderr, "mmap: %s\n", strerror(errno));
		goto out;
	}

	words = mapping;
	if (run_benchmark(words, bytes, random_ops, &result)) {
		fprintf(stderr, "benchmark or verification failed\n");
		munmap(mapping, bytes);
		goto out;
	}
	print_result(&result, bytes, random_ops);
	munmap(mapping, bytes);
	ret = EXIT_SUCCESS;

out:
	if (fd >= 0)
		close(fd);
	return ret;
}

#endif
