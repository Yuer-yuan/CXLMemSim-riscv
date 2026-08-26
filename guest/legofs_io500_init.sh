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
	echo "LEGOFS_IO500_TIME_SYNC role=$role index=$index requested=$requested observed=$observed"
}

mkdir -p /proc /sys /dev /run /tmp /payload /state /results /etc
mount -t proc proc /proc || fail mount-proc
mount -t sysfs sysfs /sys || fail mount-sys
mount -t devtmpfs devtmpfs /dev || grep -q ' /dev devtmpfs ' /proc/mounts || fail mount-dev
exec </dev/console >/dev/console 2>&1
mount -t tmpfs tmpfs /run || fail mount-run
mount -t tmpfs tmpfs /tmp || fail mount-tmp
mkdir -p /tmp/posix /dev/pts
mount -t devpts devpts /dev/pts || fail mount-devpts

# The non-MPI v2 HDM-DB/BI gate has an isolated serial-only control mode.
# It intentionally returns before any role parsing, network configuration,
# legacy lifecycle environment, baseline server, or MPI launcher is reached.
v2_mode="$(cmdline_value legofs.v2= 2>/dev/null || true)"
if [ "$v2_mode" = 1 ]; then
	attempt=0
	while [ "$attempt" -lt 600 ] && [ ! -b /dev/vda ]; do
		attempt=$((attempt + 1))
		sleep 0.1
	done
	[ -b /dev/vda ] || fail v2-payload-device
	mount -t ext2 -o ro /dev/vda /payload || fail v2-mount-payload
	export PATH=/payload/bin:/bin:/sbin:/usr/bin:/usr/sbin
	export LD_LIBRARY_PATH=/payload/lib:/lib
	echo "LEGOFS_V2_CONTROL_READY network_devices_unconfigured=1 payload_read_only=1"
	while IFS= read -r v2_command; do
		/bin/busybox sh -c "$v2_command"
		v2_rc=$?
		echo "LEGOFS_V2_CONTROL_COMMAND_EXIT rc=$v2_rc"
	done
	fail v2-console-eof
fi

role="$(cmdline_value io500.role=)" || fail missing-role
index="$(cmdline_value io500.index=)" || fail missing-index
stage="$(cmdline_value io500.stage=)" || fail missing-stage
server_count="$(cmdline_value io500.server_count=)" || fail missing-server-count
client_count="$(cmdline_value io500.client_count=)" || fail missing-client-count
filesystem_mode="$(cmdline_value io500.filesystem_mode=)" || fail missing-filesystem-mode
case "$role" in server|client) ;; *) fail invalid-role ;; esac
case "$filesystem_mode" in legacy-cxl-reference|rdwo-candidate) ;; *) fail invalid-filesystem-mode ;; esac
case "$index" in ''|*[!0-9]*) fail invalid-index ;; esac
case "$server_count" in 1|2) ;; *) fail invalid-server-count ;; esac
case "$client_count" in 1|2|3|4|5|6|7|8|9|10) ;; *) fail invalid-client-count ;; esac
if [ "$role" = server ] && [ "$index" -ge "$server_count" ]; then
	fail invalid-server-index
fi

mount -t ext2 -o ro /dev/vda /payload || fail mount-payload
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
printf '%s\n' "$client_count" > /run/client-count
printf '%s\n' "$filesystem_mode" > /run/filesystem-mode

lifecycle_devices=
cxl_server_ids=
server_ordinal=0
while [ "$server_ordinal" -lt "$server_count" ]; do
	if [ -n "$lifecycle_devices" ]; then
		lifecycle_devices="$lifecycle_devices,$dax_path"
		cxl_server_ids="$cxl_server_ids,$server_ordinal"
	else
		lifecycle_devices="$dax_path"
		cxl_server_ids="$server_ordinal"
	fi
	server_ordinal=$((server_ordinal + 1))
done
printf '%s\n' "$lifecycle_devices" > /run/cxl-devices
printf '%s\n' "$cxl_server_ids" > /run/cxl-server-ids

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

echo "LEGOFS_IO500_CXL_READY role=$role index=$index mode=$filesystem_mode dax=$dax_name size=$dax_size align=$dax_align driver=$dax_driver ip=$ip_address"

control_max_clients=16
if [ "$client_count" -gt "$control_max_clients" ]; then
	control_max_clients="$client_count"
fi

case "$filesystem_mode" in
legacy-cxl-reference)
	printf '%s\n' "$lifecycle_devices" > /run/lifecycle-devices
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
	export BADFS_TRACK_OPEN_SET=0
	export BADFS_FABRIC_STAGED_IO=0
	export BADFS_DISABLE_FABRIC_MMAP=0
	export BADFS_LIFECYCLE_COHERENT_PUBLICATION=1
	export BADFS_LIFECYCLE_COHERENT_READ_CACHE=1
	export BADFS_LIFECYCLE_READ_CACHE_ENTRIES=2048
	export BADFS_LIFECYCLE_WRITE_ARENA_SLOTS=64
	export BADFS_CLIENT_ENDPOINT_ID="$index"
	export BADFS_CONTROL_TRANSPORT=cxl
	export BADFS_CXL_SERVER_COUNT="$server_count"
	export BADFS_CXL_SERVER_IDS="$cxl_server_ids"
	export BADFS_CXL_MAX_CLIENTS="$control_max_clients"
	export BADFS_CXL_CONTROL_RING_SIZE=262144
	if [ "$stage" = tiny ]; then
		export RUST_LOG=info,tarpc=error
	else
		export RUST_LOG=warn,tarpc=error
	fi
	;;
rdwo-candidate)
	export LEGOFS_RDWO_CXL_DEVICE="$dax_path"
	export LEGOFS_RDWO_CXL_DEVICES="$lifecycle_devices"
	export LEGOFS_RDWO_REGION_BYTES=68719476736
	export LEGOFS_RDWO_ENDPOINT_ID="$index"
	export LEGOFS_RDWO_SERVER_COUNT="$server_count"
	export LEGOFS_RDWO_SERVER_IDS="$cxl_server_ids"
	export LEGOFS_RDWO_MAX_CLIENTS="$control_max_clients"
	export LEGOFS_RDWO_CONTROL_RING_BYTES=262144
	;;
esac

if [ "$role" = server ]; then
	control_slot_stride=$((((4096 + 2 * BADFS_CXL_CONTROL_RING_SIZE + 4095) / 4096) * 4096))
	control_server_stride=$((((4096 + control_slot_stride * BADFS_CXL_MAX_CLIENTS + 2097151) / 2097152) * 2097152))
	control_size=$((((4096 + control_server_stride * server_count + 2097151) / 2097152) * 2097152))
	partition_size=$((((68719476736 - control_size) / server_count / 2097152) * 2097152))
	partition_offset=$((control_size + index * partition_size))
	if [ "$filesystem_mode" = legacy-cxl-reference ]; then
		export BADFS_LIFECYCLE_DEVICE="$dax_path"
		export BADFS_LIFECYCLE_REGION_SIZE=68719476736
		export BADFS_LIFECYCLE_POOL_OFFSET="$partition_offset"
		export BADFS_LIFECYCLE_POOL_SIZE="$partition_size"
		export BADFS_LIFECYCLE_MAX_EXTENTS="$((32760 / server_count))"
		export BADFS_SERVER_ID="$index"
		export BADFS_READY_FILE=/run/badfs-cxl-server.ready
		export BADFS_DATA_DIR=/state/badfs-data
		# Keep the high-frequency audit trace off the durable metadata disk. Tiny
		# still streams it to the console for the strict lifecycle/BI proof.
		export BADFS_LIFECYCLE_TRACE=/tmp/lifecycle.jsonl
		if [ "$stage" = tiny ]; then
			export BADFS_LIFECYCLE_TRACE_MODE=ordering
			export BADFS_LIFECYCLE_TRACE_STDOUT=1
			export BADFS_LIFECYCLE_RUN_ID="io500-tiny-${client_count}c${server_count}s-server$index"
		else
			export BADFS_LIFECYCLE_TRACE_MODE=off
		fi
		/bin/busybox rm -f "$BADFS_READY_FILE"
		/payload/bin/badfs-server &
		server_pid=$!
	else
		[ -x /payload/bin/badfs-rdwo-host-agent ] || fail missing-rdwo-host-agent
		[ -x /payload/bin/badfs-rdwo-server ] || fail missing-rdwo-server
		[ -s /payload/etc/legofs-rdwo-engine.manifest ] || fail missing-rdwo-engine-manifest
		[ -s /payload/etc/legofs-rdwo-capabilities.manifest ] || fail missing-rdwo-capability-manifest
		export LEGOFS_RDWO_POOL_OFFSET="$partition_offset"
		export LEGOFS_RDWO_POOL_BYTES="$partition_size"
		export LEGOFS_RDWO_SERVER_ID="$index"
		export LEGOFS_RDWO_HOST_AGENT_READY=/run/legofs-rdwo-host-agent.ready
		export LEGOFS_RDWO_SERVER_READY=/run/legofs-rdwo-server.ready
		/bin/busybox rm -f "$LEGOFS_RDWO_HOST_AGENT_READY" "$LEGOFS_RDWO_SERVER_READY"
		/payload/bin/badfs-rdwo-host-agent &
		host_agent_pid=$!
		/payload/bin/badfs-rdwo-server &
		server_pid=$!
	fi
	attempt=0
	while [ "$attempt" -lt 240 ]; do
		if ! kill -0 "$server_pid" 2>/dev/null; then
			if wait "$server_pid"; then
				server_rc=0
			else
				server_rc=$?
			fi
			fail server-exit "$server_rc"
		fi
		if [ "$filesystem_mode" = rdwo-candidate ] && ! kill -0 "$host_agent_pid" 2>/dev/null; then
			if wait "$host_agent_pid"; then
				host_agent_rc=0
			else
				host_agent_rc=$?
			fi
			fail rdwo-host-agent-exit "$host_agent_rc"
		fi
		server_ready=0
		if [ "$filesystem_mode" = legacy-cxl-reference ]; then
			if [ -s "$BADFS_READY_FILE" ] &&
				grep -q '^control_transport=cxl-dax-ring$' "$BADFS_READY_FILE"; then
				server_ready=1
			fi
		else
			if [ -s "$LEGOFS_RDWO_HOST_AGENT_READY" ] &&
				grep -q '^engine=rdwo-product$' "$LEGOFS_RDWO_HOST_AGENT_READY" &&
				[ -s "$LEGOFS_RDWO_SERVER_READY" ] &&
				grep -q '^engine=rdwo-product$' "$LEGOFS_RDWO_SERVER_READY"; then
				server_ready=1
			fi
		fi
		if [ "$server_ready" -eq 1 ]; then
			echo "LEGOFS_IO500_SERVER_READY index=$index mode=$filesystem_mode pid=$server_pid transport=cxl-dax-ring control_bytes=$control_size"
			while IFS= read -r server_line; do
				case "$server_line" in
				LEGOFS_SET_TIME\ *)
					set_guest_time "${server_line#LEGOFS_SET_TIME }" || true
					;;
				LEGOFS_POWEROFF)
					echo "LEGOFS_IO500_POWEROFF index=$index"
					kill "$server_pid" 2>/dev/null || true
					if [ "$filesystem_mode" = rdwo-candidate ]; then
						kill "$host_agent_pid" 2>/dev/null || true
						wait "$host_agent_pid" 2>/dev/null || true
					fi
					wait "$server_pid" 2>/dev/null || true
					sync
					poweroff -f
					;;
				*) echo "LEGOFS_IO500_COMMAND_ERROR index=$index" ;;
				esac
			done
			fail console-eof
		fi
		attempt=$((attempt + 1))
		sleep 0.25
	done
	fail server-ready-timeout
fi

if [ "$filesystem_mode" = legacy-cxl-reference ]; then
	export BADFS_LIFECYCLE_DEVICES="$lifecycle_devices"
else
	[ -x /payload/bin/badfs-rdwo-client ] || fail missing-rdwo-client
	[ -s /payload/lib/libbadfs_rdwo_intercept.so ] || fail missing-rdwo-intercept
	[ -s /payload/etc/legofs-rdwo-engine.manifest ] || fail missing-rdwo-engine-manifest
	[ -s /payload/etc/legofs-rdwo-capabilities.manifest ] || fail missing-rdwo-capability-manifest
fi
echo "LEGOFS_IO500_CLIENT_READY index=$index mode=$filesystem_mode"

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
				"$application" "$mpi_stage" "$client_count"
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
		if /payload/bin/io500-verify "/results/$verify_stage/config.ini" \
			"/results/$verify_stage/result.txt" 1; then
			rc=0
		else
			rc=$?
		fi
		echo "LEGOFS_IO500_VERIFY_EXIT stage=$verify_stage rc=$rc"
		;;
	LEGOFS_INSPECT)
		if [ "$filesystem_mode" != legacy-cxl-reference ]; then
			echo "LEGOFS_IO500_COMMAND_REJECTED index=$index mode=$filesystem_mode command=legacy-inspect"
			continue
		fi
		if BADFS_BENCH_MODE=inspect /payload/bin/badfs-bench; then
			rc=0
		else
			rc=$?
		fi
		echo "LEGOFS_IO500_INSPECT_EXIT index=$index rc=$rc"
		;;
	LEGOFS_DUMP_SUMMARIES)
		if [ "$filesystem_mode" != legacy-cxl-reference ]; then
			echo "LEGOFS_IO500_COMMAND_REJECTED index=$index mode=$filesystem_mode command=legacy-summary"
			continue
		fi
		for summary in /tmp/posix/*.json; do
			[ -f "$summary" ] || continue
			printf 'LEGOFS_IO500_POSIX_SUMMARY index=%s file=%s ' "$index" "${summary##*/}"
			cat "$summary"
		done
		echo "LEGOFS_IO500_SUMMARIES_DONE index=$index"
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
