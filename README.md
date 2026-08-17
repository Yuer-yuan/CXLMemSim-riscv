# CXLMemSim-riscv

`CXLMemSim-riscv` is a reproducible integration platform for the
synthetic SiFive U CXL stack. It builds QEMU, U-Boot, OpenSBI, Linux,
freestanding RISC-V guest programs, an external ext2 benchmark image, and the
CXLMemSim server, then proves a Type 3 endpoint issuing reads and writes
through PGAS shared memory.

LegoFS is pinned as `components/legofs`. The platform is the top-level owner of
the complete experiment, matching the original superproject layout and avoiding
a reverse `LegoFS -> platform -> LegoFS` dependency.

## Clone, build, and run

On a native Linux host:

```bash
git clone https://github.com/Yuer-yuan/CXLMemSim-riscv.git
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
  build-essential cmake ninja-build meson pkg-config \
  python3 python3-venv python3-packaging python3-dev \
  gcc-riscv64-linux-gnu g++-riscv64-linux-gnu binutils-riscv64-linux-gnu \
  device-tree-compiler e2fsprogs librdmacm-dev libibverbs-dev \
  libpmem-dev libslirp-dev \
  libglib2.0-dev libpixman-1-dev libspdlog-dev libbpf-dev libelf-dev \
  zlib1g-dev libzstd-dev flex bison libssl-dev bc swig cpio
```

The pinned QEMU requires Meson 1.5 or newer. When the distribution package is
older, the top-level CXLMemSim-riscv checkout supplies a uv environment at
`.cxl-bi-tools/uv`; `run-legofs-type3.sh` discovers it automatically. Generic
libraries and cross tools still come from the distribution rather than a
repository-local sysroot. `scripts/check-deps.sh` only reports missing commands;
it never invokes `sudo` or a package manager.

The Type-3 build explicitly enables libpmem and libslirp. They are runtime
requirements for `pmem=on` file-backed memory and the guest TCP forwarding
used by the two-node proof, so configuration fails immediately if either
development package is absent.

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
- `components/legofs`: BadFS lifecycle, server, client, and IO500 integration;
- `components/opensbi`: OpenSBI v1.5.1;
- `components/hifive-premier-tools`: pinned board-tool reference;
- `components/meta-sifive`: pinned Yocto-layer reference.

HiFive Premier tools and `meta-sifive` are reference components and are not
built by the default SiFive U QEMU target.

The Legofs Type-3 and IO500 builds resolve LegoFS only at
`components/legofs`. The build manifest records its path, commit, tree, and
clean state without blocking a fast edit/build/run cycle.

## Runtime topology

The recorded QEMU argv begins exactly:

```text
qemu-system-riscv64 -M sifive_u
```

It adds a synthetic `pxb-cxl` bridge with
`hdm_for_passthrough=on`, one CXL root port, and one 256 MiB Type 3 endpoint.
U-Boot programs HPA `0x1000000000` with host decoder control `0x600` and
endpoint decoder control `0x1600`.

The Legofs runners now advertise the window as `DEVMEM | PMEM | BI`
(`cxl-fmw.0.restrictions=0x29`), put both the root port and Type 3 endpoint in
256-byte-flit mode, expose the endpoint HDM-DB BI Decoder, and publish a 64-byte
Zicbom CMO node in ACPI RHCT. The Type 3 coherence-v2 path remains disabled
after reset until Linux commits the standard BI Decoder control. Inconsistent
`coherence-v2`, `hdm-db`, or flit-mode configuration fails device realization.

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

Use a different absolute output root for each baseline/candidate build so the
two binaries and results can coexist during interleaved comparison:

```bash
LEGOFS_TYPE3_OUT=/approved/scratch/cxl-bi/B-P0 \
  ./run-legofs-type3.sh --build-only
LEGOFS_TYPE3_OUT=/approved/scratch/cxl-bi/B-P0 \
  ./run-legofs-type3.sh --run-only --bytes 65536 --timeout 1200
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
node1 direct unmap < snoop send < model ACK < dirty completion < store_direct_success
```

Read-exclusive is a deterministic proof policy used only by this two-node
harness; it is not enabled implicitly by the IO500 runner. The TCP MESI-v2
messages are a private functional adapter, not CXL.mem wire packets. In
particular, the adapter carries dirty 64-byte data in its model `SNOOP_ACK`,
whereas real HDM-DB has separate CXL.mem dirty-data and BIRsp ordering. The
proof therefore validates correlated state/data/order, not flit encoding,
credits, link timing, or protocol compliance.

The result is written to:

```text
out/legofs-type3/runs/<run-id>/result.json
```

The three-endpoint 2-client/1-server calibration uses the same build output:

```bash
./run-legofs-type3-2c1s.sh --build-only --jobs 8
./run-legofs-type3-2c1s.sh --run-only --bytes 65536 --timeout 1200
```

Its results are kept under `out/legofs-type3/runs-2c1s/<run-id>`. Client
phase coordination uses a host-side TCP barrier through the QEMU user-network
gateway; it never creates marker files in Legofs and therefore does not enter
the measured namespace, lifecycle, persistence, or coherence paths. The five
cases are disjoint write, same-range write, writer/reader handoff, shared read,
and client crash. A workload failure is a valid rejected baseline, and the
runner preserves its result and cleans up only the processes it owns.

The result also records both QEMU argv arrays, overlapping process lifetimes,
runtime artifact paths, the two CXL SSD backing files, all address correlations,
Legofs direct-path and fallback counters, and final coherence error counters.
`status: "passed"` requires zero timeouts, protocol errors, delivery failures,
server-copy failures, fallback I/O, pending operations, quarantined extents,
and active leases.

The Legofs Type-3 path deliberately does not bind `--run-only` to artifact
content hashes or a clean/source-HEAD snapshot. It checks that the current
runtime artifacts exist, are non-empty, and are executable where required;
the build manifest records paths and sizes. This keeps local component and
LegoFS iteration incremental. Independent output directories, rather
than hash gates, separate baseline and candidate experiments.

This is functional QEMU/TCG and CXLMemSim model evidence. The CXL SSDs are
file-backed simulated persistent-memory devices; this does not claim a
physical CXL link, CPU-cache or CXL.cache coherence, media durability across a
host crash, or hardware performance.

The persistence path is fail-closed: devdax `fsync` performs the advertised
64-byte Zicbom range clean followed by `pmem_wmb`; QEMU hands the pending
provider to CXLMemSim `Fence`; and a successful coherence-v2 Fence now requires
the selected backing backend to flush successfully. For `ssd-stream` that ends
in backing-file `fsync`. This proves the file-backed model boundary only; it is
not evidence for physical NAND PLP or host-power-loss recovery.

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
