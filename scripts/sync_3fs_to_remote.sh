#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${GIGA_HOST:-giga}"
REMOTE_ROOT="${GIGA_REMOTE_ROOT:-/root/cxlmemsim-riscv-io500}"
MODE="${1:-}"
shift || true

case "$MODE" in
	upload)
		rsync -az \
			--exclude='.git/' --exclude='.agents/' --exclude='target/' --exclude='out/' \
			--exclude='__pycache__/' --exclude='.pytest_cache/' \
			"$ROOT/" "$HOST:$REMOTE_ROOT/"
		;;
	download)
		if [[ "${1:-}" != "--prefix" || -z "${2:-}" || "${2:-}" == */* || "${2:-}" == . || "${2:-}" == .. ]]; then
			echo 'download requires --prefix SAFE_PREFIX' >&2
			exit 2
		fi
		prefix="$2"
		mkdir -p "$ROOT/target/results/giga-native-3fs"
		for topology in 1c1s 2c1s 3c1s; do
			rsync -az "$HOST:$REMOTE_ROOT/target/results/giga-native-3fs/$prefix-$topology" \
				"$ROOT/target/results/giga-native-3fs/"
		done
		rsync -az "$HOST:$REMOTE_ROOT/target/results/giga-native-3fs/$prefix-summary.json" \
			"$ROOT/target/results/giga-native-3fs/"
		;;
	*)
		echo 'usage: sync_3fs_to_remote.sh upload | download --prefix SAFE_PREFIX' >&2
		exit 2
		;;
esac
