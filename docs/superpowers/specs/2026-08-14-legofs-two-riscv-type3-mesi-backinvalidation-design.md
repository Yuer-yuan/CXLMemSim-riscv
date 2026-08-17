# Legofs on Two RISC-V Type-3 Endpoints with MESI Back-Invalidation

> **Historical note (2026-08-14):** This design records the original proof,
> including its former `components/legofs` gitlink. The maintained platform now
> consumes the parent LegoFS checkout at `../..` as its sole LegoFS source.

## Objective

Run `Zettai-US/legofs` end to end across two concurrently running
`qemu-system-riscv64 -M sifive_u` guests. Each guest owns one modeled CXL
Type-3 endpoint. Both endpoints join one CXLMemSim MESI write-back coherent
domain, and a real Legofs lifecycle-direct data operation must cause and
complete a remote back-invalidation.

The final run must prove all of the following in one bounded artifact set:

- two `sifive_u` QEMU processes overlapped in time;
- Linux discovered and configured one Type-3 memory device in each guest;
- the endpoints registered with distinct coherence host IDs;
- Legofs used strict lifecycle-direct writes and reads through guest DAX;
- the server independently read a client-written candidate during
  `candidate_checksum()`;
- that read caused at least one MESI-v2 `SNP_DATA_INV` carrying dirty data and
  at least one matching model-level ACK;
- the benchmark completed with the expected checksum;
- Blob, staging, and legacy payload paths remained unused.

This is QEMU/TCG functional model evidence. It does not claim physical CXL
link behavior, native guest CPU-cache invalidation, real CXL.cache, or hardware
performance.

## Pinned Sources

The superproject will reference these exact inputs:

- Legofs: `https://github.com/Zettai-US/legofs.git` at
  `96f733940251d6484dad0ba2cfbe99dcf5259776`;
- CXLMemSim: `https://github.com/SlugLab/CXLMemSim.git` at
  `716c16c9efc7a733006d0772f8c6c4bb055f7b15`, from
  `codex/type2-hw-cc-fullsystem-20260809`;
- QEMU: the existing `components/qemu` SiFive U CXL branch, extended for a
  protocol-v2 Type-3 endpoint;
- Linux: the existing `components/linux` SiFive U CXL branch;
- U-Boot and OpenSBI: the existing pinned superproject revisions.

Legofs will be added as `components/legofs`. Existing component checkouts will
not be replaced with unrelated local working trees.

## Firmware and Linux Contract

The machine command remains exactly:

```text
qemu-system-riscv64 -M sifive_u
```

The RISC-V `virt` machine is not an allowed substitute.

The current SiFive U QEMU and Linux branches already contain the mechanisms
needed from the June 2026 RISC-V CXL patch series:

- a CXL host-register region and fixed-memory-window aperture;
- CEDT and ACPI0017 publication;
- ACPI0017 `_DEP` entries for ACPI0016 CXL host bridges;
- a dedicated 256 MiB below-4-GiB non-prefetchable MMIO window for CXL BARs;
- Linux dependency release from `acpi_pci_root_add()` after the host bridge is
  attached.

The implementation must retain and test these SiFive U adaptations rather
than applying the `riscv/virt` patch literally. QEMU must continue publishing
the same firmware handoff used by U-Boot and Linux. Each guest must show the
Type-3 endpoint, CXL root/port topology, committed HDM decoder, region, and DAX
character device before Legofs starts.

## Coherence Architecture

### Server

One host-side `cxlmemsim_server` runs with explicit MESI write-back protocol v2
enabled. It owns the authoritative bytes, sparse directory, endpoint-session
registry, and snoop transactions for the shared region.

The server accepts two TCP protocol-v2 endpoint sessions. TCP is selected
because the two QEMU processes require independent asynchronous receive paths
for unsolicited snoops; the legacy PGAS SHM slot protocol is not sufficient.
The server rejects protocol-v1 traffic in this coherent domain.

### QEMU Type-3 Endpoint

The reusable `cxl_memsim_v2` endpoint-cache implementation currently used by
the Type-2 model will be connected to Type-3 accesses. The Type-3 device gains
explicit properties for:

- MESI-v2 enablement;
- server host and port;
- a unique coherence host ID;
- cache capacity and associativity;
- snoop timeout;
- write-through selection, disabled for this write-back proof.

The endpoint registers `MODEL_SNOOP` only. It must not claim `NATIVE_FLUSH`.
Every guest load or store routed through `cxl_type3_read()` or
`cxl_type3_write()` uses the endpoint cache. Conflicting operations are sent
to the server, while unsolicited snoops are consumed by the endpoint receive
path and acknowledged only after the modeled cache transition and any dirty
data return complete.

When MESI-v2 is selected, registration, transport, protocol, timeout, and
server-copy errors fail closed. No error may silently fall back to the local
QEMU memory backend.

### Endpoint Identity

The node0 Type-3 device uses coherence host ID 0. The node1 Type-3 device uses
coherence host ID 1. Session IDs remain server-issued and are recorded in the
run result. A duplicate live host ID or unexpected reconnect fails the run.

Both guests use the same guest-visible HPA layout and region geometry, but
their QEMU device objects and endpoint caches are independent. The shared
identity is the CXLMemSim coherent domain, not a host file mapped directly by
both guests.

## Legofs Data Flow

Node0 boots first and runs `badfs-server`. Node1 then boots and runs
`badfs-bench` as the client. QEMU user networking exposes node0's Legofs TCP
port to node1 through the host; this control path is separate from the
host-side MESI-v2 connections.

Both guests resolve their own DAX character-device path after Linux creates
the CXL region. The server and client must agree on the same modeled region
identity and size, without exchanging host pathnames.

Legofs is configured with:

- lifecycle data mode enabled;
- strict direct-final writes required;
- strict direct reads required;
- a CXL lifecycle device pointing to the guest DAX character device;
- fallback disabled by validation, not merely discouraged by configuration.

For a direct write, the sequence is:

1. Node0 reserves and prepares a candidate extent through its Type-3 endpoint.
2. Node1 receives the exact lifecycle grant and maps only that DAX range.
3. Node1 writes the benchmark payload and unmaps it, leaving dirty lines in its
   modeled Type-3 endpoint cache.
4. Node1 calls `lifecycle_store_direct()` with the full checksum.
5. Node0 executes Legofs `candidate_checksum()` and reads the same extent
   through its own DAX mapping and Type-3 endpoint.
6. CXLMemSim issues `SNP_DATA_INV` to node1, receives the dirty line and model
   ACK, commits it to authoritative storage, and then completes node0's read.
7. Legofs compares the checksum, persists, commits, and publishes the extent.

Thus the required back-invalidation is caused by the actual Legofs seal path,
not by an unrelated litmus test. Direct reads then verify the published data.

## Build and Run Interface

The superproject will provide one top-level entry point for the bounded proof.
It will:

1. validate pinned submodules and required host tools;
2. build CXLMemSim with protocol v2;
3. build QEMU with SiFive U, CXL, and Type-3 MESI-v2 support;
4. build the existing Linux, U-Boot, and OpenSBI artifacts as needed;
5. cross-build the required Legofs RISC-V binaries;
6. create per-node guest images without loop mounting;
7. start the CXLMemSim server;
8. boot node0 and wait for CXL/DAX and Legofs readiness;
9. boot node1 and run the bounded benchmark;
10. stop only processes started by this run;
11. validate logs and atomically publish a JSON result.

The command builder must expose both complete QEMU command arrays in the
result. Tests must assert that each begins with
`qemu-system-riscv64 -M sifive_u` and includes exactly one Type-3 endpoint.

Run directories are unique and contain node logs, server logs, manifests,
commands, extracted counters, and the final JSON. Shared-memory objects,
ports, and process IDs are run-scoped. Cleanup uses recorded process IDs and
must not kill unrelated QEMU or CXLMemSim processes.

## Telemetry and Correlation

Protocol-v2 telemetry must expose at least:

- registered endpoint and session IDs;
- GETS, GETM, UPGRADE, PUTS, and PUTM counts;
- snoop counts by opcode;
- model ACK counts by snoop opcode;
- dirty-data snoop completions;
- timeouts, protocol errors, and failed server-copy commits;
- directory state for the lines involved in the proof, or equivalent
  transaction trace records.

The runner captures a counter snapshot after both nodes are ready but before
the benchmark starts, then another after Legofs finishes. Acceptance uses the
delta. Boot-time or preflight snoops do not count.

Legofs lifecycle trace records and MESI transaction records must share enough
information to establish temporal and address-range correlation: the
`SNP_DATA_INV` transaction occurs after the direct client write and before the
corresponding `store_direct` succeeds, and the cache-line address lies within
the lifecycle grant's mapped range.

## Error Handling

The run fails if any of these conditions occurs:

- either QEMU exits before benchmark completion;
- the two QEMU process lifetimes do not overlap;
- the machine is not exactly `sifive_u`;
- a guest lacks the expected CXL endpoint, region, decoder, or DAX device;
- endpoint registration is missing, duplicated, or uses the wrong host ID;
- Legofs strict-direct initialization fails;
- Legofs uses Blob, staging, or a legacy payload path;
- the server reports a protocol error, snoop timeout, failed data commit, or
  unresolved session;
- Legofs reports a checksum failure, rejected lease, quarantine event, or
  leaked active lease;
- the benchmark checksum is wrong;
- the benchmark-scoped `SNP_DATA_INV`, dirty-data completion, or matching ACK
  delta is zero.

On failure the runner preserves logs and writes a failed result with the first
failure reason. It still performs scoped process and temporary-object cleanup.

## Verification Layers

### Static and Unit Tests

- Type-3 property parsing and invalid configuration rejection.
- Type-3 read/write routing through the v2 endpoint cache.
- Fail-closed behavior for transport and registration errors.
- Snoop invalidation and dirty-data ACK behavior using the QEMU unit fake.
- Two-node command construction preserving exact `sifive_u` topology and
  distinct host IDs.
- Legofs submodule pin and RISC-V artifact checks.
- Result parser rejection for zero or uncorrelated invalidation evidence.

### Component Integration Tests

- Existing CXLMemSim protocol-v2, directory, endpoint-cache, session, SHM, and
  TCP tests.
- Existing QEMU SiFive U firmware/ACPI and Type-3 tests.
- Existing Linux, U-Boot, and superproject build-contract tests.
- Legofs core tests plus RISC-V cross-build checks.

### End-to-End Acceptance

A successful result requires:

- two overlapping live QEMU processes;
- two successful guest boot markers;
- two Type-3 endpoint registrations with host IDs 0 and 1;
- nonzero `trusted_direct_write_ops` and `trusted_direct_write_bytes`;
- nonzero `trusted_direct_read_ops` and `trusted_direct_read_bytes`;
- zero Blob and staging operation/byte counters;
- zero legacy data-path counters;
- zero checksum failures, lease rejections, quarantine events, and active
  leases after shutdown;
- correct benchmark byte counts and checksum;
- benchmark-scoped `SNP_DATA_INV > 0`;
- benchmark-scoped dirty-data snoop completions and matching model ACKs greater
  than zero;
- at least one correlated Legofs grant/transaction address range.

The final JSON reports the exact commits, artifact hashes, commands,
environment, topology, guest evidence, Legofs counters, coherence counter
deltas, correlation records, cleanup status, and the explicit functional-model
claim boundary.

## Repository and Change Boundaries

Changes are limited to:

- the CXLMemSim branch when server telemetry or protocol behavior is missing;
- the QEMU branch for Type-3 MESI-v2 integration;
- Legofs only where RISC-V guest orchestration, trace correlation, or strict
  direct-path reporting requires it;
- the superproject for the new Legofs submodule, build/run orchestration,
  tests, documentation, and pinned revisions.

The existing Linux CXL dependency and SiFive U resource fixes are tested but
not rewritten unless the end-to-end run reveals a concrete defect. Type-2,
physical-hardware, latency-injection, IO500, MPI, and performance-comparison
work are outside this milestone.
