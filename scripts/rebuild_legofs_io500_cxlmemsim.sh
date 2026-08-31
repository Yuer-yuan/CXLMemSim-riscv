#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$ROOT/out/legofs-type3/build/cxlmemsim"
PLATFORM="$ROOT/target/build/riscv-io500/platform"
MANIFEST="$ROOT/target/results/legofs-io500/build-manifest.json"
source "$ROOT/scripts/legofs_toolchain_path.sh"
legofs_toolchain_activate io500-cxlmemsim \
	bash getconf cmake ctest ninja make mktemp install mv rm python3
JOBS="$(getconf _NPROCESSORS_ONLN 2>/dev/null || printf '1\n')"

usage()
{
	printf '%s\n' 'Usage: ./scripts/rebuild_legofs_io500_cxlmemsim.sh [--jobs N]'
}

while (($#)); do
	case "$1" in
	--jobs) (($# >= 2)) || { printf '%s\n' 'error: --jobs requires a value' >&2; exit 2; }; JOBS="$2"; shift 2 ;;
	--help) usage; exit 0 ;;
	*) printf 'error: unknown argument: %s\n' "$1" >&2; exit 2 ;;
	esac
done

[[ "$JOBS" =~ ^[1-9][0-9]*$ ]] || { printf '%s\n' 'error: jobs must be a positive integer' >&2; exit 2; }
[[ -f "$MANIFEST" ]] || { printf 'error: missing build manifest: %s\n' "$MANIFEST" >&2; exit 2; }
[[ -d "$BUILD" ]] || { printf 'error: missing configured CXLMemSim build: %s\n' "$BUILD" >&2; exit 2; }
[[ -d "$PLATFORM" ]] || { printf 'error: missing IO500 platform directory: %s\n' "$PLATFORM" >&2; exit 2; }

cmake --build "$BUILD" \
	--target test_coherence_protocol_v2 test_coherence_server_v2 cxlmemsim_server \
	--parallel "$JOBS"
ctest --test-dir "$BUILD" --output-on-failure \
	-R 'test_coherence_(protocol|server)_v2'

temporary="$(mktemp "$PLATFORM/.cxlmemsim_server.XXXXXX")"
trap 'rm -f -- "$temporary"' EXIT
install -m 0755 "$BUILD/cxlmemsim_server" "$temporary"
mv -f -- "$temporary" "$PLATFORM/cxlmemsim_server"
trap - EXIT

python3 "$ROOT/scripts/update_legofs_io500_artifact.py" \
	--root "$ROOT" --manifest "$MANIFEST" \
	--artifact "cxlmemsim_server=$PLATFORM/cxlmemsim_server"
printf '[io500-cxlmemsim] artifact %s\n' "$PLATFORM/cxlmemsim_server"
printf '[io500-cxlmemsim] manifest %s\n' "$MANIFEST"
