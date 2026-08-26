#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BUILD_ONLY=0
RUN_ONLY=0
STAGE=tiny
FILESYSTEM_MODE=legacy-cxl-reference
SERVER_COUNT=1
CLIENT_COUNT=10
RESULT_LABEL=
EVALUATION_MANIFEST=
TIMEOUT=7200
FULL_COHERENCE_TRACE=0
LOCAL_CANDIDATE_GATE=0
JOBS="$(getconf _NPROCESSORS_ONLN 2>/dev/null || printf '1\n')"

usage()
{
	cat <<'EOF'
Usage: ./run-legofs-io500.sh [OPTIONS]

  --build-only             build the RISC-V platform and current LegoFS payload
  --run-only               use the existing workspace build
  --stage hello|tiny|easy-smoke|hard-smoke|metadata-smoke|rnd4k|scc|standard
  --filesystem-mode MODE  legacy-cxl-reference|rdwo-candidate
  --server-count 1|2       LegoFS server guests (default: 1)
  --client-count N         client guests / MPI ranks, 1..10 (default: 10)
  --result-label LABEL     separate runtime/result directory name
  --evaluation-manifest P use an existing external evaluation manifest
  --timeout SECONDS        stage timeout (default: 7200)
  --full-coherence-trace   diagnostic: record every MESI event
  --local-candidate-gate   require complete local C0/C2 evidence; never grants C3/C4
  --jobs N                 parallel build jobs
  --help

The fixed runtime and evidence roots are target/{run,results}/legofs-io500/
<filesystem-mode>/<stage-or-label>. Existing roots are never replaced.
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
	--stage) (($# >= 2)) || die '--stage requires a value'; STAGE="$2"; shift 2 ;;
	--filesystem-mode) (($# >= 2)) || die '--filesystem-mode requires a value'; FILESYSTEM_MODE="$2"; shift 2 ;;
	--server-count) (($# >= 2)) || die '--server-count requires a value'; SERVER_COUNT="$2"; shift 2 ;;
	--client-count) (($# >= 2)) || die '--client-count requires a value'; CLIENT_COUNT="$2"; shift 2 ;;
	--result-label) (($# >= 2)) || die '--result-label requires a value'; RESULT_LABEL="$2"; shift 2 ;;
	--evaluation-manifest) (($# >= 2)) || die '--evaluation-manifest requires a value'; EVALUATION_MANIFEST="$2"; shift 2 ;;
	--timeout) (($# >= 2)) || die '--timeout requires a value'; TIMEOUT="$2"; shift 2 ;;
	--full-coherence-trace) FULL_COHERENCE_TRACE=1; shift ;;
	--local-candidate-gate) LOCAL_CANDIDATE_GATE=1; shift ;;
	--jobs) (($# >= 2)) || die '--jobs requires a value'; JOBS="$2"; shift 2 ;;
	--help) usage; exit 0 ;;
	*) die "unknown argument: $1" ;;
	esac
done

((BUILD_ONLY == 0 || RUN_ONLY == 0)) || die '--build-only and --run-only are mutually exclusive'
case "$STAGE" in hello|tiny|easy-smoke|hard-smoke|metadata-smoke|rnd4k|scc|standard) ;; *) die "invalid stage: $STAGE" ;; esac
case "$FILESYSTEM_MODE" in legacy-cxl-reference|rdwo-candidate) ;; *) die "invalid filesystem-mode: $FILESYSTEM_MODE" ;; esac
[[ "$TIMEOUT" =~ ^[1-9][0-9]*$ ]] || die 'timeout must be a positive integer'
[[ "$JOBS" =~ ^[1-9][0-9]*$ ]] || die 'jobs must be a positive integer'
case "$SERVER_COUNT" in 1|2) ;; *) die 'server-count must be 1 or 2' ;; esac
[[ "$CLIENT_COUNT" =~ ^[1-9]$|^10$ ]] || die 'client-count must be between 1 and 10'
if [[ -n "$RESULT_LABEL" && ! "$RESULT_LABEL" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
	die 'result-label contains unsupported characters'
fi
if [[ "$STAGE" = hello && -n "$EVALUATION_MANIFEST" ]]; then
	die 'hello does not consume an IO500 evaluation manifest'
fi

if ((RUN_ONLY == 0)); then
	if [[ "$FILESYSTEM_MODE" = rdwo-candidate ]]; then
		die 'rdwo-candidate build is unavailable until the V1 candidate-only packages exist; use --run-only only with a complete manifested candidate build'
	fi
	"$ROOT/scripts/build_legofs_io500.sh" --jobs "$JOBS"
fi
if ((BUILD_ONLY == 0)); then
	if [[ "$STAGE" != hello && -z "$EVALUATION_MANIFEST" ]]; then
		profile="${RESULT_LABEL:-$STAGE}"
		manifest_dir="$ROOT/target/results/legofs-io500/evaluation-manifests/$FILESYSTEM_MODE"
		EVALUATION_MANIFEST="$manifest_dir/$profile.json"
		effective_config="$manifest_dir/$profile.ini"
		[[ ! -e "$EVALUATION_MANIFEST" && ! -e "$effective_config" ]] ||
			die "evaluation manifest output already exists: $profile"
		"$ROOT/scripts/legofs_evaluation_manifest.py" \
			--source-config "$ROOT/configs/io500-$STAGE.ini" \
			--output-config "$effective_config" \
			--build-manifest "$ROOT/target/results/legofs-io500/build-manifest.json" \
			--output-manifest "$EVALUATION_MANIFEST" \
			--filesystem-mode "$FILESYSTEM_MODE" \
			--io500-mode standard \
			--profile-name "$profile" \
			--server-count "$SERVER_COUNT" \
			--client-count "$CLIENT_COUNT"
	fi
	args=(--stage "$STAGE" --server-count "$SERVER_COUNT" \
		--client-count "$CLIENT_COUNT" --timeout "$TIMEOUT" \
		--filesystem-mode "$FILESYSTEM_MODE")
	if [[ -n "$EVALUATION_MANIFEST" ]]; then
		args+=(--evaluation-manifest "$EVALUATION_MANIFEST")
	fi
	if [[ -n "$RESULT_LABEL" ]]; then
		args+=(--result-label "$RESULT_LABEL")
	fi
	if ((FULL_COHERENCE_TRACE)); then
		args+=(--full-coherence-trace)
	fi
	if ((LOCAL_CANDIDATE_GATE)); then
		args+=(--local-candidate-gate)
	fi
	exec python3 "$ROOT/scripts/legofs_io500.py" "${args[@]}"
fi
