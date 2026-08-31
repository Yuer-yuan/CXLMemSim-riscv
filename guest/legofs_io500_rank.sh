#!/bin/busybox sh
set -eu

cmdline_value()
{
	prefix="$1"
	for word in $(cat /proc/cmdline); do
		case "$word" in
		"$prefix"*) printf '%s\n' "${word#"$prefix"}"; return 0 ;;
		esac
	done
	return 1
}

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
export BADFS_TRACK_OPEN_SET=1
export BADFS_FABRIC_STAGED_IO=0
export BADFS_DISABLE_FABRIC_MMAP=0
export BADFS_LIFECYCLE_COHERENT_PUBLICATION=1
export BADFS_LIFECYCLE_COHERENT_READ_CACHE=1
export BADFS_LIFECYCLE_READ_CACHE_ENTRIES=2048
# metadata-smoke is a fixed 32-write/rank read-path pilot. Match its one-use
# arena to that exact prerequisite instead of persisting 32 unused 2 MiB slots
# per rank. Score/tiny/product profiles retain the production default below.
case "$stage" in
metadata-smoke) export BADFS_LIFECYCLE_WRITE_ARENA_SLOTS=32 ;;
*) export BADFS_LIFECYCLE_WRITE_ARENA_SLOTS=64 ;;
esac
export BADFS_CLIENT_ENDPOINT_ID="$endpoint_id"
if [ -s /run/serving-transport ]; then
	serving_transport="$(cat /run/serving-transport)"
else
	serving_transport="$(cmdline_value io500.serving_transport= 2>/dev/null || printf 'legacy\n')"
fi
case "$serving_transport" in legacy|cxl) ;; *) exit 64 ;; esac
export BADFS_SERVING_TRANSPORT="$serving_transport"
if [ -s /run/cursor-mode ]; then
	cursor_mode="$(cat /run/cursor-mode)"
else
	cursor_token="$(cmdline_value c= 2>/dev/null || printf 'o\n')"
	case "$cursor_token" in
	l) cursor_mode=legacy_shared ;;
	o) cursor_mode=owned ;;
	*) exit 64 ;;
	esac
fi
case "$cursor_mode" in legacy_shared|owned) ;; *) exit 64 ;; esac
export BADFS_SERVING_CURSOR_MODE="$cursor_mode"
if [ -s /run/cq-wait-mode ]; then
	cq_wait_mode="$(cat /run/cq-wait-mode)"
else
	cq_wait_token="$(cmdline_value w= 2>/dev/null || printf 's\n')"
	case "$cq_wait_token" in
	s) cq_wait_mode=timer_sleep ;;
	y) cq_wait_mode=cooperative_yield ;;
	*) exit 64 ;;
	esac
fi
case "$cq_wait_mode" in timer_sleep|cooperative_yield) ;; *) exit 64 ;; esac
export BADFS_SERVING_CQ_WAIT_MODE="$cq_wait_mode"
export BADFS_SERVING_MAX_CLIENTS=64
export BADFS_LIFECYCLE_REGION_SIZE=68719476736
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
if [ "$stage" = tiny ] && [ "$serving_transport" = cxl ]; then
	client_count="$(cmdline_value io500.client_count=)"
	# Lanes [0, client_count) belong to IO500 ranks and lane client_count is
	# reserved by the post-run inspector.  Syscall preflight uses the next
	# disjoint range so its completed generation cannot block either consumer.
	diagnostic_endpoint="$((client_count + 1 + endpoint_id))"
	[ "$diagnostic_endpoint" -lt "$BADFS_SERVING_MAX_CLIENTS" ] || exit 64
	BADFS_OBSERVATION_MODE=off \
	BADFS_CLIENT_ENDPOINT_ID="$diagnostic_endpoint" BADFS_BENCH_MODE=syscall-matrix \
		LD_PRELOAD= /payload/bin/badfs-bench.real
	# Preflight is intentionally outside the measured IO500 phases and can be
	# slow under TCG.  Do not let different guest clocks drift during it and
	# then assign incomparable ctime values to the shared namespace.
	/bin/busybox rm -f /run/io500-clock-synced
	echo "LEGOFS_IO500_PREFLIGHT_READY endpoint=$endpoint_id"
	while [ ! -e /run/io500-clock-synced ]; do
		/bin/busybox sleep 0.05
	done
	echo "LEGOFS_IO500_PREFLIGHT_RELEASED endpoint=$endpoint_id"
fi
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
	if [ "$serving_transport" = cxl ]; then
		client_count="$(cmdline_value io500.client_count=)"
		case "$client_count" in ''|*[!0-9]*) exit 64 ;; esac
		# Serving format v5 reserves the final 16 authority-internal and four
		# recovery-control lanes.  The runner supports at most ten clients, so
		# 2N+1 is a fresh CLIENT_FS lane after ranks, inspector and preflight.
		serving_reserved_control_lanes=20
		serving_client_lane_count="$((BADFS_SERVING_MAX_CLIENTS - serving_reserved_control_lanes))"
		exporter_endpoint="$((2 * client_count + 1))"
		[ "$exporter_endpoint" -lt "$serving_client_lane_count" ] || exit 64
		/bin/busybox mkdir -p /tmp/posix-export
		BADFS_OBSERVATION_MODE=off \
		BADFS_CLIENT_ENDPOINT_ID="$exporter_endpoint" \
			BADFS_POSIX_TRACE_DIR=/tmp/posix-export \
			PMI_RANK= PMIX_RANK= OMPI_COMM_WORLD_RANK= \
			/payload/bin/export-io500-results "$stage"
		export_summary_count=0
		for summary in /tmp/posix-export/*.json; do
			[ -f "$summary" ] || continue
			printf 'LEGOFS_IO500_EXPORT_POSIX_SUMMARY endpoint=%s file=%s ' \
				"$exporter_endpoint" "${summary##*/}"
			LD_PRELOAD= /bin/busybox cat "$summary"
			export_summary_count="$((export_summary_count + 1))"
		done
		if [ "$export_summary_count" -ne 1 ]; then
			echo "LEGOFS_IO500_FATAL exporter-summary-count=$export_summary_count"
			exit 70
		fi
	else
		/payload/bin/export-io500-results "$stage"
	fi
fi
