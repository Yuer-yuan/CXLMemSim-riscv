#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${ROOT}/out"
BUILD="${OUT}/build"
IMAGES="${OUT}/images"
RESULTS="${OUT}/results"
LOGS="${OUT}/logs"
RUNTIME_BIN="${OUT}/runtime-bin"
CROSS_COMPILE="${CROSS_COMPILE:-riscv64-linux-gnu-}"
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

[[ "${JOBS}" =~ ^[1-9][0-9]*$ ]] ||
	die "jobs must be a positive integer"

mkdir -p \
	"${BUILD}/qemu" \
	"${BUILD}/u-boot" \
	"${BUILD}/opensbi" \
	"${BUILD}/linux" \
	"${BUILD}/cxlmemsim" \
	"${IMAGES}/initramfs" \
	"${RESULTS}" \
	"${LOGS}" \
	"${RUNTIME_BIN}"

exec > >(tee -a "${LOGS}/build.log") 2>&1

printf '%s\n' "[build] QEMU riscv64-softmmu"
(
	cd "${BUILD}/qemu"
	"${ROOT}/components/qemu/configure" \
		--target-list=riscv64-softmmu \
		--disable-docs \
		--disable-werror \
		--prefix="${BUILD}/qemu-install"
)
ninja -C "${BUILD}/qemu" -j "${JOBS}" qemu-system-riscv64

printf '%s\n' "[build] OpenSBI generic fw_dynamic"
make -C "${ROOT}/components/opensbi" O="${BUILD}/opensbi" \
	CROSS_COMPILE="${CROSS_COMPILE}" PLATFORM=generic \
	'platform-cflags-y=-std=gnu11' -j "${JOBS}"

printf '%s\n' "[build] U-Boot sifive_unleashed_qemu_cxl_defconfig"
make -C "${ROOT}/components/u-boot" O="${BUILD}/u-boot" \
	CROSS_COMPILE="${CROSS_COMPILE}" \
	sifive_unleashed_qemu_cxl_defconfig
python3 "${ROOT}/scripts/prepare_uboot_pylibfdt.py" \
	--source \
	"${ROOT}/components/u-boot/scripts/dtc/pylibfdt/libfdt.i_shipped" \
	--output "${BUILD}/u-boot/scripts/dtc/pylibfdt/libfdt.i"
make -C "${ROOT}/components/u-boot" O="${BUILD}/u-boot" \
	CROSS_COMPILE="${CROSS_COMPILE}" \
	OPENSBI="${BUILD}/opensbi/platform/generic/firmware/fw_dynamic.bin" \
	-j "${JOBS}"

printf '%s\n' "[build] CXLMemSim server"
cmake -S "${ROOT}/components/cxlmemsim" -B "${BUILD}/cxlmemsim" \
	-DCMAKE_BUILD_TYPE=Release
cmake --build "${BUILD}/cxlmemsim" \
	--target cxlmemsim_server --parallel "${JOBS}"

guest_flags=(
	-O2
	-std=c11
	-Wall
	-Wextra
	-Werror
	-static
	-nostdlib
	-fno-builtin
	-fno-stack-protector
	-fno-pie
	-no-pie
	-march=rv64imafdc
	-mabi=lp64d
)

printf '%s\n' "[build] freestanding guest payloads"
"${CROSS_COMPILE}gcc" "${guest_flags[@]}" \
	-DCXL_BENCH_FREESTANDING \
	"${ROOT}/guest/cxl_mmap_bench.c" \
	-o "${IMAGES}/cxl_mmap_bench"
"${CROSS_COMPILE}gcc" "${guest_flags[@]}" \
	"${ROOT}/guest/init.c" \
	-o "${IMAGES}/initramfs/init"
chmod 0755 "${IMAGES}/cxl_mmap_bench" "${IMAGES}/initramfs/init"

for guest_binary in \
	"${IMAGES}/cxl_mmap_bench" \
	"${IMAGES}/initramfs/init"; do
	"${CROSS_COMPILE}readelf" -h "${guest_binary}" |
		grep -q 'Machine:.*RISC-V'
	if "${CROSS_COMPILE}readelf" -l "${guest_binary}" |
		grep -q 'INTERP'; then
		die "guest binary is dynamically linked: ${guest_binary}"
	fi
	if "${CROSS_COMPILE}readelf" -A "${guest_binary}" |
		grep -q '_v'; then
		die "guest binary unexpectedly requires RVV: ${guest_binary}"
	fi
done

printf '%s\n' "[build] Linux CXL Image with built-in initramfs"
make -C "${ROOT}/components/linux" O="${BUILD}/linux" \
	ARCH=riscv CROSS_COMPILE="${CROSS_COMPILE}" defconfig
ARCH=riscv CROSS_COMPILE="${CROSS_COMPILE}" \
	"${ROOT}/components/linux/scripts/kconfig/merge_config.sh" \
	-O "${BUILD}/linux" \
	"${BUILD}/linux/.config" \
	"${ROOT}/configs/linux-cxl.config"
"${ROOT}/components/linux/scripts/config" \
	--file "${BUILD}/linux/.config" \
	--set-str CONFIG_INITRAMFS_SOURCE "${IMAGES}/initramfs"
make -C "${ROOT}/components/linux" O="${BUILD}/linux" \
	ARCH=riscv CROSS_COMPILE="${CROSS_COMPILE}" olddefconfig

required_kernel_options=(
	CONFIG_PCI
	CONFIG_PCIEPORTBUS
	CONFIG_EFI
	CONFIG_EFI_STUB
	CONFIG_CXL_BUS
	CONFIG_CXL_PCI
	CONFIG_CXL_ACPI
	CONFIG_CXL_PORT
	CONFIG_CXL_REGION
	CONFIG_CXL_TYPE2_ACCEL
	CONFIG_DEVTMPFS
	CONFIG_DEVTMPFS_MOUNT
	CONFIG_DEVMEM
	CONFIG_VIRTIO
	CONFIG_VIRTIO_PCI
	CONFIG_VIRTIO_BLK
	CONFIG_EXT4_FS
	CONFIG_EXT4_USE_FOR_EXT2
	CONFIG_BLK_DEV_INITRD
	CONFIG_PROC_FS
	CONFIG_SYSFS
	CONFIG_TMPFS
	CONFIG_BINFMT_ELF
)
for option in "${required_kernel_options[@]}"; do
	grep -qx "${option}=y" "${BUILD}/linux/.config" ||
		die "required kernel option is not built in: ${option}"
done
grep -Fqx \
	"CONFIG_INITRAMFS_SOURCE=\"${IMAGES}/initramfs\"" \
	"${BUILD}/linux/.config" ||
	die "CONFIG_INITRAMFS_SOURCE does not match generated initramfs"

make -C "${ROOT}/components/linux" O="${BUILD}/linux" \
	ARCH=riscv CROSS_COMPILE="${CROSS_COMPILE}" -j "${JOBS}" Image

printf '%s\n' "[build] external ext2 benchmark image"
benchmark_disk="${IMAGES}/cxl-type3-benchmark.ext2"
truncate -s 16M "${benchmark_disk}"
mke2fs -q -t ext2 -F "${benchmark_disk}"
debugfs -w \
	-R "write ${IMAGES}/cxl_mmap_bench /cxl_mmap_bench" \
	"${benchmark_disk}"
debugfs -w \
	-R "set_inode_field /cxl_mmap_bench mode 0100755" \
	"${benchmark_disk}"
debugfs -R "stat /cxl_mmap_bench" "${benchmark_disk}" |
	grep -q 'Mode:.*0755'

ln -sfn ../build/qemu/qemu-system-riscv64 \
	"${RUNTIME_BIN}/qemu-system-riscv64"

qemu_binary="${BUILD}/qemu/qemu-system-riscv64"
opensbi_binary="${BUILD}/opensbi/platform/generic/firmware/fw_dynamic.bin"
uboot_binary="${BUILD}/u-boot/u-boot.bin"
linux_binary="${BUILD}/linux/arch/riscv/boot/Image"
cxlmemsim_server="${BUILD}/cxlmemsim/cxlmemsim_server"

for artifact in \
	"${qemu_binary}" \
	"${opensbi_binary}" \
	"${uboot_binary}" \
	"${linux_binary}" \
	"${benchmark_disk}" \
	"${IMAGES}/cxl_mmap_bench" \
	"${cxlmemsim_server}"; do
	[[ -s "${artifact}" ]] || die "missing build artifact: ${artifact}"
done

python3 "${ROOT}/scripts/write_manifest.py" \
	--root "${ROOT}" \
	--output "${RESULTS}/build-manifest.json" \
	--artifact "qemu=${qemu_binary}" \
	--artifact "opensbi=${opensbi_binary}" \
	--artifact "u_boot=${uboot_binary}" \
	--artifact "linux=${linux_binary}" \
	--artifact "benchmark_disk=${benchmark_disk}" \
	--artifact "guest_benchmark=${IMAGES}/cxl_mmap_bench" \
	--artifact "cxlmemsim_server=${cxlmemsim_server}"

printf '%s\n' "[build] complete"
