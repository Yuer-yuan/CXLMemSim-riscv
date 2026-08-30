#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
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
FAULT_PROFILE=none
JOBS="$(getconf _NPROCESSORS_ONLN 2>/dev/null || printf '1\n')"

usage()
{
	cat <<'EOF'
Usage: ./run-legofs-io500.sh [OPTIONS]

  --build-only             build the RISC-V platform and current LegoFS payload
  --run-only               use the existing workspace build
  --payload-only           rebuild only LegoFS guest payload, then run
  --stage hello|tiny|easy-smoke|hard-smoke|metadata-smoke|rnd4k|scc|standard
  --server-count 1|2       LegoFS server guests (default: 1)
  --client-count N         client guests / MPI ranks, 1..10 (default: 10)
  --result-label LABEL     separate runtime/result directory name
  --timeout SECONDS        stage timeout (default: 7200)
  --full-coherence-trace   diagnostic: record every MESI event
  --serving-transport MODE LegoFS serving path: legacy or cxl (default: legacy)
  --fault-profile PROFILE none, clean-server-restart, reject-unauthorized-clean-restart, or reject-active-clean-retirement (default: none)
  --jobs N                 parallel build jobs
  --help

The fixed runtime and evidence roots are target/run/legofs-io500/<stage> and
target/results/legofs-io500/<stage>.  Existing stage roots are never replaced.
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
	--fault-profile) (($# >= 2)) || die '--fault-profile requires a value'; FAULT_PROFILE="$2"; shift 2 ;;
	--jobs) (($# >= 2)) || die '--jobs requires a value'; JOBS="$2"; shift 2 ;;
	--help) usage; exit 0 ;;
	*) die "unknown argument: $1" ;;
	esac
done

((BUILD_ONLY + RUN_ONLY + PAYLOAD_ONLY <= 1)) ||
	die '--build-only, --run-only and --payload-only are mutually exclusive'
case "$STAGE" in hello|tiny|easy-smoke|hard-smoke|metadata-smoke|rnd4k|scc|standard) ;; *) die "invalid stage: $STAGE" ;; esac
[[ "$TIMEOUT" =~ ^[1-9][0-9]*$ ]] || die 'timeout must be a positive integer'
[[ "$JOBS" =~ ^[1-9][0-9]*$ ]] || die 'jobs must be a positive integer'
case "$SERVER_COUNT" in 1|2) ;; *) die 'server-count must be 1 or 2' ;; esac
[[ "$CLIENT_COUNT" =~ ^[1-9]$|^10$ ]] || die 'client-count must be between 1 and 10'
case "$SERVING_TRANSPORT" in legacy|cxl) ;; *) die 'serving-transport must be legacy or cxl' ;; esac
case "$FAULT_PROFILE" in none|clean-server-restart|reject-unauthorized-clean-restart|reject-active-clean-retirement) ;; *) die 'invalid fault-profile' ;; esac
if [[ -n "$RESULT_LABEL" && ! "$RESULT_LABEL" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
	die 'result-label contains unsupported characters'
fi

if ((BUILD_ONLY)); then
	"$ROOT/scripts/build_legofs_io500.sh" --jobs "$JOBS"
elif ((PAYLOAD_ONLY)); then
	"$ROOT/scripts/rebuild_legofs_io500_payload.sh" --jobs "$JOBS"
elif ((RUN_ONLY == 0)); then
	"$ROOT/scripts/build_legofs_io500.sh" --jobs "$JOBS"
fi
if ((BUILD_ONLY == 0)); then
	args=(--stage "$STAGE" --server-count "$SERVER_COUNT" \
		--client-count "$CLIENT_COUNT" --timeout "$TIMEOUT" \
		--serving-transport "$SERVING_TRANSPORT")
	args+=(--fault-profile "$FAULT_PROFILE")
	if [[ -n "$RESULT_LABEL" ]]; then
		args+=(--result-label "$RESULT_LABEL")
	fi
	if ((FULL_COHERENCE_TRACE)); then
		args+=(--full-coherence-trace)
	fi
	exec python3 "$ROOT/scripts/legofs_io500.py" "${args[@]}"
fi
