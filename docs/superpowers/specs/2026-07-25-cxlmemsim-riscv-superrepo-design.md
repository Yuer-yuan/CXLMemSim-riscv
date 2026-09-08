# CXLMemSim-riscv Superproject Design

## Goal

Create a public GitHub repository named `SlugLab/CXLMemSim-riscv` that pins
the validated SiFive U CXL software stack and turns it into one reproducible
native-Linux workflow:

```text
git clone --recurse-submodules https://github.com/SlugLab/CXLMemSim-riscv
cd CXLMemSim-riscv
./run.sh
```

The default workflow builds QEMU, U-Boot, OpenSBI, Linux, the freestanding
RISC-V guest payload, the external ext2 benchmark image, and the CXLMemSim
server. It then boots the exact machine selection
`qemu-system-riscv64 -M sifive_u`, connects the Type 3 device to
`/cxlmemsim_pgas`, and fails unless the data path is proved end to end.

This repository is a reproducible integration superproject. It does not
rewrite or duplicate the component histories.

## Scope

The first release includes:

- the existing synthetic SiFive U PCIe/CXL host bridge;
- the existing CXL-capable U-Boot, including Type 2 and Type 3 discovery and
  HDM decoder programming;
- the matching Linux CXL branch;
- CXLMemSim PGAS shared-memory transport;
- a Type 3-only default end-to-end smoke benchmark;
- one-command source build and run automation;
- machine-readable proof artifacts;
- HiFive Premier P550 tools and `meta-sifive` as pinned reference components.

The default end-to-end run deliberately isolates one Type 3 endpoint. The
QEMU, U-Boot, and Linux component revisions retain Type 2 support, but a
mixed Type 2 + Type 3 benchmark and a Type 2 benchmark are not default
targets in this first release.

Active latency injection, latency sweeps, Yocto builds, P550 image builds,
container images, package installation through `sudo`, and publication of
large compiled binaries are outside the first release.

## Repository Model

The new repository is a Git superproject with seven submodules. Each
submodule is checked out at an exact commit:

| Path | Repository | Commit |
| --- | --- | --- |
| `components/qemu` | `Zettai-US/qemu-cxl-type2` | `81cd7ad9a5e14470427c8ebafeccff4f52e555b4` |
| `components/u-boot` | `SlugLab/u-boot` | `d5948c7033dc8b2352099b4fd906cb8cc9cc0bdd` |
| `components/linux` | `vickiegpt/linux-cxl-type2` | `108e1b383db789b7f8292ff62a73efa441820dca` |
| `components/cxlmemsim` | `SlugLab/CXLMemSim` | `d37e3ab9b44cc1ebdf9eb5d64c9390d309e8e529` |
| `components/opensbi` | `riscv-software-src/opensbi` | `43cace6c3671e5172d0df0a8963e552bb04b7b20` (`v1.5.1`) |
| `components/hifive-premier-tools` | `SlugLab/hifive-premier-p550-tools` | `a0d52ef83c9f0ac120a10bd59b77c6c88466e167` |
| `components/meta-sifive` | `SlugLab/meta-sifive` | `c26f3490b5927cad96e71c0e4b2e92b7e5af34c0` |

The CXLMemSim submodule pins the clean remote `main` commit. It must not
capture or publish the unrelated dirty state in the existing local
`${CXLMEMSIM_ROOT}` checkout.

Submodule updates are intentional reviewable changes to the superproject.
The automation never follows moving branch heads during a normal build.

## Repository Layout

```text
CXLMemSim-riscv/
├── .gitmodules
├── .gitignore
├── README.md
├── run.sh
├── components/
│   ├── qemu/
│   ├── u-boot/
│   ├── linux/
│   ├── cxlmemsim/
│   ├── opensbi/
│   ├── hifive-premier-tools/
│   └── meta-sifive/
├── configs/
│   └── linux-cxl.config
├── guest/
│   ├── init.c
│   └── cxl_mmap_bench.c
├── scripts/
│   ├── check-deps.sh
│   ├── build.sh
│   └── run.py
├── docs/
│   └── superpowers/
│       └── specs/
└── out/                         # generated and gitignored
    ├── build/
    ├── images/
    ├── logs/
    └── results/
```

`run.sh` is the stable user entry point. Shell code handles dependency checks
and compilation; Python owns concurrent process lifecycle, console
automation, timeout handling, validation, and JSON results.

## Command-Line Contract

The supported entry points are:

```text
./run.sh
./run.sh --build-only
./run.sh --run-only
./run.sh --jobs N
./run.sh --benchmark-bytes N
```

The no-argument command performs a complete build followed by the Type 3 SHM
smoke run.

- Before checking dependencies, `run.sh` initializes any missing submodule at
  the gitlink recorded by the superproject. If an already-populated submodule
  contains local changes or is checked out at a different commit, it fails
  closed instead of resetting or overwriting that checkout.
- `--build-only` compiles and packages all default artifacts without starting
  CXLMemSim or QEMU.
- `--run-only` verifies that all expected artifacts exist and then runs the
  end-to-end test without rebuilding.
- `--jobs N` controls parallel compilation and must be a positive integer.
- `--benchmark-bytes N` changes the guest benchmark range and must be positive,
  8-byte aligned, and no larger than the 256 MiB Type 3 capacity.

Combining `--build-only` with `--run-only`, using unknown arguments, or
supplying invalid numeric values fails before any build or process is started.

The default benchmark size is 1 MiB (`1048576` bytes). The default run uses
three sequential write passes, three checksum read passes, 100,000
deterministic random loads, and a final bit-exact verification pass.

## Dependency Policy

`scripts/check-deps.sh` reports all missing host commands in one pass and
prints an Ubuntu/Debian package hint. It never invokes `sudo` or modifies the
host.

The native-Linux build expects, at minimum:

- Git and Git submodule support;
- GCC, G++, Make, Ninja, CMake, pkg-config, Python 3, and Meson/QEMU build
  prerequisites;
- the `riscv64-linux-gnu-` cross toolchain;
- device-tree compiler;
- `mke2fs` and `debugfs`;
- common QEMU development libraries required by the pinned source revision.

Missing dependencies stop the workflow before compilation. Component build
failures retain their logs and return the failing command's non-zero status.

## Build Design

All generated state lives below `out/`. Source submodules remain unmodified;
out-of-tree build directories are used wherever the component supports them.

### QEMU

Build only `riscv64-softmmu` from `components/qemu` into
`out/build/qemu`. Documentation is disabled to reduce unrelated
dependencies. The required artifact is:

```text
out/build/qemu/qemu-system-riscv64
```

### U-Boot

Configure `components/u-boot` with:

```text
sifive_unleashed_qemu_cxl_defconfig
```

Build out of tree with `CROSS_COMPILE=riscv64-linux-gnu-` and
`NO_PYTHON=1`. The required artifact is:

```text
out/build/u-boot/u-boot.bin
```

### OpenSBI

Build OpenSBI v1.5.1 with `PLATFORM=generic` and the same cross compiler. The
required firmware is:

```text
out/build/opensbi/platform/generic/firmware/fw_dynamic.bin
```

### Guest Programs

Build `guest/init.c` and `guest/cxl_mmap_bench.c` as libc-free static
`rv64imafdc/lp64d` executables. This ISA choice is required because SiFive
U54 does not implement RVV, while the installed static RISC-V glibc startup
has previously executed RVV instructions before `main`.

Both programs use direct Linux syscalls and are compiled with:

```text
-static -nostdlib -fno-builtin -fno-stack-protector
-fno-pie -no-pie -march=rv64imafdc -mabi=lp64d
```

The init program mounts `proc`, `sysfs`, and `devtmpfs`, waits for
`/dev/vda`, mounts the external ext2 image read-only at `/mnt`, validates the
Linux CXL topology, executes `/mnt/cxl_mmap_bench`, propagates a clear pass or
failure marker, and then powers off. It never depends on BusyBox or a guest
libc.

### Linux

Build the pinned Linux branch out of tree for `ARCH=riscv`. Start from the
branch's RISC-V default configuration, apply `configs/linux-cxl.config`, and
run `olddefconfig`.

The resulting configuration must provide the existing EFI boot path and
enable the required built-in functionality:

- PCI and PCIe port support;
- EFI and EFI stub support;
- CXL bus, PCI, ACPI, port, region, and Type 2 accelerator support;
- virtio PCI and virtio block;
- devtmpfs;
- `/dev/mem`;
- ext4 with ext2 compatibility;
- an initramfs containing the freestanding `/init`.

After configuration, the build script checks the final `.config` rather than
assuming that the fragment was accepted. Any required option that resolves to
disabled or a loadable module fails the build because no module filesystem is
provided.

The required kernel artifact is:

```text
out/build/linux/arch/riscv/boot/Image
```

### External ext2 Image

Create `out/images/cxl-type3-benchmark.ext2` without loop mounting. The image
contains the freestanding benchmark as `/cxl_mmap_bench` with executable
permissions. `mke2fs` creates the filesystem and `debugfs` populates it.
The run attaches it read-only using `virtio-blk-pci` on `pcie.0`.

### CXLMemSim

Configure a Release CMake build from `components/cxlmemsim` into
`out/build/cxlmemsim`. The required server artifact is:

```text
out/build/cxlmemsim/cxlmemsim_server
```

The build script records the exact superproject and submodule revisions plus
SHA-256 hashes of all runtime artifacts in
`out/results/build-manifest.json`.

## Runtime Topology

The QEMU command array must begin with these exact two arguments:

```text
qemu-system-riscv64 -M sifive_u
```

The harness may use the absolute path to the built binary internally, but it
must expose that binary as `qemu-system-riscv64` through a controlled `PATH`
entry so the recorded command preserves the required spelling and first
tokens.

The remaining topology is:

- CXL enabled on `sifive_u`;
- one 4 GiB CFMWS targeting `cxl.1`, restrictions `0xe`;
- one `pxb-cxl` on `pcie.0`, bus number 64;
- `hdm_for_passthrough=on`;
- one CXL root port;
- one 256 MiB volatile Type 3 endpoint with a 2 MiB LSA;
- the external ext2 image on `virtio-blk-pci,bus=pcie.0`;
- OpenSBI supplied with `-bios`;
- U-Boot supplied with `-kernel`;
- Linux `Image` loaded at `0x90000000`.

U-Boot is expected to program the 256 MiB endpoint at HPA
`0x1000000000`. The expected committed controls are:

```text
CXL host decoder0 ctrl 0x600
Type 3 endpoint decoder0 ctrl 0x1600
```

Linux is started through U-Boot's EFI boot path using the loaded image and
the U-Boot control FDT.

## CXLMemSim SHM Lifecycle

Before QEMU starts, `scripts/run.py` launches the pinned server with:

```text
--comm-mode=pgas-shm
--pgas-shm-name=/cxlmemsim_pgas
--capacity=256
--default_latency=100
--topology=components/cxlmemsim/qemu_integration/topology_simple.txt
```

The QEMU child receives:

```text
CXL_TRANSPORT_MODE=shm
CXL_PGAS_SHM=/cxlmemsim_pgas
CXL_LATENCY_INJECT=0
```

This phase validates SHM transport and server accounting only.
`default_latency=100` remains server configuration data, while
`CXL_LATENCY_INJECT=0` explicitly prevents active latency injection in QEMU.

The harness refuses to delete or reuse an SHM object owned by an unrelated
live server. After launching its own server, it waits for:

1. a live server process;
2. `/dev/shm/cxlmemsim_pgas`;
3. header magic `0x43584c53484d454d`;
4. protocol version `1`;
5. `server_ready=1`;
6. advertised capacity of 256 MiB.

The server is always stopped by the harness after QEMU exits. It first
requests graceful termination so final counters are printed. If that times
out, it terminates only the exact child process it created. Cleanup must not
target other CXLMemSim or QEMU processes.

## End-to-End Data Flow

1. CXLMemSim creates and initializes `/cxlmemsim_pgas`.
2. QEMU opens and validates that object for the Type 3 backend.
3. OpenSBI transfers control to U-Boot.
4. U-Boot discovers `41.00.0`, programs the host and endpoint HDM decoders,
   and exposes the resulting FDT state.
5. The harness runs `cxl list`, `cxl info 41.00.0`, and a second `cxl init`
   at the U-Boot prompt.
6. U-Boot boots the Linux EFI image.
7. The freestanding init validates the Linux CXL region and mounts the
   external ext2 image.
8. The benchmark maps `/dev/mem` at `0x1000000000`, issues deterministic
   reads and writes, and verifies the final contents bit for bit.
9. QEMU translates the Type 3 accesses into PGAS SHM requests.
10. CXLMemSim processes the requests and emits non-zero read and write
    counters.
11. The harness correlates all proof markers and atomically publishes a
    result only if every gate passes.

## Proof Gates

The default run passes only if all of these conditions hold:

1. All expected build artifacts exist and match the build manifest.
2. The SHM header is valid and ready before QEMU starts.
3. The QEMU command begins exactly
   `qemu-system-riscv64 -M sifive_u`.
4. QEMU reports
   `CXL Type3: SHM connected to /cxlmemsim_pgas`.
5. U-Boot lists Type 3 endpoint `41.00.0`.
6. U-Boot programs the host and endpoint decoder at HPA `0x1000000000`,
   size `0x10000000`, target `0`, with controls `0x600` and `0x1600`.
7. A second `cxl init` reproduces the same decoder state.
8. Linux binds `0000:41:00.0` to `cxl_pci`.
9. Linux exposes `decoder0.0`, `region0`, and `/proc/iomem` at the expected
   HPA and size.
10. The external ext2 image appears as `/dev/vda` and mounts read-only.
11. The benchmark reports `status=pass` and `verified=true`.
12. CXLMemSim's final `Server Statistics` reports `Total Reads > 0` and
    `Total Writes > 0`.
13. No QEMU SHM transport error appears.
14. The harness-created SHM object is absent after cleanup.

The run fails if a guest benchmark succeeds through a fallback backend but
the QEMU connection marker or server counters are missing.

## Results and Logs

Each run retains:

```text
out/logs/build.log
out/logs/qemu-console.log
out/logs/cxlmemsim-server.log
out/results/build-manifest.json
out/results/type3-shm-result.json
```

The result JSON includes:

- UTC timestamp;
- exact command arguments and relevant environment settings;
- superproject and submodule commits;
- SHA-256 hashes of QEMU, OpenSBI, U-Boot, Linux, ext2, guest benchmark, and
  CXLMemSim server artifacts;
- SHM magic, version, readiness, name, and capacity;
- firmware and Linux topology proof status;
- all guest timing samples and data-verification status;
- parsed CXLMemSim read and write counts;
- `latency_injection=false`;
- an explicit statement that timings describe QEMU/TCG and synchronous
  software transport, not physical CXL hardware performance.

The final result JSON is written through a temporary file and renamed only
after every proof gate passes. Diagnostic logs remain available after a
failure.

## Failure Handling

- A missing dependency fails before compilation and includes package hints.
- A submodule at the wrong revision fails closed; normal runs do not silently
  fetch a different branch head.
- A build failure identifies the component and preserves the log.
- A stale or already-owned SHM object is not unlinked automatically.
- Early CXLMemSim exit includes the server log tail.
- QEMU, U-Boot, Linux, guest, and server phases each have explicit timeouts.
- QEMU SHM connection errors fail the run even if the guest reaches Linux.
- Missing, malformed, or zero server counters fail the run.
- Keyboard interrupt and failure paths terminate only child processes created
  by the harness.
- A failed run does not overwrite the last successful result JSON.

## Test Strategy

Implementation follows test-driven development for the integration-owned
code.

### Static and Unit Tests

- shell syntax checks for `run.sh`, `check-deps.sh`, and `build.sh`;
- argument parser tests for valid and conflicting options;
- QEMU command construction test that asserts the exact first tokens and
  required CXL/virtio topology;
- SHM header parser tests for valid, stale, malformed, and not-ready headers;
- console parser tests for U-Boot, Linux, QEMU, guest, and CXLMemSim success
  and failure markers;
- cleanup tests proving that only harness-owned children are targeted;
- host-native tests of deterministic benchmark pattern generation where
  practical;
- cross-compiled ELF inspection confirming RISC-V, static linkage, and no RVV
  ISA requirement.

### Build Test

`./run.sh --build-only` must succeed from a clean recursive clone and produce
the manifest plus all required artifacts. Re-running it without source
changes must be safe.

### End-to-End Test

`./run.sh` must complete the 1 MiB Type 3 SHM smoke and satisfy every proof
gate. The delivered evidence is the console log, server log, and
machine-readable result JSON.

The older 128 MiB QEMU-backend measurement remains a separate historical
baseline. It is not overwritten or presented as the SHM smoke result.

## Publication

Create `SlugLab/CXLMemSim-riscv` as a new public repository with `main` as its
default branch. The initial publication includes integration source,
documentation, tests, and submodule gitlinks, but excludes `out/`, build
trees, logs, SHM objects, and compiled binaries.

Before pushing:

1. verify every gitlink matches the pinned table;
2. verify the superproject contains no component source copies or generated
   build artifacts;
3. run static and unit tests;
4. run the build-only gate;
5. run the default Type 3 SHM end-to-end gate;
6. review the exact staged file list.

No force-push is used. Existing component repositories and their default
branches are not modified by creating or publishing this superproject.

## Deferred Work

Future, separately designed work may add:

- `CXL_LATENCY_INJECT=1` and controlled latency sweeps;
- full 128 MiB SHM benchmarks;
- mixed Type 2 + Type 3 end-to-end runs;
- a Type 2 accelerator benchmark;
- HiFive Premier P550 or Yocto image build targets;
- CI runners capable of executing the complete RISC-V QEMU workflow;
- optional prebuilt release artifacts.
