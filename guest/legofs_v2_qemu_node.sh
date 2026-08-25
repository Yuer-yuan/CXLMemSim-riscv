#!/bin/sh
# Non-MPI LegoFS v2 guest control for the QEMU Type-3 HDM-DB/BI gate.

set -u

V2_ROOT=/run/legofs-v2
FIXTURE=/payload/fixture
CXL_DEVICE=/dev/legofs-cxl
BIN=/payload/bin
LIB=/payload/lib

fatal()
{
    echo "LEGOFS_V2_FATAL step=$1 detail=${2:-unknown}"
    exit 1
}

dump_bounded_start_logs()
{
    slot=$1
    for component in liveness activation pin persistence local-init workload; do
        case "$component" in
        workload) log="$V2_ROOT/workload-$slot.stderr" ;;
        *) log="$V2_ROOT/$component-$slot.log" ;;
        esac
        echo "LEGOFS_V2_DAEMON_LOG_BEGIN path=$log"
        cat "$log" 2>/dev/null || true
        echo "LEGOFS_V2_DAEMON_LOG_END path=$log"
    done
}

require_file()
{
    [ -f "$1" ] || fatal missing-file "$1"
}

wait_marker()
{
    pid=$1
    log=$2
    marker=$3
    failure_slot=${4:-}
    attempt=0
    while [ "$attempt" -lt 1800 ]; do
        if ! kill -0 "$pid" 2>/dev/null; then
            echo "LEGOFS_V2_DAEMON_LOG_BEGIN path=$log"
            cat "$log" 2>/dev/null || true
            echo "LEGOFS_V2_DAEMON_LOG_END path=$log"
            [ -z "$failure_slot" ] || dump_bounded_start_logs "$failure_slot"
            fatal daemon-exit "$marker"
        fi
        if grep -q "$marker" "$log" 2>/dev/null; then
            return 0
        fi
        attempt=$((attempt + 1))
        sleep 0.1
    done
    [ -z "$failure_slot" ] || dump_bounded_start_logs "$failure_slot"
    fatal daemon-ready-timeout "$marker"
}

record_pid()
{
    name=$1
    pid=$2
    printf '%s\n' "$pid" > "$V2_ROOT/pids/$name"
}

process_has_socket()
{
    pid=$1
    [ -d "/proc/$pid/fd" ] || return 1
    for descriptor in "/proc/$pid/fd/"*; do
        [ -e "$descriptor" ] || continue
        target=$(/bin/busybox readlink "$descriptor" 2>/dev/null || true)
        case "$target" in
        socket:\[*\]) return 0 ;;
        esac
    done
    return 1
}

report_process_sockets()
{
    process_name=$1
    pid=$2
    [ -d "/proc/$pid/fd" ] || return 0
    for descriptor in "/proc/$pid/fd/"*; do
        [ -e "$descriptor" ] || continue
        target=$(/bin/busybox readlink "$descriptor" 2>/dev/null || true)
        case "$target" in
        socket:\[*\])
            fd=${descriptor##*/}
            command=$(cat "/proc/$pid/comm" 2>/dev/null || true)
            echo "LEGOFS_V2_SOCKET_FD process=$process_name pid=$pid command=$command fd=$fd target=$target"
            ;;
        esac
    done
}

audit_persistent_sockets()
{
    seen=0
    for pid_file in "$V2_ROOT/pids/"*; do
        [ -f "$pid_file" ] || continue
        pid=$(cat "$pid_file")
        if kill -0 "$pid" 2>/dev/null && process_has_socket "$pid"; then
            report_process_sockets "${pid_file##*/}" "$pid"
            seen=1
        fi
    done
    return "$seen"
}

prepare()
{
    mkdir -p "$V2_ROOT" "$V2_ROOT/pids"
    require_file "$FIXTURE/layout.env"
    require_file "$LIB/libunwind.so.1.0"
    . "$FIXTURE/layout.env"

    # One-use launchers intentionally clear their child's environment. Make
    # the audited runtime SONAME available through the guest's standard loader
    # path instead of weakening that environment boundary.
    /bin/busybox ln -sfn "$LIB/libunwind.so.1.0" /lib/libunwind.so.1 || fatal loader-runtime libunwind

    dax_name=
    attempt=0
    while [ "$attempt" -lt 1200 ]; do
        set -- /sys/bus/dax/devices/dax*
        if [ "$#" -eq 1 ] && [ -e "$1/dev" ]; then
            dax_name=${1##*/}
            break
        fi
        attempt=$((attempt + 1))
        sleep 0.1
    done
    [ -n "$dax_name" ] || fatal dax-device-count timeout
    dax_sys=/sys/bus/dax/devices/$dax_name
    set -- $(tr ':' ' ' < "$dax_sys/dev")
    [ "$#" -eq 2 ] || fatal dax-dev-number "$dax_name"
    dax_path=/dev/$dax_name
    [ -e "$dax_path" ] || mknod "$dax_path" c "$1" "$2" || fatal mknod-dax "$dax_name"
    dax_size=$(cat "$dax_sys/size") || fatal dax-size read
    [ "$dax_size" -ge "$CAPACITY_BYTES" ] || fatal dax-size-small "$dax_size"
    dax_align=$(cat "$dax_sys/align" 2>/dev/null || printf '4096')
    case "$dax_align" in ''|*[!0-9]*) fatal dax-align "$dax_align" ;; esac
    dax_driver=$(/bin/busybox basename "$(/bin/busybox readlink "$dax_sys/driver")")
    [ "$dax_driver" = device_dax ] || fatal dax-driver "$dax_driver"
    initial_dax_align=$dax_align
    alignment_mechanism=preconfigured
    if [ "$dax_align" -ne "$PROVIDER_MMAP_ALIGNMENT" ]; then
        # Use the upstream Linux device-DAX alignment control ABI before any
        # listener or mapping exists. This is not the reverted private kernel
        # patch that hard-coded 4 KiB CXL DAX alignment.
        dax_driver_dir=/sys/bus/dax/drivers/device_dax
        [ -w "$dax_driver_dir/unbind" ] || fatal dax-align-unbind unavailable
        printf '%s\n' "$dax_name" > "$dax_driver_dir/unbind" || fatal dax-align-unbind "$dax_name"
        printf '%s\n' "$PROVIDER_MMAP_ALIGNMENT" > "$dax_sys/align" || fatal dax-align-config "$PROVIDER_MMAP_ALIGNMENT"
        printf '%s\n' "$dax_name" > "$dax_driver_dir/bind" || fatal dax-align-rebind "$dax_name"
        attempt=0
        while [ "$attempt" -lt 100 ]; do
            [ -e "$dax_sys/driver" ] && break
            attempt=$((attempt + 1))
            sleep 0.1
        done
        [ -e "$dax_sys/driver" ] || fatal dax-align-rebind-timeout "$dax_name"
        dax_driver=$(/bin/busybox basename "$(/bin/busybox readlink "$dax_sys/driver")")
        [ "$dax_driver" = device_dax ] || fatal dax-driver-after-align "$dax_driver"
        dax_align=$(cat "$dax_sys/align") || fatal dax-align-after-config read
        alignment_mechanism=linux-device-dax-sysfs
    fi
    [ "$dax_align" -eq "$PROVIDER_MMAP_ALIGNMENT" ] || fatal dax-align-mismatch "$dax_align"
    network_devices=0
    for interface in /sys/class/net/*; do
        [ -e "$interface" ] || continue
        interface_name=${interface##*/}
        if [ "$interface_name" != lo ]; then
            device_target=$(/bin/busybox readlink "$interface/device" 2>/dev/null || printf none)
            echo "LEGOFS_V2_NETWORK_DEVICE name=$interface_name device=$device_target"
            network_devices=$((network_devices + 1))
        fi
    done
    [ "$network_devices" -eq 0 ] || fatal guest-network-device "$network_devices"
    /bin/busybox rm -f "$CXL_DEVICE"
    /bin/busybox ln -s "$dax_path" "$CXL_DEVICE" || fatal dax-link "$dax_path"
    echo "LEGOFS_V2_GUEST_READY dax=$dax_name path=$CXL_DEVICE size=$dax_size initial_align=$initial_dax_align align=$dax_align alignment_mechanism=$alignment_mechanism driver=$dax_driver network_devices=$network_devices loader_runtime=audited-libunwind"
}

server_start()
{
    . "$FIXTURE/layout.env"
    if [ -n "${BOUNDED_CLIENT_COUNT:-}" ]; then
        bounded_server_start
        return
    fi
    digest=$(cat "$FIXTURE/bootstrap-digest.txt") || fatal bootstrap-digest read

    "$BIN/badfs-v2-guest-layout-initializer" \
        "$CXL_DEVICE" \
        "$FIXTURE/provider-bootstrap.bin" \
        "$digest" \
        "$FIXTURE/region-set.bin" \
        "$FIXTURE/locators.bin" \
        "$FIXTURE/runtime-persistence.bin" \
        > "$V2_ROOT/initializer.log" 2>&1 || {
            cat "$V2_ROOT/initializer.log"
            fatal initializer failed
        }

    env \
        BADFS_PROVIDER_ROUTER_LAUNCHER_DAEMON="$BIN/badfs-provider-router" \
        BADFS_PROVIDER_ROUTER_LAUNCHER_BOOTSTRAP="$FIXTURE/provider-bootstrap.bin" \
        BADFS_PROVIDER_ROUTER_LAUNCHER_BOOTSTRAP_DIGEST="$digest" \
        BADFS_PROVIDER_ROUTER_LAUNCHER_SELECTOR="$FIXTURE/selector.bin" \
        BADFS_PROVIDER_ROUTER_LAUNCHER_REGION_MANIFEST="$FIXTURE/region-set.bin" \
        BADFS_PROVIDER_ROUTER_LAUNCHER_LOCATOR_CATALOG="$FIXTURE/locators.bin" \
        BADFS_PROVIDER_ROUTER_LAUNCHER_NAMESPACE="$FIXTURE/namespace.bin" \
        BADFS_PROVIDER_ROUTER_LAUNCHER_RUNTIME_PERSISTENCE_MANIFEST="$FIXTURE/runtime-persistence.bin" \
        BADFS_PROVIDER_ROUTER_LAUNCHER_PROVISIONING="$CXL_DEVICE" \
        BADFS_PROVIDER_ROUTER_LAUNCHER_PROVISIONING_PROVIDER_OFFSET="$PROVIDER_PROVISIONING_OFFSET" \
        BADFS_PROVIDER_ROUTER_LAUNCHER_PROCESS_INCARNATION=109 \
        BADFS_PROVIDER_ROUTER_LAUNCHER_EXEC_INCARNATION=113 \
        BADFS_PROVIDER_ROUTER_LAUNCHER_NONCE=$(printf '83%.0s' $(seq 1 32)) \
        BADFS_PROVIDER_ROUTER_LAUNCHER_CONTROLLER_ID=$(printf '84%.0s' $(seq 1 16)) \
        BADFS_PROVIDER_ROUTER_LAUNCHER_CONTROLLER_TERM=127 \
        BADFS_PROVIDER_ROUTER_LAUNCHER_CONTROLLER_TOKEN=131 \
        "$BIN/badfs-provider-router-launcher" \
        > "$V2_ROOT/provider.log" 2>&1 &
    provider_pid=$!
    record_pid provider "$provider_pid"

    "$BIN/badfs-v2-serving-controller" \
        "$CXL_DEVICE" \
        "$FIXTURE/provider-bootstrap.bin" \
        "$digest" \
        "$FIXTURE/region-set.bin" \
        "$FIXTURE/locators.bin" \
        120000 > "$V2_ROOT/serving.json" 2>&1 || {
            cat "$V2_ROOT/provider.log" 2>/dev/null || true
            cat "$V2_ROOT/serving.json" 2>/dev/null || true
            fatal serving-controller failed
        }
    kill -0 "$provider_pid" 2>/dev/null || {
        cat "$V2_ROOT/provider.log" 2>/dev/null || true
        fatal provider-exit after-serving
    }
    audit_persistent_sockets && socket_count=0 || socket_count=1
    [ "$socket_count" -eq 0 ] || fatal server-socket-fd detected
    echo "LEGOFS_V2_SERVER_READY provider_pid=$provider_pid socket_fds=0 tcp_fallbacks=0"
}

bounded_server_start()
{
    "$BIN/badfs-v2-guest-layout-initializer" --bounded "$FIXTURE" "$CXL_DEVICE" \
        > "$V2_ROOT/initializer.log" 2>&1 || {
            cat "$V2_ROOT/initializer.log"
            fatal bounded-initializer failed
        }
    # The initializer is the only writer that creates the cold CXL layout.
    # Once this marker is published, each client may provision its disjoint
    # host-local queues/capabilities while provider recovery continues.  This
    # is not filesystem admission: READY plus the exact-cohort controller
    # below remain the sole path to SERVING.
    echo "LEGOFS_V2_SERVER_LAYOUT_READY clients=$BOUNDED_CLIENT_COUNT socket_fds=0 tcp_fallbacks=0"

    env \
        BADFS_PROVIDER_ROUTER_LAUNCHER_DAEMON="$BIN/badfs-provider-router" \
        BADFS_PROVIDER_ROUTER_LAUNCHER_BOOTSTRAP="$FIXTURE/bounded-provider-bootstrap.bin" \
        BADFS_PROVIDER_ROUTER_LAUNCHER_BOOTSTRAP_DIGEST="$BOOTSTRAP_DIGEST" \
        BADFS_PROVIDER_ROUTER_LAUNCHER_SELECTOR="$FIXTURE/bounded-selector.bin" \
        BADFS_PROVIDER_ROUTER_LAUNCHER_REGION_MANIFEST="$FIXTURE/bounded-region-set.bin" \
        BADFS_PROVIDER_ROUTER_LAUNCHER_LOCATOR_CATALOG="$FIXTURE/bounded-provider-locators.bin" \
        BADFS_PROVIDER_ROUTER_LAUNCHER_NAMESPACE="$FIXTURE/bounded-namespace.bin" \
        BADFS_PROVIDER_ROUTER_LAUNCHER_PROVISIONING="$CXL_DEVICE" \
        BADFS_PROVIDER_ROUTER_LAUNCHER_PROVISIONING_PROVIDER_OFFSET="$PROVIDER_PROVISIONING_OFFSET" \
        BADFS_PROVIDER_ROUTER_LAUNCHER_PROCESS_INCARNATION=109 \
        BADFS_PROVIDER_ROUTER_LAUNCHER_EXEC_INCARNATION=113 \
        BADFS_PROVIDER_ROUTER_LAUNCHER_NONCE=$(printf '83%.0s' $(seq 1 32)) \
        BADFS_PROVIDER_ROUTER_LAUNCHER_CONTROLLER_ID=$(printf '84%.0s' $(seq 1 16)) \
        BADFS_PROVIDER_ROUTER_LAUNCHER_CONTROLLER_TERM=127 \
        BADFS_PROVIDER_ROUTER_LAUNCHER_CONTROLLER_TOKEN=131 \
        "$BIN/badfs-provider-router-launcher" \
        > "$V2_ROOT/provider.log" 2>&1 &
    provider_pid=$!
    record_pid provider "$provider_pid"

    "$BIN/badfs-v2-serving-controller" --bounded-wait \
        "$FIXTURE" "$CXL_DEVICE" 120000 > "$V2_ROOT/recovering.json" 2>&1 || {
            cat "$V2_ROOT/provider.log" 2>/dev/null || true
            cat "$V2_ROOT/recovering.json" 2>/dev/null || true
            fatal bounded-provider-ready failed
        }
    audit_persistent_sockets && socket_count=0 || socket_count=1
    [ "$socket_count" -eq 0 ] || fatal bounded-server-socket-fd detected
    echo "LEGOFS_V2_SERVER_RECOVERING_READY provider_pid=$provider_pid clients=$BOUNDED_CLIENT_COUNT socket_fds=0 tcp_fallbacks=0"
}

bounded_server_admit()
{
    . "$FIXTURE/layout.env"
    [ -n "${BOUNDED_CLIENT_COUNT:-}" ] || fatal bounded-server-admit missing-fixture
    "$BIN/badfs-v2-serving-controller" --bounded \
        "$FIXTURE" "$CXL_DEVICE" 120000 > "$V2_ROOT/serving.json" 2>&1 || {
            cat "$V2_ROOT/provider.log" 2>/dev/null || true
            cat "$V2_ROOT/serving.json" 2>/dev/null || true
            fatal bounded-serving-controller failed
        }
    audit_persistent_sockets && socket_count=0 || socket_count=1
    [ "$socket_count" -eq 0 ] || fatal bounded-server-socket-fd detected
    echo "LEGOFS_V2_SERVER_READY provider_pid=$(cat "$V2_ROOT/pids/provider") clients=$BOUNDED_CLIENT_COUNT socket_fds=0 tcp_fallbacks=0"
}

bounded_server_stop()
{
    . "$FIXTURE/layout.env"
    [ -n "${BOUNDED_CLIENT_COUNT:-}" ] || fatal bounded-server-stop missing-fixture
    provider_pid=$(cat "$V2_ROOT/pids/provider") || fatal bounded-server-stop missing-provider-pid
    "$BIN/badfs-v2-serving-controller" --bounded-stop \
        "$FIXTURE" "$CXL_DEVICE" 120000 > "$V2_ROOT/stopped.json" 2>&1 || {
            cat "$V2_ROOT/provider.log" 2>/dev/null || true
            cat "$V2_ROOT/stopped.json" 2>/dev/null || true
            fatal bounded-provider-stop failed
        }
    # Each console command runs in a fresh shell, so the long-lived provider
    # was reparented after server-start and is not wait(2)-able here.  The
    # lifecycle controller above already waited for the authoritative STOPPED
    # publication; do not misclassify shell ECHILD as a daemon failure.
    telemetry=$(grep '^LEGOFS_V2_AUTHORITY_TELEMETRY ' "$V2_ROOT/provider.log")
    [ -n "$telemetry" ] || {
        cat "$V2_ROOT/provider.log" 2>/dev/null || true
        fatal bounded-provider-stop missing-telemetry
    }
    echo "LEGOFS_V2_AUTHORITY_TELEMETRY_BEGIN"
    echo "${telemetry#LEGOFS_V2_AUTHORITY_TELEMETRY }"
    echo "LEGOFS_V2_AUTHORITY_TELEMETRY_END"
    echo "LEGOFS_V2_PROVIDER_STOP_BEGIN"
    cat "$V2_ROOT/stopped.json"
    echo "LEGOFS_V2_PROVIDER_STOP_END"
    echo "LEGOFS_V2_SERVER_STOPPED provider_pid=$provider_pid clients=$BOUNDED_CLIENT_COUNT socket_fds=0 tcp_fallbacks=0"
}

start_bounded_liveness()
{
    slot=$1
    host_incarnation=$2
    status_path="$V2_ROOT/client-$slot-liveness.local"
    log_path="$V2_ROOT/liveness-$slot.log"
    /bin/busybox rm -f "$status_path"
    # FunctionalModelOnly liveness remains fail-closed. These are watchdog
    # bounds, never cohort-start synchronization or isolation evidence.
    env \
        BADFS_HOST_AGENT_MODE=cxl-gate-liveness \
        BADFS_V2_CXL_DEVICE="$CXL_DEVICE" \
        BADFS_V2_PROVIDER_BOOTSTRAP="$FIXTURE/bounded-provider-bootstrap.bin" \
        BADFS_V2_PROVIDER_BOOTSTRAP_DIGEST="$BOOTSTRAP_DIGEST" \
        BADFS_V2_REGION_SET_MANIFEST="$FIXTURE/bounded-region-set.bin" \
        BADFS_V2_PROVIDER_LOCATOR_CATALOG="$FIXTURE/bounded-provider-locators.bin" \
        BADFS_HOST_LIVENESS_STATUS="$status_path" \
        BADFS_HOST_INCARNATION="$host_incarnation" \
        BADFS_V2_CLIENT_SLOT="$slot" \
        BADFS_HOST_LIVENESS_TTL_NS=5000000000 \
        BADFS_HOST_LIVENESS_MAX_POLL_GAP_NS=1000000000 \
        BADFS_HOST_LIVENESS_POLL_US=100000 \
        BADFS_HOST_LIVENESS_REARM_NONCE=$((70000 + slot)) \
        "$BIN/badfs-host-agent" > "$log_path" 2>&1 &
    liveness_pid=$!
    record_pid "liveness-$slot" "$liveness_pid"
    # File existence only proves that the local status page was initialized
    # EXPIRED.  The ProcessClient launcher may read HostAdmission only after
    # the HostAgent has published the exact ARMED record through CXL.
    wait_marker "$liveness_pid" "$log_path" \
        "CXL gate-liveness: armed authority_id=1 client_slot=$slot"
}

bounded_client_admit()
{
    slot=$1
    case "$slot" in ''|*[!0-9]*) fatal bounded-client-slot "$slot" ;; esac
    . "$FIXTURE/layout.env"
    [ -n "${BOUNDED_CLIENT_COUNT:-}" ] || fatal bounded-client-admit missing-fixture
    [ "$slot" -lt "$BOUNDED_CLIENT_COUNT" ] || fatal bounded-client-slot range
    eval host_provisioning_offset=\$CLIENT_${slot}_HOST_PROVISIONING_OFFSET
    eval client_provisioning_offset=\$CLIENT_${slot}_CLIENT_PROVISIONING_OFFSET
    eval host_incarnation=\$CLIENT_${slot}_HOST_INCARNATION
    eval process_incarnation=\$CLIENT_${slot}_PROCESS_INCARNATION
    eval exec_incarnation=\$CLIENT_${slot}_EXEC_INCARNATION

    env \
        BADFS_HOST_AGENT_PERSISTENCE_LAUNCHER_DAEMON="$BIN/badfs-host-agent" \
        BADFS_HOST_AGENT_PERSISTENCE_LAUNCHER_REGION_MANIFEST="$FIXTURE/bounded-region-set.bin" \
        BADFS_HOST_AGENT_PERSISTENCE_LAUNCHER_MANIFEST="$FIXTURE/runtime-persistence-$slot.bin" \
        BADFS_HOST_AGENT_PERSISTENCE_LAUNCHER_LOCATOR_CATALOG="$FIXTURE/host-$slot-locators.bin" \
        BADFS_HOST_AGENT_PERSISTENCE_LAUNCHER_PROVISIONING="$CXL_DEVICE" \
        BADFS_HOST_AGENT_PERSISTENCE_LAUNCHER_PROVISIONING_PROVIDER_OFFSET="$host_provisioning_offset" \
        BADFS_HOST_AGENT_PERSISTENCE_LAUNCHER_LOCAL_QUEUE="$V2_ROOT/persist-$slot.queue" \
        BADFS_HOST_AGENT_PERSISTENCE_LAUNCHER_LOCAL_QUEUE_CAPACITY=256 \
        BADFS_HOST_AGENT_PERSISTENCE_LAUNCHER_PROCESS_INCARNATION=$((40960 + slot)) \
        BADFS_HOST_AGENT_PERSISTENCE_LAUNCHER_EXEC_INCARNATION=$((45056 + slot)) \
        BADFS_HOST_AGENT_PERSISTENCE_LAUNCHER_NONCE=$(printf 'a1%.0s' $(seq 1 32)) \
        BADFS_HOST_AGENT_PERSISTENCE_LAUNCHER_CONTROLLER_ID=$(printf 'a2%.0s' $(seq 1 16)) \
        BADFS_HOST_AGENT_PERSISTENCE_LAUNCHER_CONTROLLER_TERM=$((57344 + slot)) \
        BADFS_HOST_AGENT_PERSISTENCE_LAUNCHER_CONTROLLER_TOKEN=$((61440 + slot)) \
        "$BIN/badfs-host-agent-persistence-launcher" > "$V2_ROOT/persistence-$slot.log" 2>&1 &
    persistence_pid=$!
    record_pid "persistence-$slot" "$persistence_pid"
    wait_marker "$persistence_pid" "$V2_ROOT/persistence-$slot.log" "provisioned-persistence: ready"

    start_bounded_liveness "$slot" "$host_incarnation"

    env \
        BADFS_CLIENT_LAUNCHER_PROVISIONING_MANIFEST="$FIXTURE/client-$slot-provisioning.bin" \
        BADFS_CLIENT_LAUNCHER_BOOTSTRAP="$FIXTURE/client-$slot-bootstrap.bin" \
        BADFS_CLIENT_LAUNCHER_REGION_SET="$FIXTURE/bounded-region-set.bin" \
        BADFS_CLIENT_LAUNCHER_LOCATOR_CATALOG="$FIXTURE/client-$slot-locators.bin" \
        BADFS_CLIENT_LAUNCHER_NAMESPACE_CATALOG="$FIXTURE/bounded-namespace.bin" \
        BADFS_CLIENT_LAUNCHER_AUTHORITY_CATALOG="$FIXTURE/client-$slot-authorities.bin" \
        BADFS_CLIENT_LAUNCHER_MARKER_CATALOG="$FIXTURE/client-$slot-markers.bin" \
        BADFS_CLIENT_LAUNCHER_SELECTOR="$FIXTURE/bounded-selector.bin" \
        BADFS_CLIENT_LAUNCHER_PROVISIONING="$CXL_DEVICE" \
        BADFS_CLIENT_LAUNCHER_PROVISIONING_PROVIDER_OFFSET="$client_provisioning_offset" \
        BADFS_CLIENT_LAUNCHER_PROCESS_INCARNATION="$process_incarnation" \
        BADFS_CLIENT_LAUNCHER_EXEC_INCARNATION="$exec_incarnation" \
        BADFS_CLIENT_LAUNCHER_NONCE=$(printf '94%.0s' $(seq 1 32)) \
        BADFS_CLIENT_LAUNCHER_CONTROLLER_ID=$(printf '95%.0s' $(seq 1 16)) \
        BADFS_CLIENT_LAUNCHER_CONTROLLER_TERM=$((589824 + slot * 4)) \
        BADFS_CLIENT_LAUNCHER_CONTROLLER_TOKEN=$((655360 + slot)) \
        "$BIN/badfs-client-launcher" -- "$BIN/badfs-client-admission-target" \
        > "$V2_ROOT/client-admission-$slot.log" 2>&1 || {
            cat "$V2_ROOT/client-admission-$slot.log" 2>/dev/null || true
            fatal bounded-client-launch failed
        }
    grep -q "LEGOFS_BOUNDED_CLIENT_READY slot=$slot" "$V2_ROOT/client-admission-$slot.log" || {
        cat "$V2_ROOT/client-admission-$slot.log" 2>/dev/null || true
        fatal bounded-client-ready missing
    }
    audit_persistent_sockets && socket_count=0 || socket_count=1
    [ "$socket_count" -eq 0 ] || fatal bounded-client-socket-fd detected
    echo "LEGOFS_V2_BOUNDED_CLIENT_ADMITTED slot=$slot socket_fds=0 tcp_fallbacks=0"
}

bounded_client_start()
{
    slot=$1
    iterations=$2
    target_kind=${BOUNDED_TARGET_KIND:-workload}
    case "$slot" in ''|*[!0-9]*) fatal bounded-client-slot "$slot" ;; esac
    case "$iterations" in ''|*[!0-9]*) fatal iterations "$iterations" ;; esac
    [ "$iterations" -gt 0 ] || fatal iterations zero
    . "$FIXTURE/layout.env"
    [ -n "${BOUNDED_CLIENT_COUNT:-}" ] || fatal bounded-client-start missing-fixture
    [ "$slot" -lt "$BOUNDED_CLIENT_COUNT" ] || fatal bounded-client-slot range
    eval host_provisioning_offset=\$CLIENT_${slot}_HOST_PROVISIONING_OFFSET
    eval client_provisioning_offset=\$CLIENT_${slot}_CLIENT_PROVISIONING_OFFSET
    eval host_incarnation=\$CLIENT_${slot}_HOST_INCARNATION
    eval process_incarnation=\$CLIENT_${slot}_PROCESS_INCARNATION
    eval exec_incarnation=\$CLIENT_${slot}_EXEC_INCARNATION
    lane_id=$((slot * 2 + 1))
    lane_count=$((BOUNDED_CLIENT_COUNT * 2))
    registered_slab_bytes=$((BOUNDED_CLIENT_COUNT * 2097152))

    /bin/busybox rm -f \
        "$V2_ROOT/activation-$slot.queue" \
        "$V2_ROOT/pin-$slot.queue" \
        "$V2_ROOT/persist-$slot.queue" \
        "$V2_ROOT/client-$slot-liveness.local" \
        "$V2_ROOT/client-$slot-qsbr.local" \
        "$V2_ROOT/workload-$slot.rc" \
        "$V2_ROOT/workload-$slot.socket-seen" \
        "$V2_ROOT/workload-$slot.phase"
    /bin/busybox rm -rf "$V2_ROOT/evidence-$slot"
    mkdir -p "$V2_ROOT/evidence-$slot"
    if [ "$target_kind" = workload ]; then
        /bin/busybox mkfifo "$V2_ROOT/workload-$slot.phase"
        (
            while IFS= read -r marker; do
                case "$marker" in
                LEGOFS_V2_PHASE_*) echo "$marker" ;;
                *) fatal bounded-phase-marker malformed ;;
                esac
            done < "$V2_ROOT/workload-$slot.phase"
        ) &
        record_pid "workload-phase-$slot" "$!"
    else
        [ "$target_kind" = calibration ] || fatal bounded-target-kind "$target_kind"
        require_file "$BIN/badfs-vd-bi-calibration-target"
    fi

    env \
        BADFS_HOST_AGENT_LOCAL_QUEUE="$V2_ROOT/activation-$slot.queue" \
        BADFS_HOST_AGENT_LOCAL_QUEUE_CAPACITY=16 \
        BADFS_MUTATION_AUTHORITY_ID=1 \
        BADFS_MUTATION_LANE_COUNT="$lane_count" \
        BADFS_MUTATION_LANE_DEPTH=256 \
        BADFS_MUTATION_REGISTERED_SLAB_BYTES="$registered_slab_bytes" \
        BADFS_MUTATION_FORMAT_GENERATION=2 \
        BADFS_MUTATION_CATALOG_GENERATION=41 \
        BADFS_MUTATION_REGION="$CXL_DEVICE" \
        BADFS_MUTATION_REGION_PROVIDER_OFFSET="$MUTATION_OFFSET" \
        BADFS_MUTATION_ASSIGNED_LANE_ID="$lane_id" \
        "$BIN/badfs-host-agent" > "$V2_ROOT/activation-$slot.log" 2>&1 &
    activation_pid=$!
    record_pid "activation-$slot" "$activation_pid"
    wait_marker "$activation_pid" "$V2_ROOT/activation-$slot.log" "activation-only P2 profile: ready"

    env \
        BADFS_HOST_AGENT_MODE=pin-registry \
        BADFS_HOST_AGENT_PIN_QUEUE="$V2_ROOT/pin-$slot.queue" \
        BADFS_HOST_AGENT_PIN_QUEUE_CAPACITY=16 \
        "$BIN/badfs-host-agent" > "$V2_ROOT/pin-$slot.log" 2>&1 &
    pin_pid=$!
    record_pid "pin-$slot" "$pin_pid"
    wait_marker "$pin_pid" "$V2_ROOT/pin-$slot.log" "pin-registry: ready"

    env \
        BADFS_HOST_AGENT_PERSISTENCE_LAUNCHER_DAEMON="$BIN/badfs-host-agent" \
        BADFS_HOST_AGENT_PERSISTENCE_LAUNCHER_REGION_MANIFEST="$FIXTURE/bounded-region-set.bin" \
        BADFS_HOST_AGENT_PERSISTENCE_LAUNCHER_MANIFEST="$FIXTURE/runtime-persistence-$slot.bin" \
        BADFS_HOST_AGENT_PERSISTENCE_LAUNCHER_LOCATOR_CATALOG="$FIXTURE/host-$slot-locators.bin" \
        BADFS_HOST_AGENT_PERSISTENCE_LAUNCHER_PROVISIONING="$CXL_DEVICE" \
        BADFS_HOST_AGENT_PERSISTENCE_LAUNCHER_PROVISIONING_PROVIDER_OFFSET="$host_provisioning_offset" \
        BADFS_HOST_AGENT_PERSISTENCE_LAUNCHER_LOCAL_QUEUE="$V2_ROOT/persist-$slot.queue" \
        BADFS_HOST_AGENT_PERSISTENCE_LAUNCHER_LOCAL_QUEUE_CAPACITY=256 \
        BADFS_HOST_AGENT_PERSISTENCE_LAUNCHER_PROCESS_INCARNATION=$((40960 + slot)) \
        BADFS_HOST_AGENT_PERSISTENCE_LAUNCHER_EXEC_INCARNATION=$((45056 + slot)) \
        BADFS_HOST_AGENT_PERSISTENCE_LAUNCHER_NONCE=$(printf 'a1%.0s' $(seq 1 32)) \
        BADFS_HOST_AGENT_PERSISTENCE_LAUNCHER_CONTROLLER_ID=$(printf 'a2%.0s' $(seq 1 16)) \
        BADFS_HOST_AGENT_PERSISTENCE_LAUNCHER_CONTROLLER_TERM=$((57344 + slot)) \
        BADFS_HOST_AGENT_PERSISTENCE_LAUNCHER_CONTROLLER_TOKEN=$((61440 + slot)) \
        "$BIN/badfs-host-agent-persistence-launcher" > "$V2_ROOT/persistence-$slot.log" 2>&1 &
    persistence_pid=$!
    record_pid "persistence-$slot" "$persistence_pid"
    wait_marker "$persistence_pid" "$V2_ROOT/persistence-$slot.log" "provisioned-persistence: ready"

    start_bounded_liveness "$slot" "$host_incarnation"

    "$BIN/badfs-bounded-client-local-init" \
        "$FIXTURE/client-$slot-bootstrap.bin" \
        "$FIXTURE/client-$slot-authorities.bin" \
        > "$V2_ROOT/local-init-$slot.log" 2>&1 || {
            cat "$V2_ROOT/local-init-$slot.log" 2>/dev/null || true
            fatal bounded-local-init failed
        }

    if [ "$target_kind" = calibration ]; then
      (
        env \
            BADFS_CLIENT_LAUNCHER_PROVISIONING_MANIFEST="$FIXTURE/client-$slot-provisioning.bin" \
            BADFS_CLIENT_LAUNCHER_BOOTSTRAP="$FIXTURE/client-$slot-bootstrap.bin" \
            BADFS_CLIENT_LAUNCHER_REGION_SET="$FIXTURE/bounded-region-set.bin" \
            BADFS_CLIENT_LAUNCHER_LOCATOR_CATALOG="$FIXTURE/client-$slot-locators.bin" \
            BADFS_CLIENT_LAUNCHER_NAMESPACE_CATALOG="$FIXTURE/bounded-namespace.bin" \
            BADFS_CLIENT_LAUNCHER_AUTHORITY_CATALOG="$FIXTURE/client-$slot-authorities.bin" \
            BADFS_CLIENT_LAUNCHER_MARKER_CATALOG="$FIXTURE/client-$slot-markers.bin" \
            BADFS_CLIENT_LAUNCHER_SELECTOR="$FIXTURE/bounded-selector.bin" \
            BADFS_CLIENT_LAUNCHER_PROVISIONING="$CXL_DEVICE" \
            BADFS_CLIENT_LAUNCHER_PROVISIONING_PROVIDER_OFFSET="$client_provisioning_offset" \
            BADFS_CLIENT_LAUNCHER_PROCESS_INCARNATION="$process_incarnation" \
            BADFS_CLIENT_LAUNCHER_EXEC_INCARNATION="$exec_incarnation" \
            BADFS_CLIENT_LAUNCHER_NONCE=$(printf '94%.0s' $(seq 1 32)) \
            BADFS_CLIENT_LAUNCHER_CONTROLLER_ID=$(printf '95%.0s' $(seq 1 16)) \
            BADFS_CLIENT_LAUNCHER_CONTROLLER_TERM=$((589824 + slot * 4)) \
            BADFS_CLIENT_LAUNCHER_CONTROLLER_TOKEN=$((655360 + slot)) \
            BADFS_BASE_PATH=/badfs \
            BADFS_HOST_AGENT_PIN_QUEUE="$V2_ROOT/pin-$slot.queue" \
            BADFS_PROFILE_C_PERSISTENCE_QUEUE="$V2_ROOT/persist-$slot.queue" \
            BADFS_HOST_AGENT_LOCAL_QUEUE="$V2_ROOT/activation-$slot.queue" \
            BADFS_HOST_AGENT_LOCAL_QUEUE_CAPACITY=16 \
            BADFS_MUTATION_PROCESS_LANE_CAP=1 \
            BADFS_BOUNDED_READY_MARKER=1 \
            "$BIN/badfs-client-launcher" -- \
            "$BIN/badfs-vd-bi-calibration-target" "$iterations" \
            > "$V2_ROOT/workload-$slot.stdout" 2> "$V2_ROOT/workload-$slot.stderr"
        rc=$?
        printf '%s\n' "$rc" > "$V2_ROOT/workload-$slot.rc"
        exit "$rc"
      ) &
    else
      (
        env \
            BADFS_CLIENT_LAUNCHER_PROVISIONING_MANIFEST="$FIXTURE/client-$slot-provisioning.bin" \
            BADFS_CLIENT_LAUNCHER_BOOTSTRAP="$FIXTURE/client-$slot-bootstrap.bin" \
            BADFS_CLIENT_LAUNCHER_REGION_SET="$FIXTURE/bounded-region-set.bin" \
            BADFS_CLIENT_LAUNCHER_LOCATOR_CATALOG="$FIXTURE/client-$slot-locators.bin" \
            BADFS_CLIENT_LAUNCHER_NAMESPACE_CATALOG="$FIXTURE/bounded-namespace.bin" \
            BADFS_CLIENT_LAUNCHER_AUTHORITY_CATALOG="$FIXTURE/client-$slot-authorities.bin" \
            BADFS_CLIENT_LAUNCHER_MARKER_CATALOG="$FIXTURE/client-$slot-markers.bin" \
            BADFS_CLIENT_LAUNCHER_SELECTOR="$FIXTURE/bounded-selector.bin" \
            BADFS_CLIENT_LAUNCHER_PROVISIONING="$CXL_DEVICE" \
            BADFS_CLIENT_LAUNCHER_PROVISIONING_PROVIDER_OFFSET="$client_provisioning_offset" \
            BADFS_CLIENT_LAUNCHER_PROCESS_INCARNATION="$process_incarnation" \
            BADFS_CLIENT_LAUNCHER_EXEC_INCARNATION="$exec_incarnation" \
            BADFS_CLIENT_LAUNCHER_NONCE=$(printf '94%.0s' $(seq 1 32)) \
            BADFS_CLIENT_LAUNCHER_CONTROLLER_ID=$(printf '95%.0s' $(seq 1 16)) \
            BADFS_CLIENT_LAUNCHER_CONTROLLER_TERM=$((589824 + slot * 4)) \
            BADFS_CLIENT_LAUNCHER_CONTROLLER_TOKEN=$((655360 + slot)) \
            BADFS_CLIENT_LAUNCHER_CHILD_LD_PRELOAD="$LIB/libbadfs_intercept.so" \
            BADFS_CLIENT_LAUNCHER_CHILD_LD_LIBRARY_PATH="$LIB" \
            BADFS_BASE_PATH=/badfs \
            BADFS_HOST_AGENT_PIN_QUEUE="$V2_ROOT/pin-$slot.queue" \
            BADFS_PROFILE_C_PERSISTENCE_QUEUE="$V2_ROOT/persist-$slot.queue" \
            BADFS_HOST_AGENT_LOCAL_QUEUE="$V2_ROOT/activation-$slot.queue" \
            BADFS_HOST_AGENT_LOCAL_QUEUE_CAPACITY=16 \
            BADFS_MUTATION_PROCESS_LANE_CAP=1 \
            BADFS_BOUNDED_READY_MARKER=1 \
            BADFS_POSIX_TRACE_DIR="$V2_ROOT/evidence-$slot" \
            "$BIN/badfs-client-launcher" -- \
            "$BIN/v2-io500-interface-workload" /badfs "$iterations" "$slot" \
            "$V2_ROOT/workload-$slot.phase" \
            > "$V2_ROOT/workload-$slot.stdout" 2> "$V2_ROOT/workload-$slot.stderr"
        rc=$?
        printf '%s\n' "$rc" > "$V2_ROOT/workload-$slot.rc"
        exit "$rc"
      ) &
    fi
    workload_pid=$!
    record_pid "workload-$slot" "$workload_pid"
    (
        while kill -0 "$workload_pid" 2>/dev/null; do
            children=$(cat "/proc/$workload_pid/task/$workload_pid/children" 2>/dev/null || true)
            for socket_process in $workload_pid $children; do
                if process_has_socket "$socket_process"; then
                    if [ ! -f "$V2_ROOT/workload-$slot.socket-seen" ]; then
                        report_process_sockets "workload-$slot" "$socket_process"
                    fi
                    : > "$V2_ROOT/workload-$slot.socket-seen"
                fi
            done
            sleep 0.01
        done
    ) &
    record_pid "workload-audit-$slot" "$!"
    wait_marker "$workload_pid" "$V2_ROOT/workload-$slot.stderr" \
        "LEGOFS_BOUNDED_CLIENT_READY slot=$slot" "$slot"
    if [ "$target_kind" = calibration ]; then
        echo "LEGOFS_VD_BI_CALIBRATION_WAITING slot=$slot lane=$lane_id socket_fds=0 tcp_fallbacks=0"
    else
        echo "LEGOFS_V2_BOUNDED_WORKLOAD_WAITING slot=$slot lane=$lane_id socket_fds=0 tcp_fallbacks=0"
    fi
}

bounded_client_calibrate()
{
    BOUNDED_TARGET_KIND=calibration bounded_client_start "$1" "$2"
}

bounded_client_calibration_wait()
{
    slot=$1
    case "$slot" in ''|*[!0-9]*) fatal bounded-client-slot "$slot" ;; esac
    rc_file="$V2_ROOT/workload-$slot.rc"
    attempt=0
    while [ "$attempt" -lt 18000 ]; do
        [ -f "$rc_file" ] && break
        attempt=$((attempt + 1))
        sleep 0.1
    done
    [ -f "$rc_file" ] || fatal bounded-calibration-timeout "$slot"
    rc=$(cat "$rc_file")
    socket_seen=0
    [ ! -f "$V2_ROOT/workload-$slot.socket-seen" ] || socket_seen=1
    if audit_persistent_sockets; then :; else socket_seen=1; fi
    echo "LEGOFS_VD_BI_CLIENT_CALIBRATION_BEGIN slot=$slot"
    sed -n 's/^LEGOFS_VD_BI_CLIENT_CALIBRATION //p' \
        "$V2_ROOT/workload-$slot.stdout" 2>/dev/null || true
    echo "LEGOFS_VD_BI_CLIENT_CALIBRATION_END slot=$slot"
    [ "$rc" -eq 0 ] || {
        cat "$V2_ROOT/workload-$slot.stderr" 2>/dev/null || true
        fatal bounded-calibration-exit "$rc"
    }
    calibration_count=$(grep -c '^LEGOFS_VD_BI_CLIENT_CALIBRATION ' \
        "$V2_ROOT/workload-$slot.stdout" 2>/dev/null || true)
    [ "$calibration_count" -eq 1 ] || fatal bounded-calibration-count "$calibration_count"
    [ "$socket_seen" -eq 0 ] || fatal calibration-socket-fd detected
    echo "LEGOFS_VD_BI_CALIBRATION_EXIT slot=$slot rc=0 records=1 socket_fds=0"
}

bounded_client_wait()
{
    slot=$1
    case "$slot" in ''|*[!0-9]*) fatal bounded-client-slot "$slot" ;; esac
    rc_file="$V2_ROOT/workload-$slot.rc"
    attempt=0
    while [ "$attempt" -lt 18000 ]; do
        [ -f "$rc_file" ] && break
        attempt=$((attempt + 1))
        sleep 0.1
    done
    [ -f "$rc_file" ] || fatal bounded-workload-timeout "$slot"
    rc=$(cat "$rc_file")
    socket_seen=0
    [ ! -f "$V2_ROOT/workload-$slot.socket-seen" ] || socket_seen=1
    if audit_persistent_sockets; then :; else socket_seen=1; fi
    echo "LEGOFS_V2_WORKLOAD_STDOUT_BEGIN slot=$slot"
    cat "$V2_ROOT/workload-$slot.stdout" 2>/dev/null || true
    echo "LEGOFS_V2_WORKLOAD_STDOUT_END slot=$slot"
    echo "LEGOFS_V2_WORKLOAD_STDERR_BEGIN slot=$slot"
    cat "$V2_ROOT/workload-$slot.stderr" 2>/dev/null || true
    echo "LEGOFS_V2_WORKLOAD_STDERR_END slot=$slot"
    summary_count=0
    for summary in "$V2_ROOT/evidence-$slot/"*.json; do
        [ -f "$summary" ] || continue
        summary_count=$((summary_count + 1))
        echo "LEGOFS_V2_PATH_SUMMARY_BEGIN slot=$slot file=${summary##*/}"
        cat "$summary"
        echo "LEGOFS_V2_PATH_SUMMARY_END slot=$slot file=${summary##*/}"
    done
    [ "$rc" -eq 0 ] || {
        echo "LEGOFS_V2_DAEMON_LOG_BEGIN path=$V2_ROOT/liveness-$slot.log"
        cat "$V2_ROOT/liveness-$slot.log" 2>/dev/null || true
        echo "LEGOFS_V2_DAEMON_LOG_END path=$V2_ROOT/liveness-$slot.log"
        echo "LEGOFS_V2_BOUNDED_WORKLOAD_EXIT slot=$slot rc=$rc summaries=$summary_count socket_fds=$socket_seen"
        exit "$rc"
    }
    [ "$summary_count" -eq 1 ] || fatal summary-count "$summary_count"
    [ "$socket_seen" -eq 0 ] || fatal workload-socket-fd detected
    echo "LEGOFS_V2_BOUNDED_WORKLOAD_EXIT slot=$slot rc=0 summaries=1 socket_fds=0"
}

client_start()
{
    writer_windows=$1
    case "$writer_windows" in ''|*[!0-9]*) fatal writer-windows "$writer_windows" ;; esac
    [ "$writer_windows" -gt 0 ] && [ "$writer_windows" -le 512 ] || fatal writer-windows range
    . "$FIXTURE/layout.env"
    digest=$(cat "$FIXTURE/bootstrap-digest.txt") || fatal bootstrap-digest read

    env \
        BADFS_HOST_AGENT_LOCAL_QUEUE="$V2_ROOT/activation.queue" \
        BADFS_HOST_AGENT_LOCAL_QUEUE_CAPACITY=16 \
        BADFS_MUTATION_AUTHORITY_ID=1 \
        BADFS_MUTATION_LANE_COUNT=2 \
        BADFS_MUTATION_LANE_DEPTH=256 \
        BADFS_MUTATION_REGISTERED_SLAB_BYTES=2097152 \
        BADFS_MUTATION_FORMAT_GENERATION=2 \
        BADFS_MUTATION_CATALOG_GENERATION=4 \
        BADFS_MUTATION_REGION="$CXL_DEVICE" \
        BADFS_MUTATION_REGION_PROVIDER_OFFSET="$MUTATION_OFFSET" \
        "$BIN/badfs-host-agent" > "$V2_ROOT/activation.log" 2>&1 &
    activation_pid=$!
    record_pid activation "$activation_pid"
    wait_marker "$activation_pid" "$V2_ROOT/activation.log" "activation-only P2 profile: ready"

    env \
        BADFS_HOST_AGENT_MODE=pin-registry \
        BADFS_HOST_AGENT_PIN_QUEUE="$V2_ROOT/pin.queue" \
        BADFS_HOST_AGENT_PIN_QUEUE_CAPACITY=16 \
        "$BIN/badfs-host-agent" > "$V2_ROOT/pin.log" 2>&1 &
    pin_pid=$!
    record_pid pin "$pin_pid"
    wait_marker "$pin_pid" "$V2_ROOT/pin.log" "pin-registry: ready"

    env \
        BADFS_HOST_AGENT_PERSISTENCE_LAUNCHER_DAEMON="$BIN/badfs-host-agent" \
        BADFS_HOST_AGENT_PERSISTENCE_LAUNCHER_REGION_MANIFEST="$FIXTURE/region-set.bin" \
        BADFS_HOST_AGENT_PERSISTENCE_LAUNCHER_MANIFEST="$FIXTURE/runtime-persistence.bin" \
        BADFS_HOST_AGENT_PERSISTENCE_LAUNCHER_LOCATOR_CATALOG="$FIXTURE/host-locators.bin" \
        BADFS_HOST_AGENT_PERSISTENCE_LAUNCHER_PROVISIONING="$CXL_DEVICE" \
        BADFS_HOST_AGENT_PERSISTENCE_LAUNCHER_PROVISIONING_PROVIDER_OFFSET="$HOST_PROVISIONING_OFFSET" \
        BADFS_HOST_AGENT_PERSISTENCE_LAUNCHER_LOCAL_QUEUE="$V2_ROOT/persist.queue" \
        BADFS_HOST_AGENT_PERSISTENCE_LAUNCHER_LOCAL_QUEUE_CAPACITY=256 \
        BADFS_HOST_AGENT_PERSISTENCE_LAUNCHER_PROCESS_INCARNATION=301 \
        BADFS_HOST_AGENT_PERSISTENCE_LAUNCHER_EXEC_INCARNATION=307 \
        BADFS_HOST_AGENT_PERSISTENCE_LAUNCHER_NONCE=$(printf 'a1%.0s' $(seq 1 32)) \
        BADFS_HOST_AGENT_PERSISTENCE_LAUNCHER_CONTROLLER_ID=$(printf 'a2%.0s' $(seq 1 16)) \
        BADFS_HOST_AGENT_PERSISTENCE_LAUNCHER_CONTROLLER_TERM=311 \
        BADFS_HOST_AGENT_PERSISTENCE_LAUNCHER_CONTROLLER_TOKEN=313 \
        "$BIN/badfs-host-agent-persistence-launcher" > "$V2_ROOT/persistence.log" 2>&1 &
    persistence_pid=$!
    record_pid persistence "$persistence_pid"
    wait_marker "$persistence_pid" "$V2_ROOT/persistence.log" "provisioned-persistence: ready"

    # FunctionalModelOnly liveness watchdog; see the bounded path above.
    env \
        BADFS_HOST_AGENT_MODE=cxl-gate-liveness \
        BADFS_V2_CXL_DEVICE="$CXL_DEVICE" \
        BADFS_V2_PROVIDER_BOOTSTRAP="$FIXTURE/provider-bootstrap.bin" \
        BADFS_V2_PROVIDER_BOOTSTRAP_DIGEST="$digest" \
        BADFS_V2_REGION_SET_MANIFEST="$FIXTURE/region-set.bin" \
        BADFS_V2_PROVIDER_LOCATOR_CATALOG="$FIXTURE/locators.bin" \
        BADFS_HOST_LIVENESS_STATUS="$V2_ROOT/liveness.status" \
        BADFS_HOST_INCARNATION=77 \
        BADFS_HOST_LIVENESS_TTL_NS=5000000000 \
        BADFS_HOST_LIVENESS_MAX_POLL_GAP_NS=1000000000 \
        BADFS_HOST_LIVENESS_POLL_US=100000 \
        BADFS_HOST_LIVENESS_REARM_NONCE=317 \
        "$BIN/badfs-host-agent" > "$V2_ROOT/liveness.log" 2>&1 &
    liveness_pid=$!
    record_pid liveness "$liveness_pid"
    wait_marker "$liveness_pid" "$V2_ROOT/liveness.log" "CXL gate-liveness: ready"

    /bin/busybox rm -rf "$V2_ROOT/client"
    env \
        BADFS_V2_CXL_DEVICE="$CXL_DEVICE" \
        BADFS_V2_CLIENT_BOOTSTRAP_OUTPUT="$V2_ROOT/client" \
        BADFS_V2_PROVIDER_BOOTSTRAP="$FIXTURE/provider-bootstrap.bin" \
        BADFS_V2_PROVIDER_BOOTSTRAP_DIGEST="$digest" \
        BADFS_ACTIVE_FORMAT_SELECTOR="$FIXTURE/selector.bin" \
        BADFS_V2_REGION_SET_MANIFEST="$FIXTURE/region-set.bin" \
        BADFS_V2_PROVIDER_LOCATOR_CATALOG="$FIXTURE/locators.bin" \
        BADFS_V2_CLIENT_LOCATOR_CATALOG="$FIXTURE/client-locators.bin" \
        BADFS_V2_NAMESPACE_CATALOG="$FIXTURE/namespace.bin" \
        BADFS_V2_RUNTIME_PERSISTENCE_MANIFEST="$FIXTURE/runtime-persistence.bin" \
        BADFS_HOST_LIVENESS_STATUS="$V2_ROOT/liveness.status" \
        BADFS_V2_QSBR_PATH="$V2_ROOT/client.qsbr" \
        BADFS_HOST_AGENT_LOCAL_QUEUE="$V2_ROOT/activation.queue" \
        BADFS_HOST_AGENT_LOCAL_QUEUE_CAPACITY=16 \
        BADFS_V2_BOOTSTRAP_PROCESS_INCARNATION=71 \
        BADFS_V2_CLIENT_PROCESS_INCARNATION=77 \
        BADFS_V2_WRITER_WINDOW_COUNT="$writer_windows" \
        BADFS_V2_BOOTSTRAP_TIMEOUT_MS=30000 \
        "$BIN/badfs-v2-client-bootstrap" > "$V2_ROOT/bootstrap.json" 2>&1 || {
            cat "$V2_ROOT/bootstrap.json" 2>/dev/null || true
            fatal client-bootstrap failed
        }
    audit_persistent_sockets && socket_count=0 || socket_count=1
    [ "$socket_count" -eq 0 ] || fatal client-service-socket-fd detected
    echo "LEGOFS_V2_CLIENT_READY writer_windows=$writer_windows socket_fds=0 tcp_fallbacks=0"
}

client_run()
{
    iterations=$1
    case "$iterations" in ''|*[!0-9]*) fatal iterations "$iterations" ;; esac
    [ "$iterations" -gt 0 ] || fatal iterations zero
    . "$FIXTURE/layout.env"
    mkdir -p "$V2_ROOT/evidence"

    /bin/busybox rm -f "$V2_ROOT/workload.phase"
    /bin/busybox mkfifo "$V2_ROOT/workload.phase"
    (
        while IFS= read -r marker; do
            case "$marker" in
            LEGOFS_V2_PHASE_*) echo "$marker" ;;
            *) fatal phase-marker malformed ;;
            esac
        done < "$V2_ROOT/workload.phase"
    ) &
    phase_marker_pid=$!
    record_pid workload-phase "$phase_marker_pid"
    env \
        BADFS_CLIENT_LAUNCHER_PROVISIONING_MANIFEST="$V2_ROOT/client/client-provisioning.bin" \
        BADFS_CLIENT_LAUNCHER_BOOTSTRAP="$V2_ROOT/client/client-bootstrap.bin" \
        BADFS_CLIENT_LAUNCHER_REGION_SET="$FIXTURE/region-set.bin" \
        BADFS_CLIENT_LAUNCHER_LOCATOR_CATALOG="$V2_ROOT/client/client-locators.bin" \
        BADFS_CLIENT_LAUNCHER_NAMESPACE_CATALOG="$FIXTURE/namespace.bin" \
        BADFS_CLIENT_LAUNCHER_AUTHORITY_CATALOG="$V2_ROOT/client/client-authorities.bin" \
        BADFS_CLIENT_LAUNCHER_MARKER_CATALOG="$V2_ROOT/client/client-markers.bin" \
        BADFS_CLIENT_LAUNCHER_PROFILE_C_MANIFEST="$V2_ROOT/client/profile-c-write.bin" \
        BADFS_CLIENT_LAUNCHER_SELECTOR="$FIXTURE/selector.bin" \
        BADFS_CLIENT_LAUNCHER_PROVISIONING="$CXL_DEVICE" \
        BADFS_CLIENT_LAUNCHER_PROVISIONING_PROVIDER_OFFSET="$CLIENT_PROVISIONING_OFFSET" \
        BADFS_CLIENT_LAUNCHER_PROCESS_INCARNATION=77 \
        BADFS_CLIENT_LAUNCHER_EXEC_INCARNATION=331 \
        BADFS_CLIENT_LAUNCHER_NONCE=$(printf '94%.0s' $(seq 1 32)) \
        BADFS_CLIENT_LAUNCHER_CONTROLLER_ID=$(printf '95%.0s' $(seq 1 16)) \
        BADFS_CLIENT_LAUNCHER_CONTROLLER_TERM=337 \
        BADFS_CLIENT_LAUNCHER_CONTROLLER_TOKEN=347 \
        BADFS_CLIENT_LAUNCHER_CHILD_LD_PRELOAD="$LIB/libbadfs_intercept.so" \
        BADFS_CLIENT_LAUNCHER_CHILD_LD_LIBRARY_PATH="$LIB" \
        BADFS_BASE_PATH=/badfs \
        BADFS_HOST_AGENT_PIN_QUEUE="$V2_ROOT/pin.queue" \
        BADFS_PROFILE_C_PERSISTENCE_QUEUE="$V2_ROOT/persist.queue" \
        BADFS_HOST_AGENT_LOCAL_QUEUE="$V2_ROOT/activation.queue" \
        BADFS_HOST_AGENT_LOCAL_QUEUE_CAPACITY=16 \
        BADFS_MUTATION_PROCESS_LANE_CAP=2 \
        BADFS_POSIX_TRACE_DIR="$V2_ROOT/evidence" \
        "$BIN/badfs-client-launcher" -- \
        "$BIN/v2-io500-interface-workload" /badfs "$iterations" 0 \
        "$V2_ROOT/workload.phase" \
        > "$V2_ROOT/workload.stdout" 2> "$V2_ROOT/workload.stderr" &
    workload_pid=$!
    socket_seen_file="$V2_ROOT/workload.socket-seen"
    /bin/busybox rm -f "$socket_seen_file"
    (
        while kill -0 "$workload_pid" 2>/dev/null; do
            children=$(cat "/proc/$workload_pid/task/$workload_pid/children" 2>/dev/null || true)
            for socket_process in $workload_pid $children; do
                if process_has_socket "$socket_process"; then
                    if [ ! -f "$socket_seen_file" ]; then
                        report_process_sockets workload "$socket_process"
                    fi
                    : > "$socket_seen_file"
                fi
            done
            sleep 0.01
        done
    ) &
    socket_audit_pid=$!
    # Reap the workload in the parent.  Polling kill -0 here would keep
    # reporting a completed-but-unreaped child as alive forever and turn a
    # successful data-path run into a harness timeout.
    wait "$workload_pid"
    rc=$?
    wait "$phase_marker_pid"
    phase_marker_rc=$?
    kill "$socket_audit_pid" 2>/dev/null || true
    wait "$socket_audit_pid" 2>/dev/null || true
    socket_seen=0
    [ ! -f "$socket_seen_file" ] || socket_seen=1
    if audit_persistent_sockets; then :; else socket_seen=1; fi

    echo "LEGOFS_V2_WORKLOAD_STDOUT_BEGIN"
    cat "$V2_ROOT/workload.stdout" 2>/dev/null || true
    echo "LEGOFS_V2_WORKLOAD_STDOUT_END"
    echo "LEGOFS_V2_WORKLOAD_STDERR_BEGIN"
    cat "$V2_ROOT/workload.stderr" 2>/dev/null || true
    echo "LEGOFS_V2_WORKLOAD_STDERR_END"
    summary_count=0
    for summary in "$V2_ROOT/evidence/"*.json; do
        [ -f "$summary" ] || continue
        summary_count=$((summary_count + 1))
        echo "LEGOFS_V2_PATH_SUMMARY_BEGIN file=${summary##*/}"
        cat "$summary"
        echo "LEGOFS_V2_PATH_SUMMARY_END file=${summary##*/}"
    done
    if [ "$rc" -ne 0 ]; then
        echo "LEGOFS_V2_WORKLOAD_EXIT rc=$rc summaries=$summary_count socket_fds=$socket_seen"
        exit "$rc"
    fi
    [ "$summary_count" -eq 1 ] || fatal summary-count "$summary_count"
    [ "$phase_marker_rc" -eq 0 ] || fatal phase-marker-forward failed
    [ "$socket_seen" -eq 0 ] || fatal workload-socket-fd detected
    echo "LEGOFS_V2_WORKLOAD_EXIT rc=0 summaries=$summary_count socket_fds=0"
}

case "${1:-}" in
prepare) prepare ;;
server-start) server_start ;;
server-admit) bounded_server_admit ;;
server-stop) bounded_server_stop ;;
bounded-client-admit) bounded_client_admit "${2:-}" ;;
bounded-client-start) bounded_client_start "${2:-}" "${3:-}" ;;
bounded-client-wait) bounded_client_wait "${2:-}" ;;
bounded-client-calibrate) bounded_client_calibrate "${2:-}" "${3:-}" ;;
bounded-client-calibration-wait) bounded_client_calibration_wait "${2:-}" ;;
client-start) client_start "${2:-}" ;;
client-run) client_run "${2:-}" ;;
*) fatal command "${1:-missing}" ;;
esac
