#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
LEGOFS_ROOT="$ROOT/components/legofs"
BUILD="$ROOT/target/build/giga-native"
BIN="$BUILD/bin"
LIB="$BUILD/lib"
SOURCES="$BUILD/sources"
CARGO_TARGET="$BUILD/cargo"
SYSINT_SOURCE="$LEGOFS_ROOT/third_party/syscall-intercept-riscv"
SYSINT_BUILD="$BUILD/syscall-intercept"
RESULTS="$ROOT/target/results/giga-native"
STAMP="$BUILD/.native-build-complete"
IDENTITY_FILE="$ROOT/.giga-source-identity"

IO500_REPOSITORY=https://github.com/IO500/io500.git
IO500_COMMIT=a69cf60cf76538a34c1332bc448838cf9a560a9b
IOR_REPOSITORY=https://github.com/hpc/ior.git
IOR_COMMIT=5fcf0ba995fd92164d50e344597e2d8203298c08
PFIND_REPOSITORY=https://github.com/VI4IO/pfind.git
PFIND_COMMIT=d08501f9976caf1adabdebfb883d4701dd98fe35
PARENT_BASELINE=c828826f47a6a579bc1f1a0f8c2a2c7080461263
LEGOFS_BASELINE=0df6fddfc032f241e509ca0bf819a01adb97e211

MPICC=/usr/bin/mpicc.openmpi
MPIRUN=/usr/bin/mpirun.openmpi
JOBS="$(getconf _NPROCESSORS_ONLN 2>/dev/null || printf '1\n')"

usage()
{
	cat <<'EOF'
Usage: build_giga_native_io500.sh [--jobs N]

Build the pinned native x86_64 LegoFS, syscall-intercept and IO500 artifacts
used by the giga same-host LLC/NUMA experiment.
EOF
}

die()
{
	printf 'giga-native-build=FAIL reason=%s\n' "$*" >&2
	exit 2
}

while (($#)); do
	case "$1" in
	--jobs)
		(($# >= 2)) || die "--jobs requires a value"
		JOBS="$2"
		shift 2
		;;
	-h|--help)
		usage
		exit 0
		;;
	*) die "unknown argument: $1" ;;
	esac
done
[[ "$JOBS" =~ ^[1-9][0-9]*$ ]] || die "jobs must be a positive integer"

[ "$(uname -m)" = x86_64 ] || die "this build requires native x86_64"
[ -f "$LEGOFS_ROOT/Cargo.toml" ] || die "missing LegoFS source: $LEGOFS_ROOT"
[ -f "$SYSINT_SOURCE/CMakeLists.txt" ] || die "missing vendored syscall-intercept"

for command in bash git cargo rustc gcc cmake ninja make pkg-config file ldd \
	sha256sum readelf install python3 getconf find sort xargs awk sed grep; do
	command -v "$command" >/dev/null 2>&1 || die "missing command: $command"
done
[ -x "$MPICC" ] || die "missing OpenMPI compiler wrapper: $MPICC"
[ -x "$MPIRUN" ] || die "missing OpenMPI launcher: $MPIRUN"
for package in libpmem numa capstone; do
	pkg-config --exists "$package" || die "pkg-config package is missing: $package"
done

mkdir -p "$BIN" "$LIB" "$SOURCES" "$CARGO_TARGET" "$SYSINT_BUILD" "$RESULTS"

ensure_checkout()
{
	name="$1"
	repository="$2"
	commit="$3"
	directory="$4"
	if [ ! -d "$directory/.git" ]; then
		[ ! -e "$directory" ] || die "$name source exists but is not a Git checkout: $directory"
		git clone --no-checkout "$repository" "$directory"
		git -C "$directory" checkout --detach "$commit"
	fi
	actual_repository="$(git -C "$directory" remote get-url origin)"
	actual_commit="$(git -C "$directory" rev-parse HEAD)"
	[ "$actual_repository" = "$repository" ] ||
		die "$name repository mismatch: $actual_repository"
	[ "$actual_commit" = "$commit" ] || die "$name commit mismatch: $actual_commit"
	if git -C "$directory" symbolic-ref -q HEAD >/dev/null; then
		die "$name checkout is not detached: $directory"
	fi
}

printf '%s\n' '[giga-native-build] validating pinned source checkouts'
ensure_checkout IO500 "$IO500_REPOSITORY" "$IO500_COMMIT" "$SOURCES/io500"
ensure_checkout IOR "$IOR_REPOSITORY" "$IOR_COMMIT" "$SOURCES/io500/build/ior"
ensure_checkout pfind "$PFIND_REPOSITORY" "$PFIND_COMMIT" "$SOURCES/io500/build/pfind"

read_deployed_identity()
{
	key="$1"
	[ -f "$IDENTITY_FILE" ] || return 1
	awk -F= -v expected="$key" '$1 == expected {print substr($0, index($0, "=") + 1)}' \
		"$IDENTITY_FILE"
}

source_commit()
{
	directory="$1"
	key="$2"
	if git -C "$directory" rev-parse --verify HEAD >/dev/null 2>&1; then
		git -C "$directory" rev-parse HEAD
	else
		read_deployed_identity "$key"
	fi
}

PARENT_COMMIT="$(source_commit "$ROOT" parent_commit || true)"
LEGOFS_COMMIT="$(source_commit "$LEGOFS_ROOT" legofs_commit || true)"
[ -n "$PARENT_COMMIT" ] || die "parent source identity is unavailable"
[ -n "$LEGOFS_COMMIT" ] || die "LegoFS source identity is unavailable"

source_fingerprint()
{
	directory="$1"
	(
		cd "$directory"
		find . -type f \
			! -path './.git/*' ! -name '.git' \
			! -path './target/*' \
			! -path '*/__pycache__/*' \
			! -name '*.pyc' -print0 |
			LC_ALL=C sort -z |
			xargs -0 sha256sum |
			sha256sum | awk '{print $1}'
	)
}

LEGOFS_FINGERPRINT="$(source_fingerprint "$LEGOFS_ROOT")"
SCRIPT_FINGERPRINT="$(sha256sum "$0" | awk '{print $1}')"
SOURCE_MANIFEST="$RESULTS/source-files.sha256"
(
    cd "$ROOT"
    find components/legofs -type f ! -path '*/target/*' ! -path '*/.git/*' \
        ! -name '.git' ! -path '*/__pycache__/*' ! -name '*.pyc' -print0 |
        LC_ALL=C sort -z | xargs -0 sha256sum
    sha256sum scripts/build_giga_native_io500.sh scripts/run_giga_native_legofs_io500.sh
) > "$SOURCE_MANIFEST"
SOURCE_MANIFEST_SHA256="$(sha256sum "$SOURCE_MANIFEST" | awk '{print $1}')"

BUILD_KEY="$({
	printf 'contract=giga-native-v1\n'
	printf 'parent=%s\nlegofs=%s\n' "$PARENT_COMMIT" "$LEGOFS_COMMIT"
	printf 'legofs_files=%s\nbuild_script=%s\n' "$LEGOFS_FINGERPRINT" "$SCRIPT_FINGERPRINT"
	printf 'io500=%s\nior=%s\npfind=%s\n' "$IO500_COMMIT" "$IOR_COMMIT" "$PFIND_COMMIT"
	rustc --version
	gcc --version | sed -n '1p'
	"$MPICC" --showme:command
} | sha256sum | awk '{print $1}')"

expected_artifacts()
{
	printf '%s\n' \
		"$BIN/badfs-server" \
		"$BIN/badfs-bench" \
		"$BIN/io500" \
		"$BIN/io500-verify" \
		"$LIB/libbadfs_intercept.so" \
		"$LIB/libsyscall_intercept.so" \
		"$LIB/libsyscall_intercept.so.0"
}

reuse=true
if [ ! -f "$STAMP" ] || [ "$(sed -n '1p' "$STAMP" 2>/dev/null)" != "$BUILD_KEY" ]; then
	reuse=false
fi
while IFS= read -r artifact; do
	[ -e "$artifact" ] || reuse=false
done < <(expected_artifacts)

if [ "$reuse" = true ]; then
	printf '[giga-native-build] reusing build key %s\n' "$BUILD_KEY"
else
	printf '%s\n' '[giga-native-build] vendored native syscall-intercept'
	CAPSTONE_INCLUDE_ROOT="$(pkg-config --variable=includedir capstone)"
	if [ -f "$CAPSTONE_INCLUDE_ROOT/capstone.h" ]; then
		CAPSTONE_INCLUDE="$CAPSTONE_INCLUDE_ROOT"
	else
		CAPSTONE_INCLUDE="$CAPSTONE_INCLUDE_ROOT/capstone"
	fi
	CAPSTONE_LIBDIR="$(pkg-config --variable=libdir capstone)"
	CAPSTONE_LIBRARY="$CAPSTONE_LIBDIR/libcapstone.so"
	[ -f "$CAPSTONE_INCLUDE/capstone.h" ] ||
		die "capstone headers are missing below $CAPSTONE_INCLUDE"
	if [ ! -e "$CAPSTONE_LIBRARY" ]; then
		CAPSTONE_LIBRARY="$CAPSTONE_LIBDIR/libcapstone.a"
	fi
	[ -e "$CAPSTONE_LIBRARY" ] || die "capstone library is missing below $CAPSTONE_LIBDIR"
	CMAKE_BUILD_PARALLEL_LEVEL="$JOBS" cmake -S "$SYSINT_SOURCE" -B "$SYSINT_BUILD" \
		-G Ninja \
		-DCMAKE_BUILD_TYPE=Release \
		-DBUILD_TESTS=OFF \
		-DBUILD_EXAMPLES=OFF \
		-DBUILD_CPP_TEST=OFF \
		-DUSE_PREBUILT_CAPSTONE=ON \
		-DPREBUILT_CAPSTONE_INCLUDE_DIR="$CAPSTONE_INCLUDE" \
		-DPREBUILT_CAPSTONE_LIBRARY="$CAPSTONE_LIBRARY"
	cmake --build "$SYSINT_BUILD" --parallel "$JOBS"
	[ -e "$SYSINT_BUILD/libsyscall_intercept.so.0" ] ||
		die "native syscall-intercept build produced no shared library"
	cp -a "$SYSINT_BUILD"/libsyscall_intercept.so* "$LIB/"

	printf '%s\n' '[giga-native-build] native LegoFS products'
	LIBSYSCALL_INTERCEPT_LIB_DIR="$SYSINT_BUILD" \
	CARGO_TARGET_DIR="$CARGO_TARGET" \
		cargo build --locked --manifest-path "$LEGOFS_ROOT/Cargo.toml" --release \
		-p badfs-server -p badfs-bench
	LIBSYSCALL_INTERCEPT_LIB_DIR="$SYSINT_BUILD" \
	CARGO_TARGET_DIR="$CARGO_TARGET" \
	RUSTFLAGS="-C link-arg=-Wl,-rpath,\$ORIGIN" \
		cargo build --locked --manifest-path "$LEGOFS_ROOT/Cargo.toml" --release \
		-p badfs-intercept --features syscall-intercept-backend
	install -m 0755 "$CARGO_TARGET/release/badfs-server" "$BIN/badfs-server"
	install -m 0755 "$CARGO_TARGET/release/badfs-bench" "$BIN/badfs-bench"
	install -m 0755 "$CARGO_TARGET/release/libbadfs_intercept.so" \
		"$LIB/libbadfs_intercept.so"

	printf '%s\n' '[giga-native-build] pinned native IOR, pfind and IO500'
	IOR_SOURCE="$SOURCES/io500/build/ior"
	if [ ! -x "$IOR_SOURCE/configure" ]; then
		(
			cd "$IOR_SOURCE"
			./bootstrap
		)
	fi
	make -C "$IOR_SOURCE" distclean >/dev/null 2>&1 || true
	(
		cd "$IOR_SOURCE"
		./configure CC="$MPICC" MPICC="$MPICC" \
			--with-mpiio=no --without-cuda --without-gpuDirect \
			--prefix="$SOURCES/io500"
		make -C src -j "$JOBS" install
	)
	(
		cd "$SOURCES/io500/build/pfind"
		CC="$MPICC" ./compile.sh
	)
	(
		cd "$SOURCES/io500"
		make clean >/dev/null 2>&1 || true
		make -j "$JOBS" CC="$MPICC"
	)
	install -m 0755 "$SOURCES/io500/io500" "$BIN/io500"
	install -m 0755 "$SOURCES/io500/io500-verify" "$BIN/io500-verify"

	stamp_temporary="$BUILD/.native-build-complete.$$"
	printf '%s\n' "$BUILD_KEY" > "$stamp_temporary"
	mv -f -- "$stamp_temporary" "$STAMP"
fi

printf '%s\n' '[giga-native-build] validating native artifacts'
HASHES="$RESULTS/build-artifact-hashes.txt"
TYPES="$RESULTS/build-artifact-types.tsv"
: > "$HASHES"
: > "$TYPES"
validate_artifact()
{
	name="$1"
	path="$2"
	[ -f "$path" ] || die "artifact is missing: $path"
	description="$(file -L "$path")"
	printf '%s\n' "$description" | grep -q 'x86-64' ||
		die "artifact is not x86-64: $path"
	printf '%s\t%s\n' "$name" "$description" >> "$TYPES"
	if ! ldd "$path" > "$RESULTS/ldd-$name.txt" 2>&1; then
		die "ldd failed for $path"
	fi
	if grep -q 'not found' "$RESULTS/ldd-$name.txt"; then
		die "artifact has an unresolved dynamic dependency: $path"
	fi
	sha256sum "$path" >> "$HASHES"
}

validate_artifact badfs_server "$BIN/badfs-server"
validate_artifact badfs_bench "$BIN/badfs-bench"
validate_artifact io500 "$BIN/io500"
validate_artifact io500_verify "$BIN/io500-verify"
validate_artifact badfs_intercept "$LIB/libbadfs_intercept.so"
validate_artifact syscall_intercept "$LIB/libsyscall_intercept.so.0"
readelf -d "$LIB/libbadfs_intercept.so" |
	grep -E '\((RPATH|RUNPATH)\).*\$ORIGIN' >/dev/null ||
	die "libbadfs_intercept.so does not resolve syscall-intercept beside itself"

gcc --version > "$RESULTS/gcc-version.txt"
rustc --version --verbose > "$RESULTS/rustc-version.txt"
cargo --version --verbose > "$RESULTS/cargo-version.txt"
"$MPICC" --showme > "$RESULTS/mpicc-showme.txt"
"$MPIRUN" --version > "$RESULTS/mpirun-version.txt"

MANIFEST="$RESULTS/build-manifest.json"
MANIFEST_TMP="$RESULTS/.build-manifest.json.$$"
SOURCE_ROOT_VALUE="$ROOT" SOURCE_MANIFEST_SHA256_VALUE="$SOURCE_MANIFEST_SHA256" \
BUILD_ROOT="$BUILD" RESULT_ROOT="$RESULTS" HASH_FILE="$HASHES" TYPE_FILE="$TYPES" \
	BUILD_KEY_VALUE="$BUILD_KEY" PARENT_COMMIT_VALUE="$PARENT_COMMIT" \
	LEGOFS_COMMIT_VALUE="$LEGOFS_COMMIT" LEGOFS_FINGERPRINT_VALUE="$LEGOFS_FINGERPRINT" \
	SCRIPT_FINGERPRINT_VALUE="$SCRIPT_FINGERPRINT" \
	IO500_COMMIT_VALUE="$IO500_COMMIT" IOR_COMMIT_VALUE="$IOR_COMMIT" \
	PFIND_COMMIT_VALUE="$PFIND_COMMIT" PARENT_BASELINE_VALUE="$PARENT_BASELINE" \
	LEGOFS_BASELINE_VALUE="$LEGOFS_BASELINE" python3 - "$MANIFEST_TMP" <<'PY'
import json
import os
from pathlib import Path
import sys

output = Path(sys.argv[1])
build = Path(os.environ["BUILD_ROOT"])
results = Path(os.environ["RESULT_ROOT"])
hashes = {}
for line in Path(os.environ["HASH_FILE"]).read_text(encoding="utf-8").splitlines():
    digest, path = line.split(maxsplit=1)
    hashes[str(Path(path).resolve())] = digest
types = {}
for line in Path(os.environ["TYPE_FILE"]).read_text(encoding="utf-8").splitlines():
    name, description = line.split("\t", 1)
    types[name] = description
artifacts = {
    "badfs_server": build / "bin/badfs-server",
    "badfs_bench": build / "bin/badfs-bench",
    "io500": build / "bin/io500",
    "io500_verify": build / "bin/io500-verify",
    "badfs_intercept": build / "lib/libbadfs_intercept.so",
    "syscall_intercept": build / "lib/libsyscall_intercept.so.0",
}
record = {
    "schema_version": "giga.native-build.v1",
    "build_key": os.environ["BUILD_KEY_VALUE"],
    "source_root": os.environ["SOURCE_ROOT_VALUE"],
    "source_manifest_sha256": os.environ["SOURCE_MANIFEST_SHA256_VALUE"],
    "baselines": {
        "parent": os.environ["PARENT_BASELINE_VALUE"],
        "legofs": os.environ["LEGOFS_BASELINE_VALUE"],
    },
    "implementation": {
        "parent_commit": os.environ["PARENT_COMMIT_VALUE"],
        "legofs_commit": os.environ["LEGOFS_COMMIT_VALUE"],
        "legofs_files_sha256": os.environ["LEGOFS_FINGERPRINT_VALUE"],
        "build_script_sha256": os.environ["SCRIPT_FINGERPRINT_VALUE"],
    },
    "pinned_sources": {
        "io500": os.environ["IO500_COMMIT_VALUE"],
        "ior": os.environ["IOR_COMMIT_VALUE"],
        "pfind": os.environ["PFIND_COMMIT_VALUE"],
    },
    "tools": {
        "gcc": (results / "gcc-version.txt").read_text(encoding="utf-8"),
        "rustc": (results / "rustc-version.txt").read_text(encoding="utf-8"),
        "cargo": (results / "cargo-version.txt").read_text(encoding="utf-8"),
        "mpicc": (results / "mpicc-showme.txt").read_text(encoding="utf-8"),
        "mpirun": (results / "mpirun-version.txt").read_text(encoding="utf-8"),
    },
    "artifacts": {
        name: {
            "path": str(path),
            "size": path.stat().st_size,
            "sha256": hashes[str(path.resolve())],
            "file": types[name],
            "ldd": str(results / f"ldd-{name}.txt"),
        }
        for name, path in artifacts.items()
    },
}
with output.open("x", encoding="utf-8") as destination:
    json.dump(record, destination, indent=2, sort_keys=True)
    destination.write("\n")
    destination.flush()
    os.fsync(destination.fileno())
PY
mv -f -- "$MANIFEST_TMP" "$MANIFEST"
printf 'giga-native-build=PASS manifest=%s build_key=%s\n' "$MANIFEST" "$BUILD_KEY"
