#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BUILD=1
RUN=1
MODE=""
JOBS="$(getconf _NPROCESSORS_ONLN 2>/dev/null || printf '1\n')"
BENCHMARK_BYTES=1048576

usage()
{
	cat <<'USAGE'
Usage: ./run.sh [OPTIONS]

Build the pinned SiFive U CXL stack and run the Type 3 CXLMemSim SHM test.

Options:
  --build-only          Build artifacts without starting CXLMemSim or QEMU
  --run-only            Run with existing artifacts without rebuilding
  --jobs N              Parallel build jobs (positive integer)
  --benchmark-bytes N   Guest benchmark bytes (8-byte aligned, <= 256 MiB)
  -h, --help            Show this help
USAGE
}

die()
{
	printf 'error: %s\n' "$*" >&2
	exit 2
}

set_mode()
{
	local requested="$1"

	if [[ -n "${MODE}" && "${MODE}" != "${requested}" ]]; then
		die "--build-only and --run-only are mutually exclusive"
	fi
	MODE="${requested}"
}

while (($#)); do
	case "$1" in
	--build-only)
		set_mode build
		BUILD=1
		RUN=0
		shift
		;;
	--run-only)
		set_mode run
		BUILD=0
		RUN=1
		shift
		;;
	--jobs)
		(($# >= 2)) || die "--jobs requires a value"
		JOBS="$2"
		shift 2
		;;
	--benchmark-bytes)
		(($# >= 2)) || die "--benchmark-bytes requires a value"
		BENCHMARK_BYTES="$2"
		shift 2
		;;
	-h | --help)
		usage
		exit 0
		;;
	*)
		die "unknown argument: $1"
		;;
	esac
done

[[ "${JOBS}" =~ ^[1-9][0-9]*$ ]] ||
	die "jobs must be a positive integer"
[[ "${BENCHMARK_BYTES}" =~ ^[1-9][0-9]*$ ]] ||
	die "benchmark bytes must be a positive integer"
((BENCHMARK_BYTES % 8 == 0)) ||
	die "benchmark bytes must be 8-byte aligned"
((BENCHMARK_BYTES <= 268435456)) ||
	die "benchmark bytes must not exceed 268435456"

if git -C "${ROOT}" submodule status | grep -q '^-'; then
	git -C "${ROOT}" submodule update --init
fi

while read -r _key submodule_path; do
	full_path="${ROOT}/${submodule_path}"
	[[ -e "${full_path}/.git" ]] ||
		die "submodule is not initialized: ${submodule_path}"
	expected="$(
		git -C "${ROOT}" ls-tree HEAD "${submodule_path}" |
			awk '{print $3}'
	)"
	actual="$(git -C "${full_path}" rev-parse HEAD)"
	[[ "${actual}" == "${expected}" ]] ||
		die "submodule revision mismatch: ${submodule_path}"
	[[ -z "$(git -C "${full_path}" status --porcelain)" ]] ||
		die "submodule has local changes: ${submodule_path}"
done < <(
	git -C "${ROOT}" config -f .gitmodules \
		--get-regexp '^submodule\..*\.path$'
)

"${ROOT}/scripts/check-deps.sh"

if ((BUILD)); then
	"${ROOT}/scripts/build.sh" --jobs "${JOBS}"
fi
if ((RUN)); then
	python3 "${ROOT}/scripts/run.py" \
		--benchmark-bytes "${BENCHMARK_BYTES}"
fi
