#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LEGOFS_PARENT_ROOT="$(cd -- "${ROOT}/../.." && pwd -P)"
LOCAL_TOOL_ROOT="${LEGOFS_TYPE3_TOOL_ROOT:-${LEGOFS_PARENT_ROOT}/.cxl-bi-tools}"

if [[ -d "${LOCAL_TOOL_ROOT}" ]]; then
	local_python_sites=("${LOCAL_TOOL_ROOT}"/uv/lib/python*/site-packages)
	export PATH="${LOCAL_TOOL_ROOT}/uv/bin:${PATH}"
	if [[ -d "${local_python_sites[0]}" ]]; then
		export PYTHONPATH="${local_python_sites[0]}${PYTHONPATH:+:${PYTHONPATH}}"
	fi
fi

BUILD_ONLY=0
RUN_ONLY=0
JOBS="$(getconf _NPROCESSORS_ONLN 2>/dev/null || printf '1\n')"
BENCH_BYTES=65536
TIMEOUT=300
BUILD_SCRIPT="${LEGOFS_BUILD_SCRIPT:-${ROOT}/scripts/build_legofs_type3.sh}"
RUNNER="${LEGOFS_RUNNER:-${ROOT}/scripts/legofs_type3_2node.py}"

usage()
{
	cat <<'EOF'
Usage: ./run-legofs-type3.sh [OPTIONS]

  --build-only       build artifacts without starting QEMU
  --run-only         run existing artifacts without rebuilding
  --jobs N           parallel build jobs (default: online CPUs)
  --bytes N          benchmark bytes, 4096-aligned and <= 16777216
  --timeout N        end-to-end timeout in seconds
  --help              show this help

Environment:
  LEGOFS_TYPE3_OUT   absolute, separate build/result root for this variant
  LEGOFS_TYPE3_TOOL_ROOT
                      optional uv tool root; defaults to parent LegoFS/.cxl-bi-tools
EOF
}

die()
{
	printf 'error: %s\n' "$*" >&2
	exit 2
}

while (($#)); do
	case "$1" in
	--build-only)
		BUILD_ONLY=1
		shift
		;;
	--run-only)
		RUN_ONLY=1
		shift
		;;
	--jobs)
		(($# >= 2)) || die "--jobs requires a value"
		JOBS="$2"
		shift 2
		;;
	--bytes)
		(($# >= 2)) || die "--bytes requires a value"
		BENCH_BYTES="$2"
		shift 2
		;;
	--timeout)
		(($# >= 2)) || die "--timeout requires a value"
		TIMEOUT="$2"
		shift 2
		;;
	--help)
		usage
		exit 0
		;;
	*)
		die "unknown argument: $1"
		;;
	esac
done

((BUILD_ONLY == 0 || RUN_ONLY == 0)) ||
	die "--build-only and --run-only are mutually exclusive"
[[ "${JOBS}" =~ ^[1-9][0-9]*$ ]] || die "jobs must be a positive integer"
[[ "${TIMEOUT}" =~ ^[1-9][0-9]*$ ]] || die "timeout must be a positive integer"
[[ "${BENCH_BYTES}" =~ ^[1-9][0-9]*$ ]] || die "bytes must be a positive integer"
((BENCH_BYTES <= 16777216)) || die "bytes must not exceed 16777216"
((BENCH_BYTES % 4096 == 0)) || die "bytes must be divisible by 4096"

submodule_status="$(git -C "${ROOT}" submodule status)" ||
	die "unable to inspect submodule state"
while IFS= read -r line; do
	[[ -z "${line}" ]] && continue
	case "${line:0:1}" in
-|U)
		die "submodule is not at its recorded gitlink: ${line}"
		;;
	esac
done <<<"${submodule_status}"

if ((RUN_ONLY == 0)); then
	[[ -x "${BUILD_SCRIPT}" ]] || die "build script is not executable: ${BUILD_SCRIPT}"
	"${BUILD_SCRIPT}" --jobs "${JOBS}"
fi

if ((BUILD_ONLY == 0)); then
	[[ -f "${RUNNER}" ]] || die "two-node runner is missing: ${RUNNER}"
	exec python3 "${RUNNER}" --bytes "${BENCH_BYTES}" --timeout "${TIMEOUT}"
fi
