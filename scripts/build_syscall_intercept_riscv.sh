#!/usr/bin/env bash

set -euo pipefail

readonly SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
readonly LOCK_FILE=$SCRIPT_DIR/syscall-intercept-riscv.lock

# shellcheck source=syscall-intercept-riscv.lock
source "$LOCK_FILE"

usage() {
    cat <<'EOF'
Usage: build_syscall_intercept_riscv.sh [--prepare-only|--verify-only|--help]

Build the live workspace RISC-V syscall-intercept source directly.

Environment:
  SYSINT_ROOT             Live source tree under the LegoFS workspace
  SYSINT_CAPSTONE_MIRROR  Local Git mirror containing CAPSTONE_COMMIT
  SYSINT_BUILD_ROOT       Owned build root
  SYSINT_JOBS             Build jobs (default: 2)
  RISCV_CC                RISC-V C compiler
  RISCV_LD                RISC-V linker
  RISCV_OBJCOPY           RISC-V objcopy
  RISCV_AR                RISC-V archiver
  RISCV_RANLIB            RISC-V archive indexer
  LDFLAGS                 linker flags (default: -fuse-ld=bfd)
EOF
}

MODE=build
case "${1:-}" in
    "") ;;
    --prepare-only) MODE=prepare ;;
    --verify-only) MODE=verify ;;
    -h|--help)
        usage
        exit 0
        ;;
    *)
        usage >&2
        exit 64
        ;;
esac

readonly SOURCE_ROOT=${SYSINT_ROOT:-$SCRIPT_DIR/../third_party/syscall_intercept-riscv}
readonly BUILD_ROOT=${SYSINT_BUILD_ROOT:-$SCRIPT_DIR/../.worktmp/syscall-intercept-riscv}
readonly BUILD_DIR=${SYSINT_BUILD_DIR:-$BUILD_ROOT/build}
readonly CAPSTONE_MIRROR=${SYSINT_CAPSTONE_MIRROR:-}
SYSINT_JOBS=${SYSINT_JOBS:-2}
LDFLAGS=${LDFLAGS:--fuse-ld=bfd}
export LDFLAGS

fail() {
    printf 'syscall_intercept_build=FAIL reason=%s\n' "$*" >&2
    exit 1
}

select_tool() {
    local preferred=$1
    local fallback=$2
    if command -v "$preferred" >/dev/null 2>&1; then
        command -v "$preferred"
    elif command -v "$fallback" >/dev/null 2>&1; then
        command -v "$fallback"
    else
        fail "missing tool: $preferred or $fallback"
    fi
}

validate_source() {
    [[ -d "$SOURCE_ROOT" ]] || fail "missing source: $SOURCE_ROOT"
    SOURCE_REAL=$(cd "$SOURCE_ROOT" && pwd -P)
    [[ -f "$SOURCE_REAL/CMakeLists.txt" ]] ||
        fail "source lacks CMakeLists.txt: $SOURCE_REAL"
    printf 'source=%s\n' "$SOURCE_REAL"
}

validate_capstone_mirror() {
    local actual_repository is_bare
    [[ -n "$CAPSTONE_MIRROR" ]] ||
        fail "SYSINT_CAPSTONE_MIRROR is required; refusing network fallback"
    [[ -d "$CAPSTONE_MIRROR" ]] || fail "missing Capstone mirror: $CAPSTONE_MIRROR"
    CAPSTONE_MIRROR_REAL=$(cd "$CAPSTONE_MIRROR" && pwd -P)
    is_bare=$(git --git-dir="$CAPSTONE_MIRROR_REAL" \
        rev-parse --is-bare-repository 2>/dev/null || true)
    if [[ "$is_bare" == true ]]; then
        actual_repository=$(git --git-dir="$CAPSTONE_MIRROR_REAL" \
            remote get-url origin 2>/dev/null) || fail "Capstone mirror has no origin"
        git --git-dir="$CAPSTONE_MIRROR_REAL" \
            cat-file -e "${CAPSTONE_COMMIT}^{commit}" 2>/dev/null ||
            fail "Capstone mirror lacks commit: $CAPSTONE_COMMIT"
    else
        actual_repository=$(git -C "$CAPSTONE_MIRROR_REAL" \
            remote get-url origin 2>/dev/null) || fail "invalid Capstone mirror"
        git -C "$CAPSTONE_MIRROR_REAL" \
            cat-file -e "${CAPSTONE_COMMIT}^{commit}" 2>/dev/null ||
            fail "Capstone checkout lacks commit: $CAPSTONE_COMMIT"
    fi
    [[ "$actual_repository" == "$CAPSTONE_REPOSITORY" ]] ||
        fail "Capstone repository mismatch: $actual_repository"
}

select_cross_tools() {
    if [[ -z "${RISCV_CC:-}" ]]; then
        if [[ "$(uname -m)" == riscv64 ]]; then
            RISCV_CC=$(select_tool cc gcc)
        else
            RISCV_CC=$(select_tool riscv64-linux-gnu-gcc riscv64-linux-gnu-gcc)
        fi
    fi
    [[ -x "$RISCV_CC" ]] || RISCV_CC=$(command -v "$RISCV_CC") ||
        fail "missing RISC-V compiler: $RISCV_CC"
    RISCV_LD=${RISCV_LD:-$(select_tool riscv64-linux-gnu-ld ld)}
    RISCV_OBJCOPY=${RISCV_OBJCOPY:-$(select_tool riscv64-linux-gnu-objcopy objcopy)}
    RISCV_AR=${RISCV_AR:-$(select_tool riscv64-linux-gnu-ar ar)}
    RISCV_RANLIB=${RISCV_RANLIB:-$(select_tool riscv64-linux-gnu-ranlib ranlib)}
}

verify_build() {
    local library=$BUILD_DIR/libsyscall_intercept.so.0.1.0
    local file_tool readelf_tool nm_tool identity headers dependencies symbols
    [[ -s "$library" ]] || fail "missing library: $library"
    [[ -L "$BUILD_DIR/libsyscall_intercept.so.0" ]] || fail "missing soname symlink"
    [[ -L "$BUILD_DIR/libsyscall_intercept.so" ]] || fail "missing linker-name symlink"
    file_tool=$(select_tool file file)
    readelf_tool=${RISCV_READELF:-$(select_tool riscv64-linux-gnu-readelf readelf)}
    nm_tool=${RISCV_NM:-$(select_tool riscv64-linux-gnu-nm nm)}
    identity=$($file_tool "$library")
    headers=$($readelf_tool -h "$library")
    dependencies=$($readelf_tool -d "$library")
    symbols=$($nm_tool -D --defined-only "$library")
    grep -q 'RISC-V' <<<"$identity" || fail "library is not RISC-V"
    grep -q 'RVC' <<<"$identity" || fail "library lacks RVC identity"
    grep -q 'double-float ABI' <<<"$identity" || fail "library is not lp64d"
    grep -Eq 'Machine:[[:space:]]*RISC-V' <<<"$headers" || fail "invalid ELF machine"
    grep -Eq '[[:space:]]intercept_hook_point$' <<<"$symbols" ||
        fail "missing intercept_hook_point"
    grep -Eq '[[:space:]]syscall_no_intercept$' <<<"$symbols" ||
        fail "missing syscall_no_intercept"
    ! grep -q 'libcapstone' <<<"$dependencies" ||
        fail "library has a dynamic Capstone dependency"
    printf '%s\n' "$identity"
    printf 'SYSINT_LIB_DIR=%s\n' "$BUILD_DIR"
}

validate_source
select_cross_tools
if [[ "$MODE" == prepare ]]; then
    printf '%s\n' 'syscall_intercept_prepare=PASS'
    exit 0
fi
if [[ "$MODE" == verify ]]; then
    verify_build
    printf '%s\n' 'syscall_intercept_verify=PASS'
    exit 0
fi

[[ "$SYSINT_JOBS" =~ ^[1-9][0-9]*$ ]] || fail "invalid SYSINT_JOBS: $SYSINT_JOBS"
validate_capstone_mirror
rm -rf -- "$BUILD_DIR"
mkdir -p "$BUILD_DIR"
env \
    GIT_CONFIG_COUNT=1 \
    GIT_CONFIG_KEY_0="url.file://$CAPSTONE_MIRROR_REAL.insteadOf" \
    GIT_CONFIG_VALUE_0="$CAPSTONE_REPOSITORY" \
    GIT_ALLOW_PROTOCOL=file \
    CMAKE_BUILD_PARALLEL_LEVEL="$SYSINT_JOBS" \
    MAKEFLAGS="-j$SYSINT_JOBS" \
    CC="$RISCV_CC" \
    cmake -S "$SOURCE_REAL" -B "$BUILD_DIR" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_SYSTEM_NAME=Linux \
    -DCMAKE_SYSTEM_PROCESSOR=riscv64 \
    -DCMAKE_C_COMPILER="$RISCV_CC" \
    -DCMAKE_LINKER="$RISCV_LD" \
    -DCMAKE_OBJCOPY="$RISCV_OBJCOPY" \
    -DCMAKE_AR="$RISCV_AR" \
    -DCMAKE_RANLIB="$RISCV_RANLIB" \
    -DBUILD_TESTS=OFF \
    -DBUILD_EXAMPLES=OFF \
    -DSTATIC_CAPSTONE=ON \
    -DTREAT_WARNINGS_AS_ERRORS=OFF
cmake --build "$BUILD_DIR" -j "$SYSINT_JOBS" --target syscall_intercept_shared
verify_build
printf '%s\n' 'syscall_intercept_build=PASS'
