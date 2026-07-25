typedef unsigned long size_t;
typedef unsigned long uint64_t;

#define AT_FDCWD (-100)
#define O_RDONLY 0
#define O_RDWR 2
#define MS_RDONLY 1
#define SIGCHLD 17

#define SYS_DUP3 24
#define SYS_MKDIRAT 34
#define SYS_MOUNT 40
#define SYS_OPENAT 56
#define SYS_CLOSE 57
#define SYS_READ 63
#define SYS_WRITE 64
#define SYS_SYNC 81
#define SYS_EXIT 93
#define SYS_NANOSLEEP 101
#define SYS_REBOOT 142
#define SYS_CLONE 220
#define SYS_EXECVE 221
#define SYS_WAIT4 260
#define SYS_READLINKAT 78

#define LINUX_REBOOT_MAGIC1 0xfee1dead
#define LINUX_REBOOT_MAGIC2 672274793
#define LINUX_REBOOT_CMD_POWER_OFF 0x4321fedc

struct kernel_timespec {
	long tv_sec;
	long tv_nsec;
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

static long syscall5(long number, long arg0, long arg1, long arg2,
		     long arg3, long arg4)
{
	return syscall6(number, arg0, arg1, arg2, arg3, arg4, 0);
}

static long syscall4(long number, long arg0, long arg1, long arg2, long arg3)
{
	return syscall6(number, arg0, arg1, arg2, arg3, 0, 0);
}

static long syscall3(long number, long arg0, long arg1, long arg2)
{
	return syscall6(number, arg0, arg1, arg2, 0, 0, 0);
}

static long syscall2(long number, long arg0, long arg1)
{
	return syscall6(number, arg0, arg1, 0, 0, 0, 0);
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

static int strings_equal(const char *left, const char *right)
{
	while (*left && *right) {
		if (*left++ != *right++)
			return 0;
	}
	return *left == *right;
}

static int ends_with(const char *text, size_t length, const char *suffix)
{
	size_t suffix_length = string_length(suffix);
	size_t index;

	if (suffix_length > length)
		return 0;
	for (index = 0; index < suffix_length; index++) {
		if (text[length - suffix_length + index] != suffix[index])
			return 0;
	}
	return 1;
}

static int contains_text(const char *text, size_t length, const char *needle)
{
	size_t needle_length = string_length(needle);
	size_t index;
	size_t offset;

	if (!needle_length || needle_length > length)
		return 0;
	for (index = 0; index + needle_length <= length; index++) {
		for (offset = 0; offset < needle_length; offset++) {
			if (text[index + offset] != needle[offset])
				break;
		}
		if (offset == needle_length)
			return 1;
	}
	return 0;
}

static void write_text(const char *text)
{
	size_t remaining = string_length(text);

	while (remaining) {
		long written = syscall3(SYS_WRITE, 1, (long)text, remaining);

		if (written <= 0)
			return;
		text += written;
		remaining -= (size_t)written;
	}
}

static void write_unsigned(unsigned long value)
{
	char reversed[24];
	size_t count = 0;

	do {
		reversed[count++] = '0' + value % 10;
		value /= 10;
	} while (value);
	while (count) {
		char digit = reversed[--count];

		syscall3(SYS_WRITE, 1, (long)&digit, 1);
	}
}

static void power_off(void) __attribute__((noreturn));

static void power_off(void)
{
	syscall1(SYS_SYNC, 0);
	syscall4(SYS_REBOOT, LINUX_REBOOT_MAGIC1, LINUX_REBOOT_MAGIC2,
		 LINUX_REBOOT_CMD_POWER_OFF, 0);
	for (;;)
		__asm__ volatile("wfi");
}

static void fail(const char *phase, long error) __attribute__((noreturn));

static void fail(const char *phase, long error)
{
	if (error < 0)
		error = -error;
	if (!error)
		error = 1;
	write_text("CXL_GUEST_INIT_FAIL phase=");
	write_text(phase);
	write_text(" errno=");
	write_unsigned((unsigned long)error);
	write_text("\n");
	power_off();
}

static void make_directory(const char *path)
{
	long result = syscall3(SYS_MKDIRAT, AT_FDCWD, (long)path, 0755);

	if (result < 0 && result != -17)
		fail("mkdir", result);
}

static void mount_one(const char *source, const char *target,
		      const char *type, unsigned long flags,
		      const char *phase)
{
	long result = syscall5(SYS_MOUNT, (long)source, (long)target,
			       (long)type, flags, 0);

	if (result < 0 && result != -16)
		fail(phase, result);
}

static void reopen_console(void)
{
	long console;
	long result;
	int target;

	console = syscall4(SYS_OPENAT, AT_FDCWD, (long)"/dev/console",
			   O_RDWR, 0);
	if (console < 0)
		fail("open-console", console);
	for (target = 0; target <= 2; target++) {
		if (console == target)
			continue;
		result = syscall3(SYS_DUP3, console, target, 0);
		if (result < 0)
			fail("dup-console", result);
	}
	if (console > 2)
		syscall1(SYS_CLOSE, console);
}

static long read_file(const char *path, char *buffer, size_t capacity)
{
	long file;
	long total = 0;

	if (capacity < 2)
		return -22;
	file = syscall4(SYS_OPENAT, AT_FDCWD, (long)path, O_RDONLY, 0);
	if (file < 0)
		return file;
	while ((size_t)total + 1 < capacity) {
		long count = syscall3(SYS_READ, file, (long)(buffer + total),
				      capacity - (size_t)total - 1);

		if (count < 0) {
			syscall1(SYS_CLOSE, file);
			return count;
		}
		if (!count)
			break;
		total += count;
	}
	syscall1(SYS_CLOSE, file);
	buffer[total] = '\0';
	return total;
}

static size_t trim_newline(char *buffer, size_t length)
{
	while (length && (buffer[length - 1] == '\n' ||
			  buffer[length - 1] == '\r' ||
			  buffer[length - 1] == ' ' ||
			  buffer[length - 1] == '\t'))
		buffer[--length] = '\0';
	return length;
}

static int file_equals(const char *path, const char *expected)
{
	char buffer[128];
	long length = read_file(path, buffer, sizeof(buffer));

	if (length < 0)
		return 0;
	trim_newline(buffer, (size_t)length);
	return strings_equal(buffer, expected);
}

static void wait_for_disk(void)
{
	struct kernel_timespec delay = {1, 0};
	unsigned int attempt;

	for (attempt = 0; attempt < 20; attempt++) {
		long file = syscall4(SYS_OPENAT, AT_FDCWD,
				     (long)"/dev/vda", O_RDONLY, 0);

		if (file >= 0) {
			syscall1(SYS_CLOSE, file);
			return;
		}
		syscall2(SYS_NANOSLEEP, (long)&delay, 0);
	}
	fail("wait-vda", 2);
}

static void validate_topology(void)
{
	static const char driver_path[] =
		"/sys/bus/pci/devices/0000:41:00.0/driver";
	char buffer[4096];
	long length;

	length = syscall4(SYS_READLINKAT, AT_FDCWD, (long)driver_path,
			  (long)buffer, sizeof(buffer));
	if (length <= 0 ||
	    !ends_with(buffer, (size_t)length, "cxl_pci"))
		fail("topology", length);
	if (!file_equals("/sys/bus/cxl/devices/decoder0.0/start",
			 "0x1000000000"))
		fail("topology", 1);
	if (!file_equals("/sys/bus/cxl/devices/region0/resource",
			 "0x1000000000"))
		fail("topology", 1);
	if (!file_equals("/sys/bus/cxl/devices/region0/size", "0x10000000"))
		fail("topology", 1);
	length = read_file("/proc/iomem", buffer, sizeof(buffer));
	if (length <= 0 ||
	    !contains_text(buffer, (size_t)length,
			   "1000000000-10ffffffff : CXL Window 0"))
		fail("topology", length);
}

static int starts_with_at(const char *text, size_t length, size_t offset,
			  const char *prefix)
{
	size_t prefix_length = string_length(prefix);
	size_t index;

	if (offset + prefix_length > length)
		return 0;
	for (index = 0; index < prefix_length; index++) {
		if (text[offset + index] != prefix[index])
			return 0;
	}
	return 1;
}

static uint64_t benchmark_bytes(void)
{
	static const char key[] = "cxl_bench_bytes=";
	char buffer[4096];
	long length = read_file("/proc/cmdline", buffer, sizeof(buffer));
	size_t index;

	if (length < 0)
		fail("cmdline", length);
	for (index = 0; index < (size_t)length; index++) {
		uint64_t value = 0;
		size_t cursor;
		int found_digit = 0;

		if (!starts_with_at(buffer, (size_t)length, index, key))
			continue;
		cursor = index + string_length(key);
		while (cursor < (size_t)length &&
		       buffer[cursor] >= '0' && buffer[cursor] <= '9') {
			unsigned int digit = buffer[cursor++] - '0';

			if (value > (~(uint64_t)0 - digit) / 10)
				fail("cmdline", 34);
			value = value * 10 + digit;
			found_digit = 1;
		}
		if (!found_digit || !value || value % 8 ||
		    value > 268435456UL)
			fail("cmdline", 22);
		return value;
	}
	return 1048576UL;
}

static void decimal_text(uint64_t value, char *buffer)
{
	char reversed[24];
	size_t count = 0;
	size_t index;

	do {
		reversed[count++] = '0' + value % 10;
		value /= 10;
	} while (value);
	for (index = 0; index < count; index++)
		buffer[index] = reversed[count - index - 1];
	buffer[count] = '\0';
}

static void run_benchmark_child(uint64_t bytes)
{
	char bytes_text[24];
	char *arguments[6];
	long child;
	int status = 0;

	decimal_text(bytes, bytes_text);
	arguments[0] = (char *)"/mnt/cxl_mmap_bench";
	arguments[1] = (char *)"0x1000000000";
	arguments[2] = bytes_text;
	arguments[3] = (char *)"3";
	arguments[4] = (char *)"100000";
	arguments[5] = 0;

	child = syscall5(SYS_CLONE, SIGCHLD, 0, 0, 0, 0);
	if (child < 0)
		fail("clone", child);
	if (!child) {
		long result = syscall3(SYS_EXECVE, (long)arguments[0],
				       (long)arguments, 0);

		write_text("CXL_GUEST_INIT_FAIL phase=exec errno=");
		write_unsigned((unsigned long)-result);
		write_text("\n");
		syscall1(SYS_EXIT, 127);
		for (;;)
			;
	}
	if (syscall4(SYS_WAIT4, child, (long)&status, 0, 0) < 0)
		fail("benchmark", 10);
	if ((status & 0x7f) != 0 || ((status >> 8) & 0xff) != 0)
		fail("benchmark", status ? status : 1);
}

static void init_main(void)
{
	uint64_t bytes;

	make_directory("/proc");
	make_directory("/sys");
	make_directory("/dev");
	make_directory("/mnt");
	mount_one("proc", "/proc", "proc", 0, "mount-proc");
	mount_one("sysfs", "/sys", "sysfs", 0, "mount-sys");
	mount_one("devtmpfs", "/dev", "devtmpfs", 0, "mount-dev");
	reopen_console();
	write_text("CXL_GUEST_INIT_START\n");
	wait_for_disk();
	mount_one("/dev/vda", "/mnt", "ext2", MS_RDONLY, "mount-ext2");
	write_text("CXL_DISK_PASS\n");
	validate_topology();
	write_text("CXL_TOPOLOGY_PASS\n");
	bytes = benchmark_bytes();
	run_benchmark_child(bytes);
	write_text("CXL_QEMU_UBOOT_LINUX_BENCH_PASS\n");
	power_off();
}

void _start(void) __attribute__((noreturn));

void _start(void)
{
	init_main();
	syscall1(SYS_EXIT, 0);
	for (;;)
		;
}
