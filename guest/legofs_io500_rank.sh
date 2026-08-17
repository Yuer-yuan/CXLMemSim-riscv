#!/bin/busybox sh
set -eu

stage="${1:?missing IO500 stage}"
case "$stage" in tiny|easy-smoke|hard-smoke|metadata-smoke|rnd4k|scc|standard) ;; *) exit 64 ;; esac

endpoint_id="$(cat /run/endpoint-id)"
mkdir -p /tmp/posix
export BADFS_SERVERS="$(cat /run/server-addresses)"
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
else
	export RUST_LOG=warn,tarpc=error
fi
export LD_LIBRARY_PATH=/payload/lib:/lib
export LD_PRELOAD=/payload/lib/libbadfs_intercept.so
if [ "$stage" = tiny ]; then
	export BADFS_CXL_DIRECT_TRACE="/tmp/direct-$endpoint_id.jsonl"
	export BADFS_CXL_DIRECT_TRACE_STDOUT=1
fi

echo "LEGOFS_IO500_RANK_EXEC stage=$stage rank=${PMI_RANK:-unset} size=${PMI_SIZE:-unset} endpoint=$endpoint_id dax=$(cat /run/dax-path)"
case "$stage" in
tiny|easy-smoke|hard-smoke|metadata-smoke|rnd4k)
	exec /payload/bin/io500 "/payload/etc/io500-$stage.ini"
	;;
esac

set +e
/payload/bin/io500 "/payload/etc/io500-$stage.ini"
io500_rc=$?
set -e
if [ "$io500_rc" -ne 0 ]; then
	exit "$io500_rc"
fi

if [ "${PMI_RANK:-unset}" = 0 ]; then
	/payload/bin/export-io500-results "$stage"
fi
