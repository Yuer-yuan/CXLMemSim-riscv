#!/usr/bin/env bash

set -euo pipefail

readonly SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
readonly LOCK_FILE=$SCRIPT_DIR/syscall-intercept-riscv.lock
readonly PATCH_DIR=$SCRIPT_DIR/patches/syscall-intercept-riscv

# shellcheck source=syscall-intercept-riscv.lock
source "$LOCK_FILE"

usage() {
    cat <<'EOF'
Usage: build_syscall_intercept_riscv.sh [--prepare-only|--verify-only|--help]

Build a reproducibly patched copy of the pinned RISC-V syscall interceptor.

Environment:
  SYSINT_ROOT             Pinned alpha-unito source checkout
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
readonly SOURCE_COPY=${SYSINT_SOURCE_COPY:-$BUILD_ROOT/source}
readonly CAPSTONE_SOURCE_COPY=${SYSINT_CAPSTONE_SOURCE_COPY:-$BUILD_ROOT/capstone-source}
readonly CAPSTONE_BUILD_DIR=${SYSINT_CAPSTONE_BUILD_DIR:-$BUILD_ROOT/capstone-build}
readonly CAPSTONE_LIBRARY=$CAPSTONE_BUILD_DIR/libcapstone.a
readonly MANIFEST=$BUILD_ROOT/manifest.txt
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
    local actual_commit actual_repository source_git_root
    [[ -d "$SOURCE_ROOT" ]] || fail "missing source: $SOURCE_ROOT"
    SOURCE_REAL=$(cd "$SOURCE_ROOT" && pwd -P)
    source_git_root=$(git -c "safe.directory=$SOURCE_REAL" -C "$SOURCE_REAL" \
        rev-parse --show-toplevel 2>/dev/null || true)
    [[ "$source_git_root" == "$SOURCE_REAL" ]] ||
        fail "source is not a standalone Git checkout: $SOURCE_REAL"
    actual_repository=$(git -c "safe.directory=$SOURCE_REAL" -C "$SOURCE_REAL" \
        remote get-url origin 2>/dev/null) || fail "source has no origin remote"
    actual_commit=$(git -c "safe.directory=$SOURCE_REAL" -C "$SOURCE_REAL" \
        rev-parse HEAD)
    [[ "$actual_repository" == "$SYSINT_REPOSITORY" ]] ||
        fail "source repository mismatch: $actual_repository"
    [[ "$actual_commit" == "$SYSINT_COMMIT" ]] ||
        fail "source commit mismatch: $actual_commit"
}

prepare_source() {
    local patch_file
    validate_source
    [[ "$SOURCE_REAL" != "$SOURCE_COPY" ]] || fail "source copy must differ from source"
    rm -rf -- "$SOURCE_COPY"
    mkdir -p "$SOURCE_COPY"
    git -c "safe.directory=$SOURCE_REAL" -C "$SOURCE_REAL" \
        archive --format=tar "$SYSINT_COMMIT" | tar -xf - -C "$SOURCE_COPY" ||
        fail "could not export pinned source commit"
    for patch_file in "$PATCH_DIR"/*.patch; do
        (
            cd "$SOURCE_COPY"
            GIT_DIR=/dev/null git apply --whitespace=nowarn "$patch_file"
        ) || fail "could not apply patch: $patch_file"
    done
    grep -q 'intercept(void)' "$SOURCE_COPY/src/intercept.c" ||
        fail "musl-safe no-argument constructor patch is missing"
    grep -q 'getauxval(AT_EXECFN)' "$SOURCE_COPY/src/intercept.c" ||
        fail "musl-safe AT_EXECFN constructor patch is missing"
    grep -q 'ld-musl-' "$SOURCE_COPY/src/intercept.c" ||
        fail "musl loader-as-libc patch is missing"
    printf 'source=%s\nsource_copy=%s\n' "$SOURCE_REAL" "$SOURCE_COPY"
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

prepare_capstone_source() {
    [[ "$CAPSTONE_MIRROR_REAL" != "$CAPSTONE_SOURCE_COPY" ]] ||
        fail "Capstone source copy must differ from mirror"
    rm -rf -- "$CAPSTONE_SOURCE_COPY" "$CAPSTONE_BUILD_DIR"
    mkdir -p "$CAPSTONE_SOURCE_COPY" "$CAPSTONE_BUILD_DIR"
    if [[ "$(git --git-dir="$CAPSTONE_MIRROR_REAL" rev-parse --is-bare-repository 2>/dev/null || true)" == true ]]; then
        git --git-dir="$CAPSTONE_MIRROR_REAL" archive --format=tar "$CAPSTONE_COMMIT"
    else
        git -c "safe.directory=$CAPSTONE_MIRROR_REAL" -C "$CAPSTONE_MIRROR_REAL" \
            archive --format=tar "$CAPSTONE_COMMIT"
    fi | tar -xf - -C "$CAPSTONE_SOURCE_COPY" ||
        fail "could not export pinned Capstone commit"
}

build_capstone() {
    env -u CXX make -C "$CAPSTONE_SOURCE_COPY" \
        BUILDDIR="$CAPSTONE_BUILD_DIR" \
        CC="$RISCV_CC" \
        AR="$RISCV_AR" \
        RANLIB="$RISCV_RANLIB" \
        CAPSTONE_ARCHS=riscv \
        CAPSTONE_BUILD_CORE_ONLY=yes \
        CAPSTONE_STATIC=yes \
        CAPSTONE_SHARED=no \
        -j "$SYSINT_JOBS"
    [[ -s "$CAPSTONE_LIBRARY" ]] ||
        fail "missing static RISC-V Capstone archive: $CAPSTONE_LIBRARY"
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
    local file_tool readelf_tool nm_tool sha_tool identity headers dependencies symbols
    local capstone_hash library_hash patch_file patch_hash
    [[ -s "$library" ]] || fail "missing library: $library"
    [[ -L "$BUILD_DIR/libsyscall_intercept.so.0" ]] || fail "missing soname symlink"
    [[ -L "$BUILD_DIR/libsyscall_intercept.so" ]] || fail "missing linker-name symlink"
    file_tool=$(select_tool file file)
    readelf_tool=${RISCV_READELF:-$(select_tool riscv64-linux-gnu-readelf readelf)}
    nm_tool=${RISCV_NM:-$(select_tool riscv64-linux-gnu-nm nm)}
    sha_tool=$(select_tool sha256sum sha256sum)
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
    [[ -s "$CAPSTONE_LIBRARY" ]] || fail "missing Capstone archive"
    library_hash=$($sha_tool "$library")
    library_hash=${library_hash%%[[:space:]]*}
    capstone_hash=$($sha_tool "$CAPSTONE_LIBRARY")
    capstone_hash=${capstone_hash%%[[:space:]]*}
    mkdir -p "$BUILD_ROOT"
    {
        printf 'source_repository=%s\n' "$SYSINT_REPOSITORY"
        printf 'source_commit=%s\n' "$SYSINT_COMMIT"
        printf 'capstone_repository=%s\n' "$CAPSTONE_REPOSITORY"
        printf 'capstone_commit=%s\n' "$CAPSTONE_COMMIT"
        printf 'capstone_archs=riscv\n'
        printf 'capstone_library=%s\n' "$CAPSTONE_LIBRARY"
        printf 'capstone_library_sha256=%s\n' "$capstone_hash"
        for patch_file in "$PATCH_DIR"/*.patch; do
            patch_hash=$($sha_tool "$patch_file")
            patch_hash=${patch_hash%%[[:space:]]*}
            printf 'patch=%s sha256=%s\n' "${patch_file##*/}" "$patch_hash"
        done
        printf 'library=%s\n' "$library"
        printf 'library_sha256=%s\n' "$library_hash"
        printf 'elf_machine=RISC-V\n'
    } >"$MANIFEST"
    printf '%s\n' "$identity"
    printf 'manifest=%s\n' "$MANIFEST"
    printf 'SYSINT_LIB_DIR=%s\n' "$BUILD_DIR"
}

select_cross_tools
if [[ "$MODE" == prepare ]]; then
    prepare_source
    printf '%s\n' 'syscall_intercept_prepare=PASS'
    exit 0
fi
if [[ "$MODE" == verify ]]; then
    verify_build
    printf '%s\n' 'syscall_intercept_verify=PASS'
    exit 0
fi

[[ "$SYSINT_JOBS" =~ ^[1-9][0-9]*$ ]] || fail "invalid SYSINT_JOBS: $SYSINT_JOBS"
prepare_source
validate_capstone_mirror
prepare_capstone_source
build_capstone
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
    cmake -S "$SOURCE_COPY" -B "$BUILD_DIR" \
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
    -DBUILD_CPP_TEST=OFF \
    -DUSE_PREBUILT_CAPSTONE=ON \
    -DPREBUILT_CAPSTONE_INCLUDE_DIR="$CAPSTONE_SOURCE_COPY/include/capstone" \
    -DPREBUILT_CAPSTONE_LIBRARY="$CAPSTONE_LIBRARY" \
    -DSTATIC_CAPSTONE=ON \
    -DTREAT_WARNINGS_AS_ERRORS=OFF
cmake --build "$BUILD_DIR" -j "$SYSINT_JOBS" --target syscall_intercept_shared
verify_build
printf '%s\n' 'syscall_intercept_build=PASS'
