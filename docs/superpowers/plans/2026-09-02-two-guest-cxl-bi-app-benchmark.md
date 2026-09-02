# Two-Guest CXL BI Application Benchmark Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and execute a reproducible two-QEMU-guest test that proves application-visible DAX coherence through CXLMemSim's BI/MESI-v2 path and reports emulator and analytical performance separately.

**Architecture:** A static RISC-V PID 1 maps each guest's local CXL Type-3 DAX device and performs a dirty-owner litmus, bidirectional ping-pong, and verified streaming hand-off. A Python runner starts one strict MESI-v2 CXLMemSim server and two QEMU SiFive U guests with distinct backing files, automates U-Boot, parses guest records, correlates BI trace events by DPA, and emits a machine-readable verdict.

**Tech Stack:** C11/RISC-V Linux syscalls and DAX mmap, Python 3 standard library, QEMU `sifive_u`, Linux CXL/DAX, CXLMemSim coherence-v2 JSONL, Bash, unittest/pytest.

**Spec:** `docs/superpowers/specs/2026-09-02-two-guest-cxl-bi-app-benchmark-design.md`

## Global Constraints

- Preserve every pre-existing modified or untracked user file; add isolated files and generated output only under `out/cxl-bi-app/`.
- Use two different 256 MiB host backing files while presenting the same logical DPA space to CXLMemSim.
- Enable `coherence-v2`, HDM-DB, BI FMW restrictions, and guest HDM-DB commit for the positive run.
- Do not call `msync`, `fsync`, cache flush instructions, or a host side-channel during coherence phases.
- Require application payload correctness and address-correlated dirty BI trace evidence as separate gates.
- Label observed timing `qemu_tcg_tcp_wallclock` and calculated timing `analytical_not_measured`.
- Never characterize QEMU/TCG/TCP timing as physical CXL hardware performance.
- Retain complete per-run evidence and clean up every spawned process on success or failure.

---

## File Map

- `guest/cxl_bi_app_init.c`: PID 1, DAX discovery/mapping, synchronization protocol, correctness checks, guest timing, and structured console records.
- `scripts/cxl_bi_app.py`: artifact discovery, command construction, U-Boot control, process lifecycle, record parsing, trace analysis, analytical model, and result JSON.
- `scripts/build_cxl_bi_app.sh`: static init build and isolated Linux output/configuration.
- `run-cxl-bi-app.sh`: repository-root entry point that selects build/run modes.
- `tests/test_cxl_bi_app.py`: pure host tests for calculations, logs, trace joins, and QEMU command invariants.
- `docs/cxl-bi-app-benchmark.md`: invocation, outputs, interpretation, and claim boundary.

### Task 1: Host Result Model and Evidence Parser

**Files:**
- Create: `scripts/cxl_bi_app.py`
- Create: `tests/test_cxl_bi_app.py`

**Interfaces:**
- Produces: `parse_guest_records(text: str) -> list[dict[str, object]]`
- Produces: `percentiles_ns(samples: list[int]) -> dict[str, float]`
- Produces: `analytical_envelope(link_gbps: float, media_ns: float, request_ns: float, bi_ns: float) -> dict[str, object]`
- Produces: `analyze_trace(path: Path, target_dpas: set[int]) -> dict[str, object]`

- [ ] **Step 1: Write failing parser and calculation tests**

```python
def test_parse_guest_records_ignores_console_noise():
    text = 'boot noise\nCXLBI_JSON {"event":"mapped","role":"reader"}\n'
    assert parse_guest_records(text) == [{"event": "mapped", "role": "reader"}]

def test_analytical_envelope_is_explicitly_not_measured():
    result = analytical_envelope(32.0, 100.0, 150.0, 200.0)
    assert result["classification"] == "analytical_not_measured"
    assert result["dirty_read_latency_ns"] == 450.0
    assert result["streaming_ceiling_GBps"] == 4.0
```

- [ ] **Step 2: Run the focused tests and confirm import failure**

Run: `python3 -m pytest -q tests/test_cxl_bi_app.py`

Expected: FAIL because `scripts.cxl_bi_app` and its functions do not exist.

- [ ] **Step 3: Implement deterministic parsing, percentiles, and formulas**

```python
RECORD_PREFIX = "CXLBI_JSON "

def parse_guest_records(text: str) -> list[dict[str, object]]:
    records = []
    for line in text.splitlines():
        marker = line.find(RECORD_PREFIX)
        if marker >= 0:
            records.append(json.loads(line[marker + len(RECORD_PREFIX):]))
    return records

def analytical_envelope(link_gbps, media_ns, request_ns, bi_ns):
    return {
        "classification": "analytical_not_measured",
        "assumptions": {
            "link_payload_Gbps": link_gbps,
            "media_latency_ns": media_ns,
            "request_response_ns": request_ns,
            "bi_snoop_ack_ns": bi_ns,
        },
        "dirty_read_latency_ns": request_ns + bi_ns + media_ns,
        "clean_transfer_latency_ns": request_ns + bi_ns,
        "streaming_ceiling_GBps": link_gbps / 8.0,
        "serialized_dirty_64B_GBps": 64.0 / (request_ns + bi_ns + media_ns),
    }
```

Use nearest-rank p50/p95/p99 with sorted integer nanoseconds; reject empty
samples and non-positive analytical inputs with `ValueError`.

- [ ] **Step 4: Add synthetic JSONL trace tests**

The fixture contains a request, `SNP_DATA_INV`, dirty `SNOOP_ACK` with 128 hex
characters, and completion at DPA `0x200000`. Assert that `analyze_trace`
returns `dirty_paths_by_dpa["0x200000"].complete == True`, event counts, and a
failure reason for an unrelated target address.

- [ ] **Step 5: Run and commit the pure logic**

Run: `python3 -m pytest -q tests/test_cxl_bi_app.py`

Expected: PASS.

```bash
git add scripts/cxl_bi_app.py tests/test_cxl_bi_app.py
git commit -m "test: add CXL BI result and trace analysis"
```

### Task 2: Static Guest DAX Litmus and Benchmark

**Files:**
- Create: `guest/cxl_bi_app_init.c`

**Interfaces:**
- Consumes kernel command line keys: `cxlbi.role`, `cxlbi.iterations`,
  `cxlbi.stream_bytes`, and `cxlbi.timeout_ms`.
- Produces one-line JSON records prefixed by `CXLBI_JSON ` with events
  `mapped`, `litmus`, `pingpong`, `stream`, `summary`, or `fatal`.
- Publishes target DPA offsets in the `mapped` record for trace correlation.

- [ ] **Step 1: Add compile-time layout assertions**

Define a 2 MiB test window whose control and payload fields are 64-byte
aligned. Include assertions equivalent to:

```c
_Static_assert(offsetof(struct shared_area, ready) % 64 == 0, "ready line");
_Static_assert(offsetof(struct shared_area, payload) % 64 == 0, "payload line");
_Static_assert(sizeof(((struct shared_area *)0)->payload) == 64, "one line");
```

- [ ] **Step 2: Implement PID 1 setup and DAX discovery**

Mount `/proc`, `/sys`, and `/dev`, read `/sys/bus/dax/devices/dax0.0/dev`,
create the character node if devtmpfs did not, open it `O_RDWR`, and map 2 MiB
with `MAP_SHARED`. On every failure emit `fatal` with `errno` and power off.

- [ ] **Step 3: Implement cache-line access primitives**

Use aligned volatile 64-bit accesses plus:

```c
static inline void full_fence(void) {
    __asm__ __volatile__("fence rw,rw" ::: "memory");
}
```

Implement bounded polling with `clock_gettime(CLOCK_MONOTONIC)`. Do not add any
flush or persistence syscall.

- [ ] **Step 4: Implement the dirty-owner litmus**

Reader initializes generation 0 and primes the payload. Writer waits for
ready, writes eight deterministic words derived from generation 1, fences,
and publishes generation 1. Reader waits for generation 1 and validates all
eight words. Emit the payload offset, elapsed nanoseconds, and mismatch count.

- [ ] **Step 5: Implement bidirectional ping-pong and samples**

Use separate reader-to-writer and writer-to-reader sequence/checksum lines.
Reader records one RTT sample per iteration. Writer validates every incoming
checksum before replying. Emit sample count, min/mean/p50/p95/p99/max, and
errors; cap iterations at 100000 to bound initramfs memory use.

- [ ] **Step 6: Implement two-direction verified streaming hand-off**

For direction 0, writer fills cache lines with `pattern(line, generation)`,
publishes a generation, and reader verifies them. For direction 1, reverse
the roles and use a new generation. Emit bytes, elapsed nanoseconds, computed
MiB/s, and mismatches for each role.

- [ ] **Step 7: Cross-compile and inspect the binary**

Run:

```bash
out/legofs-type3/toolchain/musl-rv64gc/bin/musl-gcc \
  -static -O2 -g -Wall -Wextra -Werror -march=rv64imafdc -mabi=lp64d \
  -o /tmp/cxl-bi-app-init guest/cxl_bi_app_init.c
file /tmp/cxl-bi-app-init
readelf -h /tmp/cxl-bi-app-init
```

Expected: statically linked RISC-V ELF64 executable and no compiler warning.

- [ ] **Step 8: Commit the guest program**

```bash
git add guest/cxl_bi_app_init.c
git commit -m "feat: add two-guest DAX coherence litmus"
```

### Task 3: Isolated Kernel Build

**Files:**
- Create: `scripts/build_cxl_bi_app.sh`

**Interfaces:**
- Consumes: `guest/cxl_bi_app_init.c`, `components/linux`, and the existing
  RISC-V musl/GNU toolchains.
- Produces: `out/cxl-bi-app/images/Image`,
  `out/cxl-bi-app/images/initramfs/init`, and `build-manifest.json`.

- [ ] **Step 1: Add shell preflight and deterministic paths**

Use `set -euo pipefail`, resolve the repository root from the script path,
check all compiler/source inputs, create only `out/cxl-bi-app/{build,images}`,
and compile the static init with the flags from Task 2.

- [ ] **Step 2: Create the minimal initramfs tree**

Install the binary as mode 0755 at `images/initramfs/init` and create empty
`dev`, `proc`, `sys`, `tmp`, and `run` directories. Do not copy the existing
Legofs payload or mutate `out/legofs-type3`.

- [ ] **Step 3: Configure an independent Linux output directory**

Start from `defconfig`, merge the repository's existing CXL/DAX requirements,
and force:

```text
CONFIG_BLK_DEV_INITRD=y
CONFIG_CXL_BUS=y
CONFIG_CXL_PCI=y
CONFIG_CXL_MEM=y
CONFIG_CXL_ACPI=y
CONFIG_CXL_PMEM=y
CONFIG_CXL_REGION=y
CONFIG_DEV_DAX=y
CONFIG_DEV_DAX_CXL=y
CONFIG_DAX=y
CONFIG_INITRAMFS_SOURCE="<absolute out/cxl-bi-app/images/initramfs>"
CONFIG_INITRAMFS_COMPRESSION_NONE=y
```

Run `olddefconfig`, verify every required symbol and the exact initramfs path,
then build `Image` with `make -j$(nproc)` and copy it into `images/`.

- [ ] **Step 4: Record provenance and validate boot inputs**

Write JSON containing source commits, compiler versions, image SHA-256, and
the reused QEMU/CXLMemSim/OpenSBI/U-Boot paths. Run `test -s`, `file`, and
`sha256sum` on the produced files.

- [ ] **Step 5: Commit and run the build**

```bash
git add scripts/build_cxl_bi_app.sh
git commit -m "build: add isolated CXL BI guest image"
./scripts/build_cxl_bi_app.sh
```

Expected: `out/cxl-bi-app/images/Image` exists and the existing Legofs Image
checksum remains unchanged.

### Task 4: Dual-QEMU Orchestration and Automated Verdict

**Files:**
- Modify: `scripts/cxl_bi_app.py`
- Modify: `tests/test_cxl_bi_app.py`

**Interfaces:**
- Produces: `build_qemu_command(cfg: RunConfig, node: int) -> list[str]`
- Produces: `run_experiment(cfg: RunConfig) -> dict[str, object]`
- CLI emits a timestamped `out/cxl-bi-app/runs/<run-id>/result.json`.

- [ ] **Step 1: Write command-invariant tests**

For both nodes assert distinct `mem0-nodeN.raw` paths, distinct host IDs, one
common CXLMemSim TCP port, `coherence-v2=on`, `hdm-db=on`, and a BI-capable
fixed-memory window. Assert `read-exclusive=on` only for node 0.

- [ ] **Step 2: Implement artifact/config dataclasses and preflight**

Validate every executable and image, reserve a localhost port, reject an
existing listener, create a unique run directory, and create two sparse 256
MiB backing files. Compare canonical paths and `(st_dev, st_ino)` and abort if
they are equal.

- [ ] **Step 3: Implement CXLMemSim launch**

Launch strict MESI-v2 with `ssd-stream`, 256 MiB authoritative capacity,
topology file `components/cxlmemsim/qemu_integration/topology_simple.txt`, and
coherence JSONL in the run directory. Wait for its listening log line or TCP
readiness with a bounded deadline.

- [ ] **Step 4: Implement two QEMU commands and U-Boot automation**

Reuse the exact machine, firmware, CXL root-port, Type-3, FMW, HDM-DB, and
coherence-v2 properties from `scripts/legofs_type3_2node.py`. Use 1 GiB RAM
per guest, five harts, no disk payload, and append role/iteration/stream
parameters to Linux bootargs. Send `cxl list`, `cxl init`, bootargs, and
`bootefi` after their expected U-Boot prompts.

- [ ] **Step 5: Parse completion and enforce application gates**

Wait until each serial log contains one `summary` record or the global timeout
expires. Require mapped/litmus/pingpong/two stream records, `errors == 0`, and
matching iteration/byte counts. Capture guest measurements under
`qemu_tcg_tcp_wallclock`.

- [ ] **Step 6: Join BI trace evidence to guest DPAs**

Use offsets emitted by the guest to require, at minimum, one dirty
`SNP_DATA_INV`, one dirty `SNOOP_ACK` with 64 bytes, and one successful
completion for a payload or synchronization line. Store matching sequence
IDs and payload hashes in `result.json`; do not declare PASS from event counts
alone.

- [ ] **Step 7: Add robust process cleanup and failure bundle**

Start every child in a new session, terminate its process group, wait five
seconds, then kill only that recorded process group if needed. Always write a
partial result with phase, reason, child return codes, and last 80 serial lines.

- [ ] **Step 8: Run tests and commit orchestration**

Run:

```bash
python3 -m pytest -q tests/test_cxl_bi_app.py
python3 -m py_compile scripts/cxl_bi_app.py
```

Expected: PASS.

```bash
git add scripts/cxl_bi_app.py tests/test_cxl_bi_app.py
git commit -m "feat: orchestrate two-guest CXL BI benchmark"
```

### Task 5: Entry Point and Operator Documentation

**Files:**
- Create: `run-cxl-bi-app.sh`
- Create: `docs/cxl-bi-app-benchmark.md`
- Modify: `tests/test_cxl_bi_app.py`

**Interfaces:**
- CLI modes: default build-and-run, `--build-only`, `--run-only`, and optional
  `--negative-control`.
- Tunables: `--iterations`, `--stream-bytes`, `--timeout`, `--link-gbps`,
  `--media-ns`, `--request-ns`, and `--bi-ns`.

- [ ] **Step 1: Add CLI help and validation tests**

Assert that invalid zero iterations, non-cache-line stream sizes, negative
latencies, and simultaneous `--build-only --run-only` fail with exit status 2.

- [ ] **Step 2: Implement the root wrapper**

Resolve the repository root, run the build script unless `--run-only`, stop
after build for `--build-only`, and otherwise `exec python3
scripts/cxl_bi_app.py "$@"`. Forward signals through `exec`.

- [ ] **Step 3: Document exact commands and interpretation**

Document the one-command positive run, a fast smoke configuration, the
optional negative control, evidence directory layout, PASS gates, and the
emulator-versus-hardware claim boundary. Include the warning that GPF and
persistence are outside this coherence-only test.

- [ ] **Step 4: Run static checks and commit**

Run:

```bash
bash -n run-cxl-bi-app.sh scripts/build_cxl_bi_app.sh
python3 scripts/cxl_bi_app.py --help
python3 -m pytest -q tests/test_cxl_bi_app.py
```

Expected: PASS and complete help output.

```bash
git add run-cxl-bi-app.sh docs/cxl-bi-app-benchmark.md tests/test_cxl_bi_app.py
git commit -m "docs: add CXL BI benchmark entry point"
```

### Task 6: Full Local Execution and Evidence Review

**Files:**
- Generated: `out/cxl-bi-app/runs/<run-id>/*`
- Modify only if a test exposes a defect: files owned by Tasks 1-5.

**Interfaces:**
- Consumes the complete CLI.
- Produces a final `result.json` whose `verdict` is `PASS` only after both
  application and trace gates pass.

- [ ] **Step 1: Run the host unit suite and capture baseline checksums**

Run the focused pytest suite and record checksums of reused artifacts in the
new run manifest.

- [ ] **Step 2: Build the isolated guest image**

Run: `./run-cxl-bi-app.sh --build-only`

Expected: a static initramfs and dedicated Image are produced without changing
existing tracked or Legofs output files.

- [ ] **Step 3: Execute a bounded smoke run**

Run:

```bash
./run-cxl-bi-app.sh --run-only --iterations 16 \
  --stream-bytes 65536 --timeout 240
```

Expected: both guests boot and `result.json` reaches either a precise failed
gate or PASS; repair only evidenced defects and repeat the focused test first.

- [ ] **Step 4: Execute the representative run**

Run:

```bash
./run-cxl-bi-app.sh --run-only --iterations 256 \
  --stream-bytes 1048576 --timeout 300 \
  --link-gbps 32 --media-ns 100 --request-ns 150 --bi-ns 200
```

Expected: `verdict: PASS`, zero application errors, a complete dirty BI path,
and separately labeled observed and analytical performance.

- [ ] **Step 5: Review evidence rather than only the exit code**

Inspect `result.json`, both serial logs, CXLMemSim JSONL, distinct backing-file
metadata, child return codes, and exact QEMU commands. Confirm no guest record
mentions a flush operation and no process remains running.

- [ ] **Step 6: Run regressions and report the platform conclusion**

Run the new pytest suite plus the repository's existing coherence-v2 tests
that do not require a rebuild. Report exact run directory, observed model
statistics, analytical assumptions/results, and the scoped conclusion: this
platform is sufficient for application-visible BI coherence experiments, but
not a substitute for native hardware or persistence validation.
