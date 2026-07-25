# CXLMemSim-riscv Superproject Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish `SlugLab/CXLMemSim-riscv` as a pinned-source superproject whose `./run.sh` builds the complete SiFive U CXL stack and proves a Type 3 device issuing non-zero reads and writes through CXLMemSim PGAS SHM.

**Architecture:** Seven Git submodules pin QEMU, U-Boot, Linux, OpenSBI, CXLMemSim, HiFive tools, and `meta-sifive`. A thin shell entry point validates arguments and invokes a native-Linux build script; a Python harness owns CXLMemSim/QEMU process lifecycle, U-Boot console automation, SHM validation, guest proof parsing, cleanup, and atomic JSON results.

**Tech Stack:** Git submodules, Bash, Python 3 standard library, C11 freestanding RISC-V binaries, GNU RISC-V cross toolchain, QEMU TCG, OpenSBI, U-Boot EFI, Linux CXL, CMake, Meson/Ninja, ext2/debugfs, POSIX shared memory.

---

## Working directory and file structure

Execute this plan from `/root/cxl-u-boot`. Create the new independent Git
repository at `/root/cxl-u-boot/CXLMemSim-riscv`; do not initialize or reuse
the non-repository sentinel at `/root/cxl-u-boot/.git`.

Created superproject files and responsibilities:

- `CXLMemSim-riscv/.gitmodules`: immutable component source URLs.
- `CXLMemSim-riscv/.gitignore`: excludes all generated state.
- `CXLMemSim-riscv/README.md`: clone, dependency, build, run, evidence, and
  limitation documentation.
- `CXLMemSim-riscv/run.sh`: stable CLI, submodule safety checks, and
  build/run dispatch.
- `CXLMemSim-riscv/configs/linux-cxl.config`: static required Linux options.
- `CXLMemSim-riscv/guest/cxl_mmap_bench.c`: libc-free CXL mapping benchmark.
- `CXLMemSim-riscv/guest/init.c`: libc-free PID 1 and topology validator.
- `CXLMemSim-riscv/scripts/check-deps.sh`: host dependency report.
- `CXLMemSim-riscv/scripts/build.sh`: component builds, initramfs, ext2 image,
  and manifest.
- `CXLMemSim-riscv/scripts/write_manifest.py`: deterministic build metadata
  and SHA-256 generation.
- `CXLMemSim-riscv/scripts/run.py`: runtime command construction, console
  automation, SHM/server lifecycle, validation, and result publication.
- `CXLMemSim-riscv/tests/test_cli.py`: offline `run.sh` argument tests.
- `CXLMemSim-riscv/tests/test_guest.py`: native and cross guest build tests.
- `CXLMemSim-riscv/tests/test_manifest.py`: manifest tests.
- `CXLMemSim-riscv/tests/test_runtime.py`: offline runtime/parser/lifecycle
  tests.
- `CXLMemSim-riscv/docs/superpowers/specs/2026-07-25-cxlmemsim-riscv-superrepo-design.md`:
  approved design.
- `CXLMemSim-riscv/docs/superpowers/plans/2026-07-25-cxlmemsim-riscv-superrepo.md`:
  this plan.
- `CXLMemSim-riscv/components/*`: seven pinned Git submodules.
- `CXLMemSim-riscv/out/*`: generated, ignored build and evidence files.

### Task 1: Initialize the superproject and pin every component

**Files:**
- Create: `CXLMemSim-riscv/.git`
- Create: `CXLMemSim-riscv/.gitmodules`
- Create: `CXLMemSim-riscv/.gitignore`
- Create: `CXLMemSim-riscv/docs/superpowers/specs/2026-07-25-cxlmemsim-riscv-superrepo-design.md`
- Create: `CXLMemSim-riscv/docs/superpowers/plans/2026-07-25-cxlmemsim-riscv-superrepo.md`
- Create gitlinks: `CXLMemSim-riscv/components/*`

- [ ] **Step 1: Verify all remote commits before creating state**

Run:

```bash
git ls-remote https://github.com/Zettai-US/qemu-cxl-type2.git \
  refs/heads/feat/sifive-u-cxl-qemu
git ls-remote https://github.com/SlugLab/u-boot.git \
  refs/heads/feat/sifive-u-cxl-uboot
git ls-remote https://github.com/vickiegpt/linux-cxl-type2.git \
  refs/heads/feat/sifive-u-cxl-linux
git ls-remote https://github.com/SlugLab/CXLMemSim.git \
  refs/heads/main
git ls-remote https://github.com/riscv-software-src/opensbi.git \
  'refs/tags/v1.5.1^{}'
git ls-remote https://github.com/SlugLab/hifive-premier-p550-tools.git \
  HEAD
git ls-remote https://github.com/SlugLab/meta-sifive.git \
  HEAD
```

Expected: the first columns, in order, equal the seven commit IDs in the
approved design; OpenSBI tag `v1.5.1^{}` resolves to
`43cace6c3671e5172d0df0a8963e552bb04b7b20`.

- [ ] **Step 2: Initialize a clean main branch**

Run:

```bash
test ! -e CXLMemSim-riscv
mkdir CXLMemSim-riscv
git -C CXLMemSim-riscv init -b main
```

Expected: a new empty repository on `main`. If the target already exists,
inspect it and stop instead of deleting or overwriting it.

- [ ] **Step 3: Add and detach all submodules at the pinned commits**

Run from `CXLMemSim-riscv`:

```bash
git submodule add https://github.com/Zettai-US/qemu-cxl-type2.git components/qemu
git submodule add https://github.com/SlugLab/u-boot.git components/u-boot
git submodule add https://github.com/vickiegpt/linux-cxl-type2.git components/linux
git submodule add https://github.com/SlugLab/CXLMemSim.git components/cxlmemsim
git submodule add https://github.com/riscv-software-src/opensbi.git components/opensbi
git submodule add https://github.com/SlugLab/hifive-premier-p550-tools.git components/hifive-premier-tools
git submodule add https://github.com/SlugLab/meta-sifive.git components/meta-sifive

git -C components/qemu checkout --detach 81cd7ad9a5e14470427c8ebafeccff4f52e555b4
git -C components/u-boot checkout --detach d5948c7033dc8b2352099b4fd906cb8cc9cc0bdd
git -C components/linux checkout --detach 108e1b383db789b7f8292ff62a73efa441820dca
git -C components/cxlmemsim checkout --detach d37e3ab9b44cc1ebdf9eb5d64c9390d309e8e529
git -C components/opensbi checkout --detach 43cace6c3671e5172d0df0a8963e552bb04b7b20
git -C components/hifive-premier-tools checkout --detach a0d52ef83c9f0ac120a10bd59b77c6c88466e167
git -C components/meta-sifive checkout --detach c26f3490b5927cad96e71c0e4b2e92b7e5af34c0
```

Expected: all component worktrees are detached and clean.

- [ ] **Step 4: Add ignore rules and approved documents**

Create `.gitignore` with:

```gitignore
/out/
__pycache__/
*.py[cod]
```

Copy the approved documents:

```bash
mkdir -p docs/superpowers/specs docs/superpowers/plans
cp ../docs/superpowers/specs/2026-07-25-cxlmemsim-riscv-superrepo-design.md \
  docs/superpowers/specs/
cp ../docs/superpowers/plans/2026-07-25-cxlmemsim-riscv-superrepo.md \
  docs/superpowers/plans/
```

- [ ] **Step 5: Verify gitlinks exactly**

Run:

```bash
git submodule status
test "$(git -C components/qemu rev-parse HEAD)" = 81cd7ad9a5e14470427c8ebafeccff4f52e555b4
test "$(git -C components/u-boot rev-parse HEAD)" = d5948c7033dc8b2352099b4fd906cb8cc9cc0bdd
test "$(git -C components/linux rev-parse HEAD)" = 108e1b383db789b7f8292ff62a73efa441820dca
test "$(git -C components/cxlmemsim rev-parse HEAD)" = d37e3ab9b44cc1ebdf9eb5d64c9390d309e8e529
test "$(git -C components/opensbi rev-parse HEAD)" = 43cace6c3671e5172d0df0a8963e552bb04b7b20
test "$(git -C components/hifive-premier-tools rev-parse HEAD)" = a0d52ef83c9f0ac120a10bd59b77c6c88466e167
test "$(git -C components/meta-sifive rev-parse HEAD)" = c26f3490b5927cad96e71c0e4b2e92b7e5af34c0
```

Expected: every test exits zero and no submodule status line begins with `+`
or `-`.

- [ ] **Step 6: Commit the reproducible source graph**

Run:

```bash
git add .gitmodules .gitignore components docs
git commit -m "chore: pin SiFive U CXL component sources"
```

Expected: the first commit contains only documentation, configuration, and
seven gitlinks; no compiled file is present.

### Task 2: Implement the safe CLI and dependency preflight

**Files:**
- Create: `CXLMemSim-riscv/tests/test_cli.py`
- Create: `CXLMemSim-riscv/run.sh`
- Create: `CXLMemSim-riscv/scripts/check-deps.sh`

- [ ] **Step 1: Write failing CLI tests**

Create `tests/test_cli.py` with tests that invoke `run.sh` in subprocesses:

```python
import os
import pathlib
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUN = ROOT / "run.sh"
DEPS = ROOT / "scripts" / "check-deps.sh"


class CliTest(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run(
            [str(RUN), *args], cwd=ROOT, text=True, capture_output=True)

    def test_help_does_not_build(self):
        run = self.run_cli("--help")
        self.assertEqual(run.returncode, 0)
        self.assertIn("--build-only", run.stdout)
        self.assertIn("--benchmark-bytes", run.stdout)

    def test_conflicting_modes_fail(self):
        run = self.run_cli("--build-only", "--run-only")
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("mutually exclusive", run.stderr)

    def test_invalid_bytes_fail_before_build(self):
        for value in ("0", "7", "268435464", "text"):
            with self.subTest(value=value):
                run = self.run_cli("--benchmark-bytes", value)
                self.assertNotEqual(run.returncode, 0)
                self.assertIn("benchmark bytes", run.stderr.lower())

    def test_dependency_checker_lists_every_missing_command(self):
        env = os.environ.copy()
        env["CXL_REQUIRED_COMMANDS"] = \
            "definitely_missing_one definitely_missing_two"
        run = subprocess.run(
            [str(DEPS)], cwd=ROOT, env=env, text=True, capture_output=True)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("definitely_missing_one", run.stderr)
        self.assertIn("definitely_missing_two", run.stderr)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
python3 -m unittest -v tests/test_cli.py
```

Expected: import/setup succeeds but tests fail because both scripts are
missing.

- [ ] **Step 3: Implement `scripts/check-deps.sh`**

The script must use `set -eu`, iterate over
`$CXL_REQUIRED_COMMANDS` when set, and otherwise check:

```text
bash git make gcc g++ cmake ninja meson pkg-config python3
riscv64-linux-gnu-gcc riscv64-linux-gnu-ld
riscv64-linux-gnu-readelf dtc mke2fs debugfs
```

Collect all missing names before exiting. Its failure format is:

```text
missing required host command(s): name1 name2
Ubuntu/Debian hint: install build-essential cmake ninja-build meson pkg-config python3 gcc-riscv64-linux-gnu binutils-riscv64-linux-gnu device-tree-compiler e2fsprogs
```

Do not run a package manager or `sudo`.

- [ ] **Step 4: Implement `run.sh`**

Use this dispatch contract:

```bash
#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BUILD=1
RUN=1
JOBS="$(getconf _NPROCESSORS_ONLN 2>/dev/null || printf '1\n')"
BENCHMARK_BYTES=1048576
```

Parse `--build-only`, `--run-only`, `--jobs N`,
`--benchmark-bytes N`, and `--help`. Validate:

```text
JOBS matches ^[1-9][0-9]*$
BENCHMARK_BYTES matches ^[1-9][0-9]*$
BENCHMARK_BYTES % 8 == 0
BENCHMARK_BYTES <= 268435456
```

Perform all argument validation before submodule or dependency work. For each
populated submodule, fail when `git status --porcelain` is non-empty or HEAD
differs from the superproject gitlink. Initialize only missing submodules with:

```bash
git -C "$ROOT" submodule update --init --recursive
```

Then call:

```bash
"$ROOT/scripts/check-deps.sh"
"$ROOT/scripts/build.sh" --jobs "$JOBS"          # when BUILD=1
python3 "$ROOT/scripts/run.py" \
  --benchmark-bytes "$BENCHMARK_BYTES"           # when RUN=1
```

Set executable modes with:

```bash
chmod +x run.sh scripts/check-deps.sh
```

- [ ] **Step 5: Run the CLI tests and syntax checks**

Run:

```bash
bash -n run.sh scripts/check-deps.sh
python3 -m unittest -v tests/test_cli.py
```

Expected: shell syntax succeeds and all four tests pass.

- [ ] **Step 6: Commit**

Run:

```bash
git add run.sh scripts/check-deps.sh tests/test_cli.py
git commit -m "feat: add safe build and run entry point"
```

### Task 3: Import and verify the libc-free CXL benchmark

**Files:**
- Create: `CXLMemSim-riscv/guest/cxl_mmap_bench.c`
- Create: `CXLMemSim-riscv/tests/test_guest.py`

- [ ] **Step 1: Write failing benchmark tests**

Create `tests/test_guest.py` that:

1. compiles `guest/cxl_mmap_bench.c` natively without
   `CXL_BENCH_FREESTANDING`;
2. runs `--self-test`;
3. parses the only stdout line as JSON;
4. asserts `status == "pass"`, three positive write samples, three positive
   read samples, positive random latency, and `verified is True`;
5. cross-compiles with the exact freestanding flags;
6. asserts `readelf -h` reports RISC-V and `readelf -l` contains no
   `INTERP` segment;
7. asserts `readelf -A` does not advertise vector ISA `v`.

Use a temporary directory for both binaries so the test leaves no artifacts
in the repository.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
python3 -m unittest -v tests/test_guest.py
```

Expected: FAIL because `guest/cxl_mmap_bench.c` is missing.

- [ ] **Step 3: Import the already validated source**

Copy the validated implementation without changing benchmark behavior:

```bash
mkdir -p guest
cp ../compiled-results/benchmark/cxl_mmap_bench.c guest/cxl_mmap_bench.c
```

The freestanding invocation remains:

```text
/mnt/cxl_mmap_bench 0x1000000000 BYTES 3 100000
```

The JSON must include `status`, `bytes`, three `write_seconds`, three
`read_seconds`, median write/read MiB/s, `random_ops`,
`random_ns_per_load`, `checksum`, and `verified`.

- [ ] **Step 4: Run guest tests and verify GREEN**

Run:

```bash
python3 -m unittest -v tests/test_guest.py
```

Expected: native bit-exact self-test passes; freestanding RISC-V ELF has no
dynamic interpreter or RVV requirement.

- [ ] **Step 5: Commit**

Run:

```bash
git add guest/cxl_mmap_bench.c tests/test_guest.py
git commit -m "feat: add freestanding CXL memory benchmark"
```

### Task 4: Implement the freestanding guest init

**Files:**
- Modify: `CXLMemSim-riscv/tests/test_guest.py`
- Create: `CXLMemSim-riscv/guest/init.c`

- [ ] **Step 1: Add failing init contract tests**

Extend `tests/test_guest.py` to cross-compile `guest/init.c` with the same
freestanding `rv64imafdc/lp64d` flags. Assert:

```python
source = (ROOT / "guest" / "init.c").read_text()
for marker in (
    "CXL_GUEST_INIT_START",
    "CXL_DISK_PASS",
    "CXL_TOPOLOGY_PASS",
    "CXL_QEMU_UBOOT_LINUX_BENCH_PASS",
    "CXL_GUEST_INIT_FAIL",
):
    self.assertIn(marker, source)
self.assertNotIn("system(", source)
self.assertNotIn("popen(", source)
```

Inspect the resulting ELF exactly as for the benchmark.

- [ ] **Step 2: Run the new test and verify RED**

Run:

```bash
python3 -m unittest -v tests/test_guest.py
```

Expected: benchmark tests pass; init test fails because `guest/init.c` is
missing.

- [ ] **Step 3: Implement direct-syscall PID 1**

Implement `guest/init.c` with no libc headers or runtime. Define RISC-V Linux
syscall wrappers around `ecall` and constants for:

```text
mkdirat=34 mount=40 openat=56 close=57 read=63 write=64
readlinkat=78 nanosleep=101 reboot=142 clone=220 execve=221
wait4=260
```

Define `_start` as the ELF entry point; it calls the PID 1 routine and invokes
the exit syscall only if the reboot fallback unexpectedly returns.

Use these exact phases:

1. print `CXL_GUEST_INIT_START`;
2. mount `proc` on `/proc`, `sysfs` on `/sys`, and `devtmpfs` on `/dev`;
3. create `/mnt` and wait up to 20 seconds for `/dev/vda`;
4. mount `/dev/vda` as read-only ext2 and print `CXL_DISK_PASS`;
5. verify the `cxl_pci` driver symlink, `decoder0.0/start`,
   `region0/resource`, `region0/size`, and `/proc/iomem`;
6. print `CXL_TOPOLOGY_PASS`;
7. parse `cxl_bench_bytes=N` from `/proc/cmdline`, defaulting to `1048576`
   and enforcing the same 8-byte and 256 MiB bounds as `run.sh`;
8. `clone(SIGCHLD)`, then `execve("/mnt/cxl_mmap_bench", argv, envp)` with
   base `0x1000000000`, three iterations, and 100,000 random loads;
9. `wait4()` and require child exit status zero;
10. print `CXL_QEMU_UBOOT_LINUX_BENCH_PASS`;
11. invoke `reboot(LINUX_REBOOT_MAGIC1, LINUX_REBOOT_MAGIC2,
    LINUX_REBOOT_CMD_POWER_OFF, 0)` and spin if it returns.

The topology values must be exact:

```text
/sys/bus/cxl/devices/decoder0.0/start  -> 0x1000000000
/sys/bus/cxl/devices/region0/resource -> 0x1000000000
/sys/bus/cxl/devices/region0/size     -> 0x10000000
/proc/iomem contains 1000000000-10ffffffff : CXL Window 0
```

Every failure prints
`CXL_GUEST_INIT_FAIL phase=<fixed-phase-name> errno=<positive-number>` and
powers off. Use fixed phase names `mount-proc`, `mount-sys`, `mount-dev`,
`wait-vda`, `mount-ext2`, `topology`, `cmdline`, `clone`, `exec`, and
`benchmark`.

- [ ] **Step 4: Run tests and verify GREEN**

Run:

```bash
python3 -m unittest -v tests/test_guest.py
```

Expected: both guest programs cross-compile as static non-RVV RISC-V ELF
files, and all marker/behavior checks pass.

- [ ] **Step 5: Commit**

Run:

```bash
git add guest/init.c tests/test_guest.py
git commit -m "feat: add freestanding CXL validation init"
```

### Task 5: Add the Linux configuration contract

**Files:**
- Create: `CXLMemSim-riscv/configs/linux-cxl.config`
- Create: `CXLMemSim-riscv/tests/test_build_contract.py`

- [ ] **Step 1: Write the failing configuration test**

Create `tests/test_build_contract.py` that parses `configs/linux-cxl.config`
and asserts these entries equal `y`:

```text
CONFIG_PCI
CONFIG_PCIEPORTBUS
CONFIG_EFI
CONFIG_EFI_STUB
CONFIG_CXL_BUS
CONFIG_CXL_PCI
CONFIG_CXL_ACPI
CONFIG_CXL_PORT
CONFIG_CXL_REGION
CONFIG_CXL_TYPE2_ACCEL
CONFIG_DEVTMPFS
CONFIG_DEVTMPFS_MOUNT
CONFIG_DEVMEM
CONFIG_VIRTIO
CONFIG_VIRTIO_PCI
CONFIG_VIRTIO_BLK
CONFIG_EXT4_FS
CONFIG_EXT4_USE_FOR_EXT2
CONFIG_BLK_DEV_INITRD
```

Also assert `CONFIG_INITRAMFS_SOURCE` is absent from the static fragment,
because `build.sh` must set its absolute generated path.

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
python3 -m unittest -v tests/test_build_contract.py
```

Expected: FAIL because the fragment is missing.

- [ ] **Step 3: Create the configuration fragment**

Create `configs/linux-cxl.config` with one `CONFIG_NAME=y` line for every
entry above. Add `CONFIG_PROC_FS=y`, `CONFIG_SYSFS=y`,
`CONFIG_TMPFS=y`, and `CONFIG_BINFMT_ELF=y`, which PID 1 and the benchmark
require.

- [ ] **Step 4: Run the contract test and verify GREEN**

Run:

```bash
python3 -m unittest -v tests/test_build_contract.py
```

Expected: all required built-in configuration entries pass.

- [ ] **Step 5: Commit**

Run:

```bash
git add configs/linux-cxl.config tests/test_build_contract.py
git commit -m "build: define Linux CXL guest configuration"
```

### Task 6: Implement deterministic build-manifest generation

**Files:**
- Create: `CXLMemSim-riscv/tests/test_manifest.py`
- Create: `CXLMemSim-riscv/scripts/write_manifest.py`

- [ ] **Step 1: Write failing manifest tests**

Create a temporary fake repository with two artifact files and invoke:

```text
python3 scripts/write_manifest.py
  --root /tmp/manifest-test-root
  --output /tmp/manifest-test-root/out/results/build-manifest.json
  --artifact name=/tmp/manifest-test-root/out/example.bin
  --artifact other=/tmp/manifest-test-root/out/other.bin
```

Assert the JSON contains:

```json
{
  "schema_version": 1,
  "superproject_commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "submodules": {
    "components/qemu": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  },
  "artifacts": {
    "name": {
      "path": "out/example.bin",
      "size": 3,
      "sha256": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    }
  }
}
```

Also assert the output is atomically replaced and an absent artifact returns
non-zero without replacing an existing valid manifest.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
python3 -m unittest -v tests/test_manifest.py
```

Expected: FAIL because `scripts/write_manifest.py` is missing.

- [ ] **Step 3: Implement the manifest writer**

Use `argparse`, `hashlib.sha256`, `json`, `pathlib`, `subprocess.run`, and
`os.replace`. Parse `name=path` with `partition("=")` and reject empty or
duplicate names. Read submodule commits from:

```text
git -C ROOT submodule status --recursive
```

Hash files in 1 MiB chunks. Write sorted, indented JSON to
`OUTPUT.tmp`, `flush()`, `os.fsync()`, and `os.replace()`.

- [ ] **Step 4: Run tests and verify GREEN**

Run:

```bash
python3 -m unittest -v tests/test_manifest.py
```

Expected: all manifest success and failure-path tests pass.

- [ ] **Step 5: Commit**

Run:

```bash
git add scripts/write_manifest.py tests/test_manifest.py
git commit -m "build: record reproducible artifact manifest"
```

### Task 7: Implement the complete source build

**Files:**
- Create: `CXLMemSim-riscv/scripts/build.sh`
- Modify: `CXLMemSim-riscv/tests/test_build_contract.py`

- [ ] **Step 1: Add failing static build-script tests**

Extend `tests/test_build_contract.py` to assert `scripts/build.sh` contains
the exact source/build contracts:

```text
--target-list=riscv64-softmmu
sifive_unleashed_qemu_cxl_defconfig
NO_PYTHON=1
PLATFORM=generic
-march=rv64imafdc
-mabi=lp64d
-nostdlib
CONFIG_INITRAMFS_SOURCE
olddefconfig
mke2fs
debugfs
cxlmemsim_server
write_manifest.py
```

Run `bash -n scripts/build.sh` as part of the test.

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
python3 -m unittest -v tests/test_build_contract.py
```

Expected: configuration test passes and script test fails because
`scripts/build.sh` is missing.

- [ ] **Step 3: Implement argument and directory setup**

Start `scripts/build.sh` with `set -euo pipefail`, accept only
`--jobs N`, and calculate:

```bash
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/out"
BUILD="$OUT/build"
IMAGES="$OUT/images"
RESULTS="$OUT/results"
CROSS_COMPILE="${CROSS_COMPILE:-riscv64-linux-gnu-}"
```

Create only the specific directories above plus `out/logs` and
`out/runtime-bin`. Do not clean existing build trees.

Set `scripts/build.sh` executable before invoking it:

```bash
chmod +x scripts/build.sh
```

- [ ] **Step 4: Add QEMU, U-Boot, OpenSBI, and CXLMemSim builds**

Use these commands:

```bash
"$ROOT/components/qemu/configure" \
  --target-list=riscv64-softmmu \
  --disable-docs \
  --prefix="$BUILD/qemu-install"
ninja -C "$BUILD/qemu" -j "$JOBS" qemu-system-riscv64

make -C "$ROOT/components/u-boot" O="$BUILD/u-boot" \
  CROSS_COMPILE="$CROSS_COMPILE" NO_PYTHON=1 \
  sifive_unleashed_qemu_cxl_defconfig
make -C "$ROOT/components/u-boot" O="$BUILD/u-boot" \
  CROSS_COMPILE="$CROSS_COMPILE" NO_PYTHON=1 -j "$JOBS"

make -C "$ROOT/components/opensbi" O="$BUILD/opensbi" \
  CROSS_COMPILE="$CROSS_COMPILE" PLATFORM=generic -j "$JOBS"

cmake -S "$ROOT/components/cxlmemsim" -B "$BUILD/cxlmemsim" \
  -DCMAKE_BUILD_TYPE=Release
cmake --build "$BUILD/cxlmemsim" --target cxlmemsim_server -j "$JOBS"
```

Run QEMU configure from `"$BUILD/qemu"` so its output is out of tree. Preserve
each command's stdout/stderr in `out/logs/build.log` while still returning the
real pipeline status.

- [ ] **Step 5: Build guest payloads and initramfs source tree**

Compile both guest programs with:

```bash
"${CROSS_COMPILE}gcc" -O2 -std=c11 -Wall -Wextra -Werror \
  -static -nostdlib -fno-builtin -fno-stack-protector \
  -fno-pie -no-pie -march=rv64imafdc -mabi=lp64d \
  -DCXL_BENCH_FREESTANDING guest/cxl_mmap_bench.c \
  -o "$IMAGES/cxl_mmap_bench"
```

Compile `guest/init.c` with the same flags but without
`-DCXL_BENCH_FREESTANDING`. Install it at
`out/images/initramfs/init` mode `0755`. Verify both with
`${CROSS_COMPILE}readelf`.

- [ ] **Step 6: Configure and build Linux**

Run:

```bash
make -C "$ROOT/components/linux" O="$BUILD/linux" \
  ARCH=riscv CROSS_COMPILE="$CROSS_COMPILE" defconfig
"$ROOT/components/linux/scripts/kconfig/merge_config.sh" \
  -O "$BUILD/linux" "$BUILD/linux/.config" \
  "$ROOT/configs/linux-cxl.config"
"$ROOT/components/linux/scripts/config" \
  --file "$BUILD/linux/.config" \
  --set-str CONFIG_INITRAMFS_SOURCE "$IMAGES/initramfs"
make -C "$ROOT/components/linux" O="$BUILD/linux" \
  ARCH=riscv CROSS_COMPILE="$CROSS_COMPILE" olddefconfig
make -C "$ROOT/components/linux" O="$BUILD/linux" \
  ARCH=riscv CROSS_COMPILE="$CROSS_COMPILE" -j "$JOBS" Image
```

Export `ARCH=riscv` and `CROSS_COMPILE="$CROSS_COMPILE"` for the
`merge_config.sh` invocation so its internal make call uses the same target.
After `olddefconfig`, loop over the required options from Task 5 and require
`^CONFIG_NAME=y$`. Require the final line to equal
`CONFIG_INITRAMFS_SOURCE="$IMAGES/initramfs"` after expanding `IMAGES` to its
absolute value and adding the Kconfig quotes. Fail before compiling Linux if
any option is missing, modular, or changed to `n`.

- [ ] **Step 7: Create the external ext2 image without mounting**

Create a 16 MiB sparse file at
`out/images/cxl-type3-benchmark.ext2`, format it with:

```bash
mke2fs -q -t ext2 -F "$IMAGES/cxl-type3-benchmark.ext2"
```

Use
`debugfs -w -R "write $IMAGES/cxl_mmap_bench /cxl_mmap_bench"
"$IMAGES/cxl-type3-benchmark.ext2"` and
`debugfs -w -R "set_inode_field /cxl_mmap_bench mode 0100755"` to populate
the executable. Verify with `debugfs -R "stat /cxl_mmap_bench"`.

- [ ] **Step 8: Expose the controlled QEMU command name and write manifest**

Create `out/runtime-bin/qemu-system-riscv64` as a relative symlink to the
built QEMU binary. Invoke `scripts/write_manifest.py` with artifacts named:

```text
qemu
opensbi
u_boot
linux
benchmark_disk
guest_benchmark
cxlmemsim_server
```

Use the paths fixed in the design document. The manifest itself is not listed
as an input artifact.

- [ ] **Step 9: Run offline tests and commit**

Run:

```bash
bash -n scripts/build.sh
python3 -m unittest discover -s tests -v
git add scripts/build.sh tests/test_build_contract.py
git commit -m "build: compile the complete SiFive U CXL stack"
```

Expected: all offline tests pass; no `out/` file is staged.

### Task 8: Implement runtime command and evidence parsers

**Files:**
- Create: `CXLMemSim-riscv/tests/test_runtime.py`
- Create: `CXLMemSim-riscv/scripts/run.py`

- [ ] **Step 1: Write failing command and parser tests**

Import `scripts/run.py` by path and test:

```python
command = runtime.build_qemu_command(paths)
self.assertEqual(command[:3],
                 ["qemu-system-riscv64", "-M", "sifive_u"])
joined = " ".join(command)
self.assertIn("hdm_for_passthrough=on", joined)
self.assertIn("cxl-type3", joined)
self.assertNotIn("cxl-type2", joined)
self.assertIn("virtio-blk-pci,drive=bench,bus=pcie.0", joined)
```

Test `extract_guest_json()` with zero, one, and two lines beginning with
`CXL_BENCH_JSON ` followed by `{"status":"pass","verified":true}`. Test
`parse_server_counts()` against:

```text
[info] Server Statistics:
[info]   Total Reads: 123
[info]   Total Writes: 45
```

and require `(123, 45)`. Missing or zero counters must raise `ValueError`.

Test `validate_console()` with a fixture containing every expected marker,
then remove each marker in subtests and require a specific failure.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
python3 -m unittest -v tests/test_runtime.py
```

Expected: FAIL because `scripts/run.py` is missing.

- [ ] **Step 3: Implement paths, command construction, and parsers**

Define:

```python
@dataclasses.dataclass(frozen=True)
class RuntimePaths:
    qemu_dir: pathlib.Path
    bios: pathlib.Path
    uboot: pathlib.Path
    linux: pathlib.Path
    disk: pathlib.Path
    server: pathlib.Path
    topology: pathlib.Path
    manifest: pathlib.Path
    logs: pathlib.Path
    results: pathlib.Path
```

`build_qemu_command()` must return the exact topology already validated in
`compiled-results/cxl_type3_benchmark.py`, with literal argv prefix:

```python
["qemu-system-riscv64", "-M", "sifive_u",
 "-machine", "cxl=on",
 "-machine",
 "cxl-fmw.0.targets.0=cxl.1,cxl-fmw.0.size=4G,"
 "cxl-fmw.0.restrictions=0xe",
 "-smp", "5", "-m", "2G", "-display", "none",
 "-serial", "stdio", "-monitor", "none", "-no-reboot"]
```

Append OpenSBI, U-Boot, Linux loader at `0x90000000`, 256 MiB Type 3 memory,
2 MiB LSA, `pxb-cxl` bus 64 with `hdm_for_passthrough=on`, one root port,
one `cxl-type3`, and the read-only ext2 virtio disk on `pcie.0`.

Define constants for the exact host and Type 3 decoder lines, QEMU connection
marker, guest markers, and every SHM error string in the design.

Set the runtime script executable:

```bash
chmod +x scripts/run.py
```

- [ ] **Step 4: Run parser tests and verify GREEN**

Run:

```bash
python3 -m unittest -v tests/test_runtime.py
```

Expected: command, JSON, server-count, and console fixtures all pass.

- [ ] **Step 5: Commit**

Run:

```bash
git add scripts/run.py tests/test_runtime.py
git commit -m "feat: define Type 3 SHM runtime contract"
```

### Task 9: Add SHM and child-process lifecycle management

**Files:**
- Modify: `CXLMemSim-riscv/tests/test_runtime.py`
- Modify: `CXLMemSim-riscv/scripts/run.py`

- [ ] **Step 1: Write failing SHM header tests**

Pack a 64-byte header with:

```python
struct.pack(
    "<QIIIIQQQII8x",
    0x43584C53484D454D, 1, 64, 1, 1,
    0, 268435456, 4194304, 1, 128)
```

Assert `parse_shm_header()` returns magic, version, `server_ready`,
`memory_size`, and `num_slots`. Add failure tests for bad magic, version,
not-ready state, and capacity other than 256 MiB.

Mock `subprocess.Popen`, `os.kill`, and a temporary SHM path to assert cleanup
signals only the exact PIDs stored by the harness. Assert a pre-existing SHM
path causes failure before server launch.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
python3 -m unittest -v tests/test_runtime.py
```

Expected: earlier tests pass; new header and ownership tests fail.

- [ ] **Step 3: Implement CXLMemSim lifecycle**

Launch:

```python
[
    str(paths.server),
    "--comm-mode=pgas-shm",
    "--pgas-shm-name=/cxlmemsim_pgas",
    "--capacity=256",
    "--default_latency=100",
    f"--topology={paths.topology.resolve()}",
]
```

Redirect combined server output to
`out/logs/cxlmemsim-server.log`. Before launch, require
`/dev/shm/cxlmemsim_pgas` absent. Poll for up to 30 seconds, checking server
exit on every iteration, and validate the 64-byte header with
`struct.unpack("<QIIIIQQQII8x", header_bytes)`.

On shutdown, send SIGINT to the exact child PID, wait up to 10 seconds, then
SIGTERM that same PID, then SIGKILL only if another 5 seconds expires. Never
use `pkill`, `killall`, name matching, process groups not created by the
harness, or an SHM unlink while another owner may be live.

- [ ] **Step 4: Implement QEMU environment**

Start QEMU with:

```python
env["PATH"] = str(paths.qemu_dir) + os.pathsep + env["PATH"]
env["CXL_TRANSPORT_MODE"] = "shm"
env["CXL_PGAS_SHM"] = "/cxlmemsim_pgas"
env["CXL_LATENCY_INJECT"] = "0"
```

Record only these non-secret environment keys in results.

- [ ] **Step 5: Run lifecycle tests and commit**

Run:

```bash
python3 -m unittest -v tests/test_runtime.py
git add scripts/run.py tests/test_runtime.py
git commit -m "feat: manage CXLMemSim PGAS SHM lifecycle"
```

Expected: every offline lifecycle test passes.

### Task 10: Complete U-Boot, Linux, and result automation

**Files:**
- Modify: `CXLMemSim-riscv/tests/test_runtime.py`
- Modify: `CXLMemSim-riscv/scripts/run.py`

- [ ] **Step 1: Add failing state-machine and atomic-result tests**

Build a fake console stream that includes:

```text
CXL host decoder0: HPA 0000001000000000 size 0000000010000000 target 0 ctrl 00000600
41.00.0 Type 3 decoder0: HPA 0000001000000000 size 0000000010000000 target 0 ctrl 00001600
CXL Type3: SHM connected to /cxlmemsim_pgas
CXL_GUEST_INIT_START
CXL_DISK_PASS
CXL_TOPOLOGY_PASS
CXL_BENCH_JSON {"status":"pass","bytes":1048576,"write_seconds":[1,1,1],"read_seconds":[1,1,1],"write_mib_s_median":1,"read_mib_s_median":1,"random_ns_per_load":1,"verified":true}
CXL_QEMU_UBOOT_LINUX_BENCH_PASS
```

Mock the console send/wait interface and assert the state machine sends:

```text
cxl list
cxl info 41.00.0
cxl init
setenv bootargs 'earlycon=sbi console=hvc0 loglevel=4 cxl_bench_bytes=N'
bootefi 90000000:<image-size-hex> ${fdtcontroladdr}
```

Assert `atomic_write_result()` leaves the old successful JSON intact when
validation raises and uses `os.replace` only after successful validation.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
python3 -m unittest -v tests/test_runtime.py
```

Expected: lifecycle tests pass; state-machine/result tests fail.

- [ ] **Step 3: Reuse the proven console primitive**

Port the `Console` class from
`../compiled-results/cxl_sifive_u_e2e.py` into `scripts/run.py`. Keep its
PTY-based incremental output, bounded waits, exact child PID ownership, and
full console accumulation. Add a callback that appends all new console bytes
to `out/logs/qemu-console.log`.

- [ ] **Step 4: Implement the U-Boot and guest state machine**

At the first `=> ` prompt:

1. require both decoder proof lines already present;
2. run tagged `cxl list` and require `41.00.0` plus `Type 3`;
3. run tagged `cxl info 41.00.0`;
4. run tagged `cxl init` and require the identical decoder proof lines;
5. set bootargs with `cxl_bench_bytes`;
6. run `bootefi` using the built Linux image size;
7. wait for `CXL_QEMU_UBOOT_LINUX_BENCH_PASS`;
8. wait for QEMU to exit after guest poweroff, with a bounded timeout.

The complete console must contain the SHM connection marker and no string
from:

```text
Failed to open shared memory
SHM invalid magic
SHM server not ready
SHM slot busy timeout
SHM request timeout
```

- [ ] **Step 5: Validate and publish the result**

After QEMU exits:

1. stop the server gracefully;
2. parse final server counts and require both greater than zero;
3. require `/dev/shm/cxlmemsim_pgas` absent;
4. parse exactly one guest JSON object and validate positive timing arrays,
   positive medians/random latency, `status=pass`, and `verified=true`;
5. reread `build-manifest.json` and verify every runtime artifact hash;
6. create `out/results/type3-shm-result.json.tmp`;
7. include timestamp, argv, the three environment keys, all Git revisions,
   artifact hashes, SHM fields, proof booleans, guest result, server counts,
   `latency_injection=false`, and the QEMU/TCG disclaimer;
8. fsync and atomically replace `type3-shm-result.json`.

On any exception, retain both logs, remove only the temporary result, clean
up owned children, and return non-zero.

- [ ] **Step 6: Run all offline tests and commit**

Run:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/run.py scripts/write_manifest.py
git add scripts/run.py tests/test_runtime.py
git commit -m "feat: automate U-Boot Linux CXL SHM proof"
```

Expected: all offline tests pass.

### Task 11: Document the one-command workflow

**Files:**
- Create: `CXLMemSim-riscv/README.md`

- [ ] **Step 1: Write README content**

Document:

```bash
git clone --recurse-submodules \
  https://github.com/SlugLab/CXLMemSim-riscv.git
cd CXLMemSim-riscv
./run.sh
```

Include:

- the seven pinned components and why HiFive tools/meta-sifive are reference
  only in the default target;
- native Ubuntu/Debian dependency package hints;
- `--build-only`, `--run-only`, `--jobs`, and `--benchmark-bytes`;
- output paths;
- the exact required QEMU prefix;
- Type 3 HPA/capacity and U-Boot decoder controls;
- `/cxlmemsim_pgas` and `CXL_LATENCY_INJECT=0`;
- the complete proof gates;
- expected `Total Reads > 0` and `Total Writes > 0`;
- the non-hardware-performance disclaimer;
- failure recovery that never recommends blindly deleting a live SHM object.

- [ ] **Step 2: Check documentation commands and links**

Run:

```bash
test -x run.sh
test -x scripts/build.sh
test -x scripts/check-deps.sh
test -x scripts/run.py
rg -n "qemu-system-riscv64 -M sifive_u|/cxlmemsim_pgas|CXL_LATENCY_INJECT=0|Total Reads|Total Writes" README.md
```

Expected: all executable checks pass and every critical contract appears.

- [ ] **Step 3: Commit**

Run:

```bash
git add README.md
git commit -m "docs: explain one-command CXL SHM workflow"
```

### Task 12: Verify a clean full build

**Files:**
- Generate only ignored files under: `CXLMemSim-riscv/out/`

- [ ] **Step 1: Run all fast pre-build gates**

Run:

```bash
bash -n run.sh scripts/check-deps.sh scripts/build.sh
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/run.py scripts/write_manifest.py
git diff --check
git status --short
```

Expected: all tests pass; tracked state is clean; `out/` does not appear.

- [ ] **Step 2: Build every runtime component**

Run:

```bash
./run.sh --build-only --jobs "$(nproc)"
```

Expected: exit zero and creation of all seven manifest artifacts.

- [ ] **Step 3: Verify built payload contracts**

Run:

```bash
riscv64-linux-gnu-readelf -h out/images/cxl_mmap_bench
riscv64-linux-gnu-readelf -l out/images/cxl_mmap_bench
riscv64-linux-gnu-readelf -h out/images/initramfs/init
debugfs -R "stat /cxl_mmap_bench" out/images/cxl-type3-benchmark.ext2
python3 -m json.tool out/results/build-manifest.json
```

Expected: both ELF files are static RISC-V executables, ext2 contains an
executable benchmark, and the manifest parses.

- [ ] **Step 4: Check source cleanliness**

Run:

```bash
git status --short
git submodule foreach --recursive 'test -z "$(git status --porcelain)"'
```

Expected: the superproject and all submodules are clean.

### Task 13: Run the Type 3 CXLMemSim SHM end-to-end proof

**Files:**
- Generate: `CXLMemSim-riscv/out/logs/qemu-console.log`
- Generate: `CXLMemSim-riscv/out/logs/cxlmemsim-server.log`
- Generate: `CXLMemSim-riscv/out/results/type3-shm-result.json`

- [ ] **Step 1: Confirm no unrelated SHM owner exists**

Run:

```bash
test ! -e /dev/shm/cxlmemsim_pgas
pgrep -af cxlmemsim_server || true
```

Expected: the SHM path is absent. If a server is live, inspect it and stop;
do not terminate or unlink it merely to make the test pass.

- [ ] **Step 2: Execute only the built runtime**

Run:

```bash
./run.sh --run-only --benchmark-bytes 1048576
```

Expected: exit zero after guest poweroff and server cleanup.

- [ ] **Step 3: Validate evidence independently**

Run:

```bash
rg -n "CXL Type3: SHM connected to /cxlmemsim_pgas|CXL_QEMU_UBOOT_LINUX_BENCH_PASS" \
  out/logs/qemu-console.log
rg -n "Server Statistics:|Total Reads:|Total Writes:" \
  out/logs/cxlmemsim-server.log
python3 -m json.tool out/results/type3-shm-result.json
test ! -e /dev/shm/cxlmemsim_pgas
```

Use a short Python assertion to require:

```python
result["guest"]["status"] == "pass"
result["guest"]["verified"] is True
result["server"]["total_reads"] > 0
result["server"]["total_writes"] > 0
result["latency_injection"] is False
result["topology"]["machine"] == "sifive_u"
```

Expected: every condition is true. If not, retain both logs, apply
`superpowers:systematic-debugging`, fix the root cause under TDD, rebuild only
the affected component, and rerun this task.

### Task 14: Final review, create GitHub repository, and publish

**Files:**
- Inspect: all tracked files and gitlinks
- Remote create: `SlugLab/CXLMemSim-riscv`
- Remote push: `main`

- [ ] **Step 1: Run verification-before-completion**

Invoke `superpowers:verification-before-completion`, then freshly run:

```bash
python3 -m unittest discover -s tests -v
git diff --check
git status --short
git submodule status --recursive
test -s out/results/type3-shm-result.json
```

Expected: tests pass, tracked state is clean, gitlinks are exact, and E2E
evidence exists.

- [ ] **Step 2: Review the publish set**

Run:

```bash
git ls-tree -r --name-only HEAD
git ls-tree HEAD components
git log --oneline --decorate --max-count=20
```

Expected: no `out/`, binary, log, token, local path credential, or component
source copy is tracked. Confirm the approved spec and this plan are present.

- [ ] **Step 3: Create the public repository only if absent**

Run:

```bash
gh repo view SlugLab/CXLMemSim-riscv --json nameWithOwner,visibility
```

If it is absent, run:

```bash
gh repo create SlugLab/CXLMemSim-riscv \
  --public --source=. --remote=origin
```

If it exists, verify it is the intended repository and inspect its current
default branch before adding or changing a remote. Do not overwrite an
unrelated repository with the same name.

- [ ] **Step 4: Push without force**

Run:

```bash
git push -u origin main
```

Expected: normal push succeeds; never use `--force`.

- [ ] **Step 5: Verify remote state independently**

Run:

```bash
gh repo view SlugLab/CXLMemSim-riscv \
  --json nameWithOwner,url,visibility,defaultBranchRef
git ls-remote --heads origin refs/heads/main
```

Expected:

```text
nameWithOwner = SlugLab/CXLMemSim-riscv
visibility = PUBLIC
defaultBranchRef.name = main
remote main commit = local main commit
```

Clone into a temporary directory with `--recurse-submodules`, verify all seven
gitlinks, and run the offline test suite there. Remove only that exact
temporary directory after verification.

- [ ] **Step 6: Report the final proof boundary**

Provide:

- the GitHub repository URL;
- the verified remote main commit;
- the seven pinned component commits;
- the exact successful `./run.sh` command;
- guest `verified=true`;
- non-zero server read/write counts from the result JSON;
- paths to local logs and JSON;
- the statement that active latency injection is disabled and the numbers
  characterize QEMU/TCG plus synchronous software transport, not real CXL
  hardware.
