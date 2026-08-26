#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
LEGOFS_ROOT="${ROOT}/components/legofs"
TARGET_ROOT="${ROOT}/target/build/riscv-io500"
SOURCES="${ROOT}/target/build/sources"
PLATFORM="${TARGET_ROOT}/platform"
PAYLOAD_ROOT="${TARGET_ROOT}/payload-root"
INITRAMFS="${TARGET_ROOT}/initramfs"
IMAGES="${TARGET_ROOT}/images"
RESULTS="${ROOT}/target/results/legofs-io500"
CARGO_TARGET="${TARGET_ROOT}/cargo-rv64imafdc-compiler-rt"
MPICH_BUILD="${TARGET_ROOT}/mpich-rv64imafdc-compiler-rt-build"
MPICH_PREFIX="${TARGET_ROOT}/mpich-rv64imafdc-compiler-rt-install"
SYSINT_BUILD_ROOT="${TARGET_ROOT}/syscall-intercept-rv64imafdc-compiler-rt"
CROSS_COMPILE="${CROSS_COMPILE:-riscv64-linux-gnu-}"
NOV_GCC="${ROOT}/scripts/riscv64-nov-gcc"
CLANG="${CLANG:-clang}"
LLVM_AR="${LLVM_AR:-llvm-ar}"
LLVM_MC="${LLVM_MC:-llvm-mc}"
LLVM_RANLIB="${LLVM_RANLIB:-llvm-ranlib}"
RUST_MUSL_TARGET=riscv64gc-unknown-linux-musl
MUSL_VERSION=1.2.5
MUSL_TARBALL_SHA256=a9a118bbe84d8764da0ea0d28b3ab3fae8477fc7e4085d90102b8596fc7c75e4
MUSL_HOTPATCH_PADDING_PATCH="${ROOT}/scripts/patches/musl/0001-riscv64-syscall-hotpatch-padding.patch"
LLVM_REPOSITORY=https://github.com/llvm/llvm-project.git
LLVM_TAG=llvmorg-20.1.8
LLVM_COMMIT=87f0227cb60147a26a1eeb4fb06e3b505e9c7261
JOBS="$(getconf _NPROCESSORS_ONLN 2>/dev/null || printf '1\n')"

MPICH_REPOSITORY=https://github.com/pmodels/mpich.git
MPICH_COMMIT=15f59ab2b740539472dfd130f7fe01b61c28bba4
IO500_REPOSITORY=https://github.com/IO500/io500.git
IO500_COMMIT=a69cf60cf76538a34c1332bc448838cf9a560a9b
IOR_REPOSITORY=https://github.com/hpc/ior.git
IOR_COMMIT=5fcf0ba995fd92164d50e344597e2d8203298c08
PFIND_REPOSITORY=https://github.com/VI4IO/pfind.git
PFIND_COMMIT=d08501f9976caf1adabdebfb883d4701dd98fe35
BUSYBOX_REPOSITORY=https://github.com/mirror/busybox.git
BUSYBOX_COMMIT=be7d1b7b1701d225379bc1665487ed0871b592a5
SYSINT_REPOSITORY=https://github.com/alpha-unito/syscall_intercept.git
SYSINT_COMMIT=7dbdf6ab9c576f96843ef2553b7efc7d15cf66b4
CAPSTONE_REPOSITORY=https://github.com/capstone-engine/capstone.git
CAPSTONE_COMMIT=accf4df62f1fba6f92cae692985d27063552601c

die()
{
	printf 'error: %s\n' "$*" >&2
	exit 2
}

while (($#)); do
	case "$1" in
	--jobs)
		(($# >= 2)) || die "--jobs requires a value"
		JOBS="$2"
		shift 2
		;;
	*) die "unknown argument: $1" ;;
	esac
done
[[ "$JOBS" =~ ^[1-9][0-9]*$ ]] || die "jobs must be a positive integer"

for command in git make cmake ninja cargo rustc mke2fs debugfs truncate tar \
	sha256sum \
	file install rsync "${CROSS_COMPILE}gcc" \
	"${CROSS_COMPILE}ar" "${CROSS_COMPILE}ld" \
	"${CROSS_COMPILE}objcopy" "${CROSS_COMPILE}objdump" \
	"${CROSS_COMPILE}ranlib" "${CROSS_COMPILE}readelf" \
	"${CROSS_COMPILE}strip" "$CLANG" "$LLVM_AR" "$LLVM_MC" \
	"$LLVM_RANLIB"; do
	command -v "$command" >/dev/null || die "required command is missing: $command"
done
rust_sysroot="$(rustc --print sysroot)"
rust_std=("$rust_sysroot/lib/rustlib/$RUST_MUSL_TARGET/lib"/libstd-*.rlib)
[ -f "${rust_std[0]}" ] ||
	die "active rustc sysroot lacks target: $RUST_MUSL_TARGET ($rust_sysroot)"
[ -x "$NOV_GCC" ] || die "missing no-RVV compiler wrapper: $NOV_GCC"

mkdir -p "$TARGET_ROOT" "$SOURCES" "$PLATFORM" "$PAYLOAD_ROOT" \
	"$INITRAMFS" "$IMAGES" "$RESULTS" "$CARGO_TARGET"

ensure_checkout()
{
	name="$1"
	repository="$2"
	commit="$3"
	directory="$4"
	if [ ! -d "$directory/.git" ]; then
		[ ! -e "$directory" ] || die "$name source exists but is not a Git checkout: $directory"
		git init "$directory"
		git -C "$directory" remote add origin "$repository"
	fi
	actual_repository="$(git -C "$directory" remote get-url origin)"
	[ "$actual_repository" = "$repository" ] ||
		die "$name repository mismatch: $actual_repository"
	if ! git -C "$directory" cat-file -e "${commit}^{commit}" 2>/dev/null; then
		git -C "$directory" fetch --depth=1 origin "$commit"
	fi
	git -C "$directory" checkout --detach "$commit"
	actual_commit="$(git -C "$directory" rev-parse HEAD)"
	[ "$actual_commit" = "$commit" ] ||
		die "$name commit mismatch: $actual_commit"
}

printf '%s\n' '[io500-build] pinned source checkouts'
ensure_checkout MPICH "$MPICH_REPOSITORY" "$MPICH_COMMIT" "$SOURCES/mpich"
ensure_checkout IO500 "$IO500_REPOSITORY" "$IO500_COMMIT" "$SOURCES/io500"
ensure_checkout IOR "$IOR_REPOSITORY" "$IOR_COMMIT" "$SOURCES/io500/build/ior"
ensure_checkout pfind "$PFIND_REPOSITORY" "$PFIND_COMMIT" "$SOURCES/io500/build/pfind"
ensure_checkout BusyBox "$BUSYBOX_REPOSITORY" "$BUSYBOX_COMMIT" "$SOURCES/busybox"
ensure_checkout SyscallIntercept "$SYSINT_REPOSITORY" "$SYSINT_COMMIT" \
	"$SOURCES/syscall-intercept"
LLVM_SOURCE="$SOURCES/llvm-project"
if [ ! -d "$LLVM_SOURCE/.git" ]; then
	[ ! -e "$LLVM_SOURCE" ] || die "LLVM source exists but is not a Git checkout: $LLVM_SOURCE"
	git init "$LLVM_SOURCE"
	git -C "$LLVM_SOURCE" remote add origin "$LLVM_REPOSITORY"
fi
[ "$(git -C "$LLVM_SOURCE" remote get-url origin)" = "$LLVM_REPOSITORY" ] ||
	die 'LLVM repository mismatch'
if ! git -C "$LLVM_SOURCE" cat-file -e "${LLVM_COMMIT}^{commit}" 2>/dev/null; then
	git -C "$LLVM_SOURCE" fetch --depth=1 --filter=blob:none origin "$LLVM_COMMIT"
fi
git -C "$LLVM_SOURCE" sparse-checkout init --cone
git -C "$LLVM_SOURCE" sparse-checkout set \
	cmake llvm/cmake llvm/utils/llvm-lit runtimes libunwind compiler-rt
git -C "$LLVM_SOURCE" checkout --detach "$LLVM_COMMIT"
[ "$(git -C "$LLVM_SOURCE" rev-parse HEAD)" = "$LLVM_COMMIT" ] ||
	die 'LLVM commit mismatch'
if [ ! -d "$LLVM_SOURCE/compiler-rt" ]; then
	git -C "$LLVM_SOURCE" sparse-checkout add compiler-rt
fi
for path in cmake llvm/cmake llvm/utils/llvm-lit runtimes libunwind compiler-rt; do
	[ -d "$LLVM_SOURCE/$path" ] || die "LLVM sparse checkout lacks: $path"
done
if [ ! -d "$SOURCES/capstone.git" ]; then
	git init --bare "$SOURCES/capstone.git"
	git --git-dir="$SOURCES/capstone.git" remote add origin "$CAPSTONE_REPOSITORY"
fi
[ "$(git --git-dir="$SOURCES/capstone.git" remote get-url origin)" = "$CAPSTONE_REPOSITORY" ] ||
	die 'Capstone mirror repository mismatch'
if ! git --git-dir="$SOURCES/capstone.git" \
	cat-file -e "${CAPSTONE_COMMIT}^{commit}" 2>/dev/null; then
	git --git-dir="$SOURCES/capstone.git" fetch --depth=1 origin "$CAPSTONE_COMMIT"
fi
git --git-dir="$SOURCES/capstone.git" cat-file -e "${CAPSTONE_COMMIT}^{commit}" ||
	die 'Capstone mirror lacks the pinned commit'

printf '%s\n' '[io500-build] baseline SiFive U CXL platform'
"${ROOT}/scripts/build_legofs_type3.sh" --jobs "$JOBS"
BASELINE="${ROOT}/out/legofs-type3/build"
install -m 0755 "$BASELINE/qemu/qemu-system-riscv64" "$PLATFORM/qemu-system-riscv64"
install -m 0755 "$BASELINE/cxlmemsim/cxlmemsim_server" "$PLATFORM/cxlmemsim_server"
install -m 0644 "$BASELINE/opensbi/platform/generic/firmware/fw_dynamic.bin" "$PLATFORM/fw_dynamic.bin"
install -m 0644 "$BASELINE/u-boot/u-boot.bin" "$PLATFORM/u-boot.bin"

ISA_REPORT="$RESULTS/isa-gate.txt"
: > "$ISA_REPORT"
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
			--mattr=+m,+a,+f,+d,+c,+zicsr,+zifencei \
			--disassemble 2>&1 >/dev/null)
	invalid_count=$(printf '%s\n' "$decoder_diagnostics" |
		grep -c 'invalid instruction encoding' || true)
	printf 'artifact=%s invalid_instruction_count=%s vector_instruction_count=%s attribute=%s\n' \
		"$binary" "$invalid_count" "$vector_count" "$attribute" >> "$ISA_REPORT"
	if [ -n "$decoder_diagnostics" ]; then
		printf '%s\n' "$decoder_diagnostics" | sed -n '1,40p' >> "$ISA_REPORT"
		die "SiFive U payload contains instructions outside rv64imafdc: $binary"
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

	# The current RISC-V preload intentionally has two complementary entry
	# paths: these libc symbols cover normal POSIX calls (and DIR helpers),
	# while libsyscall_intercept covers direct ecalls and calls in other DSOs.
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

require_musl_hotpatch_padding()
{
	binary="$1"
	report="$2"
	if ! "${CROSS_COMPILE}objdump" -d "$binary" |
		awk '
			$1 ~ /^[0-9a-f]+:$/ && $2 ~ /^[0-9a-f]+$/ {
				if ($3 == "ecall") {
					total++
					if (previous_encoding != "00000013") {
						bad++
						printf "bad_ecall=%s previous_encoding=%s\n", \
							$1, previous_encoding
					}
				}
				previous_encoding = $2
			}
			END {
				printf "ecall_count=%d bad_ecall_count=%d required_previous_encoding=00000013\n", \
					total + 0, bad + 0
				exit total == 0 || bad != 0
			}' > "$report"; then
		cat "$report" >&2
		die "musl libc lacks a four-byte nop immediately before every ecall: $binary"
	fi
}

reject_glibc_versions()
{
	binary="$1"
	if "${CROSS_COMPILE}readelf" --version-info "$binary" |
		grep -q 'GLIBC_'; then
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
	[ -n "$unwind_symbols" ] ||
		die "$consumer has no auditable undefined unwind symbols"
	while IFS= read -r symbol; do
		[ -n "$symbol" ] || continue
		"${CROSS_COMPILE}readelf" --wide --symbols "$provider" |
			awk -v expected="$symbol" \
				'$7 != "UND" && $8 == expected {found = 1}
				 END {exit !found}' ||
			die "$provider does not define required unwind symbol: $symbol"
	done <<< "$unwind_symbols"
}

printf '%s\n' '[io500-build] bootstrap musl headers for compiler-rt'
MUSL_UPSTREAM_SOURCE="${ROOT}/out/legofs-type3/toolchain-src/musl-${MUSL_VERSION}"
MUSL_TARBALL="${ROOT}/out/legofs-type3/toolchain-src/musl-${MUSL_VERSION}.tar.gz"
MUSL_SOURCE="${TARGET_ROOT}/musl-${MUSL_VERSION}-io500-source"
MUSL_SOURCE_STAMP="${MUSL_SOURCE}/.legofs-io500-source"
MUSL_BOOTSTRAP_BUILD="${TARGET_ROOT}/musl-rv64imafdc-bootstrap-build"
MUSL_BOOTSTRAP_PREFIX="${TARGET_ROOT}/musl-rv64imafdc-bootstrap"
[ -x "$MUSL_UPSTREAM_SOURCE/configure" ] || die "baseline build did not prepare musl source"
[ -s "$MUSL_TARBALL" ] || die "baseline build did not prepare musl tarball"
[ -s "$MUSL_HOTPATCH_PADDING_PATCH" ] ||
	die "missing musl syscall hotpatch-padding patch: $MUSL_HOTPATCH_PADDING_PATCH"
actual_musl_tarball_sha256=$(sha256sum "$MUSL_TARBALL" | awk '{print $1}')
[ "$actual_musl_tarball_sha256" = "$MUSL_TARBALL_SHA256" ] ||
	die "musl tarball hash mismatch: $actual_musl_tarball_sha256"
MUSL_HOTPATCH_PADDING_SHA256=$(sha256sum "$MUSL_HOTPATCH_PADDING_PATCH" |
	awk '{print $1}')
MUSL_SOURCE_ID="musl-${MUSL_VERSION} tar_sha256=${MUSL_TARBALL_SHA256} hotpatch_padding_sha256=${MUSL_HOTPATCH_PADDING_SHA256}"
if [ ! -f "$MUSL_SOURCE_STAMP" ] ||
	[ "$(cat "$MUSL_SOURCE_STAMP" 2>/dev/null || true)" != "$MUSL_SOURCE_ID" ]; then
	rm -rf -- "$MUSL_SOURCE"
	mkdir -p "$MUSL_SOURCE"
	tar -xf "$MUSL_TARBALL" -C "$MUSL_SOURCE" --strip-components=1
	(
		cd "$MUSL_SOURCE"
		GIT_DIR=/dev/null git apply "$MUSL_HOTPATCH_PADDING_PATCH"
	)
	printf '%s\n' "$MUSL_SOURCE_ID" > "$MUSL_SOURCE_STAMP"
fi
[ -x "$MUSL_SOURCE/configure" ] || die "patched musl source is incomplete"
MUSL_BOOTSTRAP_STAMP="$MUSL_BOOTSTRAP_PREFIX/.legofs-musl-source"
if [ ! -x "$MUSL_BOOTSTRAP_PREFIX/bin/musl-gcc" ] ||
	! cmp -s "$MUSL_SOURCE_STAMP" "$MUSL_BOOTSTRAP_STAMP" ||
	! grep -Fqx "prefix = $MUSL_BOOTSTRAP_PREFIX" \
		"$MUSL_BOOTSTRAP_BUILD/config.mak" 2>/dev/null ||
	! grep -Fq -- "-specs \"$MUSL_BOOTSTRAP_PREFIX/lib/musl-gcc.specs\"" \
		"$MUSL_BOOTSTRAP_PREFIX/bin/musl-gcc" 2>/dev/null; then
	rm -rf -- "$MUSL_BOOTSTRAP_BUILD" "$MUSL_BOOTSTRAP_PREFIX"
	mkdir -p "$MUSL_BOOTSTRAP_BUILD" "$MUSL_BOOTSTRAP_PREFIX"
	(
		cd "$MUSL_BOOTSTRAP_BUILD"
		CC="$NOV_GCC" AR="${CROSS_COMPILE}ar" RANLIB="${CROSS_COMPILE}ranlib" \
			"$MUSL_SOURCE/configure" --prefix="$MUSL_BOOTSTRAP_PREFIX" \
			--target=riscv64-linux-musl
		make -j "$JOBS"
		make install
	)
	cp "$MUSL_SOURCE_STAMP" "$MUSL_BOOTSTRAP_STAMP"
fi

# compiler-rt's clear-cache implementation includes Linux UAPI headers even
# though the bootstrap sysroot otherwise only needs musl headers and libc.
make -C "$ROOT/components/linux" O="$BASELINE/linux" ARCH=riscv \
	INSTALL_HDR_PATH="$MUSL_BOOTSTRAP_PREFIX" headers_install >/dev/null

printf '%s\n' '[io500-build] pinned rv64imafdc compiler-rt builtins'
BUILTINS_BUILD="${TARGET_ROOT}/compiler-rt-rv64imafdc-build"
BUILTINS_ARCHIVE="$BUILTINS_BUILD/lib/linux/libclang_rt.builtins-riscv64.a"
if [ ! -f "$BUILTINS_ARCHIVE" ]; then
	cmake -S "$LLVM_SOURCE/compiler-rt" -B "$BUILTINS_BUILD" -G Ninja \
		-DCMAKE_BUILD_TYPE=Release \
		-DCMAKE_SYSTEM_NAME=Linux \
		-DCMAKE_SYSTEM_PROCESSOR=riscv64 \
		-DCMAKE_SYSROOT="$MUSL_BOOTSTRAP_PREFIX" \
		-DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY \
		-DCMAKE_C_COMPILER="$(command -v "$CLANG")" \
		-DCMAKE_C_COMPILER_TARGET=riscv64-unknown-linux-musl \
		-DCMAKE_ASM_COMPILER="$(command -v "$CLANG")" \
		-DCMAKE_ASM_COMPILER_TARGET=riscv64-unknown-linux-musl \
		-DCMAKE_C_FLAGS='-march=rv64imafdc -mabi=lp64d' \
		-DCMAKE_ASM_FLAGS='-march=rv64imafdc -mabi=lp64d' \
		-DCMAKE_AR="$(command -v "$LLVM_AR")" \
		-DCMAKE_RANLIB="$(command -v "$LLVM_RANLIB")" \
		-DCOMPILER_RT_DEFAULT_TARGET_ONLY=ON \
		-DCOMPILER_RT_BAREMETAL_BUILD=ON \
		-DCOMPILER_RT_BUILD_BUILTINS=ON \
		-DCOMPILER_RT_BUILD_CRT=OFF \
		-DCOMPILER_RT_BUILD_SANITIZERS=OFF \
		-DCOMPILER_RT_BUILD_XRAY=OFF \
		-DCOMPILER_RT_BUILD_LIBFUZZER=OFF \
		-DCOMPILER_RT_BUILD_PROFILE=OFF \
		-DCOMPILER_RT_BUILD_ORC=OFF \
		-DCOMPILER_RT_INCLUDE_TESTS=OFF
	cmake --build "$BUILTINS_BUILD" --target clang_rt.builtins-riscv64 \
		-j "$JOBS"
fi
[ -s "$BUILTINS_ARCHIVE" ] || die "missing compiler-rt builtins: $BUILTINS_ARCHIVE"
require_sifive_u_isa "$BUILTINS_ARCHIVE"

printf '%s\n' '[io500-build] final musl runtime with compiler-rt'
MUSL_BUILD="${TARGET_ROOT}/musl-rv64imafdc-compiler-rt-build"
MUSL_PREFIX="${TARGET_ROOT}/musl-rv64imafdc-compiler-rt"
MUSL_FINAL_STAMP="$MUSL_PREFIX/.legofs-musl-source"
if [ ! -x "$MUSL_PREFIX/bin/musl-gcc" ] ||
	! cmp -s "$MUSL_SOURCE_STAMP" "$MUSL_FINAL_STAMP" ||
	! grep -Fqx "prefix = $MUSL_PREFIX" "$MUSL_BUILD/config.mak" 2>/dev/null ||
	! grep -Fqx "LIBCC = $BUILTINS_ARCHIVE" "$MUSL_BUILD/config.mak" 2>/dev/null ||
	! grep -Fq -- "-specs \"$MUSL_PREFIX/lib/musl-gcc.specs\"" \
		"$MUSL_PREFIX/bin/musl-gcc" 2>/dev/null; then
	rm -rf -- "$MUSL_BUILD" "$MUSL_PREFIX"
	mkdir -p "$MUSL_BUILD" "$MUSL_PREFIX"
	(
		cd "$MUSL_BUILD"
		CC="$NOV_GCC" AR="${CROSS_COMPILE}ar" RANLIB="${CROSS_COMPILE}ranlib" \
			"$MUSL_SOURCE/configure" --prefix="$MUSL_PREFIX" \
			--target=riscv64-linux-musl "LIBCC=$BUILTINS_ARCHIVE"
		make -j "$JOBS"
		make install
	)
	cp "$MUSL_SOURCE_STAMP" "$MUSL_FINAL_STAMP"
fi
grep -Fqx "LIBCC = $BUILTINS_ARCHIVE" "$MUSL_BUILD/config.mak" ||
	die 'final musl was not linked with the pinned compiler-rt archive'
MUSL_BUILTINS="$MUSL_PREFIX/lib/libgcc.a"
MUSL_EMPTY_LIBGCC_EH="$MUSL_PREFIX/lib/libgcc_eh.a"
install -m 0644 "$BUILTINS_ARCHIVE" "$MUSL_BUILTINS"
EMPTY_LIBGCC_EH="$TARGET_ROOT/empty-libgcc_eh.a"
if [ ! -f "$EMPTY_LIBGCC_EH" ]; then
	"${CROSS_COMPILE}ar" rcs "$EMPTY_LIBGCC_EH"
fi
[ -z "$("${CROSS_COMPILE}ar" t "$EMPTY_LIBGCC_EH")" ] ||
	die "compiler runtime placeholder is not empty: $EMPTY_LIBGCC_EH"
install -m 0644 "$EMPTY_LIBGCC_EH" "$MUSL_EMPTY_LIBGCC_EH"

# The stock musl GCC specs use libgcc.a%s.  GCC resolves that %s suffix in its
# own installation before honoring *link_libgcc -L paths, which silently
# selected Ubuntu's B/V-enabled libgcc.  Bind absolute archives so every
# downstream static or shared link uses the ISA-checked compiler runtime.
MUSL_SPECS="$MUSL_PREFIX/lib/musl-gcc.specs"
sed -i \
	"s|^libgcc\\.a%s %:if-exists(libgcc_eh\\.a%s)$|$MUSL_BUILTINS $MUSL_EMPTY_LIBGCC_EH|" \
	"$MUSL_SPECS"
grep -Fqx "$MUSL_BUILTINS $MUSL_EMPTY_LIBGCC_EH" "$MUSL_SPECS" ||
	die 'musl GCC specs do not bind the pinned compiler runtime'
MUSL_CC="$MUSL_PREFIX/bin/musl-gcc"
export REALGCC="$NOV_GCC"
require_sifive_u_isa "$MUSL_PREFIX/lib/libc.so"
MUSL_ECALL_GATE="$RESULTS/musl-ecall-gate.txt"
require_musl_hotpatch_padding "$MUSL_PREFIX/lib/libc.so" "$MUSL_ECALL_GATE"
make -C "$ROOT/components/linux" O="$BASELINE/linux" ARCH=riscv \
	INSTALL_HDR_PATH="$MUSL_PREFIX" headers_install >/dev/null

printf '%s\n' '[io500-build] LLVM libunwind for dynamic Rust musl payload'
LIBUNWIND_BUILD="$TARGET_ROOT/libunwind-rv64imafdc-compiler-rt-build"
LIBUNWIND_PREFIX="$TARGET_ROOT/libunwind-rv64imafdc-compiler-rt"
if [ ! -e "$LIBUNWIND_PREFIX/lib/libunwind.so.1" ]; then
	cmake -S "$LLVM_SOURCE/runtimes" -B "$LIBUNWIND_BUILD" -G Ninja \
		-DLLVM_ENABLE_RUNTIMES=libunwind \
		-DCMAKE_BUILD_TYPE=Release \
		-DCMAKE_SYSTEM_NAME=Linux \
		-DCMAKE_SYSTEM_PROCESSOR=riscv64 \
		-DCMAKE_C_COMPILER="$MUSL_CC" \
		-DCMAKE_CXX_COMPILER="$MUSL_CC" \
		-DCMAKE_ASM_COMPILER="$MUSL_CC" \
		-DCMAKE_LINKER="$(command -v "${CROSS_COMPILE}ld")" \
		-DCMAKE_AR="$(command -v "${CROSS_COMPILE}ar")" \
		-DCMAKE_RANLIB="$(command -v "${CROSS_COMPILE}ranlib")" \
		-DCMAKE_INSTALL_PREFIX="$LIBUNWIND_PREFIX" \
		-DLIBUNWIND_ENABLE_SHARED=ON \
		-DLIBUNWIND_ENABLE_STATIC=ON \
		-DLIBUNWIND_ENABLE_ASSERTIONS=OFF \
		-DLIBUNWIND_INCLUDE_TESTS=OFF \
		-DLIBUNWIND_USE_COMPILER_RT=OFF
	cmake --build "$LIBUNWIND_BUILD" -j "$JOBS"
	cmake --install "$LIBUNWIND_BUILD"
fi
ln -sf libunwind.so.1 "$LIBUNWIND_PREFIX/lib/libgcc_s.so"
require_sifive_u_isa "$LIBUNWIND_PREFIX/lib/libunwind.so.1"

printf '%s\n' '[io500-build] static BusyBox guest base'
BUSYBOX_BUILD="$TARGET_ROOT/busybox-rv64imafdc-compiler-rt-build"
mkdir -p "$BUSYBOX_BUILD"
make -C "$SOURCES/busybox" O="$BUSYBOX_BUILD" \
	CROSS_COMPILE="$CROSS_COMPILE" CC="$MUSL_CC" defconfig >/dev/null
sed -i \
	-e 's/^# CONFIG_STATIC is not set/CONFIG_STATIC=y/' \
	-e 's/^CONFIG_TC=y/# CONFIG_TC is not set/' \
	-e 's/^CONFIG_SHA1_HWACCEL=y/# CONFIG_SHA1_HWACCEL is not set/' \
	-e 's/^CONFIG_SHA256_HWACCEL=y/# CONFIG_SHA256_HWACCEL is not set/' \
	"$BUSYBOX_BUILD/.config"
make -C "$SOURCES/busybox" O="$BUSYBOX_BUILD" \
	CROSS_COMPILE="$CROSS_COMPILE" CC="$MUSL_CC" \
	KCFLAGS='-march=rv64imafdc -mabi=lp64d' oldconfig </dev/null >/dev/null
rm -f -- "$BUSYBOX_BUILD/busybox"
make -C "$SOURCES/busybox" O="$BUSYBOX_BUILD" \
	CROSS_COMPILE="$CROSS_COMPILE" CC="$MUSL_CC" \
	KCFLAGS='-march=rv64imafdc -mabi=lp64d' -j "$JOBS" >/dev/null
require_sifive_u_isa "$BUSYBOX_BUILD/busybox"

printf '%s\n' '[io500-build] MPICH 4.3.2 ch3:sock + Hydra'
git -C "$SOURCES/mpich" submodule update --init
MPICH_LD="$(command -v "${CROSS_COMPILE}ld")" ||
	die "missing ${CROSS_COMPILE}ld linker wrapper"
"$MPICH_LD" -v 2>&1 | grep -Fq 'GNU ld' ||
	die "MPICH linker is not an executable GNU ld: $MPICH_LD"
if [ ! -x "$SOURCES/mpich/configure" ]; then
	(
		cd "$SOURCES/mpich"
		./autogen.sh
	)
fi
if [ ! -x "$MPICH_PREFIX/bin/mpiexec.hydra" ] ||
	! grep -Fqx "prefix=$MPICH_PREFIX" "$MPICH_PREFIX/bin/mpicc" 2>/dev/null ||
	! grep -Fqx "CC=\"$MUSL_CC\"" "$MPICH_PREFIX/bin/mpicc" 2>/dev/null; then
	mkdir -p "$MPICH_BUILD"
	(
		cd "$MPICH_BUILD"
		make distclean >/dev/null 2>&1 || true
		"$SOURCES/mpich/configure" \
			--build=x86_64-pc-linux-gnu --host=riscv64-linux-gnu \
			--prefix="$MPICH_PREFIX" \
			CC="$MUSL_CC" AR="${CROSS_COMPILE}ar" \
			RANLIB="${CROSS_COMPILE}ranlib" LD="$MPICH_LD" \
			CPPFLAGS='-include signal.h' \
			--with-device=ch3:sock --with-pm=hydra \
			--disable-fortran --disable-cxx --disable-romio \
			--with-hwloc=no --enable-shared --enable-static \
			ac_cv_func_signal=yes ac_cv_func_sigaction=yes
		make -j "$JOBS"
		make install
	)
fi
test -e "$MPICH_PREFIX/lib/libmpi.so.0.0.0" ||
	die 'MPICH shared-library payload was not produced'
MPICC="$MPICH_PREFIX/bin/mpicc"

printf '%s\n' '[io500-build] real-MPI IOR, pfind, IO500, and verifier'
IOR_SOURCE="$SOURCES/io500/build/ior"
if [ ! -x "$IOR_SOURCE/configure" ]; then
	(
		cd "$IOR_SOURCE"
		./bootstrap
	)
fi
make -C "$IOR_SOURCE" distclean >/dev/null 2>&1 || true
(
	cd "$IOR_SOURCE"
	./configure --build=x86_64-pc-linux-gnu --host=riscv64-linux-gnu \
		CC="$MPICC" MPICC="$MPICC" --with-mpiio=no \
		--without-cuda --without-gpuDirect --prefix="$SOURCES/io500"
	make -C src -j "$JOBS" install
)
(
	cd "$SOURCES/io500/build/pfind"
	CC="$MPICC" ./compile.sh
)
(
	cd "$SOURCES/io500"
	make clean >/dev/null 2>&1 || true
	make -j "$JOBS" CC="$MPICC" AR="${CROSS_COMPILE}ar"
)

printf '%s\n' '[io500-build] workspace RISC-V syscall interceptor'
SYSINT_ROOT="$SOURCES/syscall-intercept" \
SYSINT_CAPSTONE_MIRROR="$SOURCES/capstone.git" \
SYSINT_BUILD_ROOT="$SYSINT_BUILD_ROOT" SYSINT_JOBS="$JOBS" \
RISCV_CC="$MUSL_CC" \
	"$ROOT/scripts/build_syscall_intercept_riscv.sh"

printf '%s\n' '[io500-build] LegoFS preload library and baseline static server tools'
export CARGO_TARGET_DIR="$CARGO_TARGET"
export CC_riscv64gc_unknown_linux_musl="$MUSL_CC"
export CARGO_TARGET_RISCV64GC_UNKNOWN_LINUX_MUSL_LINKER="$MUSL_CC"
LIBSYSCALL_INTERCEPT_LIB_DIR="$SYSINT_BUILD_ROOT/build" \
RUSTFLAGS="-C target-feature=-crt-static -C panic=abort \
-C link-arg=-L$LIBUNWIND_PREFIX/lib" \
	cargo build --manifest-path "$LEGOFS_ROOT/Cargo.toml" \
	--release --target "$RUST_MUSL_TARGET" -p badfs-intercept \
	--features syscall-intercept-backend

printf '%s\n' '[io500-build] MPI placement probe'
"$MPICC" -O2 -Wall -Wextra -Werror "$ROOT/guest/mpi_hello.c" \
	-Wl,-rpath,/payload/lib \
	-o "$TARGET_ROOT/mpi-hello"
"$MUSL_CC" -O2 -Wall -Wextra -Werror \
	-march=rv64imafdc -mabi=lp64d \
	"$ROOT/guest/export_io500_results.c" \
	-o "$TARGET_ROOT/export-io500-results"

printf '%s\n' '[io500-build] read-only payload image'
rm -rf -- "$PAYLOAD_ROOT"
mkdir -p "$PAYLOAD_ROOT/bin" "$PAYLOAD_ROOT/lib" "$PAYLOAD_ROOT/etc"
install -m 0755 "$SOURCES/io500/io500" "$PAYLOAD_ROOT/bin/io500"
install -m 0755 "$SOURCES/io500/io500-verify" "$PAYLOAD_ROOT/bin/io500-verify"
install -m 0755 "$MPICH_PREFIX/bin/mpiexec.hydra" "$PAYLOAD_ROOT/bin/mpiexec.hydra"
install -m 0755 "$MPICH_PREFIX/bin/hydra_pmi_proxy" "$PAYLOAD_ROOT/bin/hydra_pmi_proxy"
install -m 0755 "$TARGET_ROOT/mpi-hello" "$PAYLOAD_ROOT/bin/mpi-hello"
install -m 0755 "$TARGET_ROOT/export-io500-results" \
	"$PAYLOAD_ROOT/bin/export-io500-results"
install -m 0755 "$ROOT/guest/legofs_io500_rank.sh" "$PAYLOAD_ROOT/bin/run-io500-rank"
install -m 0755 "$BASELINE/legofs-bin/badfs-server" "$PAYLOAD_ROOT/bin/badfs-server"
install -m 0755 "$BASELINE/legofs-bin/badfs-bench" "$PAYLOAD_ROOT/bin/badfs-bench"
install -m 0755 "$CARGO_TARGET/$RUST_MUSL_TARGET/release/libbadfs_intercept.so" \
	"$PAYLOAD_ROOT/lib/libbadfs_intercept.so"
cp -a "$MPICH_PREFIX/lib/"libmpi.so* "$PAYLOAD_ROOT/lib/"
cp -a "$SYSINT_BUILD_ROOT/build/"libsyscall_intercept.so* "$PAYLOAD_ROOT/lib/"
cp -a "$LIBUNWIND_PREFIX/lib/"libunwind.so* "$PAYLOAD_ROOT/lib/"
for stage in tiny easy-smoke hard-smoke metadata-smoke rnd4k scc standard; do
	install -m 0644 "$ROOT/configs/io500-$stage.ini" "$PAYLOAD_ROOT/etc/io500-$stage.ini"
done
for index in $(seq 0 9); do
	printf '10.77.0.%s:1\n' "$((index + 10))"
done > "$PAYLOAD_ROOT/etc/clients"
PAYLOAD_IMAGE="$IMAGES/io500-payload.ext2"
truncate -s 768M "$PAYLOAD_IMAGE"
mke2fs -q -t ext2 -F -d "$PAYLOAD_ROOT" "$PAYLOAD_IMAGE"

printf '%s\n' '[io500-build] IO500 initramfs and Linux image'
rm -rf -- "$INITRAMFS"
mkdir -p "$INITRAMFS/bin" "$INITRAMFS/lib" "$INITRAMFS/dev" \
	"$INITRAMFS/proc" "$INITRAMFS/sys" "$INITRAMFS/run" "$INITRAMFS/tmp"
install -m 0755 "$BUSYBOX_BUILD/busybox" "$INITRAMFS/bin/busybox"
for applet in sh mount mkdir mknod cat tr basename readlink sleep ip hostname \
	kill sync poweroff reboot nc printf seq env chmod ls ps grep sed awk find; do
	ln -s busybox "$INITRAMFS/bin/$applet"
done
install -m 0755 "$ROOT/guest/legofs_io500_init.sh" "$INITRAMFS/init"
install -m 0755 "$MUSL_PREFIX/lib/libc.so" "$INITRAMFS/lib/ld-musl-riscv64.so.1"
ln -s ld-musl-riscv64.so.1 "$INITRAMFS/lib/libc.so"

LINUX_BUILD="$TARGET_ROOT/linux"
make -C "$ROOT/components/linux" O="$LINUX_BUILD" ARCH=riscv \
	CROSS_COMPILE="$CROSS_COMPILE" defconfig
ARCH=riscv CROSS_COMPILE="$CROSS_COMPILE" \
	"$ROOT/components/linux/scripts/kconfig/merge_config.sh" -m \
	-O "$LINUX_BUILD" "$LINUX_BUILD/.config" "$ROOT/configs/linux-cxl.config"
"$ROOT/components/linux/scripts/config" --file "$LINUX_BUILD/.config" \
	--set-str CONFIG_INITRAMFS_SOURCE "$INITRAMFS"
make -C "$ROOT/components/linux" O="$LINUX_BUILD" ARCH=riscv \
	CROSS_COMPILE="$CROSS_COMPILE" olddefconfig
for option in CONFIG_CXL_BUS CONFIG_CXL_PCI CONFIG_CXL_REGION CONFIG_DEV_DAX \
	CONFIG_DEV_DAX_CXL CONFIG_VIRTIO_BLK CONFIG_VIRTIO_NET CONFIG_EXT4_FS \
	CONFIG_INET CONFIG_PACKET CONFIG_UNIX; do
	grep -qx "$option=y" "$LINUX_BUILD/.config" || die "missing kernel option: $option"
done
make -C "$ROOT/components/linux" O="$LINUX_BUILD" ARCH=riscv \
	CROSS_COMPILE="$CROSS_COMPILE" -j "$JOBS" Image
install -m 0644 "$LINUX_BUILD/arch/riscv/boot/Image" "$PLATFORM/linux-io500-Image"

for binary in "$PAYLOAD_ROOT/bin/io500" "$PAYLOAD_ROOT/bin/io500-verify" \
	"$PAYLOAD_ROOT/bin/mpiexec.hydra" "$PAYLOAD_ROOT/bin/hydra_pmi_proxy" \
	"$PAYLOAD_ROOT/bin/mpi-hello" "$PAYLOAD_ROOT/bin/export-io500-results" \
	"$PAYLOAD_ROOT/bin/badfs-server" \
	"$PAYLOAD_ROOT/bin/badfs-bench" "$PAYLOAD_ROOT/lib/libmpi.so" \
	"$PAYLOAD_ROOT/lib/libunwind.so" \
	"$PAYLOAD_ROOT/lib/libsyscall_intercept.so" \
	"$PAYLOAD_ROOT/lib/libbadfs_intercept.so"; do
	file -L "$binary" | grep -q 'RISC-V' || die "payload binary is not RISC-V: $binary"
	require_sifive_u_isa "$binary"
done
for binary in "$PAYLOAD_ROOT/bin/io500" \
	"$PAYLOAD_ROOT/bin/export-io500-results"; do
	"${CROSS_COMPILE}readelf" -l "$binary" |
		grep -q '/lib/ld-musl-riscv64.so.1' ||
		die "$binary does not use the clean musl loader"
done
for binary in "$PAYLOAD_ROOT/bin/io500" "$PAYLOAD_ROOT/bin/io500-verify" \
	"$PAYLOAD_ROOT/bin/mpiexec.hydra" "$PAYLOAD_ROOT/bin/hydra_pmi_proxy" \
	"$PAYLOAD_ROOT/bin/mpi-hello" "$PAYLOAD_ROOT/bin/export-io500-results" \
	"$PAYLOAD_ROOT/lib/libmpi.so" \
	"$PAYLOAD_ROOT/lib/libunwind.so" \
	"$PAYLOAD_ROOT/lib/libsyscall_intercept.so" \
	"$PAYLOAD_ROOT/lib/libbadfs_intercept.so"; do
	reject_glibc_versions "$binary"
done
require_needed "$PAYLOAD_ROOT/lib/libbadfs_intercept.so" \
	libsyscall_intercept.so.0
require_syscall_intercept_abi "$PAYLOAD_ROOT/lib/libbadfs_intercept.so"
require_needed "$PAYLOAD_ROOT/lib/libbadfs_intercept.so" libunwind.so.1
require_needed "$PAYLOAD_ROOT/lib/libbadfs_intercept.so" libc.so
require_needed "$PAYLOAD_ROOT/lib/libunwind.so" libc.so
require_needed "$PAYLOAD_ROOT/bin/export-io500-results" libc.so
require_unwind_provider "$PAYLOAD_ROOT/lib/libbadfs_intercept.so" \
	"$PAYLOAD_ROOT/lib/libunwind.so"
for binary in "$PAYLOAD_ROOT/bin/badfs-server" "$PAYLOAD_ROOT/bin/badfs-bench"; do
	"${CROSS_COMPILE}readelf" -l "$binary" | grep -q INTERP &&
		die "static LegoFS binary has an interpreter: $binary"
done

printf '%s\n' \
	"mpich=$MPICH_COMMIT" "io500=$IO500_COMMIT" "ior=$IOR_COMMIT" \
	"pfind=$PFIND_COMMIT" "busybox=$BUSYBOX_COMMIT" \
	"syscall_intercept=$SYSINT_COMMIT" "capstone=$CAPSTONE_COMMIT" \
	"musl_version=$MUSL_VERSION" "musl_tarball_sha256=$MUSL_TARBALL_SHA256" \
	"musl_hotpatch_padding_sha256=$MUSL_HOTPATCH_PADDING_SHA256" \
	"llvm_tag=$LLVM_TAG" "llvm=$LLVM_COMMIT" \
	> "$RESULTS/dependency-versions.txt"

python3 "$ROOT/scripts/write_manifest.py" --root "$ROOT" \
	--output "$RESULTS/build-manifest.json" \
	--source "legofs=$LEGOFS_ROOT" \
	--source "cxlmemsim=$ROOT/components/cxlmemsim" \
	--compiler "riscv_gcc=${CROSS_COMPILE}gcc --version" \
	--compiler "riscv_nov_gcc=$NOV_GCC --version" \
	--compiler "riscv_musl_gcc=$MUSL_CC --version" \
	--compiler "clang=$CLANG --version" \
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
	--artifact "badfs_server=$PAYLOAD_ROOT/bin/badfs-server" \
	--artifact "badfs_bench=$PAYLOAD_ROOT/bin/badfs-bench" \
	--artifact "compiler_rt_builtins=$BUILTINS_ARCHIVE" \
	--artifact "libunwind=$PAYLOAD_ROOT/lib/libunwind.so.1" \
	--artifact "badfs_intercept=$PAYLOAD_ROOT/lib/libbadfs_intercept.so" \
	--artifact "syscall_intercept=$PAYLOAD_ROOT/lib/libsyscall_intercept.so.0.1.0" \
	--artifact "syscall_intercept_manifest=$SYSINT_BUILD_ROOT/manifest.txt" \
	--artifact "dependency_versions=$RESULTS/dependency-versions.txt" \
	--artifact "libpmem_build=$ROOT/out/legofs-type3/results/libpmem-debs.txt" \
	--artifact "cxlmemsim_build_deps=$ROOT/out/legofs-type3/results/cxlmemsim-build-debs.txt" \
	--artifact "uboot_build_deps=$ROOT/out/legofs-type3/results/uboot-build-debs.txt" \
	--artifact "isa_gate=$ISA_REPORT" \
	--artifact "musl_ecall_gate=$MUSL_ECALL_GATE" \
	--artifact "musl_hotpatch_padding_patch=$MUSL_HOTPATCH_PADDING_PATCH"
printf '[io500-build] manifest %s\n' "$RESULTS/build-manifest.json"
