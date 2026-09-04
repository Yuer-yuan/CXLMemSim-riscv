#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_ROOT="$ROOT/target/build/riscv-io500"
PLATFORM="$TARGET_ROOT/platform"
INITRAMFS="$TARGET_ROOT/initramfs"
LINUX_BUILD="$TARGET_ROOT/linux"
CROSS_COMPILE="${CROSS_COMPILE:-riscv64-linux-gnu-}"
source "$ROOT/scripts/legofs_toolchain_path.sh"
legofs_toolchain_activate io500-bootstrap \
	bash sh getconf make sha256sum install mktemp mv rm \
	"${CROSS_COMPILE}gcc" "${CROSS_COMPILE}as" \
	"${CROSS_COMPILE}ar" "${CROSS_COMPILE}ld" \
	"${CROSS_COMPILE}nm" "${CROSS_COMPILE}objcopy" \
	"${CROSS_COMPILE}objdump" "${CROSS_COMPILE}ranlib" \
	"${CROSS_COMPILE}readelf" "${CROSS_COMPILE}strip" \
	awk sed grep sort find perl python3 bc bison flex openssl cpio
JOBS="$(getconf _NPROCESSORS_ONLN 2>/dev/null || printf '1\n')"
IMAGE_TMP=

die()
{
	printf 'error: %s\n' "$*" >&2
	exit 2
}

cleanup()
{
	if [[ -n "$IMAGE_TMP" && -e "$IMAGE_TMP" ]]; then
		rm -f -- "$IMAGE_TMP"
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
		printf '%s\n' 'Usage: scripts/rebuild_legofs_io500_bootstrap.sh [--jobs N]'
		exit 0
		;;
	*) die "unknown argument: $1" ;;
	esac
done

[[ "$JOBS" =~ ^[1-9][0-9]*$ ]] || die 'jobs must be a positive integer'
for path in \
	"$INITRAMFS/bin/busybox" \
	"$LINUX_BUILD/.config" \
	"$PLATFORM/qemu-system-riscv64" \
	"$PLATFORM/cxlmemsim_server" \
	"$PLATFORM/fw_dynamic.bin" \
	"$PLATFORM/u-boot.bin"; do
	[[ -s "$path" ]] || die "missing prerequisite artifact: $path"
done

unchanged_before="$(sha256sum \
	"$PLATFORM/qemu-system-riscv64" \
	"$PLATFORM/cxlmemsim_server" \
	"$PLATFORM/fw_dynamic.bin" \
	"$PLATFORM/u-boot.bin")"

install -m 0755 "$ROOT/guest/legofs_io500_bootstrap_init.sh" "$INITRAMFS/init"
make -C "$ROOT/components/linux" O="$LINUX_BUILD" ARCH=riscv \
	CROSS_COMPILE="$CROSS_COMPILE" -j "$JOBS" Image
IMAGE_TMP="$(mktemp "$PLATFORM/.linux-io500-Image.XXXXXX")"
install -m 0644 "$LINUX_BUILD/arch/riscv/boot/Image" "$IMAGE_TMP"
mv -f -- "$IMAGE_TMP" "$PLATFORM/linux-io500-Image"
IMAGE_TMP=

unchanged_after="$(sha256sum \
	"$PLATFORM/qemu-system-riscv64" \
	"$PLATFORM/cxlmemsim_server" \
	"$PLATFORM/fw_dynamic.bin" \
	"$PLATFORM/u-boot.bin")"
[[ "$unchanged_before" = "$unchanged_after" ]] ||
	die 'non-Linux platform artifacts changed during bootstrap-only rebuild'
printf '[io500-bootstrap] Linux image %s\n' "$PLATFORM/linux-io500-Image"
