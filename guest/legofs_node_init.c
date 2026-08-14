/* Strict freestanding PID 1 for the two-node Legofs/CXL Type-3 proof. */

typedef __SIZE_TYPE__ size_t;
typedef unsigned char uint8_t;
typedef unsigned short uint16_t;
typedef unsigned int uint32_t;
typedef unsigned long uint64_t;
typedef long int64_t;

#define AT_FDCWD (-100)
#define AT_SYMLINK_NOFOLLOW 0x100
#define O_RDONLY 0
#define O_RDWR 2
#define O_DIRECTORY 00200000
#define MS_RDONLY 1
#define S_IFCHR 0020000
#define SIGCHLD 17

#define AF_INET 2
#define SOCK_STREAM 1
#define IFF_UP 0x1
#define RTF_UP 0x1
#define RTF_GATEWAY 0x2
#define SIOCADDRT 0x890b
#define SIOCGIFFLAGS 0x8913
#define SIOCSIFFLAGS 0x8914
#define SIOCSIFADDR 0x8916
#define SIOCSIFNETMASK 0x891c

#define SYS_DUP3 24
#define SYS_IOCTL 29
#define SYS_MKNODAT 33
#define SYS_MKDIRAT 34
#define SYS_MOUNT 40
#define SYS_OPENAT 56
#define SYS_CLOSE 57
#define SYS_GETDENTS64 61
#define SYS_READ 63
#define SYS_WRITE 64
#define SYS_SYNC 81
#define SYS_EXIT 93
#define SYS_NANOSLEEP 101
#define SYS_REBOOT 142
#define SYS_SOCKET 198
#define SYS_CONNECT 203
#define SYS_CLONE 220
#define SYS_EXECVE 221
#define SYS_WAIT4 260
#define SYS_STATX 291

#define LINUX_REBOOT_MAGIC1 0xfee1dead
#define LINUX_REBOOT_MAGIC2 672274793
#define LINUX_REBOOT_CMD_POWER_OFF 0x4321fedc

#define MAX_CMDLINE 2048
#define MAX_PATH 160
#define MAX_ENV 32

struct kernel_timespec {
	long tv_sec;
	long tv_nsec;
};

struct linux_dirent64 {
	uint64_t d_ino;
	int64_t d_off;
	uint16_t d_reclen;
	uint8_t d_type;
	char d_name[];
};

struct sockaddr {
	uint16_t family;
	uint8_t data[14];
};

struct sockaddr_in {
	uint16_t family;
	uint16_t port;
	uint32_t address;
	uint8_t zero[8];
};

struct ifreq {
	char name[16];
	union {
		struct sockaddr address;
		short flags;
		uint8_t padding[24];
	} value;
};

struct rtentry {
	unsigned long pad1;
	struct sockaddr destination;
	struct sockaddr gateway;
	struct sockaddr genmask;
	unsigned short flags;
	short pad2;
	unsigned long pad3;
	void *pad4;
	short metric;
	char *device;
	unsigned long mtu;
	unsigned long window;
	unsigned short irtt;
};

struct statx_timestamp {
	int64_t seconds;
	uint32_t nanoseconds;
	int reserved;
};

struct statx_record {
	uint32_t mask;
	uint32_t block_size;
	uint64_t attributes;
	uint32_t links;
	uint32_t uid;
	uint32_t gid;
	uint16_t mode;
	uint16_t spare0;
	uint64_t inode;
	uint64_t size;
	uint64_t blocks;
	uint64_t attributes_mask;
	struct statx_timestamp atime;
	struct statx_timestamp btime;
	struct statx_timestamp ctime;
	struct statx_timestamp mtime;
	uint32_t rdev_major;
	uint32_t rdev_minor;
	uint32_t dev_major;
	uint32_t dev_minor;
	uint64_t mount_id;
	uint32_t dio_memory_align;
	uint32_t dio_offset_align;
	uint64_t subvolume;
	uint32_t atomic_write_unit_min;
	uint32_t atomic_write_unit_max;
	uint32_t atomic_write_segments_max;
	uint32_t dio_read_offset_align;
	uint64_t spare[9];
};

struct dax_device {
	char name[64];
	char path[MAX_PATH];
	uint32_t major;
	uint32_t minor;
	uint64_t size;
	uint64_t region_hi;
	uint64_t region_lo;
};

enum node_role {
	ROLE_INVALID = 0,
	ROLE_NODE0,
	ROLE_NODE1,
};

#if defined(__riscv)
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
#else
static long syscall6(long number, long arg0, long arg1, long arg2,
		     long arg3, long arg4, long arg5)
{
	(void)number;
	(void)arg0;
	(void)arg1;
	(void)arg2;
	(void)arg3;
	(void)arg4;
	(void)arg5;
	return -38;
}
#endif

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

void *memset(void *destination, int value, size_t length)
{
	uint8_t *output = destination;

	while (length--)
		*output++ = (uint8_t)value;
	return destination;
}

void *memcpy(void *destination, const void *source, size_t length)
{
	uint8_t *output = destination;
	const uint8_t *input = source;

	while (length--)
		*output++ = *input++;
	return destination;
}

static size_t text_length(const char *text)
{
	size_t length = 0;

	while (text[length])
		length++;
	return length;
}

static void memory_zero(void *pointer, size_t length)
{
	uint8_t *bytes = pointer;

	while (length--)
		*bytes++ = 0;
}

static void text_copy(char *destination, size_t capacity, const char *source)
{
	size_t index = 0;

	if (!capacity)
		return;
	while (source[index] && index + 1 < capacity) {
		destination[index] = source[index];
		index++;
	}
	destination[index] = '\0';
}

static int text_equal(const char *left, const char *right)
{
	while (*left && *right) {
		if (*left++ != *right++)
			return 0;
	}
	return *left == *right;
}

static int text_starts_with(const char *text, const char *prefix)
{
	while (*prefix) {
		if (*text++ != *prefix++)
			return 0;
	}
	return 1;
}

static void write_text(const char *text)
{
	size_t remaining = text_length(text);

	while (remaining) {
		long written = syscall3(SYS_WRITE, 1, (long)text, (long)remaining);

		if (written <= 0)
			return;
		text += written;
		remaining -= (size_t)written;
	}
}

static void write_unsigned(uint64_t value)
{
	char reversed[24];
	size_t count = 0;

	do {
		reversed[count++] = (char)('0' + value % 10);
		value /= 10;
	} while (value);
	while (count) {
		char digit = reversed[--count];

		syscall3(SYS_WRITE, 1, (long)&digit, 1);
	}
}

static void write_hex_byte(uint8_t value)
{
	static const char digits[] = "0123456789abcdef";
	char pair[2];

	pair[0] = digits[value >> 4];
	pair[1] = digits[value & 15];
	syscall3(SYS_WRITE, 1, (long)pair, 2);
}

static void write_region_id(uint64_t hi, uint64_t lo)
{
	uint8_t bytes[16];
	unsigned int index;

	for (index = 0; index < 8; index++)
		bytes[index] = (uint8_t)(hi >> (56 - index * 8));
	for (index = 0; index < 8; index++)
		bytes[index + 8] = (uint8_t)(lo >> (56 - index * 8));
	for (index = 0; index < 16; index++) {
		if (index == 4 || index == 6 || index == 8 || index == 10)
			write_text("-");
		write_hex_byte(bytes[index]);
	}
}

static void sleep_milliseconds(unsigned int milliseconds)
{
	struct kernel_timespec delay;

	delay.tv_sec = milliseconds / 1000;
	delay.tv_nsec = (long)(milliseconds % 1000) * 1000000L;
	syscall2(SYS_NANOSLEEP, (long)&delay, 0);
}

static void power_off(void) __attribute__((noreturn));

static void power_off(void)
{
	syscall1(SYS_SYNC, 0);
	syscall4(SYS_REBOOT, LINUX_REBOOT_MAGIC1, LINUX_REBOOT_MAGIC2,
		 LINUX_REBOOT_CMD_POWER_OFF, 0);
	for (;;)
#if defined(__riscv)
		__asm__ volatile("wfi");
#else
		;
#endif
}

static void fail(const char *phase, long error) __attribute__((noreturn));

static void fail(const char *phase, long error)
{
	if (error < 0)
		error = -error;
	if (!error)
		error = 1;
	write_text("LEG_OFS_FAIL phase=");
	write_text(phase);
	write_text(" errno=");
	write_unsigned((uint64_t)error);
	write_text("\n");
	power_off();
}

static void make_directory(const char *path)
{
	long result = syscall3(SYS_MKDIRAT, AT_FDCWD, (long)path, 0755);

	if (result < 0 && result != -17)
		fail("mkdir", result);
}

static void mount_one(const char *source, const char *target, const char *type,
		      unsigned long flags, const char *data, const char *phase)
{
	long result = syscall5(SYS_MOUNT, (long)source, (long)target,
			       (long)type, (long)flags, (long)data);

	if (result < 0 && result != -16)
		fail(phase, result);
}

static void reopen_console(void)
{
	long console = syscall4(SYS_OPENAT, AT_FDCWD, (long)"/dev/console", O_RDWR, 0);
	int target;

	if (console < 0)
		fail("open-console", console);
	for (target = 0; target <= 2; target++) {
		long result;

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
				      (long)(capacity - (size_t)total - 1));

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

static uint64_t parse_unsigned(const char *text, int *valid)
{
	uint64_t value = 0;
	unsigned int base = 10;
	size_t index = 0;

	*valid = 0;
	if (text[0] == '0' && (text[1] == 'x' || text[1] == 'X')) {
		base = 16;
		index = 2;
	}
	if (!text[index])
		return 0;
	for (; text[index] && text[index] != '\n' && text[index] != '\r'; index++) {
		unsigned int digit;

		if (text[index] >= '0' && text[index] <= '9')
			digit = (unsigned int)(text[index] - '0');
		else if (text[index] >= 'a' && text[index] <= 'f')
			digit = (unsigned int)(text[index] - 'a' + 10);
		else if (text[index] >= 'A' && text[index] <= 'F')
			digit = (unsigned int)(text[index] - 'A' + 10);
		else
			return 0;
		if (digit >= base || value > (~(uint64_t)0 - digit) / base)
			return 0;
		value = value * base + digit;
	}
	*valid = 1;
	return value;
}

static int read_unsigned_file(const char *path, uint64_t *value)
{
	char buffer[80];
	long length = read_file(path, buffer, sizeof(buffer));
	int valid;

	if (length <= 0)
		return 0;
	*value = parse_unsigned(buffer, &valid);
	return valid;
}

static int path_exists(const char *path, int directory)
{
	long file = syscall4(SYS_OPENAT, AT_FDCWD, (long)path,
			     O_RDONLY | (directory ? O_DIRECTORY : 0), 0);

	if (file < 0)
		return 0;
	syscall1(SYS_CLOSE, file);
	return 1;
}

static void append_text(char *destination, size_t capacity, const char *source)
{
	size_t used = text_length(destination);

	if (used < capacity)
		text_copy(destination + used, capacity - used, source);
}

static int scan_prefix(const char *directory, const char *prefix, char *only_name,
		       size_t capacity)
{
	uint8_t buffer[4096];
	long file = syscall4(SYS_OPENAT, AT_FDCWD, (long)directory,
			     O_RDONLY | O_DIRECTORY, 0);
	int count = 0;

	if (file < 0)
		return -1;
	for (;;) {
		long bytes = syscall3(SYS_GETDENTS64, file, (long)buffer, sizeof(buffer));
		long offset = 0;

		if (bytes < 0) {
			syscall1(SYS_CLOSE, file);
			return -1;
		}
		if (!bytes)
			break;
		while (offset < bytes) {
			struct linux_dirent64 *entry = (struct linux_dirent64 *)(buffer + offset);

			if (entry->d_reclen <
			    __builtin_offsetof(struct linux_dirent64, d_name) + 1 ||
			    offset + entry->d_reclen > bytes) {
				syscall1(SYS_CLOSE, file);
				return -1;
			}
			if (text_starts_with(entry->d_name, prefix)) {
				count++;
				if (count == 1 && only_name)
					text_copy(only_name, capacity, entry->d_name);
			}
			offset += entry->d_reclen;
		}
	}
	syscall1(SYS_CLOSE, file);
	return count;
}

static int wait_for_prefix(const char *directory, const char *prefix,
			   int expected_count)
{
	int attempt;
	int count = -1;

	for (attempt = 0; attempt < 120; attempt++) {
		count = scan_prefix(directory, prefix, 0, 0);
		if (count >= expected_count)
			return count;
		sleep_milliseconds(250);
	}
	return count;
}

static uint64_t linux_device_number(uint32_t major, uint32_t minor)
{
	return ((uint64_t)(major & 0xfff) << 8) | (minor & 0xff) |
	       ((uint64_t)(minor & ~0xffU) << 12) |
	       ((uint64_t)(major & ~0xfffU) << 32);
}

static uint64_t rotate_left(uint64_t value, unsigned int shift)
{
	return (value << shift) | (value >> (64 - shift));
}

static uint64_t stable_region_mix(uint64_t value)
{
	value ^= 0x62616466732d7267UL;
	value += 0x9e3779b97f4a7c15UL;
	value = (value ^ (value >> 30)) * 0xbf58476d1ce4e5b9UL;
	value = (value ^ (value >> 27)) * 0x94d049bb133111ebUL;
	return value ^ (value >> 31);
}

static void derive_region_id(struct dax_device *device)
{
	struct statx_record status;
	uint64_t dev;
	uint64_t rdev;
	long result;

	memory_zero(&status, sizeof(status));
	result = syscall5(SYS_STATX, AT_FDCWD, (long)device->path,
			  AT_SYMLINK_NOFOLLOW, 0x7ff, (long)&status);
	if (result < 0)
		fail("statx-dax", result);
	dev = linux_device_number(status.dev_major, status.dev_minor);
	rdev = linux_device_number(status.rdev_major, status.rdev_minor);
	device->region_hi = stable_region_mix(dev ^ rotate_left(rdev, 17) ^
					      rotate_left(status.size, 31) ^
					      rotate_left(2, 7));
	device->region_lo = stable_region_mix(status.inode ^ rotate_left(dev, 29) ^
					      rotate_left(rdev, 11) ^ status.size);
}

static void discover_dax(struct dax_device *device)
{
	char sysfs[MAX_PATH];
	char contents[80];
	char *colon = 0;
	long length;
	uint64_t value;
	int valid;
	size_t index;

	memory_zero(device, sizeof(*device));
	if (scan_prefix("/sys/bus/dax/devices", "dax", device->name,
			sizeof(device->name)) != 1)
		fail("dax-device-count", 19);
	text_copy(device->path, sizeof(device->path), "/dev/");
	append_text(device->path, sizeof(device->path), device->name);
	text_copy(sysfs, sizeof(sysfs), "/sys/bus/dax/devices/");
	append_text(sysfs, sizeof(sysfs), device->name);
	append_text(sysfs, sizeof(sysfs), "/dev");
	length = read_file(sysfs, contents, sizeof(contents));
	if (length <= 0)
		fail("read-dax-dev", length);
	for (index = 0; contents[index]; index++) {
		if (contents[index] == ':') {
			colon = &contents[index];
			break;
		}
	}
	if (!colon)
		fail("parse-dax-dev", 22);
	*colon = '\0';
	value = parse_unsigned(contents, &valid);
	if (!valid || value > 0xffffffffUL)
		fail("parse-dax-major", 22);
	device->major = (uint32_t)value;
	value = parse_unsigned(colon + 1, &valid);
	if (!valid || value > 0xffffffffUL)
		fail("parse-dax-minor", 22);
	device->minor = (uint32_t)value;

	text_copy(sysfs, sizeof(sysfs), "/sys/bus/dax/devices/");
	append_text(sysfs, sizeof(sysfs), device->name);
	append_text(sysfs, sizeof(sysfs), "/size");
	if (!read_unsigned_file(sysfs, &device->size) || !device->size)
		fail("read-dax-size", 22);

	value = linux_device_number(device->major, device->minor);
	length = syscall4(SYS_MKNODAT, AT_FDCWD, (long)device->path,
			  S_IFCHR | 0600, (long)value);
	if (length < 0 && length != -17)
		fail("mknod-dax", length);
	if (!path_exists(device->path, 0))
		fail("open-dax", 19);
	derive_region_id(device);
}

static uint32_t ipv4(unsigned int a, unsigned int b, unsigned int c, unsigned int d)
{
	return a | (b << 8) | (c << 16) | (d << 24);
}

static uint16_t network_u16(uint16_t value)
{
	return (uint16_t)((value << 8) | (value >> 8));
}

static void set_ifreq_name(struct ifreq *request)
{
	memory_zero(request, sizeof(*request));
	text_copy(request->name, sizeof(request->name), "eth0");
}

static void set_sockaddr(struct sockaddr *address, uint32_t ipv4_address)
{
	struct sockaddr_in *inet = (struct sockaddr_in *)address;

	memory_zero(address, sizeof(*address));
	inet->family = AF_INET;
	inet->address = ipv4_address;
}

static void configure_network(void)
{
	struct ifreq request;
	struct rtentry route;
	long socket = syscall3(SYS_SOCKET, AF_INET, SOCK_STREAM, 0);
	long result;

	if (socket < 0)
		fail("network-socket", socket);
	set_ifreq_name(&request);
	set_sockaddr(&request.value.address, ipv4(10, 0, 2, 15));
	result = syscall3(SYS_IOCTL, socket, SIOCSIFADDR, (long)&request);
	if (result < 0)
		fail("network-address", result);
	set_ifreq_name(&request);
	set_sockaddr(&request.value.address, ipv4(255, 255, 255, 0));
	result = syscall3(SYS_IOCTL, socket, SIOCSIFNETMASK, (long)&request);
	if (result < 0)
		fail("network-netmask", result);
	set_ifreq_name(&request);
	result = syscall3(SYS_IOCTL, socket, SIOCGIFFLAGS, (long)&request);
	if (result < 0)
		fail("network-get-flags", result);
	request.value.flags |= IFF_UP;
	result = syscall3(SYS_IOCTL, socket, SIOCSIFFLAGS, (long)&request);
	if (result < 0)
		fail("network-set-flags", result);

	memory_zero(&route, sizeof(route));
	set_sockaddr(&route.destination, ipv4(0, 0, 0, 0));
	set_sockaddr(&route.gateway, ipv4(10, 0, 2, 2));
	set_sockaddr(&route.genmask, ipv4(0, 0, 0, 0));
	route.flags = RTF_UP | RTF_GATEWAY;
	route.device = (char *)"eth0";
	result = syscall3(SYS_IOCTL, socket, SIOCADDRT, (long)&route);
	if (result < 0 && result != -17)
		fail("network-route", result);
	syscall1(SYS_CLOSE, socket);
}

static int connect_tcp(uint32_t address, uint16_t port)
{
	struct sockaddr_in peer;
	long socket = syscall3(SYS_SOCKET, AF_INET, SOCK_STREAM, 0);
	long result;

	if (socket < 0)
		return 0;
	memory_zero(&peer, sizeof(peer));
	peer.family = AF_INET;
	peer.port = network_u16(port);
	peer.address = address;
	result = syscall3(SYS_CONNECT, socket, (long)&peer, sizeof(peer));
	syscall1(SYS_CLOSE, socket);
	return result == 0;
}

static const char *cmdline_value(char *cmdline, const char *key)
{
	size_t key_length = text_length(key);
	char *cursor = cmdline;

	while (*cursor) {
		char *token;
		char *end;

		while (*cursor == ' ')
			cursor++;
		if (!*cursor)
			break;
		token = cursor;
		while (*cursor && *cursor != ' ' && *cursor != '\n')
			cursor++;
		end = cursor;
		if (*cursor)
			*cursor++ = '\0';
		if (text_starts_with(token, key) && token[key_length])
			return token + key_length;
		(void)end;
	}
	return 0;
}

static void unsigned_to_text(uint64_t value, char *output, size_t capacity)
{
	char reversed[24];
	size_t count = 0;
	size_t index = 0;

	if (!capacity)
		return;
	do {
		reversed[count++] = (char)('0' + value % 10);
		value /= 10;
	} while (value);
	while (count && index + 1 < capacity)
		output[index++] = reversed[--count];
	output[index] = '\0';
}

static long spawn(const char *path, char *const environment[])
{
	static char *const arguments[] = {(char *)"badfs", 0};
	long child = syscall5(SYS_CLONE, SIGCHLD, 0, 0, 0, 0);

	if (child < 0)
		return child;
	if (!child) {
		syscall3(SYS_EXECVE, (long)path, (long)arguments, (long)environment);
		syscall1(SYS_EXIT, 127);
		for (;;)
			;
	}
	return child;
}

static int wait_child(long child)
{
	int status = 0;
	long result;

	do {
		result = syscall4(SYS_WAIT4, child, (long)&status, 0, 0);
	} while (result == -4);
	if (result != child)
		return -1;
	if ((status & 0x7f) != 0)
		return -1;
	return (status >> 8) & 0xff;
}

static void wait_for_run_gate(void)
{
	static const char expected[] = "LEG_OFS_RUN";
	char line[64];
	size_t used = 0;

	while (used + 1 < sizeof(line)) {
		char byte;
		long count = syscall3(SYS_READ, 0, (long)&byte, 1);

		if (count < 0) {
			if (count == -4)
				continue;
			fail("run-gate-read", count);
		}
		if (!count)
			fail("run-gate-eof", 5);
		if (byte == '\r')
			continue;
		if (byte == '\n') {
			line[used] = '\0';
			if (text_equal(line, expected))
				return;
			used = 0;
			continue;
		}
		line[used++] = byte;
	}
	fail("run-gate-overflow", 7);
}

static size_t add_environment(char **environment, size_t count, char *entry)
{
	if (count + 1 >= MAX_ENV)
		fail("environment-capacity", 7);
	environment[count++] = entry;
	environment[count] = 0;
	return count;
}

static size_t common_environment(char **environment, char *device_entry)
{
	size_t count = 0;

	count = add_environment(environment, count, (char *)"BADFS_POSIX_DATA_PATH=lifecycle");
	count = add_environment(environment, count, (char *)"BADFS_LIFECYCLE_BLOB=1");
	count = add_environment(environment, count, (char *)"BADFS_LIFECYCLE_DIRECT_FINAL=1");
	count = add_environment(environment, count, (char *)"BADFS_LIFECYCLE_DIRECT_REQUIRED=1");
	count = add_environment(environment, count, (char *)"BADFS_LIFECYCLE_DIRECT_READ=1");
	count = add_environment(environment, count, (char *)"BADFS_LIFECYCLE_DIRECT_READ_REQUIRED=1");
	count = add_environment(environment, count, (char *)"BADFS_LIFECYCLE_DEVICE_REQUIRED=1");
	count = add_environment(environment, count, (char *)"BADFS_CXL_MAP_ALIGNMENT=4096");
	count = add_environment(environment, count, (char *)"BADFS_LIFECYCLE_TRACE=/tmp/lifecycle.jsonl");
	count = add_environment(environment, count, (char *)"BADFS_LIFECYCLE_TRACE_STDOUT=1");
	count = add_environment(environment, count, (char *)"BADFS_CXL_DIRECT_TRACE=/tmp/direct.jsonl");
	count = add_environment(environment, count, (char *)"BADFS_CXL_DIRECT_TRACE_STDOUT=1");
	count = add_environment(environment, count, (char *)"BADFS_LIFECYCLE_POOL_SIZE=268435456");
	count = add_environment(environment, count, (char *)"BADFS_LIFECYCLE_MAX_EXTENTS=128");
	count = add_environment(environment, count, (char *)"RUST_LOG=info");
	count = add_environment(environment, count, device_entry);
	return count;
}

static void run_node0(char *device_entry)
{
	char *environment[MAX_ENV] = {0};
	size_t count = common_environment(environment, device_entry);
	long server;
	unsigned int attempt;

	count = add_environment(environment, count, (char *)"BADFS_SERVER_ADDR=0.0.0.0:3345");
	(void)add_environment(environment, count, (char *)"BADFS_DATA_DIR=/tmp/badfs-data");
	server = spawn("/mnt/badfs-server", environment);
	if (server < 0)
		fail("spawn-server", server);
	for (attempt = 0; attempt < 240; attempt++) {
		if (connect_tcp(ipv4(127, 0, 0, 1), 3345)) {
			write_text("LEG_OFS_SERVER_READY addr=0.0.0.0:3345\n");
			if (wait_child(server) != 0)
				fail("server-exit", 5);
			fail("server-stopped", 5);
		}
		sleep_milliseconds(250);
	}
	fail("server-ready-timeout", 110);
}

static void run_node1(char *device_entry, uint16_t server_port, uint64_t bytes)
{
	char *environment[MAX_ENV] = {0};
	char server_entry[64] = "BADFS_SERVERS=10.0.2.2:";
	char port_text[16];
	char bytes_entry[64] = "BADFS_BENCH_FILE_SIZE=";
	char bytes_text[24];
	char mode_workload[] = "BADFS_BENCH_MODE=workload";
	char mode_inspect[] = "BADFS_BENCH_MODE=inspect";
	size_t count = common_environment(environment, device_entry);
	long child;
	int status;

	unsigned_to_text(server_port, port_text, sizeof(port_text));
	append_text(server_entry, sizeof(server_entry), port_text);
	unsigned_to_text(bytes, bytes_text, sizeof(bytes_text));
	append_text(bytes_entry, sizeof(bytes_entry), bytes_text);
	count = add_environment(environment, count, server_entry);
	count = add_environment(environment, count, (char *)"BADFS_BASE_PATH=/badfs");
	count = add_environment(environment, count, mode_workload);
	count = add_environment(environment, count, bytes_entry);
	count = add_environment(environment, count, (char *)"BADFS_BENCH_BLOCK_SIZE=4096");
	(void)add_environment(environment, count, (char *)"BADFS_BENCH_ITERATIONS=1");

	write_text("LEG_OFS_CLIENT_READY\n");
	wait_for_run_gate();
	write_text("LEG_OFS_BENCHMARK_BEGIN\n");
	child = spawn("/mnt/badfs-bench", environment);
	if (child < 0)
		fail("spawn-workload", child);
	status = wait_child(child);
	if (status != 0)
		fail("benchmark-workload", status < 0 ? 5 : status);

	for (count = 0; environment[count]; count++) {
		if (text_starts_with(environment[count], "BADFS_BENCH_MODE=")) {
			environment[count] = mode_inspect;
			break;
		}
	}
	if (!environment[count])
		fail("inspect-environment", 22);
	child = spawn("/mnt/badfs-bench", environment);
	if (child < 0)
		fail("spawn-inspect", child);
	status = wait_child(child);
	if (status != 0)
		fail("benchmark-inspect", status < 0 ? 5 : status);
	write_text("LEG_OFS_BENCHMARK_PASS\n");
	power_off();
}

void _start(void)
{
	char cmdline[MAX_CMDLINE];
	char role_copy[16];
	char port_copy[16];
	char bytes_copy[32];
	char device_entry[MAX_PATH + 32] = "BADFS_LIFECYCLE_DEVICE=";
	const char *role_value;
	const char *port_value;
	const char *bytes_value;
	enum node_role role = ROLE_INVALID;
	uint64_t port;
	uint64_t bytes;
	int valid;
	struct dax_device dax;

	make_directory("/proc");
	make_directory("/sys");
	make_directory("/dev");
	make_directory("/tmp");
	make_directory("/mnt");
	mount_one("proc", "/proc", "proc", 0, 0, "mount-proc");
	mount_one("sysfs", "/sys", "sysfs", 0, 0, "mount-sys");
	mount_one("devtmpfs", "/dev", "devtmpfs", 0, "mode=0755", "mount-dev");
	mount_one("tmpfs", "/tmp", "tmpfs", 0, "mode=0755", "mount-tmp");
	reopen_console();

	if (read_file("/proc/cmdline", cmdline, sizeof(cmdline)) <= 0)
		fail("read-cmdline", 5);
	role_value = cmdline_value(cmdline, "legofs.role=");
	if (!role_value)
		fail("missing-role", 22);
	text_copy(role_copy, sizeof(role_copy), role_value);
	if (text_equal(role_copy, "node0"))
		role = ROLE_NODE0;
	else if (text_equal(role_copy, "node1"))
		role = ROLE_NODE1;
	else
		fail("invalid-role", 22);

	/* cmdline_value mutates separators, so reread for each required key. */
	if (read_file("/proc/cmdline", cmdline, sizeof(cmdline)) <= 0)
		fail("reread-cmdline-port", 5);
	port_value = cmdline_value(cmdline, "legofs.server_port=");
	if (!port_value)
		fail("missing-server-port", 22);
	text_copy(port_copy, sizeof(port_copy), port_value);
	port = parse_unsigned(port_copy, &valid);
	if (!valid || !port || port > 65535)
		fail("invalid-server-port", 22);

	if (read_file("/proc/cmdline", cmdline, sizeof(cmdline)) <= 0)
		fail("reread-cmdline-bytes", 5);
	bytes_value = cmdline_value(cmdline, "legofs.bytes=");
	if (!bytes_value)
		fail("missing-bytes", 22);
	text_copy(bytes_copy, sizeof(bytes_copy), bytes_value);
	bytes = parse_unsigned(bytes_copy, &valid);
	if (!valid || !bytes || bytes > 16777216 || bytes % 4096)
		fail("invalid-bytes", 22);

	for (valid = 0; valid < 120 && !path_exists("/dev/vda", 0); valid++)
		sleep_milliseconds(250);
	if (!path_exists("/dev/vda", 0))
		fail("payload-disk-timeout", 110);
	mount_one("/dev/vda", "/mnt", "ext2", MS_RDONLY, 0, "mount-payload");
	if (!path_exists("/mnt/badfs-server", 0) || !path_exists("/mnt/badfs-bench", 0))
		fail("payload-binaries", 2);

	configure_network();
	for (valid = 0; valid < 120 &&
	     !path_exists("/sys/bus/cxl/devices/mem0", 1); valid++)
		sleep_milliseconds(250);
	if (!path_exists("/sys/bus/cxl/devices/mem0", 1))
		fail("missing-cxl-mem0", 19);
	if (wait_for_prefix("/sys/bus/cxl/devices", "region", 1) < 1)
		fail("missing-cxl-region", 19);
	if (wait_for_prefix("/sys/bus/cxl/devices", "decoder", 1) < 1)
		fail("missing-cxl-decoder", 19);
	if (wait_for_prefix("/sys/bus/dax/devices", "dax", 1) < 1)
		fail("missing-dax", 19);
	discover_dax(&dax);
	append_text(device_entry, sizeof(device_entry), dax.path);

	write_text("LEG_OFS_CXL_READY role=");
	write_text(role == ROLE_NODE0 ? "node0" : "node1");
	write_text(" dax=");
	write_text(dax.path);
	write_text(" size=");
	write_unsigned(dax.size);
	write_text(" major=");
	write_unsigned(dax.major);
	write_text(" minor=");
	write_unsigned(dax.minor);
	write_text(" region_id=");
	write_region_id(dax.region_hi, dax.region_lo);
	write_text("\n");

	if (role == ROLE_NODE0)
		run_node0(device_entry);
	run_node1(device_entry, (uint16_t)port, bytes);
}
