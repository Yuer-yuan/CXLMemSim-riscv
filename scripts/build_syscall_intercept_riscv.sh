#!/usr/bin/env bash

set -euo pipefail

# Compatibility entry point for workspace automation.  LegoFS owns both the
# vendored source and its reproducible RISC-V builder.
readonly SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
readonly LEGOFS_BUILDER=$SCRIPT_DIR/../components/legofs/scripts/build-syscall-intercept-riscv.sh

[[ -x "$LEGOFS_BUILDER" ]] || {
    printf 'syscall_intercept_build=FAIL reason=missing LegoFS builder: %s\n' \
        "$LEGOFS_BUILDER" >&2
    exit 1
}

exec "$LEGOFS_BUILDER" "$@"
