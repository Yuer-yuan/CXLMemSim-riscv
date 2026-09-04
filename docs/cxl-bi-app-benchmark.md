# Two-Guest CXL BI Application Benchmark

This experiment boots two independent RISC-V QEMU guests. Each guest maps its
own CXL Type-3 `/dev/dax0.0`, while both QEMU endpoint caches use one
CXLMemSim MESI-v2 authority. It tests whether normal application loads and
stores see coherent values through the modeled Back-Invalidate path.

## Run It

Build missing dedicated guest artifacts and run the representative test:

```bash
./run-cxl-bi-app.sh \
  --iterations 256 \
  --stream-bytes 1048576 \
  --timeout 300 \
  --link-gbps 32 \
  --media-ns 100 \
  --request-ns 150 \
  --bi-ns 200
```

A quicker functional smoke test is:

```bash
./run-cxl-bi-app.sh --run-only \
  --iterations 8 --stream-bytes 65536 --timeout 240
```

Build and execution can also be separated:

```bash
./run-cxl-bi-app.sh --build-only --jobs 8
./run-cxl-bi-app.sh --run-only --iterations 256 --stream-bytes 1048576
```

Generated files live only under `out/cxl-bi-app/`. Each execution creates a
new UTC timestamped directory under `out/cxl-bi-app/runs/` and prints the
absolute `result.json` path.

## What Must Pass

The runner reports `PASS` only if all independent gates succeed:

1. both guests discover and map their local DAX devices with the same logical
   offsets and geometry;
2. node 0 primes the old 64-byte payload, node 1 publishes the new payload,
   and node 0 observes the exact new checksum with no explicit flush;
3. the bidirectional ping-pong and both streaming directions have zero data
   errors;
4. the QEMU endpoint backing paths and inodes are distinct;
5. CXLMemSim has exactly two registered host IDs and no timeout, protocol,
   delivery, or server-copy error;
6. the payload DPA has an address-correlated `GETM -> SNP_DATA_INV ->` dirty
   64-byte model ACK `-> dirty_completion` chain; and
7. all runner-owned processes are gone after evidence capture.

The backing files are intentionally not shared. Therefore host page-cache or
host-LLC coherence cannot make the application result pass. Shared bytes come
from CXLMemSim's authoritative memory and the QEMU endpoint-cache BI protocol.

## Reading Performance Results

`result.json` contains two deliberately separate sections:

- `performance.observed` is classified `qemu_tcg_tcp_wallclock`. It includes
  TCG translation, host scheduling, synchronous TCP, JSON tracing, and guest
  polling. It characterizes this functional simulator setup only.
- `performance.analytical` is classified `analytical_not_measured`. It uses the
  four CLI assumptions to calculate a transparent latency/bandwidth envelope.
  Defaults are assumptions, not detected properties of the host or a CXL card.

The stream test is a coherence hand-off workload: the producer dirties every
line, publishes a generation, and the consumer reads and verifies every word.
It is not a storage benchmark for the SSD-stream file and is not a NAND device
bandwidth measurement.

## Evidence Bundle

Each run directory contains:

- `result.json`: commands, artifact commits, backing inode identities,
  application records, BI correlations, server counters, timings, and verdict;
- `node0-serial.log` and `node1-serial.log`: full firmware/kernel/application
  consoles;
- `node0-host-events.jsonl` and `node1-host-events.jsonl`: host timestamped
  console lines;
- `coherence.jsonl`: CXLMemSim MESI-v2 request/snoop/ACK/completion trace;
- `cxlmemsim.log`: server startup, shutdown, and final counters;
- two endpoint PMEM files, two LSA files, and the authority backing file.

## Interpretation Boundary

A PASS demonstrates automatic coherence at the application-visible semantic
boundary implemented by this QEMU + CXLMemSim platform: after another endpoint
writes, an ordinary DAX load returns the coherent value without application
cache maintenance.

It does not prove that a physical CPU's private caches were invalidated, does
not validate electrical/flit timing on a real CXL link, and does not establish
power-fail persistence. HDM-DB/BI controls coherence ownership; GPF and the
persistence domain require a separate persistence/crash experiment.
