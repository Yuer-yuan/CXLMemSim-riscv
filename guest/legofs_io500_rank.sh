#!/bin/busybox sh
set -eu

stage="${1:?missing IO500 stage}"
case "$stage" in tiny|easy-smoke|hard-smoke|metadata-smoke|rnd4k|scc|standard) ;; *) exit 64 ;; esac

endpoint_id="$(cat /run/endpoint-id)"
filesystem_mode="$(cat /run/filesystem-mode)"
case "$filesystem_mode" in legacy-cxl-reference|rdwo-candidate) ;; *) exit 64 ;; esac
export LD_LIBRARY_PATH=/payload/lib:/lib
case "$filesystem_mode" in
legacy-cxl-reference)
	mkdir -p /tmp/posix
	export BADFS_CONTROL_TRANSPORT=cxl
	export BADFS_CXL_SERVER_COUNT="$(cat /run/server-count)"
	export BADFS_CXL_SERVER_IDS="$(cat /run/cxl-server-ids)"
	export BADFS_CXL_MAX_CLIENTS=16
	if [ "$(cat /run/client-count)" -gt "$BADFS_CXL_MAX_CLIENTS" ]; then
		export BADFS_CXL_MAX_CLIENTS="$(cat /run/client-count)"
	fi
	export BADFS_CXL_CONTROL_RING_SIZE=262144
	export BADFS_CXL_CLIENT_SLOT="$endpoint_id"
	export BADFS_BASE_PATH=/badfs
	export BADFS_DISTRIBUTOR=consistent
	export BADFS_POSIX_DATA_PATH=lifecycle
	export BADFS_LIFECYCLE_BLOB=0
	export BADFS_LIFECYCLE_DIRECT_FINAL=1
	export BADFS_LIFECYCLE_DIRECT_REQUIRED=1
	export BADFS_LIFECYCLE_DIRECT_READ=1
	export BADFS_LIFECYCLE_DIRECT_READ_REQUIRED=1
	export BADFS_LIFECYCLE_DEVICE_REQUIRED=1
	export BADFS_LIFECYCLE_DEVICES="$(cat /run/lifecycle-devices)"
	export BADFS_CXL_MAP_ALIGNMENT="$(cat /run/dax-align)"
	export BADFS_FSYNC_ON_CLOSE=1
	export BADFS_TRACK_OPEN_SET=0
	export BADFS_FABRIC_STAGED_IO=0
	export BADFS_DISABLE_FABRIC_MMAP=0
	export BADFS_LIFECYCLE_COHERENT_PUBLICATION=1
	export BADFS_LIFECYCLE_COHERENT_READ_CACHE=1
	export BADFS_LIFECYCLE_READ_CACHE_ENTRIES=2048
	export BADFS_LIFECYCLE_WRITE_ARENA_SLOTS=64
	export BADFS_CLIENT_ENDPOINT_ID="$endpoint_id"
	export BADFS_POSIX_TRACE_DIR=/tmp/posix
	export INTERCEPT_ALL_OBJS=1
	if [ "$stage" = tiny ]; then
		export RUST_LOG=info,tarpc=error
		export BADFS_SYSCALL_ERROR_TRACE=1
		export BADFS_CXL_DIRECT_TRACE="/tmp/direct-$endpoint_id.jsonl"
		export BADFS_CXL_DIRECT_TRACE_STDOUT=1
	else
		export RUST_LOG=warn,tarpc=error
	fi
	export LD_PRELOAD=/payload/lib/libbadfs_intercept.so
	;;
rdwo-candidate)
	[ -x /payload/bin/badfs-rdwo-client ] || exit 65
	[ -s /payload/lib/libbadfs_rdwo_intercept.so ] || exit 65
	[ -s /payload/etc/legofs-rdwo-engine.manifest ] || exit 65
	[ -s /payload/etc/legofs-rdwo-capabilities.manifest ] || exit 65
	export LEGOFS_RDWO_CXL_DEVICE="$(cat /run/dax-path)"
	export LEGOFS_RDWO_CXL_DEVICES="$(cat /run/cxl-devices)"
	export LEGOFS_RDWO_ENDPOINT_ID="$endpoint_id"
	export LEGOFS_RDWO_SERVER_COUNT="$(cat /run/server-count)"
	;;
esac

echo "LEGOFS_IO500_RANK_EXEC mode=$filesystem_mode stage=$stage rank=${PMI_RANK:-unset} size=${PMI_SIZE:-unset} endpoint=$endpoint_id dax=$(cat /run/dax-path)"
case "$stage" in
tiny|easy-smoke|hard-smoke|metadata-smoke|rnd4k)
	if [ "$filesystem_mode" = rdwo-candidate ]; then
		exec /payload/bin/badfs-rdwo-client -- \
			/payload/bin/io500 "/payload/etc/io500-$stage.ini"
	fi
	exec /payload/bin/io500 "/payload/etc/io500-$stage.ini"
	;;
esac

set +e
if [ "$filesystem_mode" = rdwo-candidate ]; then
	/payload/bin/badfs-rdwo-client -- \
		/payload/bin/io500 "/payload/etc/io500-$stage.ini"
else
	/payload/bin/io500 "/payload/etc/io500-$stage.ini"
fi
io500_rc=$?
set -e
if [ "$io500_rc" -ne 0 ]; then
	exit "$io500_rc"
fi

if [ "${PMI_RANK:-unset}" = 0 ]; then
	if [ "$filesystem_mode" = rdwo-candidate ]; then
		/payload/bin/badfs-rdwo-client -- \
			/payload/bin/export-io500-results "$stage"
	else
		/payload/bin/export-io500-results "$stage"
	fi
fi
