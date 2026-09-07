#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
LEGOFS_ROOT="$ROOT/components/legofs"
BUILD_ROOT="$ROOT/target/build/giga-native"
RESULT_ROOT="$ROOT/target/results/giga-native"
BUILD_SCRIPT="$ROOT/scripts/build_giga_native_io500.sh"
RUNNER="$LEGOFS_ROOT/scripts/run_lifecycle_io500.py"

TOPOLOGY=
PROFILE=
RUN_ID=
BUILD_FIRST=false
PREFLIGHT_ONLY=false
PRINT_COMMAND=false
CQ_WAIT_MODE=timer_sleep
AUTHORITY_WAIT_MODE=timer_sleep
SERVER_CPUS=

usage()
{
	cat <<'EOF'
Usage: run_giga_native_legofs_io500.sh OPTIONS

Required:
  --topology 1c1s|2c1s|3c1s
  --profile bounded|find-valid|capacity-5s|capacity-20s|official
  --run-id ID

Optional:
  --cq-wait-mode timer_sleep|cooperative_yield (default: timer_sleep)
  --authority-wait-mode timer_sleep|cooperative_yield (default: timer_sleep)
  --server-cpus LIST  Bind server threads to CPUs in the reviewed server LLC.
  --build            Build and validate native artifacts first.
  --preflight-only   Validate the host and command without creating a run.
  --print-command    Print the quoted component-runner command and exit.
  -h, --help         Show this help.

This is a same-host volatile NUMA1/LLC-coherence model. It does not claim
physical CXL BI, HDM-DB, GPF, NAND persistence, or CXL-link performance.
EOF
}

die()
{
	printf 'giga-native-run=FAIL reason=%s\n' "$*" >&2
	exit 2
}

while (($#)); do
	case "$1" in
	--topology)
		(($# >= 2)) || die "--topology requires a value"
		TOPOLOGY="$2"
		shift 2
		;;
	--profile)
		(($# >= 2)) || die "--profile requires a value"
		PROFILE="$2"
		shift 2
		;;
	--run-id)
		(($# >= 2)) || die "--run-id requires a value"
		RUN_ID="$2"
		shift 2
		;;
	--cq-wait-mode|--authority-wait-mode)
		(($# >= 2)) || die "$1 requires a value"
		case "$2" in timer_sleep|cooperative_yield) ;; *) die "invalid wait mode: $2" ;; esac
		if [ "$1" = --cq-wait-mode ]; then CQ_WAIT_MODE="$2"; else AUTHORITY_WAIT_MODE="$2"; fi
		shift 2
		;;
	--server-cpus)
		(($# >= 2)) || die "--server-cpus requires a value"
		[[ "$2" =~ ^[0-9]+(,[0-9]+)*$ ]] || die "invalid --server-cpus: $2"
		SERVER_CPUS="$2"
		shift 2
		;;
	--build)
		BUILD_FIRST=true
		shift
		;;
	--preflight-only)
		PREFLIGHT_ONLY=true
		shift
		;;
	--print-command)
		PRINT_COMMAND=true
		shift
		;;
	-h|--help)
		usage
		exit 0
		;;
	*) die "unknown argument: $1" ;;
	esac
done

case "$TOPOLOGY" in
1c1s)
	MPI_RANKS=1
	SERVER_CPU=7
	CLIENT_CPUS=1
	;;
2c1s)
	MPI_RANKS=2
	SERVER_CPU=13
	CLIENT_CPUS=1,7
	;;
3c1s)
	MPI_RANKS=3
	SERVER_CPU=19
	CLIENT_CPUS=1,7,13
	;;
*) die "--topology must be 1c1s, 2c1s, or 3c1s" ;;
esac

case "$PROFILE" in
bounded)
	CONFIG="$LEGOFS_ROOT/scripts/lifecycle-io500-all-bounded.ini"
	TRACE_MODE=full
	TIMEOUT=7200
	;;
find-valid)
	CONFIG="$LEGOFS_ROOT/scripts/lifecycle-io500-find-valid-bounded.ini"
	TRACE_MODE=full
	TIMEOUT=7200
	;;
capacity-5s)
	CONFIG="$LEGOFS_ROOT/scripts/lifecycle-io500-capacity-safe-5s.ini"
	TRACE_MODE=off
	TIMEOUT=7200
	;;
capacity-20s)
	CONFIG="$LEGOFS_ROOT/scripts/lifecycle-io500-capacity-safe-20s.ini"
	TRACE_MODE=off
	TIMEOUT=7200
	;;
official)
	CONFIG="$LEGOFS_ROOT/scripts/lifecycle-io500-official-300s.ini"
	TRACE_MODE=off
	TIMEOUT=43200
	;;
*) die "--profile must be bounded, find-valid, capacity-5s, capacity-20s, or official" ;;
esac

[[ "$RUN_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] ||
	die "--run-id contains unsupported characters"

RUN_ROOT="/dev/shm/legofs-$RUN_ID"
BUNDLE="$RESULT_ROOT/$RUN_ID"
COMMAND_MODE=run
if [ "$PREFLIGHT_ONLY" = true ]; then
	COMMAND_MODE=preflight
fi

command=(
	python3 "$RUNNER" "$COMMAND_MODE"
	--build-manifest "$RESULT_ROOT/build-manifest.json"
	--claim cxl-numa-samehost
	--run-id "$RUN_ID"
	--run-root "$RUN_ROOT"
	--bundle-dir "$BUNDLE"
	--allowed-root "$RESULT_ROOT"
	--server-bin "$BUILD_ROOT/bin/badfs-server"
	--intercept-so "$BUILD_ROOT/lib/libbadfs_intercept.so"
	--inspector-bin "$BUILD_ROOT/bin/badfs-bench"
	--io500-bin "$BUILD_ROOT/bin/io500"
	--io500-verify "$BUILD_ROOT/bin/io500-verify"
	--mpirun /usr/bin/mpirun.openmpi
	--config-source "$CONFIG"
	--server-port 3345
	--server-count 1
	--mpi-ranks "$MPI_RANKS"
	--packed-small-segments on
	--small-segment-count 2048
	--durability-profile coherent-seal-no-writeback
	--payload-persistence-owner writer-receipt
	--close-batch-mode batched
	--pool-max-extents 524288
	--write-arena-slots 64
	--client-read-cache on
	--read-cache-entries 2048
	--timeout "$TIMEOUT"
	--lifecycle-trace "$TRACE_MODE"
	--placement-verifier "$LEGOFS_ROOT/scripts/verify_lifecycle_pool_numa.py"
	--numactl /usr/bin/numactl
	--cpu-node 0
	--memory-node 1
	--wrong-node 0
	--serving-transport cxl
	--serving-cursor-mode owned
	--serving-cq-wait-mode "$CQ_WAIT_MODE"
	--serving-authority-wait-mode "$AUTHORITY_WAIT_MODE"
	--serving-max-clients 64
	--region-size-gib 64
	--server-cpu "$SERVER_CPU"
	--client-cpus "$CLIENT_CPUS"
	--rank-launcher "$LEGOFS_ROOT/scripts/giga_native_rank.py"
)

if [ -n "$SERVER_CPUS" ]; then
	command+=(--server-cpus "$SERVER_CPUS")
fi

if [ "$PRINT_COMMAND" = true ]; then
	printf '%q ' "${command[@]}"
	printf '\n'
	exit 0
fi

[ -x "$RUNNER" ] || die "component runner is missing or not executable: $RUNNER"
[ -x "$BUILD_SCRIPT" ] || die "native build script is missing: $BUILD_SCRIPT"
if [ "$BUILD_FIRST" = true ]; then
	"$BUILD_SCRIPT"
fi
[ -d "$RESULT_ROOT" ] || die "result root is missing; run the build first"
[ ! -e "$RUN_ROOT" ] || die "run root already exists: $RUN_ROOT"
[ ! -e "$BUNDLE" ] || die "result bundle already exists: $BUNDLE"
for artifact in \
	"$BUILD_ROOT/bin/badfs-server" \
	"$BUILD_ROOT/bin/badfs-bench" \
	"$BUILD_ROOT/bin/io500" \
	"$BUILD_ROOT/bin/io500-verify" \
	"$BUILD_ROOT/lib/libbadfs_intercept.so"; do
	[ -f "$artifact" ] || die "native artifact is missing: $artifact"
done

printf 'giga-native-run topology=%s profile=%s run_id=%s mode=%s\n' \
	"$TOPOLOGY" "$PROFILE" "$RUN_ID" "$COMMAND_MODE"
exec "${command[@]}"
