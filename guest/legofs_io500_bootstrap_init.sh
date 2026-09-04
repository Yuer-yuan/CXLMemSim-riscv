#!/bin/busybox sh
# Stable initramfs bootstrap. The mutable experiment/control loop lives on the
# payload image so --payload-only iterations never rebuild Linux.

export PATH=/payload/bin:/bin:/sbin:/usr/bin:/usr/sbin

bootstrap_fail()
{
	echo "LEGOFS_IO500_FATAL step=$1 rc=${2:-1}"
	while :; do sleep 3600; done
}

mkdir -p /proc /sys /dev /run /tmp /payload /state /results /etc
mount -t proc proc /proc || bootstrap_fail mount-proc
mount -t sysfs sysfs /sys || bootstrap_fail mount-sys
mount -t devtmpfs devtmpfs /dev ||
	grep -q ' /dev devtmpfs ' /proc/mounts || bootstrap_fail mount-dev
exec </dev/console >/dev/console 2>&1
mount -t tmpfs tmpfs /run || bootstrap_fail mount-run
mount -t tmpfs tmpfs /tmp || bootstrap_fail mount-tmp
mkdir -p /tmp/posix /dev/pts
mount -t devpts devpts /dev/pts || bootstrap_fail mount-devpts
mount -t ext2 -o ro /dev/vda /payload || bootstrap_fail mount-payload
[ -x /payload/bin/legofs-io500-init ] || bootstrap_fail missing-payload-init
export LEGOFS_PAYLOAD_RUNTIME=1
exec /payload/bin/legofs-io500-init
bootstrap_fail payload-init-exit
