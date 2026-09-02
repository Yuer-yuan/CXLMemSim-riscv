#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
build_only=false
run_only=false
show_help=false
build_arguments=()
runner_arguments=()

die()
{
	printf 'error: %s\n' "$*" >&2
	exit 2
}

while (($#)); do
	case "$1" in
	--build-only)
		build_only=true
		shift
		;;
	--run-only)
		run_only=true
		shift
		;;
	--jobs)
		(($# >= 2)) || die "--jobs requires a value"
		build_arguments+=("--jobs" "$2")
		shift 2
		;;
	-h|--help)
		show_help=true
		shift
		;;
	*)
		runner_arguments+=("$1")
		shift
		;;
	esac
done

if [[ "${show_help}" == true ]]; then
	printf '%s\n' \
		'Usage: ./run-cxl-bi-app.sh [--build-only | --run-only] [--jobs N] [runner options]' \
		'' \
		'Wrapper options:' \
		'  --build-only  Build the dedicated guest Image, then stop.' \
		'  --run-only    Reuse the existing dedicated Image.' \
		'  --jobs N      Parallel Linux build jobs.' \
		'' \
		'Runner options:'
	python3 "${ROOT}/scripts/cxl_bi_app.py" --help
	exit 0
fi

if [[ "${build_only}" == true && "${run_only}" == true ]]; then
	die "--build-only and --run-only are mutually exclusive"
fi
if [[ "${run_only}" == true && ${#build_arguments[@]} -ne 0 ]]; then
	die "--jobs cannot be used with --run-only"
fi
if [[ "${build_only}" == true && ${#runner_arguments[@]} -ne 0 ]]; then
	die "runner options cannot be used with --build-only"
fi

if [[ "${run_only}" != true ]]; then
	"${ROOT}/scripts/build_cxl_bi_app.sh" "${build_arguments[@]}"
fi
if [[ "${build_only}" == true ]]; then
	exit 0
fi

exec python3 "${ROOT}/scripts/cxl_bi_app.py" "${runner_arguments[@]}"
