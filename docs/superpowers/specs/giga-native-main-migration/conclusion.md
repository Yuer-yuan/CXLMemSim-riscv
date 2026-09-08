# Current main on giga: native LLC/NUMA validation

Current main LegoFS passed all six native test cases on giga: 1C1S, 2C1S and
3C1S, each with bounded and capacity-5s profiles and all 22 IO500 phases.
The native experiment exposed and drove fixes to actual protocol state handling.
No stable-read retry limit, workload geometry, or zero-fallback acceptance gate
was increased or relaxed to obtain these results.

## Source and deployment

- Parent baseline: `c828826f47a6a579bc1f1a0f8c2a2c7080461263`.
- LegoFS baseline: `0df6fddfc032f241e509ca0bf819a01adb97e211`.
- LegoFS result commit: `16ea95dc567a03e02f1d2ae711d73674bbba0bd8`; parent scripts/spec are committed with this document.
- Deployment: `${REMOTE_DEPLOYMENT}`, an rsync mirror without `.git`.
- Shared tested build key: `84af7b8d87ce47aae6c50fd13fa5d507b57aee78bb56832b0166798159cd402d`.
- Shared source manifest SHA256: `5ec404c4e5596a4c2bb44d0a6b8875efd55a095cd2858695a13bd0cb7310da8f`.
- IO500/IOR/pfind: `a69cf60cf76538a34c1332bc448838cf9a560a9b`,
  `5fcf0ba995fd92164d50e344597e2d8203298c08`,
  `d08501f9976caf1adabdebfb883d4701dd98fe35`.

Each bundle contains `build-manifest.json`, `source-files.sha256`, launch records,
effective configuration, raw IO500/verifier logs, placement and protocol audits.
Build metadata retains the pre-commit baseline IDs and tested dirty-tree hashes.
`target/results/giga-native/main-migration/committed-source-audit.json` binds those
bytes to the resulting commits; historical manifests are not rewritten.

## Model and matrix

Native x86_64 on the selected test host: one server, one 64 GiB sparse
`MAP_SHARED` regular file on tmpfs, with every resident control, metadata and
payload page verified on CPU-less NUMA1. Current layout-v7 uses 2048 packed
2 MiB segments, batched close, coherent-seal-no-writeback and writer receipts
with the explicit volatile `msync` provider. CXL SQ/CQ uses owned cursors and
`timer_sleep`; the payload follows the current direct mapped path.

| Topology | Client CPUs / LLC IDs | Server CPU / LLC ID |
| --- | --- | --- |
| 1C1S | 1 / 0 | 7 / 1 |
| 2C1S | 1,7 / 0,1 | 13 / 2 |
| 3C1S | 1,7,13 / 0,1,2 | 19 / 3 |

These are client processes on one physical host. LLC IDs were checked at runtime;
there is no resctrl way partition or exclusion of unrelated system activity.

Bundle IDs are `main-<topology>-<profile>-grant-cancel` below
`target/results/giga-native/`, locally and in the deployment mirror.

| Topology | Profile | Phases | SQE = CQE | Snapshot retries / recoveries | Membership waits / summed ms |
| --- | --- | ---: | ---: | ---: | ---: |
| 1c1s | bounded | 22 | 12,645 | 1 / 1 | 0 / 0.000 |
| 1c1s | capacity-5s | 22 | 79,208 | 62 / 62 | 0 / 0.000 |
| 2c1s | bounded | 22 | 19,024 | 0 / 0 | 8 / 22.603 |
| 2c1s | capacity-5s | 22 | 88,558 | 60 / 60 | 85 / 7378.739 |
| 3c1s | bounded | 22 | 21,822 | 0 / 0 | 15 / 52.710 |
| 3c1s | capacity-5s | 22 | 105,220 | 67 / 67 | 22 / 2255.239 |

Every row has `io500_rc=0`, zero direct metadata command fallback, zero stable-read
exhaustion and zero root-unavailable observations. Each workload rank has one TCP
bootstrap. Post-ready filesystem TCP, legacy tarpc, Blob TCP, transport fallback
and unsupported-serving counters are all zero, with SQ/CQ balanced per rank.

The final gates also establish:

- Created payload dependencies = completed dependencies = writer completions =
  accepted receipt items; no pending, rejected or duplicate receipts, and V = D.
- Native writer queued jobs = completed jobs = receipt items = msync jobs;
  failed jobs and zicbom jobs are zero in this explicit native model.
- Whole-region resident pages are exclusively on NUMA1. The NUMA0 negative control
  rejects the same mapping. EDAC CE/UE deltas are zero.
- Directory smoke, extended POSIX classification and current read-cache accounting
  pass. Bounded traces validate real packed-cell grants and publication ordering.
- The 2C/3C bounded `lease-overlap-audit.json` files contain independent owner
  identities with overlapping granted direct-write lease intervals, both published.
- Each server and run root is cleaned up; `final-remote-post-state.json` independently
  confirms no matching workload executable users or final run roots remain.

The paired verifier returns 1 with `[OK] But this is an invalid run!` in all six
bundles: hashes match, while shortened stonewall settings retain `[INVALID]`.
`program_path=accepted` is the runner's functional gate, not official acceptance.
The matrix SSH connection closed with transport rc 255 after the redirected
batch; the six persisted per-run rc/verdict records, logs and final process state
were checked independently. This is recorded in `matrix-driver-transport.json`.

## Protocol changes justified by native failures

1. **Physical record admission.** Mixed create/close/unlink can fill an immutable
   record arena before its inode grant is exhausted. Charge actual records,
   reserve terminal credit for every live direct OFD, and use the existing exact
   generation rollover when create credit is unavailable. An unlink cannot steal
   terminal credit and uses its authoritative CXL barrier when the arena is full.
   The mapped pressure regression proves a held OFD can still close after exhaustion.
2. **Root publication.** A mutable root seqlock allowed a preempted writer to leave
   stable reads spinning on odd and exhausting the existing bound. Root v2 has an
   incarnation-immutable descriptor and one atomic even-to-even visibility cut.
   Future record rejection and final gate/cut validation remain. Codec v25 rejects
   mixed runtime ABIs; a deterministic regression pauses unrelated future preparation
   and proves the stable target needs no root retry.
3. **Overlay and admission states.** A retired empty overlay is a stable base-only
   state, not Retry. Prove empty membership plus unchanged gate and resolve the base.
   A genuinely closing catalog is a distinct admission-pending result: release the
   reader admission, await actual reopening, then perform a fresh validated read.
   The regression holds closing, polls Pending, verifies reader state `(0,0)`, and
   finishes retirement before the next successful base read. Wait counters are
   separate from snapshot retries; failed proofs still fail closed.
4. **Pending grant ownership.** The failed 2C run left parent 12459 with active lane
   13 and orphan pending lane 11. GDB showed no retirement/barrier owner; the pending
   generation had never initialized lane 11. A catalog probe can reject a grant
   before publication when a colliding directory is closed. Cancel that exact pending
   claim on this rejection and return Retry with no orphan owner. The collision
   regression preserves an already-published peer and enters the next mutation
   barrier. The final 2C capacity run exercised two such cancellations and completed.

Actual snapshot changes still cause validated re-reads. Fixed capacity, stable
absence and ownerless pending state are not treated as conditions time can repair.
The native wait totals above also expose remaining coordination cost; this work
makes no claim that the protocol is free of every possible concurrency defect.

Native support additionally uses bounded CXL audit-summary/extent-page responses,
keeps system-sync on the current writer-drain/receipt path, isolates preload setup
to the intended executable, and validates the current POSIX v4, packed-cell and
batched-read evidence rather than restoring the old implementation.

## Diagnostic rates

One capacity-5s sample per topology; these INVALID rates are functional diagnostics.
The fixed-order sweep and background system/VM activity do not establish a stable
performance ranking or a controlled comparison with the previous implementation.

| Topology | Sequential write GiB/s | Sequential read GiB/s | Random 4 KiB write GiB/s |
| --- | ---: | ---: | ---: |
| 1c1s | 0.513 | 1.016 | 0.0153 |
| 2c1s | 0.989 | 1.499 | 0.0161 |
| 3c1s | 1.411 | 1.901 | 0.0153 |

## Validation and evidence locations

All paths in this section are beneath `target/results/giga-native/`.

- `main-migration/final-matrix-audit.json`: same-run gates, shared build/source hashes,
  counts, rates and the bounded lease overlap witnesses for all six bundles.
- `main-migration/validation-commands.txt`: local checks and exact native matrix loop;
  each bundle also preserves the actual server, IO500 and verifier arguments.
- Python: 99 tests across parent wrapper, component runner, rank launcher, NUMA and
  syscall-intercept checks (`local-final-python.log`); the membership evidence update
  also passed all 58 runner tests (`local-membership-runner.log`).
- Local common/client library: 322 + 69 passed, with two pre-existing ignored tests
  (`local-membership-wait.log`). Intercept: 14 passed (`local-intercept-fixed.log`).
- Final native server: 82 passed (`remote-grant-cancel-server.log`); native common/client
  serving integration: 39 + 5 passed (`remote-grant-cancel-contracts.log`). The mapped
  admission/root/record regressions are also covered by the retained earlier native
  `remote-membership-lib.log`. Existing dead-code warnings remain non-failing.
- First failures remain in their original bundles. The orphan-grant stall has
  `main-migration/stall-membership-*.gdb`, perf data, a sparse control-region snapshot,
  decoded shared state and SHA256. The explicit abort record identifies the one
  verified IO500 rank sent SIGTERM after capture; its bundle records rejection and
  cleanup, and is not counted as an accepted run.
- `main-migration/unrelated-work-preservation.json`: original old-worktree parent and
  component tracked patches, and unrelated current parent submodule changes, match
  their initial snapshots. All old results and untracked user documents remain.

## Claim boundary

This establishes the exercised current LegoFS application paths on one host with
NUMA1-backed coherent shared memory. The regular-file provider is volatile CXL RAM;
`msync` and completed writer receipts here do not prove GPF, NAND persistence,
power-loss durability, physical BI/HDM-DB behavior or multiple-host coherence.
IO500 and lease overlap evidence do not substitute for a distributed correctness
checker. No PMU link-utilization measurement or physical bandwidth saturation claim
is made. The official 300-second profile and 10C were not rerun; find-valid and
capacity-20s entrypoints are preserved without claiming new accepted runs for them.
