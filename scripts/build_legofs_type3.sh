#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${ROOT}/out/legofs-type3"
BUILD="${OUT}/build"
IMAGES="${OUT}/images"
RESULTS="${OUT}/results"
LOGS="${OUT}/logs"
INITRAMFS="${IMAGES}/initramfs"
CARGO_TARGET="${BUILD}/cargo"
LEGOFS_BIN="${BUILD}/legofs-bin"
CROSS_COMPILE="${CROSS_COMPILE:-riscv64-linux-gnu-}"
RUST_TARGET="riscv64gc-unknown-linux-musl"
MUSL_VERSION=1.2.5
MUSL_SHA256=a9a118bbe84d8764da0ea0d28b3ab3fae8477fc7e4085d90102b8596fc7c75e4
MUSL_SOURCE_ROOT="${OUT}/toolchain-src"
MUSL_SOURCE="${MUSL_SOURCE_ROOT}/musl-${MUSL_VERSION}"
MUSL_TARBALL="${MUSL_SOURCE_ROOT}/musl-${MUSL_VERSION}.tar.gz"
MUSL_BUILD="${BUILD}/musl-rv64gc"
MUSL_PREFIX="${OUT}/toolchain/musl-rv64gc"
MUSL_CC="${MUSL_PREFIX}/bin/musl-gcc"
JOBS="$(getconf _NPROCESSORS_ONLN 2>/dev/null || printf '1\n')"

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
	*)
		die "unknown argument: $1"
		;;
	esac
done
[[ "${JOBS}" =~ ^[1-9][0-9]*$ ]] || die "jobs must be a positive integer"

for command in cargo rustc rustup "${CROSS_COMPILE}gcc" \
	"${CROSS_COMPILE}readelf" "${CROSS_COMPILE}strip" cmake ninja make mke2fs \
	debugfs truncate python3 wget sha256sum tar install stat cmp; do
	command -v "${command}" >/dev/null || die "required command is missing: ${command}"
done
if ! rustup target list --installed | grep -qx "${RUST_TARGET}"; then
	printf '%s\n' "error: Rust target ${RUST_TARGET} is not installed" >&2
	printf '%s\n' "remediation: rustup target add ${RUST_TARGET}" >&2
	exit 2
fi

mkdir -p \
	"${BUILD}/qemu" "${BUILD}/opensbi" "${BUILD}/u-boot" \
	"${BUILD}/linux" "${BUILD}/cxlmemsim" "${CARGO_TARGET}" \
	"${LEGOFS_BIN}" \
	"${MUSL_SOURCE_ROOT}" "${MUSL_BUILD}" "${MUSL_PREFIX}" \
	"${INITRAMFS}" "${IMAGES}" "${RESULTS}" "${LOGS}"
exec > >(tee -a "${LOGS}/build.log") 2>&1

printf '%s\n' '[legofs-build] pinned RV64GC musl sysroot'
if [[ ! -f "${MUSL_TARBALL}" ]]; then
	wget -O "${MUSL_TARBALL}.tmp" \
		"https://musl.libc.org/releases/musl-${MUSL_VERSION}.tar.gz"
	mv "${MUSL_TARBALL}.tmp" "${MUSL_TARBALL}"
fi
printf '%s  %s\n' "${MUSL_SHA256}" "${MUSL_TARBALL}" | sha256sum -c -
if [[ ! -x "${MUSL_SOURCE}/configure" ]]; then
	tar -xzf "${MUSL_TARBALL}" -C "${MUSL_SOURCE_ROOT}"
fi
if [[ ! -x "${MUSL_CC}" ]]; then
	(
		cd "${MUSL_BUILD}"
		"${MUSL_SOURCE}/configure" --prefix="${MUSL_PREFIX}" \
			--target=riscv64-linux-musl CROSS_COMPILE="${CROSS_COMPILE}" \
			CFLAGS='-O2 -march=rv64gc -mabi=lp64d'
		make -j "${JOBS}"
		make install
	)
fi

export CARGO_TARGET_DIR="${CARGO_TARGET}"
export CC_riscv64gc_unknown_linux_musl="${MUSL_CC}"
export CARGO_TARGET_RISCV64GC_UNKNOWN_LINUX_MUSL_LINKER="${MUSL_CC}"
export RUSTFLAGS='-C target-feature=+crt-static -C link-arg=-march=rv64gc -C link-arg=-mabi=lp64d'

printf '%s\n' '[legofs-build] static RISC-V server and benchmark'
cargo build --manifest-path "${ROOT}/components/legofs/Cargo.toml" --release \
	--target "${RUST_TARGET}" -p badfs-server -p badfs-bench
RUSTFLAGS= cargo test --manifest-path "${ROOT}/components/legofs/Cargo.toml" -p badfs-bench \
	benchmark_mode_uses_semantic_values_and_rejects_unknown_input

badfs_server_unstripped="${CARGO_TARGET}/${RUST_TARGET}/release/badfs-server"
badfs_bench_unstripped="${CARGO_TARGET}/${RUST_TARGET}/release/badfs-bench"
badfs_server="${LEGOFS_BIN}/badfs-server"
badfs_bench="${LEGOFS_BIN}/badfs-bench"
install -m 0755 "${badfs_server_unstripped}" "${badfs_server}"
install -m 0755 "${badfs_bench_unstripped}" "${badfs_bench}"
"${CROSS_COMPILE}strip" --strip-debug "${badfs_server}" "${badfs_bench}"
for binary in "${badfs_server}" "${badfs_bench}"; do
	[[ -s "${binary}" ]] || die "missing Legofs binary: ${binary}"
	if "${CROSS_COMPILE}readelf" -l "${binary}" | grep -q INTERP; then
		die "Legofs binary has an ELF interpreter: ${binary}"
	fi
	if "${CROSS_COMPILE}readelf" -A "${binary}" | grep -q '_v'; then
		die "Legofs binary unexpectedly requires RVV: ${binary}"
	fi
done

guest_flags=(
	-O2 -std=c11 -Wall -Wextra -Werror -static -nostdlib -fno-builtin
	-fno-stack-protector -fno-pie -no-pie -march=rv64imafdc -mabi=lp64d
)
printf '%s\n' '[legofs-build] freestanding role-aware PID 1'
"${CROSS_COMPILE}gcc" "${guest_flags[@]}" \
	"${ROOT}/guest/legofs_node_init.c" -o "${INITRAMFS}/init"
chmod 0755 "${INITRAMFS}/init"
if "${CROSS_COMPILE}readelf" -l "${INITRAMFS}/init" | grep -q INTERP; then
	die "guest PID 1 has an ELF interpreter"
fi
if "${CROSS_COMPILE}readelf" -A "${INITRAMFS}/init" | grep -q '_v'; then
	die "guest PID 1 unexpectedly requires RVV"
fi

printf '%s\n' '[legofs-build] read-only ext2 payload image'
legofs_disk="${IMAGES}/legofs-type3.ext2"
payload_bytes=$(($(stat -c %s "${badfs_server}") + $(stat -c %s "${badfs_bench}")))
image_mib=$(((payload_bytes + 32 * 1024 * 1024 + 1024 * 1024 - 1) / (1024 * 1024)))
truncate -s "${image_mib}M" "${legofs_disk}"
mke2fs -q -t ext2 -F "${legofs_disk}"
debugfs -w -R "write ${badfs_server} /badfs-server" "${legofs_disk}"
debugfs -w -R "set_inode_field /badfs-server mode 0100755" "${legofs_disk}"
debugfs -w -R "write ${badfs_bench} /badfs-bench" "${legofs_disk}"
debugfs -w -R "set_inode_field /badfs-bench mode 0100755" "${legofs_disk}"
debugfs -R 'stat /badfs-server' "${legofs_disk}" | grep -q 'Mode:.*0755'
debugfs -R 'stat /badfs-bench' "${legofs_disk}" | grep -q 'Mode:.*0755'
verify_server="${BUILD}/verify-badfs-server"
verify_bench="${BUILD}/verify-badfs-bench"
debugfs -R "dump /badfs-server ${verify_server}" "${legofs_disk}"
debugfs -R "dump /badfs-bench ${verify_bench}" "${legofs_disk}"
cmp "${badfs_server}" "${verify_server}" || die 'badfs-server ext2 payload is incomplete'
cmp "${badfs_bench}" "${verify_bench}" || die 'badfs-bench ext2 payload is incomplete'

printf '%s\n' '[legofs-build] QEMU riscv64-softmmu with Type-3 MESI v2 BI'
(
	cd "${BUILD}/qemu"
	"${ROOT}/components/qemu/configure" --target-list=riscv64-softmmu \
		--disable-docs --disable-werror --extra-cflags=-Wno-error \
		--prefix="${BUILD}/qemu-install"
)
ninja -C "${BUILD}/qemu" -j "${JOBS}" qemu-system-riscv64

printf '%s\n' '[legofs-build] CXLMemSim MESI-v2 server'
cmake -S "${ROOT}/components/cxlmemsim" -B "${BUILD}/cxlmemsim" \
	-DCMAKE_BUILD_TYPE=Release
cmake --build "${BUILD}/cxlmemsim" --target cxlmemsim_server \
	--parallel "${JOBS}"

printf '%s\n' '[legofs-build] OpenSBI and CXL U-Boot'
make -C "${ROOT}/components/opensbi" O="${BUILD}/opensbi" \
	CROSS_COMPILE="${CROSS_COMPILE}" PLATFORM=generic \
	'platform-cflags-y=-std=gnu11' -j "${JOBS}"
make -C "${ROOT}/components/u-boot" O="${BUILD}/u-boot" \
	CROSS_COMPILE="${CROSS_COMPILE}" sifive_unleashed_qemu_cxl_defconfig
python3 "${ROOT}/scripts/prepare_uboot_pylibfdt.py" \
	--source "${ROOT}/components/u-boot/scripts/dtc/pylibfdt/libfdt.i_shipped" \
	--output "${BUILD}/u-boot/scripts/dtc/pylibfdt/libfdt.i"
make -C "${ROOT}/components/u-boot" O="${BUILD}/u-boot" \
	CROSS_COMPILE="${CROSS_COMPILE}" \
	OPENSBI="${BUILD}/opensbi/platform/generic/firmware/fw_dynamic.bin" \
	-j "${JOBS}"

printf '%s\n' '[legofs-build] Linux CXL devdax image'
make -C "${ROOT}/components/linux" O="${BUILD}/linux" \
	ARCH=riscv CROSS_COMPILE="${CROSS_COMPILE}" defconfig
ARCH=riscv CROSS_COMPILE="${CROSS_COMPILE}" \
	"${ROOT}/components/linux/scripts/kconfig/merge_config.sh" -m \
	-O "${BUILD}/linux" "${BUILD}/linux/.config" \
	"${ROOT}/configs/linux-cxl.config"
"${ROOT}/components/linux/scripts/config" --file "${BUILD}/linux/.config" \
	--set-str CONFIG_INITRAMFS_SOURCE "${INITRAMFS}"
make -C "${ROOT}/components/linux" O="${BUILD}/linux" \
	ARCH=riscv CROSS_COMPILE="${CROSS_COMPILE}" olddefconfig

required_kernel_options=(
	CONFIG_PCI CONFIG_PCIEPORTBUS CONFIG_EFI CONFIG_EFI_STUB CONFIG_RISCV_SBI
	CONFIG_NONPORTABLE CONFIG_HVC_RISCV_SBI CONFIG_CXL_BUS CONFIG_CXL_PCI
	CONFIG_CXL_ACPI CONFIG_CXL_MEM CONFIG_CXL_PORT CONFIG_CXL_REGION
	CONFIG_DAX CONFIG_DEV_DAX CONFIG_DEV_DAX_CXL CONFIG_NET CONFIG_INET
	CONFIG_UNIX CONFIG_PACKET CONFIG_VIRTIO CONFIG_VIRTIO_PCI CONFIG_VIRTIO_BLK
	CONFIG_VIRTIO_NET CONFIG_IP_PNP CONFIG_IP_PNP_DHCP CONFIG_EXT4_FS
	CONFIG_EXT4_USE_FOR_EXT2 CONFIG_DEVTMPFS CONFIG_DEVTMPFS_MOUNT
	CONFIG_BLK_DEV_INITRD CONFIG_PROC_FS CONFIG_SYSFS CONFIG_TMPFS CONFIG_BINFMT_ELF
)
for option in "${required_kernel_options[@]}"; do
	grep -qx "${option}=y" "${BUILD}/linux/.config" ||
		die "required kernel option is not built in: ${option}"
done
grep -Fqx "CONFIG_INITRAMFS_SOURCE=\"${INITRAMFS}\"" "${BUILD}/linux/.config" ||
	die 'CONFIG_INITRAMFS_SOURCE does not match the Legofs PID 1 directory'
make -C "${ROOT}/components/linux" O="${BUILD}/linux" \
	ARCH=riscv CROSS_COMPILE="${CROSS_COMPILE}" -j "${JOBS}" Image

qemu="${BUILD}/qemu/qemu-system-riscv64"
opensbi="${BUILD}/opensbi/platform/generic/firmware/fw_dynamic.bin"
u_boot="${BUILD}/u-boot/u-boot.bin"
linux_legofs="${BUILD}/linux/arch/riscv/boot/Image"
cxlmemsim_server="${BUILD}/cxlmemsim/cxlmemsim_server"
for artifact in "${qemu}" "${opensbi}" "${u_boot}" "${linux_legofs}" \
	"${legofs_disk}" "${badfs_server}" "${badfs_bench}" "${cxlmemsim_server}"; do
	[[ -s "${artifact}" ]] || die "missing build artifact: ${artifact}"
done

python3 "${ROOT}/scripts/write_manifest.py" \
	--root "${ROOT}" --output "${RESULTS}/build-manifest.json" \
	--compiler "rustc=rustc --version" \
	--compiler "cargo=cargo --version" \
	--compiler "riscv_musl_gcc=${MUSL_CC} --version" \
	--compiler "qemu=${qemu} --version" \
	--artifact "qemu=${qemu}" \
	--artifact "opensbi=${opensbi}" \
	--artifact "u_boot=${u_boot}" \
	--artifact "linux_legofs=${linux_legofs}" \
	--artifact "legofs_disk=${legofs_disk}" \
	--artifact "badfs_server=${badfs_server}" \
	--artifact "badfs_bench=${badfs_bench}" \
	--artifact "cxlmemsim_server=${cxlmemsim_server}"

printf '%s\n' "[legofs-build] manifest ${RESULTS}/build-manifest.json"
