#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${CXL_BI_APP_OUT:-${ROOT}/out/cxl-bi-app}"
BUILD="${OUT}/build"
IMAGES="${OUT}/images"
RESULTS="${OUT}/results"
LOGS="${OUT}/logs"
INITRAMFS="${IMAGES}/initramfs"
LINUX_BUILD="${BUILD}/linux"
LEGACY_OUT="${LEGOFS_TYPE3_OUT:-${ROOT}/out/legofs-type3}"
LEGACY_BUILD="${LEGACY_OUT}/build"
MUSL_CC="${CXL_BI_MUSL_CC:-${LEGACY_OUT}/toolchain/musl-rv64gc/bin/musl-gcc}"
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
	-h|--help)
		printf 'Usage: %s [--jobs N]\n' "$0"
		exit 0
		;;
	*)
		die "unknown argument: $1"
		;;
	esac
done

[[ "${OUT}" == /* ]] || die "CXL_BI_APP_OUT must be an absolute path"
[[ "${LEGACY_OUT}" == /* ]] || die "LEGOFS_TYPE3_OUT must be an absolute path"
[[ "${JOBS}" =~ ^[1-9][0-9]*$ ]] || die "jobs must be a positive integer"

required_commands=(
	make install cp sha256sum file python3 getconf
	"${CROSS_COMPILE}gcc" "${CROSS_COMPILE}readelf" "${CROSS_COMPILE}strip"
)
for command_name in "${required_commands[@]}"; do
	command -v "${command_name}" >/dev/null 2>&1 ||
		die "required command is unavailable: ${command_name}"
done

required_files=(
	"${ROOT}/guest/cxl_bi_app_init.c"
	"${ROOT}/configs/linux-cxl.config"
	"${ROOT}/components/linux/Makefile"
	"${ROOT}/components/linux/scripts/config"
	"${ROOT}/components/linux/scripts/kconfig/merge_config.sh"
	"${MUSL_CC}"
	"${LEGACY_BUILD}/qemu/qemu-system-riscv64"
	"${LEGACY_BUILD}/cxlmemsim/cxlmemsim_server"
	"${LEGACY_BUILD}/opensbi/platform/generic/firmware/fw_dynamic.bin"
	"${LEGACY_BUILD}/u-boot/u-boot.bin"
)
for path in "${required_files[@]}"; do
	[[ -s "${path}" ]] || die "required source or artifact is missing: ${path}"
done
[[ -x "${MUSL_CC}" ]] || die "musl compiler is not executable: ${MUSL_CC}"
[[ -x "${LEGACY_BUILD}/qemu/qemu-system-riscv64" ]] ||
	die "QEMU artifact is not executable"
[[ -x "${LEGACY_BUILD}/cxlmemsim/cxlmemsim_server" ]] ||
	die "CXLMemSim server artifact is not executable"

mkdir -p "${BUILD}" "${IMAGES}" "${RESULTS}" "${LOGS}" \
	"${INITRAMFS}/dev" "${INITRAMFS}/proc" "${INITRAMFS}/sys" \
	"${INITRAMFS}/tmp" "${INITRAMFS}/run"
exec > >(tee -a "${LOGS}/build.log") 2>&1

printf '%s\n' '[cxl-bi-build] static RISC-V DAX benchmark PID 1'
if grep -Eq '\<(msync|fsync|fdatasync|sync|clflush)\>[[:space:]]*\(' \
	"${ROOT}/guest/cxl_bi_app_init.c"; then
	die "guest benchmark contains an explicit cache/persistence flush call"
fi
"${MUSL_CC}" \
	-static -O2 -g -std=c11 -Wall -Wextra -Werror \
	-march=rv64imafdc -mabi=lp64d \
	"${ROOT}/guest/cxl_bi_app_init.c" -o "${INITRAMFS}/init"
"${CROSS_COMPILE}strip" --strip-debug "${INITRAMFS}/init"
chmod 0755 "${INITRAMFS}/init"
if "${CROSS_COMPILE}readelf" -l "${INITRAMFS}/init" | grep -q INTERP; then
	die "guest PID 1 unexpectedly has an ELF interpreter"
fi
if "${CROSS_COMPILE}readelf" -A "${INITRAMFS}/init" | grep -q '_v'; then
	die "guest PID 1 unexpectedly requires the RISC-V vector extension"
fi

legacy_linux="${LEGACY_BUILD}/linux"
legacy_image="${legacy_linux}/arch/riscv/boot/Image"
legacy_image_before=""
if [[ -s "${legacy_image}" ]]; then
	legacy_image_before="$(sha256sum "${legacy_image}" | awk '{print $1}')"
fi

if [[ ! -f "${LINUX_BUILD}/.config" ]]; then
	if [[ -f "${legacy_linux}/.config" && -s "${legacy_image}" ]]; then
		printf '%s\n' '[cxl-bi-build] seed independent Linux output from known-good build'
		mkdir -p "${LINUX_BUILD}"
		cp -a --reflink=auto "${legacy_linux}/." "${LINUX_BUILD}/"
	else
		printf '%s\n' '[cxl-bi-build] create independent Linux defconfig'
		make -C "${ROOT}/components/linux" O="${LINUX_BUILD}" \
			ARCH=riscv CROSS_COMPILE="${CROSS_COMPILE}" defconfig
		ARCH=riscv CROSS_COMPILE="${CROSS_COMPILE}" \
			"${ROOT}/components/linux/scripts/kconfig/merge_config.sh" -m \
			-O "${LINUX_BUILD}" "${LINUX_BUILD}/.config" \
			"${ROOT}/configs/linux-cxl.config"
	fi
fi

printf '%s\n' '[cxl-bi-build] configure dedicated built-in initramfs'
"${ROOT}/components/linux/scripts/config" --file "${LINUX_BUILD}/.config" \
	--set-str CONFIG_INITRAMFS_SOURCE "${INITRAMFS}" \
	--enable CONFIG_BLK_DEV_INITRD \
	--disable CONFIG_INITRAMFS_COMPRESSION_GZIP \
	--enable CONFIG_INITRAMFS_COMPRESSION_NONE
make -C "${ROOT}/components/linux" O="${LINUX_BUILD}" \
	ARCH=riscv CROSS_COMPILE="${CROSS_COMPILE}" olddefconfig

required_kernel_options=(
	CONFIG_PCI CONFIG_PCIEPORTBUS CONFIG_EFI CONFIG_EFI_STUB CONFIG_RISCV_SBI
	CONFIG_NONPORTABLE CONFIG_HVC_RISCV_SBI CONFIG_CXL_BUS CONFIG_CXL_PCI
	CONFIG_CXL_ACPI CONFIG_CXL_MEM CONFIG_CXL_PORT CONFIG_CXL_REGION
	CONFIG_MEMORY_HOTPLUG CONFIG_MEMORY_HOTREMOVE CONFIG_SPARSEMEM_VMEMMAP
	CONFIG_ZONE_DEVICE CONFIG_DAX CONFIG_FS_DAX CONFIG_DEV_DAX
	CONFIG_DEV_DAX_CXL CONFIG_DEVTMPFS CONFIG_DEVTMPFS_MOUNT
	CONFIG_BLK_DEV_INITRD CONFIG_PROC_FS CONFIG_SYSFS CONFIG_TMPFS
	CONFIG_BINFMT_ELF CONFIG_INITRAMFS_COMPRESSION_NONE
)
for option in "${required_kernel_options[@]}"; do
	grep -qx "${option}=y" "${LINUX_BUILD}/.config" ||
		die "required kernel option is not built in: ${option}"
done
grep -Fqx '# CONFIG_DEV_DAX_KMEM is not set' "${LINUX_BUILD}/.config" ||
	die 'CONFIG_DEV_DAX_KMEM must be disabled so CXL binds device_dax'
grep -Fqx "CONFIG_INITRAMFS_SOURCE=\"${INITRAMFS}\"" "${LINUX_BUILD}/.config" ||
	die 'CONFIG_INITRAMFS_SOURCE does not match the dedicated PID 1 directory'

printf '%s\n' '[cxl-bi-build] incremental Linux Image build'
make -C "${ROOT}/components/linux" O="${LINUX_BUILD}" \
	ARCH=riscv CROSS_COMPILE="${CROSS_COMPILE}" -j "${JOBS}" Image
install -m 0644 "${LINUX_BUILD}/arch/riscv/boot/Image" "${IMAGES}/Image"

if [[ -n "${legacy_image_before}" ]]; then
	legacy_image_after="$(sha256sum "${legacy_image}" | awk '{print $1}')"
	[[ "${legacy_image_before}" == "${legacy_image_after}" ]] ||
		die "the reused Legofs Linux Image changed during the isolated build"
fi

artifacts=(
	"qemu=${LEGACY_BUILD}/qemu/qemu-system-riscv64"
	"cxlmemsim_server=${LEGACY_BUILD}/cxlmemsim/cxlmemsim_server"
	"opensbi=${LEGACY_BUILD}/opensbi/platform/generic/firmware/fw_dynamic.bin"
	"u_boot=${LEGACY_BUILD}/u-boot/u-boot.bin"
	"linux_cxl_bi=${IMAGES}/Image"
	"guest_init=${INITRAMFS}/init"
)
for specification in "${artifacts[@]}"; do
	path="${specification#*=}"
	[[ -s "${path}" ]] || die "built artifact is empty: ${path}"
done

printf '%s\n' '[cxl-bi-build] provenance manifest with content hashes'
manifest_arguments=(
	--root "${ROOT}"
	--output "${RESULTS}/build-manifest.json"
	--source "linux=${ROOT}/components/linux"
	--source "qemu=${ROOT}/components/qemu"
	--source "cxlmemsim=${ROOT}/components/cxlmemsim"
	--compiler "riscv_musl_gcc=${MUSL_CC} --version"
	--compiler "qemu=${LEGACY_BUILD}/qemu/qemu-system-riscv64 --version"
)
for specification in "${artifacts[@]}"; do
	manifest_arguments+=(--artifact "${specification}")
done
python3 "${ROOT}/scripts/write_manifest.py" "${manifest_arguments[@]}"

file "${INITRAMFS}/init" "${IMAGES}/Image"
sha256sum "${INITRAMFS}/init" "${IMAGES}/Image"
printf '[cxl-bi-build] manifest %s\n' "${RESULTS}/build-manifest.json"
