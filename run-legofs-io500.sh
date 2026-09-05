#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$ROOT/scripts/legofs_toolchain_path.sh"
legofs_toolchain_activate io500-run bash cat debugfs getconf mke2fs python3

BUILD_ONLY=0
RUN_ONLY=0
PAYLOAD_ONLY=0
STAGE=tiny
SERVER_COUNT=1
CLIENT_COUNT=10
RESULT_LABEL=
TIMEOUT=7200
FULL_COHERENCE_TRACE=0
SERVING_TRANSPORT=legacy
CURSOR_MODE=owned
CQ_WAIT_MODE=timer_sleep
DURABILITY_PROFILE=d-before-v
PAYLOAD_PERSISTENCE_OWNER=auto
WRITER_PERSIST_PROVIDER=msync
# Keep the already-gated, capacity-independent v7 allocator in the product
# and benchmark profile. v6 remains an explicit diagnostic selector.
PACKED_SMALL_SEGMENTS=on
SMALL_SEGMENT_COUNT=2048
SMALL_SEGMENT_COUNT_EXPLICIT=0
FAULT_PROFILE=none
OBSERVATION_MODE=
OBSERVATION_SAMPLE_SHIFT=
OBSERVATION_ARENA_MIB=
OBSERVATION_PROFILE_MANIFEST=
HOST_PROFILER=off
JOBS="$(getconf _NPROCESSORS_ONLN 2>/dev/null || printf '1\n')"

usage()
{
	cat <<'EOF'
Usage: ./run-legofs-io500.sh [OPTIONS]

  --build-only             build the RISC-V platform and current LegoFS payload
  --run-only               use the existing workspace build
  --payload-only           rebuild only the LegoFS guest payload
  --stage hello|tiny|stress-tiny|rollover-smoke|easy-smoke|hard-smoke|metadata-smoke|small-close-smoke|rnd4k|scc|standard
  --server-count 1|2       LegoFS server guests (default: 1)
  --client-count N         client guests / MPI ranks, 1..10 (default: 10)
  --result-label LABEL     separate runtime/result directory name
  --timeout SECONDS        stage timeout (default: 7200)
  --full-coherence-trace   diagnostic: record every MESI event
  --serving-transport MODE LegoFS serving path: legacy or cxl (default: legacy)
	  --cursor-mode MODE      CXL lane cursor algorithm: legacy_shared or owned (default: owned)
  --cq-wait-mode MODE     active CQ wait: timer_sleep or cooperative_yield (default: timer_sleep)
  --durability-profile PROFILE
                           d-before-v, coherent-seal-no-writeback, or coherent-seal-needs-writeback
  --payload-persistence-owner OWNER
                           auto, authority-bi-acquire, writer-receipt, placement-routed,
                           or writer-before-visibility
  --writer-persist-provider PROVIDER
                           msync or riscv-zicbom-dax (default: msync)
  --packed-small-segments MODE
                           lifecycle layout: on (v7 product default) or off (v6 diagnostic)
  --small-segment-count N number of 2 MiB packed segments per authority (default: 2048)
  --fault-profile PROFILE none, lifecycle fault gates, or diagnose-system-sync (default: none)
  --observation-mode MODE default, off, aggregate, or sampled
  --observation-sample-shift N
                           deterministic duration sample shift, 0..20
  --observation-arena-mib N
                           per-process tmpfs arena, 8..64 MiB
  --observation-profile-manifest PATH
                           frozen observation profile; excludes the three manual options
  --host-profiler MODE    off, stat, or record (default: off)
  --jobs N                 parallel build jobs
  --help

The fixed runtime and evidence roots are target/run/legofs-io500/<stage> and
target/results/legofs-io500/<stage>.  Existing stage roots are never replaced.

Environment:
  LEGOFS_TOOLCHAIN_PATH
                          colon-separated absolute tool directories
EOF
}

die()
{
	printf 'error: %s\n' "$*" >&2
	exit 2
}

while (($#)); do
	case "$1" in
	--build-only) BUILD_ONLY=1; shift ;;
	--run-only) RUN_ONLY=1; shift ;;
	--payload-only) PAYLOAD_ONLY=1; shift ;;
	--stage) (($# >= 2)) || die '--stage requires a value'; STAGE="$2"; shift 2 ;;
	--server-count) (($# >= 2)) || die '--server-count requires a value'; SERVER_COUNT="$2"; shift 2 ;;
	--client-count) (($# >= 2)) || die '--client-count requires a value'; CLIENT_COUNT="$2"; shift 2 ;;
	--result-label) (($# >= 2)) || die '--result-label requires a value'; RESULT_LABEL="$2"; shift 2 ;;
	--timeout) (($# >= 2)) || die '--timeout requires a value'; TIMEOUT="$2"; shift 2 ;;
	--full-coherence-trace) FULL_COHERENCE_TRACE=1; shift ;;
	--serving-transport) (($# >= 2)) || die '--serving-transport requires a value'; SERVING_TRANSPORT="$2"; shift 2 ;;
		--cursor-mode) (($# >= 2)) || die '--cursor-mode requires a value'; CURSOR_MODE="$2"; shift 2 ;;
	--cq-wait-mode) (($# >= 2)) || die '--cq-wait-mode requires a value'; CQ_WAIT_MODE="$2"; shift 2 ;;
	--durability-profile) (($# >= 2)) || die '--durability-profile requires a value'; DURABILITY_PROFILE="$2"; shift 2 ;;
	--payload-persistence-owner) (($# >= 2)) || die '--payload-persistence-owner requires a value'; PAYLOAD_PERSISTENCE_OWNER="$2"; shift 2 ;;
	--writer-persist-provider) (($# >= 2)) || die '--writer-persist-provider requires a value'; WRITER_PERSIST_PROVIDER="$2"; shift 2 ;;
	--packed-small-segments) (($# >= 2)) || die '--packed-small-segments requires a value'; PACKED_SMALL_SEGMENTS="$2"; shift 2 ;;
	--small-segment-count) (($# >= 2)) || die '--small-segment-count requires a value'; SMALL_SEGMENT_COUNT="$2"; SMALL_SEGMENT_COUNT_EXPLICIT=1; shift 2 ;;
	--fault-profile) (($# >= 2)) || die '--fault-profile requires a value'; FAULT_PROFILE="$2"; shift 2 ;;
	--observation-mode) (($# >= 2)) || die '--observation-mode requires a value'; OBSERVATION_MODE="$2"; shift 2 ;;
	--observation-sample-shift) (($# >= 2)) || die '--observation-sample-shift requires a value'; OBSERVATION_SAMPLE_SHIFT="$2"; shift 2 ;;
	--observation-arena-mib) (($# >= 2)) || die '--observation-arena-mib requires a value'; OBSERVATION_ARENA_MIB="$2"; shift 2 ;;
	--observation-profile-manifest) (($# >= 2)) || die '--observation-profile-manifest requires a value'; OBSERVATION_PROFILE_MANIFEST="$2"; shift 2 ;;
	--host-profiler) (($# >= 2)) || die '--host-profiler requires a value'; HOST_PROFILER="$2"; shift 2 ;;
	--jobs) (($# >= 2)) || die '--jobs requires a value'; JOBS="$2"; shift 2 ;;
	--help) usage; exit 0 ;;
	*) die "unknown argument: $1" ;;
	esac
done

((BUILD_ONLY + RUN_ONLY + PAYLOAD_ONLY <= 1)) ||
	die '--build-only, --run-only and --payload-only are mutually exclusive'
case "$STAGE" in hello|tiny|stress-tiny|rollover-smoke|easy-smoke|hard-smoke|metadata-smoke|small-close-smoke|rnd4k|scc|standard) ;; *) die "invalid stage: $STAGE" ;; esac
[[ "$TIMEOUT" =~ ^[1-9][0-9]*$ ]] || die 'timeout must be a positive integer'
[[ "$JOBS" =~ ^[1-9][0-9]*$ ]] || die 'jobs must be a positive integer'
case "$SERVER_COUNT" in 1|2) ;; *) die 'server-count must be 1 or 2' ;; esac
[[ "$CLIENT_COUNT" =~ ^[1-9]$|^10$ ]] || die 'client-count must be between 1 and 10'
case "$SERVING_TRANSPORT" in legacy|cxl) ;; *) die 'serving-transport must be legacy or cxl' ;; esac
case "$CURSOR_MODE" in legacy_shared|owned) ;; *) die 'cursor-mode must be legacy_shared or owned' ;; esac
case "$CQ_WAIT_MODE" in timer_sleep|cooperative_yield) ;; *) die 'cq-wait-mode must be timer_sleep or cooperative_yield' ;; esac
case "$DURABILITY_PROFILE" in d-before-v|coherent-seal-no-writeback|coherent-seal-needs-writeback) ;; *) die 'invalid durability-profile' ;; esac
case "$PAYLOAD_PERSISTENCE_OWNER" in auto|authority-bi-acquire|writer-receipt|placement-routed|writer-before-visibility) ;; *) die 'invalid payload-persistence-owner' ;; esac
case "$WRITER_PERSIST_PROVIDER" in msync|riscv-zicbom-dax) ;; *) die 'invalid writer-persist-provider' ;; esac
if [[ "$DURABILITY_PROFILE" != d-before-v && "$SERVING_TRANSPORT" != cxl ]]; then
	die 'coherent durability profiles require --serving-transport cxl'
fi
case "$PACKED_SMALL_SEGMENTS" in off|on) ;; *) die 'packed-small-segments must be off or on' ;; esac
[[ "$SMALL_SEGMENT_COUNT" =~ ^[1-9][0-9]*$ ]] || die 'small-segment-count must be a positive integer'
if ((SMALL_SEGMENT_COUNT_EXPLICIT)) && [[ "$PACKED_SMALL_SEGMENTS" != on ]]; then
	die '--small-segment-count requires --packed-small-segments on'
fi
case "$FAULT_PROFILE" in none|clean-server-restart|reject-unauthorized-clean-restart|reject-active-clean-retirement|diagnose-system-sync) ;; *) die 'invalid fault-profile' ;; esac
if [[ "$DURABILITY_PROFILE" != d-before-v && "$FAULT_PROFILE" != none ]]; then
	die 'coherent durability profiles currently require --fault-profile none'
fi
if [[ -n "$OBSERVATION_MODE" ]]; then
	case "$OBSERVATION_MODE" in default|off|aggregate|sampled) ;; *) die 'invalid observation-mode' ;; esac
fi
if [[ -n "$OBSERVATION_SAMPLE_SHIFT" && ! "$OBSERVATION_SAMPLE_SHIFT" =~ ^([0-9]|1[0-9]|20)$ ]]; then
	die 'observation-sample-shift must be between 0 and 20'
fi
if [[ -n "$OBSERVATION_ARENA_MIB" ]] &&
	{ [[ ! "$OBSERVATION_ARENA_MIB" =~ ^[0-9]+$ ]] || ((OBSERVATION_ARENA_MIB < 8 || OBSERVATION_ARENA_MIB > 64)); }; then
	die 'observation-arena-mib must be between 8 and 64'
fi
if [[ -n "$OBSERVATION_PROFILE_MANIFEST" &&
	( -n "$OBSERVATION_MODE" || -n "$OBSERVATION_SAMPLE_SHIFT" || -n "$OBSERVATION_ARENA_MIB" ) ]]; then
	die 'observation-profile-manifest is mutually exclusive with manual observation options'
fi
case "$HOST_PROFILER" in off|stat|record) ;; *) die 'host-profiler must be off, stat, or record' ;; esac
if [[ -n "$RESULT_LABEL" && ! "$RESULT_LABEL" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
	die 'result-label contains unsupported characters'
fi
if ((${#RESULT_LABEL} > 96)); then
	die 'result-label is longer than 96 characters'
fi

if ((BUILD_ONLY)); then
	"$ROOT/scripts/build_legofs_io500.sh" --jobs "$JOBS"
elif ((PAYLOAD_ONLY)); then
	"$ROOT/scripts/rebuild_legofs_io500_payload.sh" --jobs "$JOBS"
elif ((RUN_ONLY == 0)); then
	"$ROOT/scripts/build_legofs_io500.sh" --jobs "$JOBS"
fi
if ((BUILD_ONLY == 0 && PAYLOAD_ONLY == 0)); then
	args=(--stage "$STAGE" --server-count "$SERVER_COUNT" \
		--client-count "$CLIENT_COUNT" --timeout "$TIMEOUT" \
		--serving-transport "$SERVING_TRANSPORT" --cursor-mode "$CURSOR_MODE" \
		--cq-wait-mode "$CQ_WAIT_MODE" --durability-profile "$DURABILITY_PROFILE" \
		--payload-persistence-owner "$PAYLOAD_PERSISTENCE_OWNER" \
		--writer-persist-provider "$WRITER_PERSIST_PROVIDER" \
		--packed-small-segments "$PACKED_SMALL_SEGMENTS" \
		--small-segment-count "$SMALL_SEGMENT_COUNT")
	args+=(--fault-profile "$FAULT_PROFILE")
	args+=(--host-profiler "$HOST_PROFILER")
	if [[ -n "$OBSERVATION_MODE" ]]; then
		args+=(--observation-mode "$OBSERVATION_MODE")
	fi
	if [[ -n "$OBSERVATION_SAMPLE_SHIFT" ]]; then
		args+=(--observation-sample-shift "$OBSERVATION_SAMPLE_SHIFT")
	fi
	if [[ -n "$OBSERVATION_ARENA_MIB" ]]; then
		args+=(--observation-arena-mib "$OBSERVATION_ARENA_MIB")
	fi
	if [[ -n "$OBSERVATION_PROFILE_MANIFEST" ]]; then
		args+=(--observation-profile-manifest "$OBSERVATION_PROFILE_MANIFEST")
	fi
	if [[ -n "$RESULT_LABEL" ]]; then
		args+=(--result-label "$RESULT_LABEL")
	fi
	if ((FULL_COHERENCE_TRACE)); then
		args+=(--full-coherence-trace)
	fi
	exec python3 "$ROOT/scripts/legofs_io500.py" "${args[@]}"
fi
