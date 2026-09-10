#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

exec python3 "$ROOT/components/3FS/deploy/giga-native/cluster.py" \
	--repo "$ROOT" \
	--build-manifest "$ROOT/target/results/giga-native-3fs/build-manifest.json" \
	"$@"
