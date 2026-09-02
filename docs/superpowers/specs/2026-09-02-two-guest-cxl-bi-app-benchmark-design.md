# Two-Guest CXL BI Application-Visible Coherence Benchmark

Date: 2026-09-02

## Goal

Build a self-contained experiment that boots two RISC-V QEMU guests, maps one
CXL Type-3 DAX device in each guest, connects both endpoints to the same
CXLMemSim MESI-v2 authority, and verifies that ordinary application
load/store operations observe coherent data through the modeled BI path.

The experiment must also report useful timing data without presenting QEMU,
TCG, Unix scheduling, or TCP overhead as physical CXL hardware performance.

## Claim Boundary

The experiment can establish all of the following:

- two independent guest applications map their local `/dev/dax0.0` devices;
- the endpoint backing files are distinct, so a shared host mapping cannot
  accidentally provide host-LLC coherence;
- one guest can cache an old value, the other guest can write a new value, and
  the first guest subsequently observes the exact new cache-line payload
  without `msync`, `fsync`, `clflush`, or a userspace message channel;
- CXLMemSim records the matching ownership transfer, BI snoop, snoop ACK, and
  completion for the tested address;
- a dirty owner can supply the cache-line data carried by the modeled BI ACK;
- repeated bidirectional hand-offs remain correct and can be timed.

It cannot establish native CPU-cache invalidation in a physical processor,
electrical/protocol compliance of a real CXL link, GPF persistence semantics,
or real-hardware latency/bandwidth. QEMU's CXL endpoint cache is the coherent
cache modeled by this stack; TCG does not turn this into a physical CPU-cache
experiment.

## Architecture

The test reuses the repository's known-good SiFive U boot path and its existing
QEMU and CXLMemSim binaries:

```text
guest 0 application                     guest 1 application
  mmap(/dev/dax0.0)                       mmap(/dev/dax0.0)
          |                                       |
QEMU Type-3 endpoint cache 0             QEMU Type-3 endpoint cache 1
          | coherence-v2 TCP                     |
          +---------------+-----------------------+
                          |
                  CXLMemSim MESI-v2
             directory + authoritative bytes
                          |
                 SSD-stream backend model
```

Each QEMU process gets a different local 256 MiB Type-3 backing file. Both
endpoints deliberately use the same logical DPA space at the CXLMemSim server.
Guest 0 uses read-exclusive mode to make a read of a line held Modified by
guest 1 produce a deterministic dirty `SNP_DATA_INV` transaction.

The guest kernel is built in a separate output directory with a tiny static
PID 1 in its built-in initramfs. Existing build outputs and user-modified files
are not overwritten.

## Guest Protocol

The DAX area uses cache-line-separated fields so that data and synchronization
variables do not share a line accidentally. All fields are naturally aligned.
The guest program uses volatile aligned loads/stores and RISC-V `fence rw,rw`;
it performs no cache-maintenance or persistence syscall during the coherence
phases.

The protocol has four phases:

1. **Discovery and initialization.** Guest 0 creates and maps `/dev/dax0.0`,
   initializes the test area, and publishes a ready generation. Guest 1 waits
   for that generation entirely through the CXL mapping.
2. **Dirty-owner litmus.** Guest 0 primes and verifies the old payload. Guest 1
   writes a deterministic multiword payload and generation, leaving its
   endpoint copy dirty. Guest 0 reads the new generation and payload. Passing
   requires every word to match and the model trace to contain the dirty
   invalidation/ACK/completion for the payload address.
3. **Bidirectional ping-pong.** The guests alternate ownership of separated
   cache lines for a configurable number of iterations. Each receiver verifies
   the sequence and checksum before replying. Both guests report elapsed time,
   operation count, and failures.
4. **Streaming hand-off.** One guest fills a configurable range with a
   deterministic pattern, publishes a generation, and the other guest reads
   and verifies the range. Roles then reverse. This reports model wall-clock
   read/write bandwidth while preserving correctness checks.

Each phase has a bounded timeout. A timeout, mapping failure, mismatched word,
missing role, or missing trace evidence is a hard failure.

## Causality and Controls

Application-visible values alone are insufficient because an incorrectly
shared host file could create a false positive. The runner therefore enforces
and records these controls:

- endpoint backing paths and inodes must differ;
- the QEMU command line must enable `coherence-v2` and HDM-DB on both devices;
- the CXL fixed-memory-window restriction must include BI;
- both guests must report the same DAX offset/size but different endpoint IDs;
- trace analysis must join the tested DPA with a request, snoop, dirty ACK, and
  completion, and must validate the 64-byte dirty data where available;
- a `--negative-control` mode intentionally boots one endpoint without
  committing BI. The current QEMU implementation is expected to fail closed
  (DAX access error or guest-test timeout), not to return a coherent value.

The negative control is optional because it costs another dual-guest boot. The
normal run's distinct files plus address-correlated BI trace are mandatory.

## Performance Reporting

The result JSON separates two classes of number:

### Functional-model measurements

- boot duration;
- one-way dirty hand-off latency as observed by the guest program;
- ping-pong round-trip p50, p95, p99, mean, and operations per second;
- verified streaming hand-off bandwidth in each direction;
- CXLMemSim request, snoop, dirty-ACK, and completion counts.

These values characterize this emulator stack and are labeled
`qemu_tcg_tcp_wallclock`.

### Analytical hardware envelope

The runner accepts explicit assumptions for link payload bandwidth, local
media latency, CXL request/response latency, and BI snoop/ACK latency. It
computes, rather than measures:

- a serialized 64-byte hand-off bandwidth bound;
- a dirty-owner read latency estimate;
- a clean-owner transfer latency estimate;
- the link-limited streaming bandwidth ceiling.

Every analytical field includes its assumptions and is labeled
`analytical_not_measured`. No default analytical value is described as a
property of the current host.

## Files and Interfaces

New files are isolated from the existing Legofs experiment:

- `guest/cxl_bi_app_init.c`: static guest PID 1 and DAX benchmark;
- `scripts/build_cxl_bi_app.sh`: builds the static init and a dedicated kernel;
- `scripts/cxl_bi_app.py`: process orchestration, parsing, trace validation,
  analytical calculations, and result generation;
- `run-cxl-bi-app.sh`: stable top-level entry point;
- `tests/test_cxl_bi_app.py`: host-side unit tests for parsers, commands,
  evidence checks, and performance math;
- `out/cxl-bi-app/`: all generated artifacts and run results.

The default invocation builds missing artifacts, boots both guests, performs
the mandatory positive test, and writes a timestamped directory containing
serial logs, QEMU logs, CXLMemSim logs, coherence JSONL, a manifest, and
`result.json`. It exits nonzero unless every correctness and evidence gate
passes.

## Error Handling and Cleanup

The Python runner owns a process group for each child and always terminates
QEMU and CXLMemSim in `finally` cleanup. It captures the last serial lines in
the failure report, retains all logs, and never deletes earlier runs. It checks
ports before launch and uses a unique run directory and backing files per run.

Build scripts check toolchain and source prerequisites before doing expensive
work. They reuse matching existing QEMU, OpenSBI, U-Boot, and CXLMemSim
artifacts but build the dedicated guest kernel independently.

## Acceptance Criteria

A positive run is accepted only when:

1. both guests reach the benchmark and map DAX successfully;
2. the dirty-owner litmus sees the new payload with zero mismatches and no
   explicit flush operation;
3. every ping-pong and streaming verification completes with zero errors;
4. the two endpoint backing files are demonstrably distinct;
5. trace analysis finds the required address-correlated BI dirty-data path;
6. the result labels emulator measurements and analytical estimates
   separately; and
7. all processes exit or are cleaned up and a complete evidence bundle is
   retained.
