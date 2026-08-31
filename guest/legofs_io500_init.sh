#!/bin/busybox sh
# PID 1 for the one/two-server, multi-client RISC-V IO500 topology.

export PATH=/payload/bin:/bin:/sbin:/usr/bin:/usr/sbin
export LD_LIBRARY_PATH=/payload/lib:/lib

fail()
{
	echo "LEGOFS_IO500_FATAL step=$1 rc=${2:-1}"
	while :; do sleep 3600; done
}

cmdline_value()
{
	name="$1"
	for item in $(cat /proc/cmdline); do
		case "$item" in
		"$name"*) printf '%s\n' "${item#"$name"}"; return 0 ;;
		esac
	done
	return 1
}

set_guest_time()
{
	requested="$1"
	case "$requested" in
	''|*[!0-9]*)
		echo "LEGOFS_IO500_TIME_SYNC_ERROR role=$role index=$index requested=$requested"
		return 1
		;;
	esac
	/bin/busybox date -u -s "@$requested" >/dev/null 2>&1 || {
		echo "LEGOFS_IO500_TIME_SYNC_ERROR role=$role index=$index requested=$requested"
		return 1
	}
	observed="$(/bin/busybox date -u +%s)" || return 1
	# The tiny-stage syscall preflight deliberately runs before IO500.  Its
	# ranks remove this marker and wait for the runner to perform a fresh,
	# topology-wide synchronization before entering the measured phases.
	/bin/busybox touch /run/io500-clock-synced
	echo "LEGOFS_IO500_TIME_SYNC role=$role index=$index requested=$requested observed=$observed"
}

if [ "${LEGOFS_PAYLOAD_RUNTIME:-0}" != 1 ]; then
	mkdir -p /proc /sys /dev /run /tmp /payload /state /results /etc
	mount -t proc proc /proc || fail mount-proc
	mount -t sysfs sysfs /sys || fail mount-sys
	mount -t devtmpfs devtmpfs /dev || grep -q ' /dev devtmpfs ' /proc/mounts || fail mount-dev
	exec </dev/console >/dev/console 2>&1
	mount -t tmpfs tmpfs /run || fail mount-run
	mount -t tmpfs tmpfs /tmp || fail mount-tmp
	mkdir -p /tmp/posix /dev/pts
	mount -t devpts devpts /dev/pts || fail mount-devpts
	mount -t ext2 -o ro /dev/vda /payload || fail mount-payload
else
	mkdir -p /tmp/posix /state /results /etc
fi

role="$(cmdline_value io500.role=)" || fail missing-role
index="$(cmdline_value io500.index=)" || fail missing-index
stage="$(cmdline_value io500.stage=)" || fail missing-stage
server_count="$(cmdline_value io500.server_count=)" || fail missing-server-count
client_count="$(cmdline_value io500.client_count=)" || fail missing-client-count
serving_transport="$(cmdline_value io500.serving_transport=)" || fail missing-serving-transport
cursor_token="$(cmdline_value c=)" || fail missing-cursor-mode
cq_wait_token="$(cmdline_value w=)" || fail missing-cq-wait-mode
observation_spec="$(cmdline_value o=)" || fail missing-observation-spec
saved_ifs="$IFS"
IFS=,
set -- $observation_spec
IFS="$saved_ifs"
[ "$#" -eq 4 ] || fail invalid-observation-spec
case "$1" in
d) observation_mode=default ;;
o) observation_mode=off ;;
a) observation_mode=aggregate ;;
s) observation_mode=sampled ;;
*) fail invalid-observation-mode ;;
esac
observation_sample_shift="$2"
observation_arena_mib="$3"
observation_run_id="$4"
case "$role" in server|client) ;; *) fail invalid-role ;; esac
case "$index" in ''|*[!0-9]*) fail invalid-index ;; esac
case "$server_count" in 1|2) ;; *) fail invalid-server-count ;; esac
case "$serving_transport" in legacy|cxl) ;; *) fail invalid-serving-transport ;; esac
case "$cursor_token" in
l) cursor_mode=legacy_shared ;;
o) cursor_mode=owned ;;
*) fail invalid-cursor-mode ;;
esac
case "$cq_wait_token" in
s) cq_wait_mode=timer_sleep ;;
y) cq_wait_mode=cooperative_yield ;;
*) fail invalid-cq-wait-mode ;;
esac
case "$observation_mode" in default|off|aggregate|sampled) ;; *) fail invalid-observation-mode ;; esac
case "$observation_sample_shift" in ''|*[!0-9]*) fail invalid-observation-sample-shift ;; esac
[ "$observation_sample_shift" -le 20 ] || fail invalid-observation-sample-shift
case "$observation_arena_mib" in ''|*[!0-9]*) fail invalid-observation-arena-bytes ;; esac
[ "$observation_arena_mib" -ge 8 ] && [ "$observation_arena_mib" -le 64 ] ||
	fail invalid-observation-arena-bytes
observation_arena_bytes="$((observation_arena_mib * 1024 * 1024))"
case "$observation_run_id" in
''|*[!A-Za-z0-9._-]*) fail invalid-observation-run-id ;;
esac
[ "${#observation_run_id}" -le 96 ] || fail invalid-observation-run-id
case "$client_count" in 1|2|3|4|5|6|7|8|9|10) ;; *) fail invalid-client-count ;; esac
if [ "$role" = server ] && [ "$index" -ge "$server_count" ]; then
	fail invalid-server-index
fi

if [ "$role" = server ]; then
	# Product WAL/state writers issue their own fsyncs.  A synchronous mount also
	# serializes every diagnostic trace append and hides the product cost behind
	# an unrelated virtio-disk penalty.
	mount -t ext2 -o rw /dev/vdb /state || fail mount-server-state
	mkdir -p /state/badfs-data
elif [ "$index" = 0 ]; then
	mount -t ext2 -o rw,sync /dev/vdb /results || fail mount-results
	mkdir -p "/results/$stage"
fi

# Each guest must discover its sole device-DAX child from sysfs.  Never assume
# that a particular daxN.M number survives across kernels or boot order.
dax_name=
attempt=0
while [ "$attempt" -lt 240 ]; do
	set -- /sys/bus/dax/devices/dax*
	if [ "$#" -eq 1 ] && [ -e "$1/dev" ]; then
		dax_name="${1##*/}"
		break
	fi
	attempt=$((attempt + 1))
	sleep 0.25
done
[ -n "$dax_name" ] || fail dax-device-count
dax_sys="/sys/bus/dax/devices/$dax_name"
set -- $(tr ':' ' ' < "$dax_sys/dev")
[ "$#" -eq 2 ] || fail dax-dev-number
dax_path="/dev/$dax_name"
[ -e "$dax_path" ] || mknod "$dax_path" c "$1" "$2" || fail mknod-dax
dax_size="$(cat "$dax_sys/size")" || fail dax-size
[ "$dax_size" = 68719476736 ] || fail dax-size-mismatch "$dax_size"
dax_align="$(cat "$dax_sys/align" 2>/dev/null || printf '4096')"
case "$dax_align" in ''|*[!0-9]*) fail dax-align-invalid "$dax_align" ;; esac
[ "$dax_align" -ge 4096 ] && [ $((dax_align % 4096)) -eq 0 ] ||
	fail dax-align-invalid "$dax_align"
dax_driver="$(basename "$(readlink "$dax_sys/driver")")"
[ "$dax_driver" = device_dax ] || fail dax-driver
printf '%s\n' "$dax_path" > /run/dax-path
printf '%s\n' "$dax_align" > /run/dax-align
printf '%s\n' "$index" > /run/endpoint-id
printf '%s\n' "$server_count" > /run/server-count
printf '%s\n' "$serving_transport" > /run/serving-transport
printf '%s\n' "$cursor_mode" > /run/cursor-mode
printf '%s\n' "$cq_wait_mode" > /run/cq-wait-mode
printf '%s\n' "$client_count" > /run/client-count

# Observation arenas are process-local DRAM snapshots.  Configuration is
# fixed by the host runner and is identical in every guest.  In default mode
# the mode variable is deliberately absent, proving the normal path remains
# equivalent to ObserverHandle::off().
mkdir -p /tmp/legofs-observation || fail observation-directory
chmod 700 /tmp/legofs-observation || fail observation-directory-mode
export BADFS_OBSERVATION_SAMPLE_SHIFT="$observation_sample_shift"
export BADFS_OBSERVATION_ARENA_BYTES="$observation_arena_bytes"
export BADFS_OBSERVATION_RUN_ID="$observation_run_id"
export BADFS_OBSERVATION_DIR=/tmp/legofs-observation
workload_endpoints=
workload_endpoint=0
while [ "$workload_endpoint" -lt "$client_count" ]; do
	if [ -n "$workload_endpoints" ]; then
		workload_endpoints="$workload_endpoints,$workload_endpoint"
	else
		workload_endpoints="$workload_endpoint"
	fi
	workload_endpoint=$((workload_endpoint + 1))
done
export BADFS_OBSERVATION_WORKLOAD_ENDPOINTS="$workload_endpoints"
if [ "$observation_mode" = default ]; then
	unset BADFS_OBSERVATION_MODE
else
	export BADFS_OBSERVATION_MODE="$observation_mode"
fi

dump_observation()
{
	expected_file_role="$1"
	expected_endpoint="$2"
	file_count=0
	for observation_file in /tmp/legofs-observation/*.bin; do
		[ -f "$observation_file" ] || continue
		observation_base="${observation_file##*/}"
		case "$observation_base" in
		"observation-v1-$observation_run_id-$expected_file_role-$expected_endpoint-"*.bin) ;;
		*)
			echo "LEGOFS_IO500_OBSERVATION_EXPORT_ERROR role=$expected_file_role endpoint=$expected_endpoint reason=unexpected-filename file=$observation_base"
			continue
			;;
		esac
		observation_bytes="$(/bin/busybox wc -c < "$observation_file")" || continue
		case "$observation_bytes" in ''|*[!0-9]*) continue ;; esac
		if [ "$observation_bytes" -gt 67108864 ]; then
			echo "LEGOFS_IO500_OBSERVATION_EXPORT_ERROR role=$expected_file_role endpoint=$expected_endpoint reason=oversize file=$observation_base bytes=$observation_bytes"
			continue
		fi
		set -- $(/bin/busybox sha256sum "$observation_file")
		observation_sha256="$1"
		echo "LEGOFS_OBSERVATION_BEGIN role=$expected_file_role endpoint=$expected_endpoint file=$observation_base bytes=$observation_bytes sha256=$observation_sha256"
		/bin/busybox base64 -w 76 "$observation_file"
		echo "LEGOFS_OBSERVATION_END role=$expected_file_role endpoint=$expected_endpoint file=$observation_base"
		file_count=$((file_count + 1))
	done
	if [ "$file_count" -eq 0 ]; then
		echo "LEGOFS_IO500_OBSERVATION_EXPORT_ERROR role=$expected_file_role endpoint=$expected_endpoint reason=no-arena-visible"
	fi
	echo "LEGOFS_IO500_OBSERVATION_DONE role=$expected_file_role index=$expected_endpoint files=$file_count"
}

server_addresses=
lifecycle_devices=
server_ordinal=0
while [ "$server_ordinal" -lt "$server_count" ]; do
	server_address="10.77.0.$((100 + server_ordinal)):3345"
	if [ -n "$server_addresses" ]; then
		server_addresses="$server_addresses,$server_address"
		lifecycle_devices="$lifecycle_devices,$dax_path"
	else
		server_addresses="$server_address"
		lifecycle_devices="$dax_path"
	fi
	server_ordinal=$((server_ordinal + 1))
done
printf '%s\n' "$server_addresses" > /run/server-addresses
printf '%s\n' "$lifecycle_devices" > /run/lifecycle-devices

ip link set lo up || fail network-loopback
ip link set eth0 up || fail network-link
if [ "$role" = server ]; then
	ip_address="10.77.0.$((100 + index))"
	host_name="legofs-server$index"
else
	ip_address="10.77.0.$((index + 10))"
	host_name="client$index"
fi
ip addr add "$ip_address/24" dev eth0 || fail network-address
hostname "$host_name" || fail hostname

cat >/etc/hosts <<'EOF'
127.0.0.1 localhost
10.77.0.100 legofs-server legofs-server0
10.77.0.101 legofs-server1
10.77.0.10 client0
10.77.0.11 client1
10.77.0.12 client2
10.77.0.13 client3
10.77.0.14 client4
10.77.0.15 client5
10.77.0.16 client6
10.77.0.17 client7
10.77.0.18 client8
10.77.0.19 client9
EOF

echo "LEGOFS_IO500_CXL_READY role=$role index=$index dax=$dax_name size=$dax_size align=$dax_align driver=$dax_driver ip=$ip_address"

export BADFS_POSIX_DATA_PATH=lifecycle
export BADFS_LIFECYCLE_BLOB=0
export BADFS_LIFECYCLE_DIRECT_FINAL=1
export BADFS_LIFECYCLE_DIRECT_REQUIRED=1
export BADFS_LIFECYCLE_DIRECT_READ=1
export BADFS_LIFECYCLE_DIRECT_READ_REQUIRED=1
export BADFS_LIFECYCLE_DEVICE_REQUIRED=1
export BADFS_CXL_MAP_ALIGNMENT="$dax_align"
export BADFS_BASE_PATH=/badfs
export BADFS_DISTRIBUTOR=consistent
export BADFS_FSYNC_ON_CLOSE=1
export BADFS_TRACK_OPEN_SET=1
export BADFS_FABRIC_STAGED_IO=0
export BADFS_DISABLE_FABRIC_MMAP=0
export BADFS_LIFECYCLE_COHERENT_PUBLICATION=1
export BADFS_LIFECYCLE_COHERENT_READ_CACHE=1
export BADFS_LIFECYCLE_READ_CACHE_ENTRIES=2048
export BADFS_LIFECYCLE_WRITE_ARENA_SLOTS=64
export BADFS_CLIENT_ENDPOINT_ID="$index"
export BADFS_SERVING_TRANSPORT="$serving_transport"
export BADFS_SERVING_CURSOR_MODE="$cursor_mode"
export BADFS_SERVING_CQ_WAIT_MODE="$cq_wait_mode"
export BADFS_SERVING_MAX_CLIENTS=64
export BADFS_LIFECYCLE_REGION_SIZE=68719476736
export BADFS_SERVER_COUNT="$server_count"
export BADFS_LIFECYCLE_DEVICES="$lifecycle_devices"
export BADFS_CLUSTER_GENERATION=1
export BADFS_LOG_STREAM_GENERATION=1
if [ "$stage" = tiny ]; then
	export RUST_LOG=info,tarpc=error
else
	export RUST_LOG=warn,tarpc=error
fi

if [ "$role" = server ]; then
	partition_size=$((68719476736 / server_count))
	partition_offset=$((index * partition_size))
	export BADFS_LIFECYCLE_DEVICE="$dax_path"
	export BADFS_LIFECYCLE_POOL_OFFSET="$partition_offset"
	export BADFS_LIFECYCLE_POOL_SIZE="$partition_size"
	export BADFS_SERVER_INDEX="$index"
	export BADFS_LIFECYCLE_MAX_EXTENTS="$((32760 / server_count))"
	export BADFS_SERVER_ADDR=0.0.0.0:3345
	export BADFS_DATA_DIR=/state/badfs-data
	# Keep the high-frequency audit trace off the durable metadata disk.  Tiny
	# still streams it to the console for the strict lifecycle/BI proof.
	export BADFS_LIFECYCLE_TRACE=/tmp/lifecycle.jsonl
	if [ "$stage" = tiny ]; then
		export BADFS_LIFECYCLE_TRACE_MODE=ordering
		export BADFS_LIFECYCLE_TRACE_STDOUT=1
		export BADFS_LIFECYCLE_RUN_ID="io500-tiny-${client_count}c${server_count}s-server$index"
	else
		export BADFS_LIFECYCLE_TRACE_MODE=off
	fi
	/payload/bin/badfs-server &
	server_pid=$!
	server_generation="$BADFS_CLUSTER_GENERATION"
	# The host runner owns the bounded readiness deadline. Under TCG a valid
	# CXL persistence operation can consume more than 60 guest seconds, so PID 1
	# must not invent a second, shorter timeout while the server is still alive.
	while :; do
		if ! kill -0 "$server_pid" 2>/dev/null; then
			wait "$server_pid"
			fail server-exit "$?"
		fi
		if /bin/busybox nc -z -w 1 127.0.0.1 3345 2>/dev/null; then
			echo "LEGOFS_IO500_SERVER_READY index=$index pid=$server_pid addr=$ip_address:3345"
			while IFS= read -r server_line; do
				case "$server_line" in
				LEGOFS_SET_TIME\ *)
					set_guest_time "${server_line#LEGOFS_SET_TIME }" || true
					;;
				LEGOFS_POWEROFF)
					echo "LEGOFS_IO500_POWEROFF index=$index"
					kill "$server_pid" 2>/dev/null || true
					wait "$server_pid" 2>/dev/null || true
					sync
					poweroff -f
					;;
				LEGOFS_DUMP_OBSERVATION)
					dump_observation server "$index"
					;;
				LEGOFS_SERVER_CLEAN_RESTART\ *)
					target_generation="${server_line#LEGOFS_SERVER_CLEAN_RESTART }"
					case "$target_generation" in
					''|*[!0-9]*) echo "LEGOFS_IO500_SERVER_RESTART_ERROR reason=invalid-generation"; continue ;;
					esac
					if [ "$target_generation" -le "$server_generation" ]; then
						echo "LEGOFS_IO500_SERVER_RESTART_ERROR reason=non-forward-generation"
						continue
					fi
					old_pid="$server_pid"
					echo "LEGOFS_IO500_SERVER_RESTART_BEGIN index=$index old_pid=$old_pid old_generation=$server_generation target_generation=$target_generation"
					kill "$old_pid" 2>/dev/null || true
					wait "$old_pid" 2>/dev/null
					old_rc=$?
					echo "LEGOFS_IO500_SERVER_OLD_EXIT index=$index old_pid=$old_pid rc=$old_rc"
					export BADFS_CLUSTER_GENERATION="$target_generation"
					export BADFS_START_MODE=clean_restart
					/payload/bin/badfs-server &
					server_pid=$!
					server_generation="$target_generation"
					while :; do
						if ! kill -0 "$server_pid" 2>/dev/null; then
							wait "$server_pid" 2>/dev/null
							echo "LEGOFS_IO500_SERVER_RESTART_ERROR reason=successor-exit rc=$?"
							break
						fi
						if /bin/busybox nc -z -w 1 127.0.0.1 3345 2>/dev/null; then
							echo "LEGOFS_IO500_SERVER_RESTARTED index=$index old_pid=$old_pid new_pid=$server_pid generation=$server_generation"
							break
						fi
						sleep 0.25
					done
					;;
				LEGOFS_SERVER_UNAUTHORIZED_RESTART_PROBE\ *)
					target_generation="${server_line#LEGOFS_SERVER_UNAUTHORIZED_RESTART_PROBE }"
					case "$target_generation" in
					''|*[!0-9]*) echo "LEGOFS_IO500_UNAUTHORIZED_RESTART_ERROR reason=invalid-generation"; continue ;;
					esac
					if [ "$target_generation" -le "$server_generation" ]; then
						echo "LEGOFS_IO500_UNAUTHORIZED_RESTART_ERROR reason=non-forward-generation"
						continue
					fi
					if (
						export BADFS_CLUSTER_GENERATION="$target_generation"
						export BADFS_START_MODE=clean_restart
						export BADFS_SERVER_ADDR=127.0.0.1:3346
						/payload/bin/badfs-server
					); then
						probe_rc=0
					else
						probe_rc=$?
					fi
					if /bin/busybox nc -z -w 1 127.0.0.1 3346 2>/dev/null; then
						echo "LEGOFS_IO500_UNAUTHORIZED_RESTART_ERROR reason=listener-open rc=$probe_rc"
					else
						echo "LEGOFS_IO500_UNAUTHORIZED_RESTART_REJECTED index=$index target_generation=$target_generation listener_open=0 rc=$probe_rc"
					fi
					;;
				*) echo "LEGOFS_IO500_COMMAND_ERROR index=$index" ;;
				esac
			done
			fail console-eof
		fi
		sleep 0.25
	done
fi

export BADFS_SERVERS="$server_addresses"
echo "LEGOFS_IO500_CLIENT_READY index=$index"

while IFS= read -r line; do
	case "$line" in
	LEGOFS_SET_TIME\ *)
		set_guest_time "${line#LEGOFS_SET_TIME }" || true
		;;
	LEGOFS_MPI\ *)
		mpi_stage="${line#LEGOFS_MPI }"
		case "$mpi_stage" in
		hello)
			application=/payload/bin/mpi-hello
			;;
		tiny|easy-smoke|hard-smoke|metadata-smoke|rnd4k|scc|standard)
			application=/payload/bin/run-io500-rank
			;;
		*)
			echo "LEGOFS_IO500_COMMAND_ERROR command=mpi value=$mpi_stage"
			continue
			;;
		esac
		(
			export IO500_STAGE="$mpi_stage"
			/payload/bin/mpiexec.hydra -iface eth0 -launcher manual \
				-f /payload/etc/clients -ppn 1 -n "$client_count" \
				"$application" "$mpi_stage"
			rc=$?
			echo "LEGOFS_IO500_MPI_EXIT stage=$mpi_stage rc=$rc"
		) &
		echo "LEGOFS_IO500_MPI_STARTED stage=$mpi_stage pid=$!"
		;;
	LEGOFS_PROXY\ *)
		proxy_command="${line#LEGOFS_PROXY }"
		(
			/bin/busybox sh -c "$proxy_command"
			rc=$?
			echo "LEGOFS_IO500_PROXY_EXIT index=$index rc=$rc"
		) &
		echo "LEGOFS_IO500_PROXY_STARTED index=$index pid=$!"
		;;
	LEGOFS_VERIFY\ *)
		verify_stage="${line#LEGOFS_VERIFY }"
		/payload/bin/io500-verify "/results/$verify_stage/config.ini" \
			"/results/$verify_stage/result.txt" 1
		rc=$?
		echo "LEGOFS_IO500_VERIFY_EXIT stage=$verify_stage rc=$rc"
		;;
	LEGOFS_INSPECT)
		BADFS_OBSERVATION_MODE=off BADFS_BENCH_MODE=inspect /payload/bin/badfs-bench
		rc=$?
		echo "LEGOFS_IO500_INSPECT_EXIT index=$index rc=$rc"
		;;
	LEGOFS_INSPECT_ENDPOINT\ *)
		inspect_endpoint="${line#LEGOFS_INSPECT_ENDPOINT }"
		case "$inspect_endpoint" in
		''|*[!0-9]*) echo "LEGOFS_IO500_INSPECT_ENDPOINT_ERROR index=$index"; continue ;;
		esac
		if [ "$inspect_endpoint" -ge 44 ]; then
			echo "LEGOFS_IO500_INSPECT_ENDPOINT_ERROR index=$index reason=role-reserved"
			continue
		fi
		BADFS_OBSERVATION_MODE=off \
		BADFS_CLIENT_ENDPOINT_ID="$inspect_endpoint" \
		BADFS_BENCH_MODE=inspect /payload/bin/badfs-bench.real
		rc=$?
		echo "LEGOFS_IO500_INSPECT_ENDPOINT_EXIT index=$index endpoint=$inspect_endpoint rc=$rc"
		;;
	LEGOFS_CLEAN_RESTART_CONTROL\ *)
		set -- ${line#LEGOFS_CLEAN_RESTART_CONTROL }
		if [ "$#" -ne 2 ]; then
			echo "LEGOFS_IO500_CLEAN_RESTART_CONTROL_ERROR index=$index reason=arguments"
			continue
		fi
		target_generation="$1"
		nonce="$2"
		BADFS_OBSERVATION_MODE=off \
		BADFS_BENCH_MODE=clean-restart-control \
		BADFS_CLEAN_RESTART_TARGET_GENERATION="$target_generation" \
		BADFS_CLEAN_RESTART_NONCE="$nonce" \
		/payload/bin/badfs-bench.real
		rc=$?
		echo "LEGOFS_IO500_CLEAN_RESTART_CONTROL_EXIT index=$index target_generation=$target_generation rc=$rc"
		;;
	LEGOFS_CLIENT_GENERATION\ *)
		target_generation="${line#LEGOFS_CLIENT_GENERATION }"
		case "$target_generation" in
		''|*[!0-9]*) echo "LEGOFS_IO500_CLIENT_GENERATION_ERROR index=$index"; continue ;;
		esac
		export BADFS_CLUSTER_GENERATION="$target_generation"
		echo "LEGOFS_IO500_CLIENT_GENERATION_SET index=$index generation=$BADFS_CLUSTER_GENERATION"
		;;
	LEGOFS_CLEAN_RESTART_COHORT\ *)
		set -- ${line#LEGOFS_CLEAN_RESTART_COHORT }
		if [ "$#" -ne 2 ]; then
			echo "LEGOFS_IO500_CLEAN_RESTART_COHORT_ERROR index=$index reason=arguments"
			continue
		fi
		cohort_action="$1"
		cohort_endpoint="$2"
		BADFS_OBSERVATION_MODE=off \
		BADFS_BENCH_MODE=clean-restart-cohort \
		BADFS_CLEAN_RESTART_COHORT_ACTION="$cohort_action" \
		BADFS_CLIENT_ENDPOINT_ID="$cohort_endpoint" \
		/payload/bin/badfs-bench.real
		rc=$?
		echo "LEGOFS_IO500_CLEAN_RESTART_COHORT_EXIT index=$index action=$cohort_action endpoint=$cohort_endpoint generation=$BADFS_CLUSTER_GENERATION rc=$rc"
		;;
	LEGOFS_DUMP_SUMMARIES)
		for summary in /tmp/posix/*.json; do
			[ -f "$summary" ] || continue
			printf 'LEGOFS_IO500_POSIX_SUMMARY index=%s file=%s ' "$index" "${summary##*/}"
			cat "$summary"
		done
		echo "LEGOFS_IO500_SUMMARIES_DONE index=$index"
		;;
	LEGOFS_DUMP_OBSERVATION)
		dump_observation client-rank "$index"
		;;
	LEGOFS_POWEROFF)
		echo "LEGOFS_IO500_POWEROFF index=$index"
		sync
		poweroff -f
		;;
	*)
		echo "LEGOFS_IO500_COMMAND_ERROR index=$index"
		;;
	esac
done

fail console-eof
