#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

exec python3 "$ROOT/components/3FS/deploy/giga-native/build.py" \
	--repo "$ROOT" \
	--output "$ROOT/target/build/giga-native-3fs" \
	"$@"
