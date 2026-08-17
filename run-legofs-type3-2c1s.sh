#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export LEGOFS_RUNNER="${ROOT}/scripts/legofs_type3_3node.py"
exec "${ROOT}/run-legofs-type3.sh" "$@"
