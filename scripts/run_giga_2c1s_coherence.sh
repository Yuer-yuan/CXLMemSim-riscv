#!/usr/bin/env bash
# Reusable 2-client/1-server coherence probe for the CXL System-RAM on giga.
#
# Local mode copies this script and its C payload into a unique directory under
# giga:/tmp, executes remote mode, and removes the directory afterward.  The
# explicitly selected RLCXL_REMOTE_ROOT tree is read-only: its arena implementation is
# compiled into the temporary probe but is never modified.
#
# Optional overrides:
#   GIGA_HOST=giga C0_CPU=1 C1_CPU=7 SERVER_CPU=13 CXL_NODE=1 \
#   ITERATIONS=100000 SEQCLOCK_UPDATES=1000000 PAYLOAD_MIB=30 HOLD_MS=4000 \
#   ./scripts/run_giga_2c1s_coherence.sh
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_NAME="$(basename "${BASH_SOURCE[0]}")"
SOURCE_NAME="giga_2c1s_coherence.c"

GIGA_HOST="${GIGA_HOST:-giga}"
C0_CPU="${C0_CPU:-1}"
C1_CPU="${C1_CPU:-7}"
SERVER_CPU="${SERVER_CPU:-13}"
CXL_NODE="${CXL_NODE:-1}"
ITERATIONS="${ITERATIONS:-100000}"
SEQCLOCK_UPDATES="${SEQCLOCK_UPDATES:-1000000}"
PAYLOAD_MIB="${PAYLOAD_MIB:-30}"
HOLD_MS="${HOLD_MS:-4000}"
RLCXL_REMOTE_ROOT="${RLCXL_REMOTE_ROOT:?Set RLCXL_REMOTE_ROOT to the remote source directory}"

SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=8)

retry() {
    local attempt
    for attempt in 1 2 3 4 5; do
        if "$@"; then
            return 0
        fi
        [[ $attempt -eq 5 ]] || sleep 2
    done
    return 1
}

require_uint() {
    local name="$1" value="$2"
    [[ "$value" =~ ^[0-9]+$ ]] || {
        echo "$name must be an unsigned integer, got: $value" >&2
        exit 2
    }
}

for pair in \
    "C0_CPU:$C0_CPU" "C1_CPU:$C1_CPU" "SERVER_CPU:$SERVER_CPU" \
    "CXL_NODE:$CXL_NODE" "ITERATIONS:$ITERATIONS" \
    "SEQCLOCK_UPDATES:$SEQCLOCK_UPDATES" \
    "PAYLOAD_MIB:$PAYLOAD_MIB" "HOLD_MS:$HOLD_MS"; do
    require_uint "${pair%%:*}" "${pair#*:}"
done

if [[ "${1:-}" != "--remote" ]]; then
    [[ -r "${SCRIPT_DIR}/${SOURCE_NAME}" ]] || {
        echo "missing payload source: ${SCRIPT_DIR}/${SOURCE_NAME}" >&2
        exit 1
    }

    remote_dir=""
    for attempt in 1 2 3 4 5; do
        if remote_dir=$(ssh "${SSH_OPTS[@]}" "$GIGA_HOST" \
            'mktemp -d /tmp/rlcxl-2c1s.XXXXXX'); then
            break
        fi
        [[ $attempt -eq 5 ]] || sleep 2
    done
    [[ "$remote_dir" =~ ^/tmp/rlcxl-2c1s\.[A-Za-z0-9]+$ ]] || {
        echo "refusing unexpected remote temporary path: $remote_dir" >&2
        exit 1
    }

    cleanup_remote_dir() {
        local command
        command="rm -f -- '${remote_dir}/${SCRIPT_NAME}' '${remote_dir}/${SOURCE_NAME}' '${remote_dir}/coherence_2c1s' '${remote_dir}/server.log' '${remote_dir}/client0.log' '${remote_dir}/client1.log'; rmdir -- '${remote_dir}'"
        retry ssh "${SSH_OPTS[@]}" "$GIGA_HOST" "$command" >/dev/null 2>&1 ||
            echo "warning: could not fully remove $GIGA_HOST:$remote_dir" >&2
    }
    trap cleanup_remote_dir EXIT

    echo "syncing reusable probe to ${GIGA_HOST}:${remote_dir}"
    retry scp -q "${SSH_OPTS[@]}" \
        "${BASH_SOURCE[0]}" "${SCRIPT_DIR}/${SOURCE_NAME}" \
        "${GIGA_HOST}:${remote_dir}/"

    remote_command="cd '${remote_dir}' && chmod 700 '${SCRIPT_NAME}' && env C0_CPU='${C0_CPU}' C1_CPU='${C1_CPU}' SERVER_CPU='${SERVER_CPU}' CXL_NODE='${CXL_NODE}' ITERATIONS='${ITERATIONS}' SEQCLOCK_UPDATES='${SEQCLOCK_UPDATES}' PAYLOAD_MIB='${PAYLOAD_MIB}' HOLD_MS='${HOLD_MS}' RLCXL_REMOTE_ROOT='${RLCXL_REMOTE_ROOT}' './${SCRIPT_NAME}' --remote"
    for attempt in 1 2 3 4 5; do
        set +e
        ssh "${SSH_OPTS[@]}" "$GIGA_HOST" "$remote_command"
        remote_status=$?
        set -e
        if [[ $remote_status -eq 0 ]]; then
            exit 0
        fi
        # 255 is an SSH transport failure.  Any other status came from the
        # probe itself and must not be hidden by automatically rerunning it.
        [[ $remote_status -eq 255 && $attempt -lt 5 ]] || exit "$remote_status"
        sleep 2
    done
fi

WORK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="${WORK_DIR}/${SOURCE_NAME}"
BINARY="${WORK_DIR}/coherence_2c1s"
SERVER_LOG="${WORK_DIR}/server.log"
CLIENT0_LOG="${WORK_DIR}/client0.log"
CLIENT1_LOG="${WORK_DIR}/client1.log"
ARENA_NAME="/rlcxl_2c1s_${$}"
SERVER_PID=""
CLIENT0_PID=""
CLIENT1_PID=""

cleanup_run() {
    local pid
    for pid in "$CLIENT0_PID" "$CLIENT1_PID" "$SERVER_PID"; do
        [[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true
    done
    for pid in "$CLIENT0_PID" "$CLIENT1_PID" "$SERVER_PID"; do
        [[ -n "$pid" ]] && wait "$pid" 2>/dev/null || true
    done
    rm -f -- "/dev/shm/${ARENA_NAME#/}"
}
trap cleanup_run EXIT INT TERM

[[ -r "${RLCXL_REMOTE_ROOT}/src/cxl_arena.c" &&
   -r "${RLCXL_REMOTE_ROOT}/src/cxl_arena.h" ]] || {
    echo "missing read-only arena sources under ${RLCXL_REMOTE_ROOT}/src" >&2
    exit 1
}
command -v gcc >/dev/null || { echo "gcc is required on giga" >&2; exit 1; }
command -v taskset >/dev/null || { echo "taskset is required on giga" >&2; exit 1; }
[[ -d "/sys/devices/system/node/node${CXL_NODE}" ]] || {
    echo "NUMA node ${CXL_NODE} is absent" >&2
    exit 1
}

cache_id_for_cpu() {
    local cpu="$1" entry
    [[ -d "/sys/devices/system/cpu/cpu${cpu}" ]] || return 1
    for entry in /sys/devices/system/cpu/cpu"${cpu}"/cache/index*; do
        [[ -r "${entry}/level" && -r "${entry}/type" && -r "${entry}/id" ]] || continue
        if [[ "$(<"${entry}/level")" == 3 && "$(<"${entry}/type")" == Unified ]]; then
            cat "${entry}/id"
            return 0
        fi
    done
    return 1
}

C0_L3="$(cache_id_for_cpu "$C0_CPU")"
C1_L3="$(cache_id_for_cpu "$C1_CPU")"
SERVER_L3="$(cache_id_for_cpu "$SERVER_CPU")"
if [[ "$C0_L3" == "$C1_L3" || "$C0_L3" == "$SERVER_L3" ||
      "$C1_L3" == "$SERVER_L3" ]]; then
    echo "selected CPUs do not occupy three distinct L3 domains:" >&2
    echo "  client0 cpu=$C0_CPU l3=$C0_L3" >&2
    echo "  client1 cpu=$C1_CPU l3=$C1_L3" >&2
    echo "  server  cpu=$SERVER_CPU l3=$SERVER_L3" >&2
    exit 1
fi

echo "=== 2C1S probe boundary ==="
echo "host=$(hostname) arena=$ARENA_NAME cxl_node=$CXL_NODE"
echo "client0_cpu=$C0_CPU client0_l3=$C0_L3"
echo "client1_cpu=$C1_CPU client1_l3=$C1_L3"
echo "server_cpu=$SERVER_CPU server_l3=$SERVER_L3"
echo "iterations=$ITERATIONS seqlock_updates=$SEQCLOCK_UPDATES payload_per_client_MiB=$PAYLOAD_MIB"
echo "isolation=physical-L3-separation resctrl=unchanged devmem=unused"

gcc -O2 -g -Wall -Wextra -std=gnu11 -march=native \
    -I"${RLCXL_REMOTE_ROOT}/src" \
    "$SOURCE" "${RLCXL_REMOTE_ROOT}/src/cxl_arena.c" \
    -o "$BINARY" -lrt -lpthread

taskset -c "$SERVER_CPU" "$BINARY" server "$ARENA_NAME" "$CXL_NODE" \
    "$ITERATIONS" "$PAYLOAD_MIB" "$HOLD_MS" "$SEQCLOCK_UPDATES" \
    >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!

for _ in $(seq 1 300); do
    grep -q '^SERVER_READY ' "$SERVER_LOG" 2>/dev/null && break
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        cat "$SERVER_LOG" >&2
        echo "server exited before publishing the arena" >&2
        exit 1
    fi
    sleep 0.02
done
grep -q '^SERVER_READY ' "$SERVER_LOG" || {
    cat "$SERVER_LOG" >&2
    echo "server did not become ready" >&2
    exit 1
}

taskset -c "$C0_CPU" "$BINARY" client "$ARENA_NAME" 0 >"$CLIENT0_LOG" 2>&1 &
CLIENT0_PID=$!
taskset -c "$C1_CPU" "$BINARY" client "$ARENA_NAME" 1 >"$CLIENT1_LOG" 2>&1 &
CLIENT1_PID=$!

for _ in $(seq 1 300); do
    grep -q '^INSPECT_READY ' "$SERVER_LOG" 2>/dev/null && break
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        cat "$SERVER_LOG" "$CLIENT0_LOG" "$CLIENT1_LOG" >&2
        echo "a role exited before inspection" >&2
        exit 1
    fi
    sleep 0.02
done
grep -q '^INSPECT_READY ' "$SERVER_LOG" || {
    cat "$SERVER_LOG" "$CLIENT0_LOG" "$CLIENT1_LOG" >&2
    echo "the three roles did not rendezvous" >&2
    exit 1
}

echo
echo "=== live process and placement evidence ==="
grep -E '^(SERVER_READY|INSPECT_READY) ' "$SERVER_LOG"
ps -o pid=,psr=,stat=,comm= -p "$CLIENT0_PID,$CLIENT1_PID,$SERVER_PID" | sort -n
for role_pid in \
    "client0:${CLIENT0_PID}" "client1:${CLIENT1_PID}" "server:${SERVER_PID}"; do
    role="${role_pid%%:*}"
    pid="${role_pid#*:}"
    affinity="$(taskset -pc "$pid" 2>/dev/null | sed 's/^.*: //')"
    map_line="$(grep -F "${ARENA_NAME#/}" "/proc/${pid}/numa_maps" 2>/dev/null || true)"
    echo "$role pid=$pid affinity=$affinity"
    if [[ -n "$map_line" ]]; then
        echo "  numa_maps: $map_line"
    else
        echo "  numa_maps: arena mapping not found"
    fi
done

set +e
wait "$SERVER_PID"; server_status=$?
wait "$CLIENT0_PID"; client0_status=$?
wait "$CLIENT1_PID"; client1_status=$?
set -e
SERVER_PID=""
CLIENT0_PID=""
CLIENT1_PID=""

echo
echo "=== role output ==="
sed 's/^/[server]  /' "$SERVER_LOG"
sed 's/^/[client0] /' "$CLIENT0_LOG"
sed 's/^/[client1] /' "$CLIENT1_LOG"

if [[ $server_status -ne 0 || $client0_status -ne 0 || $client1_status -ne 0 ]] ||
   ! grep -q '^RESULT PASS ' "$SERVER_LOG"; then
    echo "2C1S probe FAILED (server=$server_status client0=$client0_status client1=$client1_status)" >&2
    exit 1
fi

echo
echo "2C1S probe PASSED; the shared arena and remote temporary files will now be removed."
