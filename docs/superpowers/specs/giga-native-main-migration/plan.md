# Giga native main migration implementation plan

**Goal:** Port the existing native LLC/NUMA test to current LegoFS and pass on giga.
**Architecture:** Native compatibility and evidence gates built against current
main, using the prior experiment as the scenario reference.
**Tech Stack:** Rust, Python unittest, Bash, OpenMPI, IO500, Linux NUMA, SSH/rsync.
**Spec:** [spec.md](spec.md)

## Constraints

Preserve existing dirty files, Git identities, result bundles and unrelated remote
processes. Use the selected component and deployment directories. Apply all scope
and acceptance requirements in spec.md. Execute inline in this session.

## 1. Merge and local validation

- [x] Copy the three selected 2C/3C remote capacity-5s bundles back with
  `rsync -az --ignore-existing`. The initial wider history transfer disconnected
  after approximately 2 GB; completed historical copies were retained.
- [x] Save initial status and diffs in the migration bundle.
- [x] Generate merge candidates for files changed in the old component since
  `04a30c6`, including its working diff. Inspect conflicts against current source.
- [x] Add `scripts/{build_giga_native_io500,run_giga_native_legofs_io500}.sh` and
  `tests/test_giga_native_io500_scripts.py` from the old working files.
- [x] Merge product support and `scripts/run_lifecycle_io500.py`, NUMA verifier,
  rank launcher, profiles and their tests; preserve current layout-v7 handling.
- [x] Run `python3 -m unittest discover -s tests -p test_giga_native_io500_scripts.py`
  and component tests for lifecycle runner, rank placement, NUMA verifier and
  syscall interception. Run Rust serving transport, native mapping and audit tests.

## 2. Native deployment and validation

- [x] Save remote source/build identities and manifest before deploying changed
  source files; exclude .git and target. Record per-file SHA256 and verify remotely.
- [x] Build via `bash scripts/build_giga_native_io500.sh --jobs 8`; preserve log.
- [x] Repeat component contract tests with the native toolchain.
- [x] Run `scripts/run_giga_native_legofs_io500.sh --topology TOPOLOGY --profile PROFILE
  --run-id main-TOPOLOGY-PROFILE` for 1c1s/2c1s/3c1s and bounded/capacity-5s,
  sequentially after each topology's preflight. Preserve first failures and fix
  the first failed state before rerunning under an explicit attempt identifier.

## 3. Evidence and commits

- [x] Verify completion, verifier/hash results, per-rank transport/accounting,
  placement, RAS, cleanup and source/build pairing for each accepted bundle.
- [x] Synchronize new evidence locally and write bounded `conclusion.md`.
- [x] Commit only task-owned LegoFS changes, then parent scripts/tests/spec and
  the updated component pointer. Record tested tree hashes when commit identities
  differ from pre-commit build metadata. Verify unrelated dirty state is retained.

## Findings under validation

- Current product profile is v7/2048 small segments, batched close,
  coherent-seal-no-writeback, writer-receipt with msync; all control stays CXL.
- Release root-retry test assumed another thread runs within 16 yields. Preserve
  the production bound; deterministic unit tests cover same-call recovery and
  exhaustion, and the mapped test covers either legal scheduling order.
- `main-1c1s-bounded`: inspector rejected the runner's textual `batched` value
  for a boolean environment setting. Corrected to `BADFS_LIFECYCLE_CLOSE_BATCH=1`.
- `main-1c1s-bounded-env-fixed`: all 22 IO500 phases finished, current receipt and
  whole-region NUMA gates passed before legacy trace parsing rejected packed-cell
  operations without reserve events. Add real `grant_small_cell` issuance evidence
  and validate identity/mapping before store; tracing stays enabled.
- Native bundles now retain validated source-file and binary build manifests.
- Metadata records are versioned in place; RootAnchor is mutable. Global root
  comparison may cause retries for unrelated publications. No retry bound or
  workload fallback gate has been relaxed; actual native counters will decide
  whether a product read protocol change is required in this migration.

- `main-1c1s-bounded-r1`: accepted with current packed trace, batched snapshot,
  direct RO epoch and native writer receipt accounting. One metadata snapshot
  retry recovered; zero exhaustion/fallback.
- `main-1c1s-capacity-5s-r1`: mdworkbench direct-create failed with fixed record
  Capacity. Inode budgeting assumes create+terminal, while unlink also consumes
  immutable records. Add actual record admission and reserve one terminal per
  live direct OFD. Create capacity signals the existing exact-generation rollover;
  an unlink cannot steal terminal credit and uses its authority barrier when full.
  No retry limit, region size or workload geometry increase. A mapped regression
  exhausts a fully applied arena with mixed operations, then closes a held OFD.

- `main-1c1s-capacity-5s-record-budget`: all 22 phases completed and record
  rollover progressed, but strict CXL metadata gate rejected one root-unavailable
  command fallback (two internal stable-read exhaustions, 78 retries, 46 recoveries).
  Replace the mutable root payload/seqlock window with an incarnation-immutable
  v2 descriptor plus a single atomic even-to-even publication cut. Odd is only
  initialization/disable. Preserve future-record and final gate/cut validation;
  do not raise the stable-read bound. Codec v25 prevents mixed runtime ABIs.
  The fixed root no longer carries changing dentry high-water; exact occupancy
  remains in the publisher audit. Root-odd audit fields remain zero; commit
  timing has separate fields. Test paused unrelated future preparation, atomic
  publication, immutable descriptor bytes, and immediate permanent disable.

- Both 1C atomic-root profiles accepted. Capacity-5s had 43 recovered snapshot
  changes, zero root unavailability/exhaustion/metadata fallback, 230 record
  capacity create handoffs and 96 capacity-triggered authoritative unlinks.
- 2C atomic-root capacity-5s completed phases but the first rank failed metadata
  zero-fallback gate with 42 snapshot_changed fallbacks. A stable retired empty
  overlay was incorrectly treated as Retry even though no producer need ever
  rejoin. Prove empty catalog + unchanged gate under admission and return overlay
  NotFound, allowing the existing joint reader to validate the authoritative base.
  Add a mapped post-retirement base-read regression and stage diagnostics for
  actual overlay proof changes; do not weaken concurrent membership checks.

- 2C empty-overlay capacity-5s completed 22 phases but retained 38 metadata
  command fallbacks. All 54 overlay retry diagnostics identified catalog-closing
  before traversal. Separate MembershipChanging from failed validation; release
  admission, await actual catalog reopening and acquire a fresh admission. Waiting
  follows the state with cooperative yields, without a retry budget or fallback.
  A deterministic mapped test holds catalog closing, polls the pending wait,
  verifies reader active/capture are zero, retires membership and proves completion
  plus the stable base result. Export separate membership wait count/time.

- 2C membership-wait reached mdworkbench-delete with zero metadata fallback,
  then stalled. Saved both client/server GDB stacks, perf samples and a sparse
  control-region snapshot. Rank 0 was at MPI barrier; rank 1 awaited opcode 7.
  Coordinator parent 12459 had active lane 13 and orphan pending lane 11, no
  barrier/retirement owner. Lane 11 root remained generation 8589935153 while
  its pending grant was 8589935155: publication never initialized that lane.
  Catalog scan can return MembershipChanging for a closed colliding directory,
  and finish_directory_grant previously leaked the pending reservation on that
  prepublication error. Cancel that exact claim and expose Retry; a deterministic
  mapped collision test preserves a peer publication, proves rejected roots are
  untouched, checks no pending reservation remains, and enters the next barrier.
  Captured the stall, then SIGTERM only the verified owned IO500 rank so the
  normal runner can record rejection and clean up.

- Both 2C grant-cancel profiles accepted. Capacity-5s: 88,558 balanced SQE/CQE,
  zero metadata fallback/exhaustion, 60 actual snapshot retries, 85 catalog
  admission waits (7.379 s summed), and two observed prepublication grant
  cancellations. Bounded: 19,024 balanced SQE/CQE, zero retries/fallback, eight
  catalog waits (22.603 ms summed). 3C capacity-5s is running on the same build.

- Both 3C grant-cancel profiles accepted. Capacity-5s: 105,220 balanced SQE/CQE,
  67 actual snapshot retries, 22 catalog waits (2.255 s summed), zero metadata
  fallback/exhaustion. Bounded: 21,822 balanced SQE/CQE, zero retries/fallback,
  15 catalog waits (52.710 ms summed). Final same-build 1C profiles are running.

- Final same-build 1C profiles also accepted, completing all six cases. Independent
  local audit verifies all source hashes, raw verifier diagnosis, per-rank route,
  receipts/V=D, NUMA negative controls, RAS/cleanup and 2C/3C lease overlap. The
  outer SSH transport closed with rc 255; per-run completion is established from
  the six persisted rc=0 records and final remote post-state, not that transport rc.
  Bounded final verdict and artifact locators are in conclusion.md.
