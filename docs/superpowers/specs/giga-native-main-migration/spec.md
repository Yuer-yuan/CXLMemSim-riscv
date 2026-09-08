# Current main LegoFS on giga native LLC/NUMA

The user authorizes migrating the existing giga experiment onto current main
(`c828826`, LegoFS `0df6fdd`) and validating it remotely. The previous experiment
is the reference design, including its uncommitted 3C1S, capacity-5s and fallback
diagnostics. Preserve both worktrees' unrelated changes and all old results.

## Design and acceptance

Use the old experiment only as a scenario/evidence reference. Implement native
support against current main; retain current direct-mutation, writer persistence,
reader-grace progress and layout-v7 behavior. Historical three-way candidates
are reviewed/adapted, not accepted as the product design.
Add native build/run scripts and their tests to parent main. Product changes and
tests belong in its selected LegoFS submodule, as requested by the user.

Deploy to the user-selected `${REMOTE_DEPLOYMENT}` mirror (no .git).
Resolve hardware again: x86_64, CPU-less NUMA1, one 64 GiB sparse MAP_SHARED
regular file on /dev/shm. CPUs for client/server are 1/7, 1,7/13, 1,7,13/19;
verify distinct actual LLC IDs. Preserve remote old bundles before deployment.
Use layout-v7, 2048 packed 2 MiB segments, batched closes, coherent-seal-no-writeback
and writer-receipt with the native msync functional provider. Keep protocol
ordering and receipt/grace invariants even when fast media exposes stalls.
Record-capacity admission must charge every mutation, reserve terminals for live
OFDs, and request generation rollover before publication; waiting for apply is
not evidence that immutable physical records can be reused. Root publication must
not make unrelated stable reads depend on scheduling inside an odd seqlock
window: use an immutable incarnation descriptor and atomic visibility cut,
retaining future-version rejection and final cut/gate validation.
A stable empty overlay resolves against the base. A closing membership returns a
distinct admission-pending state; its asynchronous wait owns no reader admission
or mutation ticket, and only actual catalog reopening permits the next lookup.
Keep membership wait counters separate from stable-read validation retries.
A catalog admission rejection before publication must cancel its exact pending
grant reservation; no barrier may wait on a grant whose publishing actor has
already returned. Preserve already-published peer membership during cancellation.

Run native contract tests, directory smoke and all 22 IO500 phases in bounded
and capacity-5s profiles for 1C1S, 2C1S and 3C1S. Require one TCP bootstrap per
rank; balanced CXL SQ/CQ; zero post-ready TCP, legacy, blob and transport
fallback; direct metadata accounting including reason totals; NUMA1 residency,
NUMA0 negative control over all resident region pages (control, metadata and
payload), no EDAC delta, owned process and mapping cleanup. Also require created
payload dependencies to equal completed writer dependencies, no pending/rejected/
duplicate receipts and a closed V/D prefix. Preserve first failures; do not
disable fast paths or relax gates to make a workload pass.
Retain find-valid, capacity-20s and official profile entrypoints. Do not repeat
the known capacity-exhausting 300s run. Shortened runs are expected INVALID:
matching hashes and runner acceptance do not mean official verifier acceptance.

This proves a same-host volatile shared-memory application route only, not
multi-host coherence, physical BI/HDM-DB, GPF, NAND durability or link utilization.
PMU integration, 10C diagnosis and performance optimization are outside this migration.

## Evidence

Raw migration evidence: `target/results/giga-native/main-migration/`.
Existing bundles remain in the previous worktree and the remote result root.
Execution state is in this spec's `plan.md`; final verdict goes in `conclusion.md`.
