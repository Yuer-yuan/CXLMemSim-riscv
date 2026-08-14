# CXLMemSim-riscv

`CXLMemSim-riscv` is a reproducible integration superproject for the
synthetic SiFive U CXL stack. It builds QEMU, U-Boot, OpenSBI, Linux,
freestanding RISC-V guest programs, an external ext2 benchmark image, and the
CXLMemSim server, then proves a Type 3 endpoint issuing reads and writes
through PGAS shared memory.

## Clone, build, and run

On a native Linux host:

```bash
git clone https://github.com/SlugLab/CXLMemSim-riscv.git
cd CXLMemSim-riscv
./run.sh
```

`run.sh` initializes only the required top-level submodules at the recorded
gitlinks. Avoid `git clone --recurse-submodules`: CXLMemSim contains optional
nested workload and firmware repositories that this workflow does not need.
The script refuses to reset a populated submodule with a different revision
or local changes.

Useful modes:

```bash
./run.sh --build-only
./run.sh --run-only
./run.sh --jobs 16
./run.sh --benchmark-bytes 1048576
```

The benchmark byte count must be positive, 8-byte aligned, and no larger than
the 256 MiB Type 3 capacity.

## Host dependencies

The scripts require a native Linux build environment, the
`riscv64-linux-gnu-` cross toolchain, QEMU build dependencies, device-tree
compiler, ext2 tools, and Python. On Ubuntu or Debian, the starting package
set is:

```bash
sudo apt install \
  build-essential cmake ninja-build meson pkg-config python3 \
  gcc-riscv64-linux-gnu binutils-riscv64-linux-gnu \
  device-tree-compiler e2fsprogs
```

QEMU may require additional distribution development packages reported by
its pinned `configure` script. `scripts/check-deps.sh` only reports missing
commands; it never invokes `sudo` or a package manager.

The pinned U-Boot contains legacy pylibfdt typemaps. The build creates an
output-tree-only compatibility copy for current SWIG/Python releases; the
pinned U-Boot submodule remains unmodified.

## Pinned components

The superproject records exact gitlinks for:

- `components/qemu`: SiFive U synthetic PCIe/CXL host bridge and CXL Type 2
  plus Type 3 models;
- `components/u-boot`: CXL discovery and HDM decoder programming;
- `components/linux`: matching RISC-V CXL firmware handoff support;
- `components/cxlmemsim`: PGAS SHM server;
- `components/legofs`: Badfs lifecycle-direct client, server, and benchmark;
- `components/opensbi`: OpenSBI v1.5.1;
- `components/hifive-premier-tools`: pinned board-tool reference;
- `components/meta-sifive`: pinned Yocto-layer reference.

HiFive Premier tools and `meta-sifive` are reference components and are not
built by the default SiFive U QEMU target.

## Runtime topology

The recorded QEMU argv begins exactly:

```text
qemu-system-riscv64 -M sifive_u
```

It adds a synthetic `pxb-cxl` bridge with
`hdm_for_passthrough=on`, one CXL root port, and one 256 MiB Type 3 endpoint.
U-Boot programs HPA `0x1000000000` with host decoder control `0x600` and
endpoint decoder control `0x1600`.

## Two-node Legofs Type 3 back-invalidation proof

The second workflow builds the complete stack and runs the Zettai-US Legofs
Badfs lifecycle-direct workload across two concurrent RISC-V guests:

```bash
./run-legofs-type3.sh --bytes 65536 --timeout 1200
```

Both recorded commands begin exactly with:

```text
qemu-system-riscv64 -M sifive_u
```

Each guest has one 256 MiB Type 3 endpoint attached through the synthetic
SiFive U PCIe/CXL host bridge. Each endpoint uses its own file-backed
`persistent-memdev`; CXLMemSim uses a separate file-backed `ssd-stream`
backend. U-Boot enumerates `41.00.0`, reports it as Type 3, and programs the
host and endpoint HDM decoders before Linux boots. Linux exposes the Type 3
capacity as `/dev/dax0.0`, which Legofs maps for strict lifecycle-direct
writes and reads. The external ext2 image is only the read-only delivery
disk for the static RISC-V binaries.

The node1 endpoint retains dirty modified lines. The node0 endpoint enables
the opt-in `coherence-v2-read-exclusive` QEMU policy so its server-side
checksum reads issue GETM requests. Those requests exercise QEMU's Type 3
back-invalidation handler against node1. A run passes only if the benchmark
trace contains the same operation ID and mapping range as a node1-directed
`SNP_DATA_INV`, a model ACK carrying the complete dirty 64-byte line, and a
dirty completion. Host monotonic timestamps must additionally prove:

```text
node1 direct unmap < snoop send < model ACK < dirty completion < store success
```

The result is written to:

```text
out/legofs-type3/runs/<run-id>/result.json
```

The result also records both QEMU argv arrays, overlapping process lifetimes,
artifact hashes, the two CXL SSD backing files, all address correlations,
Legofs direct-path and fallback counters, and final coherence error counters.
`status: "passed"` requires zero timeouts, protocol errors, delivery failures,
server-copy failures, fallback I/O, pending operations, quarantined extents,
and active leases.

This is functional QEMU/TCG and CXLMemSim model evidence. The CXL SSDs are
file-backed simulated persistent-memory devices; this does not claim a
physical CXL link, CPU-cache or CXL.cache coherence, media durability across a
host crash, or hardware performance.

The guest benchmark is a libc-free static `rv64imafdc` executable delivered
through a read-only external ext2 image on
`virtio-blk-pci,bus=pcie.0`. The built-in freestanding PID 1 mounts the
required filesystems, validates Linux CXL sysfs and `/proc/iomem`, executes
the benchmark, and powers off.

## CXLMemSim SHM transport

The harness creates a CXLMemSim server for:

```text
/cxlmemsim_pgas
```

QEMU receives:

```text
CXL_TRANSPORT_MODE=shm
CXL_PGAS_SHM=/cxlmemsim_pgas
CXL_LATENCY_INJECT=0
```

Active latency injection is intentionally disabled in this first workflow.
The default 1 MiB smoke separates transport connectivity and request
accounting from later latency experiments.

The harness refuses to reuse or unlink a pre-existing SHM object. If one is
present, identify its owning server before retrying; do not blindly delete
`/dev/shm/cxlmemsim_pgas`.

## Pass criteria

A run passes only when all of these are true:

- the SHM protocol header is version 1, ready, and advertises 256 MiB;
- QEMU reports `CXL Type3: SHM connected to /cxlmemsim_pgas`;
- U-Boot discovers `41.00.0` and reproduces both decoder values on a second
  `cxl init`;
- Linux binds the endpoint to `cxl_pci` and exposes the expected decoder,
  region, and CXL window;
- the external ext2 disk mounts read-only;
- the guest reports `status=pass` and `verified=true`;
- CXLMemSim's final `Server Statistics` reports `Total Reads > 0` and
  `Total Writes > 0`;
- the harness-owned SHM object is removed after server shutdown;
- no QEMU SHM transport error is present.

## Outputs

Generated state is ignored under `out/`:

```text
out/logs/build.log
out/logs/qemu-console.log
out/logs/cxlmemsim-server.log
out/results/build-manifest.json
out/results/type3-shm-result.json
```

The successful result contains exact source revisions, artifact SHA-256
hashes, QEMU argv, the three transport environment values, SHM metadata,
firmware/Linux proof states, guest timings and verification, and final server
request counts. A failed run retains logs but does not replace the last
successful result JSON.

These timings characterize QEMU/TCG, the guest software path, and synchronous
CXLMemSim transport. They are not measurements of real CXL hardware
performance.
