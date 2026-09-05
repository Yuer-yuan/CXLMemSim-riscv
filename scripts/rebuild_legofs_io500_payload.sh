#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
LEGOFS_ROOT="$ROOT/components/legofs"
TARGET_ROOT="$ROOT/target/build/riscv-io500"
SOURCES="$ROOT/target/build/sources"
PLATFORM="$TARGET_ROOT/platform"
PAYLOAD_ROOT="$TARGET_ROOT/payload-root"
IMAGES="$TARGET_ROOT/images"
RESULTS="$ROOT/target/results/legofs-io500"
STATIC_CARGO_TARGET="$TARGET_ROOT/cargo-legofs-static"
DYNAMIC_CARGO_TARGET="$TARGET_ROOT/cargo-rv64imafdc-compiler-rt"
MPICH_PREFIX="$TARGET_ROOT/mpich-rv64imafdc-compiler-rt-install"
SYSINT_BUILD_ROOT="$TARGET_ROOT/syscall-intercept-rv64imafdc-compiler-rt"
MUSL_PREFIX="$TARGET_ROOT/musl-rv64imafdc-compiler-rt"
LIBUNWIND_PREFIX="$TARGET_ROOT/libunwind-rv64imafdc-compiler-rt"
BUILTINS_ARCHIVE="$TARGET_ROOT/compiler-rt-rv64imafdc-build/lib/linux/libclang_rt.builtins-riscv64.a"
STATIC_MUSL_CC="$ROOT/out/legofs-type3/toolchain/musl-rv64gc/bin/musl-gcc"
MUSL_CC="$MUSL_PREFIX/bin/musl-gcc"
CROSS_COMPILE="${CROSS_COMPILE:-riscv64-linux-gnu-}"
LLVM_MC="${LLVM_MC:-llvm-mc}"
NOV_GCC="$ROOT/scripts/riscv64-nov-gcc"
RUST_TARGET=riscv64gc-unknown-linux-musl
PAYLOAD_IMAGE="$IMAGES/io500-payload.ext2"
DEPENDENCY_VERSIONS="$RESULTS/dependency-versions.txt"
ISA_REPORT="$RESULTS/isa-gate.txt"
source "$ROOT/scripts/legofs_toolchain_path.sh"
legofs_toolchain_activate io500-payload \
	bash sh cargo rustc getconf \
	"${CROSS_COMPILE}gcc" "${CROSS_COMPILE}ar" \
	"${CROSS_COMPILE}ld" "${CROSS_COMPILE}nm" \
	"${CROSS_COMPILE}objcopy" "${CROSS_COMPILE}objdump" \
	"${CROSS_COMPILE}ranlib" "${CROSS_COMPILE}readelf" \
	"${CROSS_COMPILE}strip" "$LLVM_MC" \
	cc ar ld git cmake make tar mke2fs debugfs truncate file install rsync \
	sha256sum mktemp cmp python3 awk grep sed sort seq cp mv rm mkdir \
	find xargs env uname dirname
JOBS="$(getconf _NPROCESSORS_ONLN 2>/dev/null || printf '1\n')"
PAYLOAD_STAGE=
PAYLOAD_IMAGE_TMP=

usage()
{
	cat <<'EOF'
Usage: scripts/rebuild_legofs_io500_payload.sh [--jobs N]

Rebuild only the LegoFS RISC-V server, benchmark and preload library, then
atomically replace the IO500 guest payload image. Existing QEMU, CXLMemSim,
OpenSBI, U-Boot, Linux, IO500, MPI and toolchain artifacts are prerequisites
and are never rebuilt by this command.
EOF
}

die()
{
	printf 'error: %s\n' "$*" >&2
	exit 2
}

cleanup()
{
	if [[ -n "$PAYLOAD_IMAGE_TMP" && -e "$PAYLOAD_IMAGE_TMP" ]]; then
		rm -f -- "$PAYLOAD_IMAGE_TMP"
	fi
	if [[ -n "$PAYLOAD_STAGE" && -d "$PAYLOAD_STAGE" ]]; then
		rm -rf -- "$PAYLOAD_STAGE"
	fi
}
trap cleanup EXIT

while (($#)); do
	case "$1" in
	--jobs)
		(($# >= 2)) || die '--jobs requires a value'
		JOBS="$2"
		shift 2
		;;
	--help)
		usage
		exit 0
		;;
	*)
		die "unknown argument: $1"
		;;
	esac
done

[[ "$JOBS" =~ ^[1-9][0-9]*$ ]] || die 'jobs must be a positive integer'

require_file()
{
	[[ -f "$1" && -s "$1" ]] || die "missing prerequisite artifact: $1"
}

require_executable()
{
	[[ -x "$1" ]] || die "missing prerequisite executable: $1"
}

RUST_SYSROOT="$(rustc --print sysroot)"
[[ -d "$RUST_SYSROOT/lib/rustlib/$RUST_TARGET/lib" ]] ||
	die "Rust sysroot lacks target libraries: $RUST_TARGET"
compgen -G "$RUST_SYSROOT/lib/rustlib/$RUST_TARGET/lib/libcore-*.rlib" \
	>/dev/null || die "Rust sysroot target is incomplete: $RUST_TARGET"

require_file "$PLATFORM/qemu-system-riscv64"
require_file "$PLATFORM/cxlmemsim_server"
require_file "$PLATFORM/fw_dynamic.bin"
require_file "$PLATFORM/u-boot.bin"
require_file "$PLATFORM/linux-io500-Image"
require_file "$SOURCES/io500/io500"
require_file "$SOURCES/io500/io500-verify"
require_file "$MPICH_PREFIX/bin/mpiexec.hydra"
require_file "$MPICH_PREFIX/bin/hydra_pmi_proxy"
require_file "$TARGET_ROOT/mpi-hello"
require_file "$ROOT/guest/export_io500_results.c"
require_file "$ROOT/guest/system_sync_probe.c"
require_file "$LIBUNWIND_PREFIX/lib/libunwind.so.1"
require_file "$BUILTINS_ARCHIVE"
require_file "$DEPENDENCY_VERSIONS"
require_file "$LEGOFS_ROOT/Cargo.toml"
require_file "$ROOT/guest/legofs_io500_rank.sh"
require_file "$ROOT/guest/legofs_io500_init.sh"
require_file "$ROOT/guest/legofs_badfs_server.sh"
require_file "$ROOT/guest/legofs_badfs_bench.sh"
require_file "$ROOT/scripts/write_manifest.py"
require_executable "$STATIC_MUSL_CC"
require_executable "$MUSL_CC"
require_executable "$NOV_GCC"

for stage in tiny stress-tiny rollover-smoke easy-smoke hard-smoke metadata-smoke small-close-smoke rnd4k scc standard; do
	require_file "$ROOT/configs/io500-$stage.ini"
done

shopt -s nullglob
mpi_libraries=("$MPICH_PREFIX/lib/"libmpi.so*)
unwind_libraries=("$LIBUNWIND_PREFIX/lib/"libunwind.so*)
((${#mpi_libraries[@]} > 0)) || die 'missing prerequisite MPICH shared libraries'
((${#unwind_libraries[@]} > 0)) || die 'missing prerequisite libunwind libraries'

platform_hashes()
{
	sha256sum \
		"$PLATFORM/qemu-system-riscv64" \
		"$PLATFORM/cxlmemsim_server" \
		"$PLATFORM/fw_dynamic.bin" \
		"$PLATFORM/u-boot.bin" \
		"$PLATFORM/linux-io500-Image"
}

platform_hashes_before="$(platform_hashes)"

printf '%s\n' '[io500-payload] vendored RISC-V syscall interceptor'
SYSINT_ROOT="$LEGOFS_ROOT/third_party/syscall-intercept-riscv" \
SYSINT_CAPSTONE_MIRROR="$SOURCES/capstone.git" \
SYSINT_BUILD_ROOT="$SYSINT_BUILD_ROOT" SYSINT_JOBS="$JOBS" \
SYSINT_MUSL_LIBC="$MUSL_PREFIX/lib/libc.so" \
RISCV_CC="$MUSL_CC" RISCV_LD="${CROSS_COMPILE}ld" \
RISCV_OBJCOPY="${CROSS_COMPILE}objcopy" \
RISCV_OBJDUMP="${CROSS_COMPILE}objdump" \
RISCV_AR="${CROSS_COMPILE}ar" RISCV_RANLIB="${CROSS_COMPILE}ranlib" \
	"$LEGOFS_ROOT/scripts/build-syscall-intercept-riscv.sh"
require_file "$SYSINT_BUILD_ROOT/build/libsyscall_intercept.so.0"
sysint_libraries=("$SYSINT_BUILD_ROOT/build/"libsyscall_intercept.so*)
((${#sysint_libraries[@]} > 0)) || die 'missing rebuilt syscall-intercept libraries'

printf '%s\n' '[io500-payload] static LegoFS server and benchmark'
mkdir -p "$STATIC_CARGO_TARGET" "$DYNAMIC_CARGO_TARGET" "$IMAGES" "$RESULTS"
CARGO_TARGET_DIR="$STATIC_CARGO_TARGET" \
CC_riscv64gc_unknown_linux_musl="$STATIC_MUSL_CC" \
CARGO_TARGET_RISCV64GC_UNKNOWN_LINUX_MUSL_LINKER="$STATIC_MUSL_CC" \
RUSTFLAGS='-C target-feature=+crt-static -C link-arg=-march=rv64gc -C link-arg=-mabi=lp64d' \
	cargo build --manifest-path "$LEGOFS_ROOT/Cargo.toml" --release \
	--target "$RUST_TARGET" --jobs "$JOBS" -p badfs-server -p badfs-bench

BADFS_SERVER="$STATIC_CARGO_TARGET/$RUST_TARGET/release/badfs-server"
BADFS_BENCH="$STATIC_CARGO_TARGET/$RUST_TARGET/release/badfs-bench"
require_file "$BADFS_SERVER"
require_file "$BADFS_BENCH"

printf '%s\n' '[io500-payload] LegoFS syscall interception library'
CARGO_TARGET_DIR="$DYNAMIC_CARGO_TARGET" \
CC_riscv64gc_unknown_linux_musl="$MUSL_CC" \
CARGO_TARGET_RISCV64GC_UNKNOWN_LINUX_MUSL_LINKER="$MUSL_CC" \
LIBSYSCALL_INTERCEPT_LIB_DIR="$SYSINT_BUILD_ROOT/build" \
RUSTFLAGS="-C target-feature=-crt-static -C panic=abort -C link-arg=-L$LIBUNWIND_PREFIX/lib" \
	cargo build --manifest-path "$LEGOFS_ROOT/Cargo.toml" --release \
	--target "$RUST_TARGET" --jobs "$JOBS" -p badfs-intercept \
	--features syscall-intercept-backend

BADFS_INTERCEPT="$DYNAMIC_CARGO_TARGET/$RUST_TARGET/release/libbadfs_intercept.so"
require_file "$BADFS_INTERCEPT"

printf '%s\n' '[io500-payload] dynamic system(3) sync regression probe'
"$MUSL_CC" -O2 -Wall -Wextra -Werror \
	-march=rv64imafdc -mabi=lp64d \
	"$ROOT/guest/export_io500_results.c" \
	-o "$TARGET_ROOT/export-io500-results"
"$MUSL_CC" -O2 -Wall -Wextra -Werror \
	-march=rv64imafdc -mabi=lp64d \
	"$ROOT/guest/system_sync_probe.c" \
	-o "$TARGET_ROOT/system-sync-probe"
require_file "$TARGET_ROOT/system-sync-probe"
"$MUSL_CC" -O2 -Wall -Wextra -Werror -pthread \
	-march=rv64imafdc -mabi=lp64d \
	"$ROOT/guest/thread_exit_probe.c" -o "$TARGET_ROOT/thread-exit-probe"

PAYLOAD_STAGE="$(mktemp -d "$TARGET_ROOT/.payload-build.XXXXXX")"
PAYLOAD_TREE="$PAYLOAD_STAGE/root"
mkdir -p "$PAYLOAD_TREE/bin" "$PAYLOAD_TREE/lib" "$PAYLOAD_TREE/etc"

install -m 0755 "$SOURCES/io500/io500" "$PAYLOAD_TREE/bin/io500"
install -m 0755 "$SOURCES/io500/io500-verify" "$PAYLOAD_TREE/bin/io500-verify"
install -m 0755 "$MPICH_PREFIX/bin/mpiexec.hydra" "$PAYLOAD_TREE/bin/mpiexec.hydra"
install -m 0755 "$MPICH_PREFIX/bin/hydra_pmi_proxy" "$PAYLOAD_TREE/bin/hydra_pmi_proxy"
install -m 0755 "$TARGET_ROOT/mpi-hello" "$PAYLOAD_TREE/bin/mpi-hello"
install -m 0755 "$TARGET_ROOT/export-io500-results" \
	"$PAYLOAD_TREE/bin/export-io500-results"
install -m 0755 "$TARGET_ROOT/system-sync-probe" \
	"$PAYLOAD_TREE/bin/system-sync-probe"
install -m 0755 "$TARGET_ROOT/thread-exit-probe" "$PAYLOAD_TREE/bin/thread-exit-probe"
install -m 0755 "$ROOT/guest/legofs_io500_rank.sh" \
	"$PAYLOAD_TREE/bin/run-io500-rank"
install -m 0755 "$ROOT/guest/legofs_io500_init.sh" \
	"$PAYLOAD_TREE/bin/legofs-io500-init"
install -m 0755 "$BADFS_SERVER" "$PAYLOAD_TREE/bin/badfs-server.real"
install -m 0755 "$ROOT/guest/legofs_badfs_server.sh" "$PAYLOAD_TREE/bin/badfs-server"
install -m 0755 "$BADFS_BENCH" "$PAYLOAD_TREE/bin/badfs-bench.real"
install -m 0755 "$ROOT/guest/legofs_badfs_bench.sh" "$PAYLOAD_TREE/bin/badfs-bench"
"${CROSS_COMPILE}strip" --strip-debug \
	"$PAYLOAD_TREE/bin/badfs-server.real" "$PAYLOAD_TREE/bin/badfs-bench.real"
install -m 0755 "$BADFS_INTERCEPT" "$PAYLOAD_TREE/lib/libbadfs_intercept.so"
cp -a "${mpi_libraries[@]}" "$PAYLOAD_TREE/lib/"
cp -a "${sysint_libraries[@]}" "$PAYLOAD_TREE/lib/"
cp -a "${unwind_libraries[@]}" "$PAYLOAD_TREE/lib/"

for stage in tiny stress-tiny rollover-smoke easy-smoke hard-smoke metadata-smoke small-close-smoke rnd4k scc standard; do
	install -m 0644 "$ROOT/configs/io500-$stage.ini" \
		"$PAYLOAD_TREE/etc/io500-$stage.ini"
done
for index in $(seq 0 9); do
	printf '10.77.0.%s:1\n' "$((index + 10))"
done > "$PAYLOAD_TREE/etc/clients"

: > "$PAYLOAD_STAGE/isa-gate.txt"
ISA_REPORT_STAGE="$PAYLOAD_STAGE/isa-gate.txt"

require_sifive_u_isa()
{
	binary="$1"
	vector_count=$("${CROSS_COMPILE}objdump" -d "$binary" |
		awk '/^[[:space:]]*[0-9a-f]+:/ && $3 ~ /^v[a-z0-9_.]+$/ {count++}
			END {print count + 0}')
	attribute=$("${CROSS_COMPILE}readelf" -A "$binary" |
		awk '/^[[:space:]]*Tag_RISCV_arch:/ && !seen {
			sub(/^[[:space:]]*Tag_RISCV_arch: /, ""); print; seen = 1
		}')
	decoder_diagnostics=$("${CROSS_COMPILE}objdump" -d "$binary" |
		awk '$1 ~ /^[0-9a-f]+:$/ && $2 ~ /^[0-9a-f]+$/ &&
			(length($2) == 4 || length($2) == 8) {
			raw = $2; output = ""
			for (pos = length(raw) - 1; pos > 0; pos -= 2)
				output = output "0x" substr(raw, pos, 2) " "
			print output
		}' |
		"$LLVM_MC" --triple=riscv64 \
			--mattr=+m,+a,+f,+d,+c,+zicsr,+zifencei,+zicbom \
			--disassemble 2>&1 >/dev/null)
	invalid_count=$(printf '%s\n' "$decoder_diagnostics" |
		grep -c 'invalid instruction encoding' || true)
	printf 'artifact=%s invalid_instruction_count=%s vector_instruction_count=%s attribute=%s\n' \
		"$binary" "$invalid_count" "$vector_count" "$attribute" >> "$ISA_REPORT_STAGE"
	if [[ -n "$decoder_diagnostics" ]]; then
		printf '%s\n' "$decoder_diagnostics" | sed -n '1,40p' >> "$ISA_REPORT_STAGE"
		die "SiFive U payload contains instructions outside rv64imafdc+zicbom: $binary"
	fi
}

require_needed()
{
	binary="$1"
	library="$2"
	"${CROSS_COMPILE}readelf" -d "$binary" |
		awk -v expected="[$library]" \
			'$2 == "(NEEDED)" && $NF == expected {found = 1}
			 END {exit !found}' ||
		die "$binary does not declare the required dependency: $library"
}

require_syscall_intercept_abi()
{
	binary="$1"
	symbols=$("${CROSS_COMPILE}readelf" --wide --dyn-syms "$binary")
	printf '%s\n' "$symbols" |
		awk '$7 == "UND" && $8 == "intercept_hook_point" {found = 1}
			END {exit !found}' ||
		die "$binary does not import the syscall-intercept hook point"
	for symbol in access close closedir creat creat64 dirfd dup dup2 dup3 \
		faccessat fdatasync fstatat fstatat64 fstatfs fstatfs64 fstatvfs \
		fstatvfs64 fsync getdents64 lseek lseek64 lstat lstat64 mkdir \
		mkdirat newfstatat open open64 openat openat64 opendir pread \
		pread64 pwrite pwrite64 read readdir readdir64 readlink readlinkat \
		rmdir stat stat64 statfs statfs64 statvfs statvfs64 statx unlink \
		unlinkat write; do
		printf '%s\n' "$symbols" |
			awk -v expected="$symbol" '
				$7 != "UND" {
					name = $8; sub(/@.*/, "", name)
					if (name == expected) found = 1
				}
				END {exit !found}' ||
			die "$binary lacks the required POSIX interception entry: $symbol"
	done
}

reject_glibc_versions()
{
	binary="$1"
	if "${CROSS_COMPILE}readelf" --version-info "$binary" | grep -q 'GLIBC_'; then
		die "musl payload has a glibc symbol-version dependency: $binary"
	fi
}

require_unwind_provider()
{
	consumer="$1"
	provider="$2"
	unwind_symbols=$("${CROSS_COMPILE}readelf" --wide --symbols "$consumer" |
		awk '$7 == "UND" && $8 ~ /^_Unwind_/ {
			sub(/@.*/, "", $8); print $8
		}' | sort -u)
	[[ -n "$unwind_symbols" ]] ||
		die "$consumer has no auditable undefined unwind symbols"
	while IFS= read -r symbol; do
		[[ -n "$symbol" ]] || continue
		"${CROSS_COMPILE}readelf" --wide --symbols "$provider" |
			awk -v expected="$symbol" \
				'$7 != "UND" && $8 == expected {found = 1}
				 END {exit !found}' ||
			die "$provider does not define required unwind symbol: $symbol"
	done <<< "$unwind_symbols"
}

for binary in "$PAYLOAD_TREE/bin/io500" "$PAYLOAD_TREE/bin/io500-verify" \
	"$PAYLOAD_TREE/bin/mpiexec.hydra" "$PAYLOAD_TREE/bin/hydra_pmi_proxy" \
	"$PAYLOAD_TREE/bin/mpi-hello" "$PAYLOAD_TREE/bin/export-io500-results" \
	"$PAYLOAD_TREE/bin/system-sync-probe" "$PAYLOAD_TREE/bin/thread-exit-probe" \
	"$PAYLOAD_TREE/bin/badfs-server.real" "$PAYLOAD_TREE/bin/badfs-bench.real" \
	"$PAYLOAD_TREE/lib/libmpi.so" "$PAYLOAD_TREE/lib/libunwind.so" \
	"$PAYLOAD_TREE/lib/libsyscall_intercept.so" \
	"$PAYLOAD_TREE/lib/libbadfs_intercept.so"; do
	file -L "$binary" | grep -q 'RISC-V' || die "payload binary is not RISC-V: $binary"
	require_sifive_u_isa "$binary"
done

for binary in "$PAYLOAD_TREE/bin/io500" \
	"$PAYLOAD_TREE/bin/export-io500-results" \
	"$PAYLOAD_TREE/bin/system-sync-probe" "$PAYLOAD_TREE/bin/thread-exit-probe"; do
	"${CROSS_COMPILE}readelf" -l "$binary" |
		grep -q '/lib/ld-musl-riscv64.so.1' ||
		die "$binary does not use the clean musl loader"
done

for binary in "$PAYLOAD_TREE/bin/io500" "$PAYLOAD_TREE/bin/io500-verify" \
	"$PAYLOAD_TREE/bin/mpiexec.hydra" "$PAYLOAD_TREE/bin/hydra_pmi_proxy" \
	"$PAYLOAD_TREE/bin/mpi-hello" "$PAYLOAD_TREE/bin/export-io500-results" \
	"$PAYLOAD_TREE/bin/system-sync-probe" "$PAYLOAD_TREE/bin/thread-exit-probe" \
	"$PAYLOAD_TREE/lib/libmpi.so" "$PAYLOAD_TREE/lib/libunwind.so" \
	"$PAYLOAD_TREE/lib/libsyscall_intercept.so" \
	"$PAYLOAD_TREE/lib/libbadfs_intercept.so"; do
	reject_glibc_versions "$binary"
done

require_needed "$PAYLOAD_TREE/lib/libbadfs_intercept.so" libsyscall_intercept.so.0
require_syscall_intercept_abi "$PAYLOAD_TREE/lib/libbadfs_intercept.so"
require_needed "$PAYLOAD_TREE/lib/libbadfs_intercept.so" libunwind.so.1
require_needed "$PAYLOAD_TREE/lib/libbadfs_intercept.so" libc.so
require_needed "$PAYLOAD_TREE/lib/libunwind.so" libc.so
require_needed "$PAYLOAD_TREE/bin/export-io500-results" libc.so
require_needed "$PAYLOAD_TREE/bin/system-sync-probe" libc.so
require_needed "$PAYLOAD_TREE/bin/thread-exit-probe" libc.so
require_unwind_provider "$PAYLOAD_TREE/lib/libbadfs_intercept.so" \
	"$PAYLOAD_TREE/lib/libunwind.so"
for binary in "$PAYLOAD_TREE/bin/badfs-server.real" "$PAYLOAD_TREE/bin/badfs-bench.real"; do
	"${CROSS_COMPILE}readelf" -l "$binary" | grep -q INTERP &&
		die "static LegoFS binary has an interpreter: $binary"
done

PAYLOAD_IMAGE_TMP="$(mktemp "$IMAGES/.io500-payload.ext2.XXXXXX")"
truncate -s 768M "$PAYLOAD_IMAGE_TMP"
mke2fs -q -t ext2 -F -d "$PAYLOAD_TREE" "$PAYLOAD_IMAGE_TMP"

for artifact in badfs-server badfs-server.real badfs-bench badfs-bench.real; do
	debugfs -R "dump /bin/$artifact $PAYLOAD_STAGE/verify-$artifact" \
		"$PAYLOAD_IMAGE_TMP" >/dev/null 2>&1
	cmp "$PAYLOAD_TREE/bin/$artifact" "$PAYLOAD_STAGE/verify-$artifact" ||
		die "payload image contains an incomplete $artifact"
done
debugfs -R "dump /lib/libbadfs_intercept.so $PAYLOAD_STAGE/verify-libbadfs_intercept.so" \
	"$PAYLOAD_IMAGE_TMP" >/dev/null 2>&1
cmp "$PAYLOAD_TREE/lib/libbadfs_intercept.so" \
	"$PAYLOAD_STAGE/verify-libbadfs_intercept.so" ||
	die 'payload image contains an incomplete libbadfs_intercept.so'

[[ "$platform_hashes_before" == "$(platform_hashes)" ]] ||
	die 'platform artifacts changed during payload-only rebuild'

mv -f -- "$PAYLOAD_IMAGE_TMP" "$PAYLOAD_IMAGE"
PAYLOAD_IMAGE_TMP=
mkdir -p "$PAYLOAD_ROOT"
rsync -a --delete "$PAYLOAD_TREE/" "$PAYLOAD_ROOT/"
install -m 0644 "$ISA_REPORT_STAGE" "$ISA_REPORT.tmp"
mv -f -- "$ISA_REPORT.tmp" "$ISA_REPORT"

python3 "$ROOT/scripts/write_manifest.py" --root "$ROOT" \
	--output "$RESULTS/build-manifest.json" \
	--source "legofs=$LEGOFS_ROOT" \
	--compiler "riscv_gcc=${CROSS_COMPILE}gcc --version" \
	--compiler "riscv_static_musl_gcc=$STATIC_MUSL_CC --version" \
	--compiler "riscv_musl_gcc=$MUSL_CC --version" \
	--compiler "riscv_nov_gcc=$NOV_GCC --version" \
	--compiler "llvm_mc=$LLVM_MC --version" \
	--compiler "rustc=rustc --version" \
	--artifact "qemu=$PLATFORM/qemu-system-riscv64" \
	--artifact "cxlmemsim_server=$PLATFORM/cxlmemsim_server" \
	--artifact "opensbi=$PLATFORM/fw_dynamic.bin" \
	--artifact "u_boot=$PLATFORM/u-boot.bin" \
	--artifact "linux=$PLATFORM/linux-io500-Image" \
	--artifact "payload=$PAYLOAD_IMAGE" \
	--artifact "io500=$PAYLOAD_ROOT/bin/io500" \
	--artifact "io500_verify=$PAYLOAD_ROOT/bin/io500-verify" \
	--artifact "mpiexec=$PAYLOAD_ROOT/bin/mpiexec.hydra" \
	--artifact "hydra_proxy=$PAYLOAD_ROOT/bin/hydra_pmi_proxy" \
	--artifact "io500_result_export=$PAYLOAD_ROOT/bin/export-io500-results" \
	--artifact "system_sync_probe=$PAYLOAD_ROOT/bin/system-sync-probe" \
	--artifact "thread_exit_probe=$PAYLOAD_ROOT/bin/thread-exit-probe" \
	--artifact "compiler_rt_builtins=$BUILTINS_ARCHIVE" \
	--artifact "libunwind=$PAYLOAD_ROOT/lib/libunwind.so.1" \
	--artifact "badfs_server=$PAYLOAD_ROOT/bin/badfs-server.real" \
	--artifact "badfs_bench=$PAYLOAD_ROOT/bin/badfs-bench.real" \
	--artifact "badfs_intercept=$PAYLOAD_ROOT/lib/libbadfs_intercept.so" \
	--artifact "dependency_versions=$DEPENDENCY_VERSIONS" \
	--artifact "isa_gate=$ISA_REPORT"

[[ "$platform_hashes_before" == "$(platform_hashes)" ]] ||
	die 'platform artifacts changed during payload-only rebuild'

printf '[io500-payload] image %s\n' "$PAYLOAD_IMAGE"
printf '[io500-payload] manifest %s\n' "$RESULTS/build-manifest.json"
