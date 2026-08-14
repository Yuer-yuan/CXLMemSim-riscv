# Legofs Two-RISC-V Type-3 MESI Back-Invalidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and run one reproducible proof in which two overlapping `qemu-system-riscv64 -M sifive_u` guests run Legofs over separate CXL Type-3 endpoints and a real lifecycle-direct write causes a dirty MESI-v2 back-invalidation before Legofs commits the data.

**Architecture:** Keep the existing SiFive U ACPI/U-Boot/Linux boot path and add a per-Type-3 protocol-v2 endpoint cache that talks to one authoritative CXLMemSim TCP coherence domain. Boot a Legofs server in node0 and `badfs-bench` in node1, use each guest's CXL DAX character device for strict direct I/O, and correlate Legofs direct-map/lifecycle events with benchmark-scoped CXLMemSim snoop events in a fail-closed host runner.

**Tech Stack:** Git submodules, QEMU C/GLib/QOM, CXLMemSim C++20/CMake/GoogleTest, Legofs Rust/Tokio/Tarpc, RISC-V GNU toolchain, Linux CXL/DEV_DAX, OpenSBI, U-Boot EFI, freestanding C PID 1, Python 3 `unittest`, QEMU TCG, TCP protocol v2, ext2/debugfs, JSONL evidence.

---

## Working directory, branch, and file structure

Execute every superproject command from:

```bash
cd /root/cxl-u-boot/CXLMemSim-riscv
```

Do not use `/root/cxl-u-boot` as a Git repository. It is only the parent
directory. Preserve the existing `main` work and create an isolated worktree
before implementation:

```bash
git worktree add ../CXLMemSim-riscv-legofs -b codex/legofs-type3-mesi-proof
cd ../CXLMemSim-riscv-legofs
git submodule update --init --recursive
```

Component branches created by this plan:

- QEMU: `codex/sifive-u-type3-mesi-v2`, based on
  `81cd7ad9a5e14470427c8ebafeccff4f52e555b4`.
- CXLMemSim: `codex/riscv-legofs-coherence-trace`, based on
  `716c16c9efc7a733006d0772f8c6c4bb055f7b15`.
- Legofs: `codex/riscv-type3-coherence-proof`, based on
  `96f733940251d6484dad0ba2cfbe99dcf5259776`.

The implementation changes these focused units:

- `.gitmodules`: adds the pinned Legofs component.
- `components/qemu/include/hw/cxl/cxl_memsim_v2.h` and
  `components/qemu/hw/cxl/cxl_memsim_v2.c`: exact reusable v2 client/cache
  imported from the existing Type-2 coherence branch.
- `components/qemu/include/hw/cxl/cxl_type3_memsim_v2.h` and
  `components/qemu/hw/cxl/cxl_type3_memsim_v2.c`: Type-3-specific validated
  configuration, lifecycle, and fail-closed access adapter.
- `components/qemu/include/hw/cxl/cxl_device.h` and
  `components/qemu/hw/mem/cxl_type3.c`: per-device state, QOM properties,
  realize/exit hooks, and read/write delegation.
- `components/qemu/tests/unit/test-cxl-type3-memsim-v2.c`: adapter contract
  tests using a socket-pair protocol peer.
- `components/cxlmemsim/include/coherence_trace_v2.h` and
  `components/cxlmemsim/src/coherence_trace_v2.cpp`: synchronized JSONL event
  sink and counter snapshot.
- `components/cxlmemsim/include/coherence_server_v2.h` and
  `components/cxlmemsim/src/coherence_server_v2.cpp`: event recording at
  registration, request, snoop-send, ACK, commit, and error boundaries.
- `components/cxlmemsim/src/main_server.cc`: `--coherence-v2-trace` CLI and
  final machine-readable summary.
- `components/legofs/badfs-common/src/lifecycle.rs`: lifecycle physical-range
  fields and optional console JSON mirror.
- `components/legofs/badfs-client/src/lib.rs`: optional console mirror of
  direct-map JSON.
- `configs/linux-cxl.config`: built-in networking and CXL devdax requirements.
- `guest/legofs_node_init.c`: role-aware PID 1, network setup, DAX discovery,
  strict Legofs environment, server/client launch, and proof markers.
- `scripts/build_legofs_type3.sh`: Legofs cross-build, dedicated initramfs,
  Linux image, ext2 payload image, and manifest.
- `scripts/legofs_type3_2node.py`: two-QEMU/server ownership, U-Boot console
  automation, overlap enforcement, trace snapshots, proof validation, and
  atomic result publication.
- `run-legofs-type3.sh`: the single user-facing build-and-run entry point.
- `tests/test_legofs_sources.py`, `tests/test_legofs_build_contract.py`,
  `tests/test_legofs_runtime.py`, and `tests/test_legofs_evidence.py`: offline
  source, command, parser, and acceptance tests.
- `README.md`: exact build/run commands and the functional-model claim
  boundary.

Generated output stays below `out/legofs-type3/` and remains ignored.

### Task 1: Pin the approved source graph in an isolated worktree

**Files:**
- Modify: `.gitmodules`
- Create gitlink: `components/legofs`
- Modify gitlink: `components/cxlmemsim`
- Create: `tests/test_legofs_sources.py`

- [ ] **Step 1: Enter the isolated worktree and create component branches**

Run:

```bash
cd /root/cxl-u-boot/CXLMemSim-riscv-legofs
git -C components/qemu switch -c codex/sifive-u-type3-mesi-v2 \
  81cd7ad9a5e14470427c8ebafeccff4f52e555b4
git -C components/cxlmemsim fetch origin \
  codex/type2-hw-cc-fullsystem-20260809
git -C components/cxlmemsim switch -c codex/riscv-legofs-coherence-trace \
  716c16c9efc7a733006d0772f8c6c4bb055f7b15
git submodule add https://github.com/Zettai-US/legofs.git components/legofs
git -C components/legofs switch -c codex/riscv-type3-coherence-proof \
  96f733940251d6484dad0ba2cfbe99dcf5259776
```

Expected: all three component worktrees are on the named local branches and
the two approved external pins resolve exactly.

- [ ] **Step 2: Write the failing source-pin tests**

Create `tests/test_legofs_sources.py`:

```python
import pathlib
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def head(component):
    return subprocess.run(
        ["git", "-C", str(ROOT / "components" / component), "rev-parse", "HEAD"],
        check=True, text=True, capture_output=True,
    ).stdout.strip()


class LegofsSourceTest(unittest.TestCase):
    def test_legofs_base_is_approved_commit(self):
        history = subprocess.run(
            ["git", "-C", str(ROOT / "components/legofs"), "merge-base",
             "HEAD", "96f733940251d6484dad0ba2cfbe99dcf5259776"],
            check=True, text=True, capture_output=True,
        ).stdout.strip()
        self.assertEqual(history, "96f733940251d6484dad0ba2cfbe99dcf5259776")

    def test_cxlmemsim_base_is_approved_commit(self):
        history = subprocess.run(
            ["git", "-C", str(ROOT / "components/cxlmemsim"), "merge-base",
             "HEAD", "716c16c9efc7a733006d0772f8c6c4bb055f7b15"],
            check=True, text=True, capture_output=True,
        ).stdout.strip()
        self.assertEqual(history, "716c16c9efc7a733006d0772f8c6c4bb055f7b15")

    def test_gitmodules_uses_approved_legofs_remote(self):
        text = (ROOT / ".gitmodules").read_text()
        self.assertIn("path = components/legofs", text)
        self.assertIn("url = https://github.com/Zettai-US/legofs.git", text)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run the source tests**

Run:

```bash
python3 -m unittest -v tests/test_legofs_sources.py
```

Expected: three tests pass. They guard ancestry rather than final HEAD because
later tasks intentionally add component commits.

- [ ] **Step 4: Commit the source graph before component code changes**

Run:

```bash
git add .gitmodules components/legofs components/cxlmemsim \
  tests/test_legofs_sources.py
git commit -m "chore: pin Legofs and MESI v2 sources"
```

Expected: the superproject commit records only gitlinks, `.gitmodules`, and
the source contract test.

### Task 2: Import and verify the reusable QEMU MESI-v2 endpoint cache

**Files:**
- Create: `components/qemu/include/hw/cxl/cxl_memsim_v2.h`
- Create: `components/qemu/hw/cxl/cxl_memsim_v2.c`
- Create: `components/qemu/tests/unit/test-cxl-memsim-v2.c`
- Create: `components/qemu/tests/unit/test-cxl-memsim-v2-cache.c`
- Modify: `components/qemu/hw/cxl/meson.build`
- Modify: `components/qemu/tests/unit/meson.build`

- [ ] **Step 1: Confirm the import source is the reviewed v2 branch**

Run:

```bash
git -C components/qemu fetch origin \
  codex/type2-hw-cc-fullsystem-qemu-20260809
git -C components/qemu rev-parse \
  origin/codex/type2-hw-cc-fullsystem-qemu-20260809
```

Expected: `b1216be2` or a descendant whose versions of the four imported files
are unchanged. Record the full object ID in the commit message.

- [ ] **Step 2: Import the protocol client and its tests exactly**

Run:

```bash
git -C components/qemu checkout \
  origin/codex/type2-hw-cc-fullsystem-qemu-20260809 -- \
  include/hw/cxl/cxl_memsim_v2.h \
  hw/cxl/cxl_memsim_v2.c \
  tests/unit/test-cxl-memsim-v2.c \
  tests/unit/test-cxl-memsim-v2-cache.c
```

Then add `cxl_memsim_v2.c` to the existing `CONFIG_CXL` source list in
`hw/cxl/meson.build`, and add both unit executables to
`tests/unit/meson.build` with `qemuutil`, `qom`, and the CXL source dependency
used by adjacent CXL unit tests.

- [ ] **Step 3: Configure and run only the imported unit tests**

Run:

```bash
mkdir -p out/legofs-type3/build/qemu
cd out/legofs-type3/build/qemu
../../../../components/qemu/configure \
  --target-list=riscv64-softmmu --disable-docs --disable-werror
ninja test-cxl-memsim-v2 test-cxl-memsim-v2-cache
meson test --print-errorlogs cxl-memsim-v2 cxl-memsim-v2-cache
```

Expected: frame encode/decode, registration, write-back retention, dirty
downgrade/invalidation, eviction, flush, fence, and failure tests all pass.

- [ ] **Step 4: Commit the reusable QEMU client**

Run:

```bash
git -C components/qemu add include/hw/cxl/cxl_memsim_v2.h \
  hw/cxl/cxl_memsim_v2.c hw/cxl/meson.build \
  tests/unit/test-cxl-memsim-v2.c \
  tests/unit/test-cxl-memsim-v2-cache.c tests/unit/meson.build
git -C components/qemu commit -m \
  "cxl: import protocol v2 endpoint cache for Type 3"
```

### Task 3: Connect each QEMU Type-3 device to one fail-closed v2 endpoint

**Files:**
- Create: `components/qemu/include/hw/cxl/cxl_type3_memsim_v2.h`
- Create: `components/qemu/hw/cxl/cxl_type3_memsim_v2.c`
- Modify: `components/qemu/include/hw/cxl/cxl_device.h`
- Modify: `components/qemu/hw/mem/cxl_type3.c`
- Modify: `components/qemu/hw/cxl/meson.build`
- Create: `components/qemu/tests/unit/test-cxl-type3-memsim-v2.c`
- Modify: `components/qemu/tests/unit/meson.build`

- [ ] **Step 1: Write failing configuration and routing tests**

Create `tests/unit/test-cxl-type3-memsim-v2.c` around the public adapter API:

```c
static void test_config_rejects_invalid_host(void)
{
    CxlType3MemsimV2Config cfg = cxl_type3_memsim_v2_default_config();
    g_autoptr(Error) err = NULL;
    cfg.enabled = true;
    cfg.server_host = "127.0.0.1";
    cfg.server_port = 9300;
    cfg.host_id = CXL_MEMSIM_V2_MAX_ENDPOINTS;
    g_assert_false(cxl_type3_memsim_v2_validate(&cfg, &err));
    g_assert_nonnull(err);
}

static void test_config_requires_write_back(void)
{
    CxlType3MemsimV2Config cfg = cxl_type3_memsim_v2_default_config();
    g_autoptr(Error) err = NULL;
    cfg.enabled = true;
    cfg.server_host = "127.0.0.1";
    cfg.server_port = 9300;
    cfg.write_through = true;
    g_assert_false(cxl_type3_memsim_v2_validate(&cfg, &err));
    g_assert_nonnull(err);
}

static void test_failed_v2_access_returns_memtx_error(void)
{
    CxlType3MemsimV2 state = { .enabled = true, .client = NULL };
    uint64_t value = 0;
    g_assert_cmpint(cxl_type3_memsim_v2_read(&state, 0, &value, 8),
                    ==, MEMTX_ERROR);
    g_assert_cmpint(cxl_type3_memsim_v2_write(&state, 0, 1, 8),
                    ==, MEMTX_ERROR);
}
```

Add a socket-pair fake server test that completes `REGISTER`, returns a line
for `GETS`, accepts `GETM`, and asserts that one 8-byte read and one 8-byte
write produce protocol traffic instead of touching a local `MemoryRegion`.

- [ ] **Step 2: Run the new test and verify RED**

Run:

```bash
ninja -C out/legofs-type3/build/qemu test-cxl-type3-memsim-v2
```

Expected: compilation fails because `cxl_type3_memsim_v2.h` does not exist.

- [ ] **Step 3: Define the complete Type-3 adapter contract**

Create `include/hw/cxl/cxl_type3_memsim_v2.h` with this public shape:

```c
#ifndef CXL_TYPE3_MEMSIM_V2_H
#define CXL_TYPE3_MEMSIM_V2_H

#include "exec/memattrs.h"
#include "hw/cxl/cxl_memsim_v2.h"

typedef struct CxlType3MemsimV2Config {
    bool enabled;
    const char *server_host;
    uint16_t server_port;
    uint16_t host_id;
    uint32_t cache_capacity;
    uint16_t cache_ways;
    uint32_t timeout_ms;
    bool write_through;
} CxlType3MemsimV2Config;

typedef struct CxlType3MemsimV2 {
    CxlType3MemsimV2Config config;
    CxlMemsimV2Client *client;
    bool enabled;
} CxlType3MemsimV2;

CxlType3MemsimV2Config cxl_type3_memsim_v2_default_config(void);
bool cxl_type3_memsim_v2_validate(const CxlType3MemsimV2Config *config,
                                  Error **errp);
bool cxl_type3_memsim_v2_realize(CxlType3MemsimV2 *state, Error **errp);
void cxl_type3_memsim_v2_unrealize(CxlType3MemsimV2 *state);
MemTxResult cxl_type3_memsim_v2_read(CxlType3MemsimV2 *state,
                                     hwaddr dpa, uint64_t *value,
                                     unsigned size);
MemTxResult cxl_type3_memsim_v2_write(CxlType3MemsimV2 *state,
                                      hwaddr dpa, uint64_t value,
                                      unsigned size);
#endif
```

The defaults are host `127.0.0.1`, port `9300`, cache capacity `1024` lines,
8 ways, timeout `5000` ms, and write-back. Validation accepts access sizes
1/2/4/8, rejects host IDs outside `[0,63]`, zero port/timeout/cache values,
non-power-of-two cache geometry, capacity not divisible by ways, and
write-through for this proof.

- [ ] **Step 4: Implement connection, access, and fail-closed behavior**

In `hw/cxl/cxl_type3_memsim_v2.c`, create the client with exactly
`CXL_MEMSIM_V2_CAP_MODEL_SNOOP` behavior provided by the imported cache, call
`cxl_memsim_v2_client_connect()`, set `CXL_MEMSIM_V2_WRITE_BACK`, and emit one
registration line containing host and session IDs. `read()` and `write()` must
return `MEMTX_ERROR` when the state is enabled but disconnected or when
`cxl_memsim_v2_load/store()` returns false. They must never call
`address_space_read/write()`.

The error path must be structurally equivalent to:

```c
if (!state->enabled || !state->client) {
    return MEMTX_ERROR;
}
if (!cxl_memsim_v2_load(state->client, dpa, size, value,
                        state->config.timeout_ms, &local_err)) {
    error_report_err(local_err);
    return MEMTX_ERROR;
}
return MEMTX_OK;
```

- [ ] **Step 5: Add per-device QOM state and properties**

Add `CxlType3MemsimV2 memsim_v2` plus owned string
`char *memsim_v2_server_host` to `CXLType3Dev`. Add these exact QOM
properties to `ct3_props`:

```text
coherence-v2                 bool, default false
cxlmemsim-addr               string, default 127.0.0.1
cxlmemsim-port               uint16, default 9300
coherence-v2-host-id         uint16, default 0
coherence-v2-cache-capacity  uint32, default 1024
coherence-v2-cache-ways      uint16, default 8
coherence-v2-timeout-ms      uint32, default 5000
coherence-v2-write-through   bool, default false
```

In `ct3_realize()`, validate and connect before guest execution. In
`ct3_exit()`, free the v2 client. In `cxl_type3_read()` and
`cxl_type3_write()`, delegate immediately after DPA translation:

```c
if (ct3d->memsim_v2.enabled) {
    return cxl_type3_memsim_v2_read(&ct3d->memsim_v2, dpa_offset,
                                    data, size);
}
```

Use the analogous write call. Keep the existing legacy SHM/TCP path only when
`coherence-v2=off`.

- [ ] **Step 6: Run focused and existing CXL tests**

Run:

```bash
ninja -C out/legofs-type3/build/qemu \
  test-cxl-type3-memsim-v2 test-cxl-memsim-v2-cache \
  qemu-system-riscv64
meson test -C out/legofs-type3/build/qemu --print-errorlogs \
  cxl-type3-memsim-v2 cxl-memsim-v2-cache
python3 -m unittest -v tests/test_runtime.py
```

Expected: all tests pass; the existing command test still begins with
`qemu-system-riscv64`, `-M`, `sifive_u`.

- [ ] **Step 7: Commit the Type-3 integration**

Run:

```bash
git -C components/qemu add include/hw/cxl/cxl_device.h \
  include/hw/cxl/cxl_type3_memsim_v2.h hw/cxl/cxl_type3_memsim_v2.c \
  hw/cxl/meson.build hw/mem/cxl_type3.c \
  tests/unit/test-cxl-type3-memsim-v2.c tests/unit/meson.build
git -C components/qemu commit -m \
  "cxl/type3: route guest memory through MESI v2"
```

### Task 4: Add machine-readable MESI-v2 counters and transaction traces

**Files:**
- Create: `components/cxlmemsim/include/coherence_trace_v2.h`
- Create: `components/cxlmemsim/src/coherence_trace_v2.cpp`
- Modify: `components/cxlmemsim/include/coherence_server_v2.h`
- Modify: `components/cxlmemsim/src/coherence_server_v2.cpp`
- Modify: `components/cxlmemsim/src/main_server.cc`
- Modify: `components/cxlmemsim/CMakeLists.txt`
- Create: `components/cxlmemsim/tests/test_coherence_trace_v2.cpp`
- Modify: `components/cxlmemsim/tests/test_coherence_server_v2.cpp`

- [ ] **Step 1: Write failing trace-schema tests**

Create a temporary trace, record one registration, one `GETM`, one
`SNP_DATA_INV`, and its dirty model ACK. Parse each JSONL line with the
project's JSON dependency and assert these exact keys:

```text
schema_version,event,monotonic_ns,opcode,src_host,dst_host,session_id,
request_id,snoop_id,line_address,epoch,payload_len,status,ack_strength,
dirty_data
```

Assert the snapshot object contains:

```text
registrations,gets,getm,upgrade,puts,putm,snp_inv,snp_downgrade,
snp_data_inv,snp_data_downgrade,host_fence,model_acks,native_acks,
dirty_data_completions,timeouts,protocol_errors,delivery_failures,
server_copy_failures
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
cmake -S components/cxlmemsim -B out/legofs-type3/build/cxlmemsim \
  -DCMAKE_BUILD_TYPE=RelWithDebInfo -DBUILD_TESTING=ON
cmake --build out/legofs-type3/build/cxlmemsim --parallel
ctest --test-dir out/legofs-type3/build/cxlmemsim -R \
  'coherence_trace_v2|coherence_server_v2' --output-on-failure
```

Expected: the new trace test is absent or fails to compile.

- [ ] **Step 3: Implement the synchronized trace sink**

Define `CoherenceV2Counters` as atomics for the keys above. Define
`CoherenceTraceV2::record(const CoherenceTraceEvent&)` to serialize one JSON
object under a mutex, append `\n`, flush, and throw on write failure. Use
`std::chrono::steady_clock` nanoseconds for `monotonic_ns`; all records are
from the single host server clock and therefore directly orderable.

Expose:

```cpp
struct CoherenceV2Snapshot { /* one uint64_t per counter key */ };

class CoherenceTraceV2 final {
public:
    explicit CoherenceTraceV2(const std::filesystem::path &path);
    void record(const CoherenceTraceEvent &event);
    CoherenceV2Snapshot snapshot() const noexcept;
    std::string snapshotJson() const;
};
```

Increment a counter in the same critical section that writes its event so a
trace byte-offset snapshot and counter snapshot cannot disagree.

- [ ] **Step 4: Instrument protocol boundaries**

Pass an optional shared `CoherenceTraceV2` into `CoherenceServerV2`. Record:

- successful and rejected registration in the `Register` dispatch branch;
- accepted `GETS`, `GETM`, `UPGRADE`, `PUTS`, and `PUTM` before engine dispatch;
- every unsolicited snoop in `sendToHost()` before sender invocation;
- every `SNOOP_ACK` with its strength, payload length, and dirty-data flag;
- completion after the engine commits dirty snoop bytes into
  `CoherenceMemoryBackend`;
- timeout, protocol rejection, delivery failure, and copy failure at the
  exact return site.

For a dirty `SNP_DATA_INV` ACK, `dirty_data` is true only when payload length
is 64, ACK strength is `MODEL`, status is `OK`, and the engine accepted the
payload for the same `snoop_id`.

- [ ] **Step 5: Add the server CLI and final summary**

Add:

```text
--coherence-v2-trace <absolute-or-relative-path>
```

Reject it unless `--coherence-v2=true` and `--comm-mode=tcp`. Create/truncate
the trace before listening. On orderly shutdown print exactly one line:

```text
COHERENCE_V2_STATS_JSON {json-object}
```

The JSON object is `snapshotJson()` and includes active host/session bindings.
The runner takes its pre-benchmark snapshot by recording the current JSONL
byte offset after both readiness markers and computes all acceptance deltas
from complete records after that offset.

- [ ] **Step 6: Run the entire CXLMemSim v2 test subset**

Run:

```bash
cmake --build out/legofs-type3/build/cxlmemsim --parallel
ctest --test-dir out/legofs-type3/build/cxlmemsim \
  -R 'protocol_v2|directory|endpoint|mesi|coherence|tcp' \
  --output-on-failure
```

Expected: all matched tests pass, including dirty-data ACK and duplicate-host
rejection tests.

- [ ] **Step 7: Commit the telemetry**

Run:

```bash
git -C components/cxlmemsim add include/coherence_trace_v2.h \
  include/coherence_server_v2.h src/coherence_trace_v2.cpp \
  src/coherence_server_v2.cpp src/main_server.cc CMakeLists.txt \
  tests/test_coherence_trace_v2.cpp tests/test_coherence_server_v2.cpp
git -C components/cxlmemsim commit -m \
  "coherence: trace MESI v2 snoop completion evidence"
```

### Task 5: Expose Legofs direct and lifecycle events on the guest console

**Files:**
- Modify: `components/legofs/badfs-common/src/lifecycle.rs`
- Modify: `components/legofs/badfs-client/src/lib.rs`

- [ ] **Step 1: Write failing Legofs trace tests**

In `badfs-common/src/lifecycle.rs`, add a test that opens a temporary
`LifecycleTrace` with console mirroring enabled, emits one event, and asserts
the encoded object includes `mapping_offset`, `mapping_length`, and the
`store_direct_begin`/`store_direct_success` event names.

In `badfs-client/src/lib.rs`, add a test for a pure helper:

```rust
#[test]
fn direct_trace_console_line_is_parseable() {
    let json = serde_json::json!({"op_id": 9, "offset": 4096, "length": 4096});
    let line = prefixed_trace_line("BADFS_DIRECT_MAP_TRACE_JSON", &json).unwrap();
    assert!(line.starts_with("BADFS_DIRECT_MAP_TRACE_JSON "));
    serde_json::from_str::<serde_json::Value>(line.split_once(' ').unwrap().1)
        .unwrap();
}
```

- [ ] **Step 2: Run the focused Rust tests and verify RED**

Run:

```bash
cargo test --manifest-path components/legofs/Cargo.toml \
  -p badfs-common lifecycle_trace -- --nocapture
cargo test --manifest-path components/legofs/Cargo.toml \
  -p badfs-client direct_trace_console_line -- --nocapture
```

Expected: the new helper/fields do not compile yet.

- [ ] **Step 3: Add physical-range lifecycle fields**

Extend `LifecycleTraceEvent` with backward-compatible defaults:

```rust
#[serde(default)]
pub mapping_offset: u64,
#[serde(default)]
pub mapping_length: u64,
```

At `store_direct()`, call the existing
`self.backend.direct_mapping(lease.extent_id, 0)` before the checksum and
validate it with the same alignment/size checks as `direct_grant()`. Emit
`store_direct_begin` immediately before `candidate_checksum()` and
`store_direct_success` after the published result, both with that exact
`mapping.offset` and `mapping.length`. Use the same `op_id`, `extent_id`, and
generation as the client grant. A missing or changed mapping returns
`Error::Io`; it does not omit the address evidence.

- [ ] **Step 4: Add opt-in console mirroring without weakening file traces**

Parse these exact booleans once when each trace object is created:

```text
BADFS_LIFECYCLE_TRACE_STDOUT=1
BADFS_CXL_DIRECT_TRACE_STDOUT=1
```

After a successful file append/flush, write the already encoded JSON without
re-serializing and explicitly flush stdout before returning:

```rust
if self.stdout {
    let mut stdout = std::io::stdout().lock();
    writeln!(stdout, "BADFS_LIFECYCLE_TRACE_JSON {}",
             std::str::from_utf8(&encoded_without_newline).map_err(|_| Error::Io)?)
        .map_err(|_| Error::Io)?;
    stdout.flush().map_err(|_| Error::Io)?;
}
```

Use `BADFS_DIRECT_MAP_TRACE_JSON` for client direct-map events. File write
failure remains fatal under strict-direct policy; console output never
substitutes for the file trace.

- [ ] **Step 5: Run Legofs core and benchmark tests**

Run:

```bash
cargo test --manifest-path components/legofs/Cargo.toml \
  -p badfs-common -p badfs-client -p badfs-server -p badfs-bench
```

Expected: all tests pass and existing trace files remain schema-compatible.

- [ ] **Step 6: Commit Legofs evidence support**

Run:

```bash
git -C components/legofs add badfs-common/src/lifecycle.rs \
  badfs-client/src/lib.rs
git -C components/legofs commit -m \
  "trace: correlate lifecycle direct DAX operations"
```

### Task 6: Build a dedicated RISC-V Legofs guest with CXL devdax

**Files:**
- Modify: `configs/linux-cxl.config`
- Create: `guest/legofs_node_init.c`
- Create: `tests/test_legofs_build_contract.py`

- [ ] **Step 1: Write failing kernel and PID-1 contract tests**

Create `tests/test_legofs_build_contract.py`:

```python
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class LegofsBuildContractTest(unittest.TestCase):
    def test_kernel_fragment_has_built_in_devdax_and_network(self):
        config = (ROOT / "configs/linux-cxl.config").read_text().splitlines()
        required = {
            "CONFIG_DAX=y", "CONFIG_DEV_DAX=y", "CONFIG_DEV_DAX_CXL=y",
            "CONFIG_NET=y", "CONFIG_INET=y", "CONFIG_UNIX=y",
            "CONFIG_PACKET=y", "CONFIG_VIRTIO_NET=y",
        }
        self.assertTrue(required.issubset(set(config)))

    def test_init_has_role_dax_and_strict_markers(self):
        source = (ROOT / "guest/legofs_node_init.c").read_text()
        for marker in (
            "legofs.role=", "LEG_OFS_CXL_READY", "LEG_OFS_SERVER_READY",
            "LEG_OFS_BENCHMARK_BEGIN", "LEG_OFS_BENCHMARK_PASS",
            "BADFS_LIFECYCLE_DIRECT_REQUIRED=1",
            "BADFS_LIFECYCLE_DIRECT_READ_REQUIRED=1",
        ):
            self.assertIn(marker, source)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
python3 -m unittest -v tests/test_legofs_build_contract.py
```

Expected: missing devdax options and guest source failures.

- [ ] **Step 3: Enable all required drivers as built-ins**

Append or replace conflicting values in `configs/linux-cxl.config`:

```text
CONFIG_TRANSPARENT_HUGEPAGE=y
CONFIG_DAX=y
CONFIG_DEV_DAX=y
CONFIG_DEV_DAX_CXL=y
CONFIG_NET=y
CONFIG_INET=y
CONFIG_UNIX=y
CONFIG_PACKET=y
CONFIG_VIRTIO_NET=y
CONFIG_IP_PNP=y
CONFIG_IP_PNP_DHCP=y
```

The build task later checks the merged `.config`; fragment presence alone is
not acceptance.

- [ ] **Step 4: Implement the role-aware freestanding PID 1**

`guest/legofs_node_init.c` must:

1. mount `/proc`, `/sys`, `/dev`, `/tmp`, and `/mnt`;
2. mount the read-only ext2 payload at `/mnt`;
3. bring up `eth0` as `10.0.2.15/24` with gateway `10.0.2.2` using socket
   ioctls and `SIOCADDRT`;
4. parse `legofs.role=node0|node1`, `legofs.server_port`, and
   `legofs.bytes` from `/proc/cmdline`;
5. scan `/sys/class/dax/dax*`, create the character node from sysfs `dev`, and
   reject zero or multiple CXL devdax devices;
6. require `/sys/bus/cxl/devices/mem0`, a committed CXL region/decoder under
   `/sys/bus/cxl/devices`, and `/dev/daxX.Y` before printing
   `LEG_OFS_CXL_READY` (the runner separately captures U-Boot `cxl list`);
7. export the discovered character path as `BADFS_LIFECYCLE_DEVICE`, print
   its locally derived `FabricRegionId`, device size, major, and minor;
8. set the strict environment below with `execve()`; and
9. power off only after node1 prints final counters and a pass/fail marker.

Common environment:

```text
BADFS_POSIX_DATA_PATH=lifecycle
BADFS_LIFECYCLE_BLOB=1
BADFS_LIFECYCLE_DIRECT_FINAL=1
BADFS_LIFECYCLE_DIRECT_REQUIRED=1
BADFS_LIFECYCLE_DIRECT_READ=1
BADFS_LIFECYCLE_DIRECT_READ_REQUIRED=1
BADFS_LIFECYCLE_DEVICE_REQUIRED=1
BADFS_CXL_MAP_ALIGNMENT=4096
BADFS_LIFECYCLE_TRACE=/tmp/lifecycle.jsonl
BADFS_LIFECYCLE_TRACE_STDOUT=1
BADFS_CXL_DIRECT_TRACE=/tmp/direct.jsonl
BADFS_CXL_DIRECT_TRACE_STDOUT=1
BADFS_LIFECYCLE_POOL_SIZE=268435456
BADFS_LIFECYCLE_MAX_EXTENTS=128
RUST_LOG=info
```

Node0 adds `BADFS_SERVER_ADDR=0.0.0.0:3345`,
`BADFS_DATA_DIR=/tmp/badfs-data`, forks `/mnt/badfs-server`, polls
`127.0.0.1:3345`, and prints `LEG_OFS_SERVER_READY` only after the connect
probe succeeds. It then remains PID 1 and reaps the server. Node1 adds
`BADFS_SERVERS=10.0.2.2:<forwarded-port>`, `BADFS_BASE_PATH=/badfs`,
`BADFS_BENCH_MODE=workload`, `BADFS_BENCH_FILE_SIZE=<bounded-size>`,
`BADFS_BENCH_BLOCK_SIZE=4096`, and `BADFS_BENCH_ITERATIONS=1`, then executes
`/mnt/badfs-bench` with no arguments. Before the workload fork, node1 prints
`LEG_OFS_CLIENT_READY` and blocks on `/dev/hvc0` until the runner sends the
exact line `LEG_OFS_RUN`; this lets the runner freeze the pre-benchmark trace
offset. After the workload exits successfully, execute the same binary again
with `BADFS_BENCH_MODE=inspect` so the lifecycle audit is printed.
`badfs-bench` is environment-driven; passing positional workload or size
arguments is prohibited because the current binary ignores them.

- [ ] **Step 5: Compile the PID 1 natively for syntax and run tests**

Run:

```bash
gcc -std=c11 -Wall -Wextra -Werror -fsyntax-only guest/legofs_node_init.c
python3 -m unittest -v tests/test_legofs_build_contract.py
```

Expected: both commands pass.

- [ ] **Step 6: Commit the guest contract**

Run:

```bash
git add configs/linux-cxl.config guest/legofs_node_init.c \
  tests/test_legofs_build_contract.py
git commit -m "guest: add strict Legofs Type 3 node image"
```

### Task 7: Add the reproducible Legofs/guest build pipeline

**Files:**
- Create: `scripts/build_legofs_type3.sh`
- Create: `run-legofs-type3.sh`
- Modify: `scripts/write_manifest.py`
- Modify: `tests/test_legofs_build_contract.py`

- [ ] **Step 1: Add failing CLI and artifact-contract tests**

Test these behaviors with subprocesses and temporary fake commands:

```text
./run-legofs-type3.sh --help                    exits 0
./run-legofs-type3.sh --build-only --run-only   exits nonzero
./run-legofs-type3.sh --bytes 0                 exits nonzero
./run-legofs-type3.sh --bytes 65536             accepts the value
```

Also assert the build script names these manifest artifacts:

```text
qemu,opensbi,u_boot,linux_legofs,legofs_disk,badfs_server,badfs_bench,
cxlmemsim_server
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
python3 -m unittest -v tests/test_legofs_build_contract.py
```

Expected: CLI/build-script tests fail because both scripts are absent.

- [ ] **Step 3: Implement the top-level entry point**

`run-legofs-type3.sh` accepts only `--build-only`, `--run-only`, `--jobs N`,
`--bytes N`, `--timeout N`, and `--help`. Default behavior runs the build and
then:

```bash
exec python3 "${ROOT}/scripts/legofs_type3_2node.py" \
  --bytes "${BENCH_BYTES}" --timeout "${TIMEOUT}"
```

Reject bytes that are zero, exceed `16777216`, or are not divisible by 4096.
Run `git submodule status --recursive` and reject lines beginning with `-`,
`+`, or `U`.

- [ ] **Step 4: Implement the dedicated build script**

`scripts/build_legofs_type3.sh` must use `set -euo pipefail` and only write to
`out/legofs-type3`. It performs these exact build gates:

```bash
cargo build --manifest-path components/legofs/Cargo.toml --release \
  --target riscv64gc-unknown-linux-gnu -p badfs-server -p badfs-bench
riscv64-linux-gnu-readelf -l <each-binary> | grep -qv INTERP
```

Set `CC_riscv64gc_unknown_linux_gnu=riscv64-linux-gnu-gcc` and
`RUSTFLAGS='-C target-feature=+crt-static'`. If the requested Rust target is
not installed, print the exact `rustup target add riscv64gc-unknown-linux-gnu`
remediation and exit without invoking `rustup`.

Build `guest/legofs_node_init.c` with the same static freestanding RV64 flags
as `scripts/build.sh`. Create a 64 MiB ext2 image using `truncate`, `mke2fs`,
and `debugfs`; install only `/badfs-server` and `/badfs-bench` with mode 0755.
Do not loop-mount the image. Run this benchmark-interface gate:

```bash
cargo test --manifest-path components/legofs/Cargo.toml -p badfs-bench \
  benchmark_mode_uses_semantic_values_and_rejects_unknown_input
```

Build a dedicated kernel in `out/legofs-type3/build/linux` with
`CONFIG_INITRAMFS_SOURCE` pointing to the Legofs PID 1 directory. Verify every
option from Task 6 is exactly `=y`. Reuse or build the pinned OpenSBI/U-Boot
artifacts, and build QEMU/CXLMemSim from their component branches.

- [ ] **Step 5: Extend the manifest and verify static binaries**

Write `out/legofs-type3/results/build-manifest.json` atomically. It contains
all component HEADs, artifact sizes/SHA-256 values, compiler versions, and the
eight artifact names from Step 1. Reject an ELF interpreter and reject a
RISC-V attributes string requiring RVV.

- [ ] **Step 6: Run the build-contract tests**

Run:

```bash
python3 -m unittest -v tests/test_legofs_build_contract.py
bash -n run-legofs-type3.sh scripts/build_legofs_type3.sh
```

Expected: all tests and syntax checks pass.

- [ ] **Step 7: Commit the build pipeline**

Run:

```bash
git add run-legofs-type3.sh scripts/build_legofs_type3.sh \
  scripts/write_manifest.py tests/test_legofs_build_contract.py
git commit -m "build: package RISC-V Legofs Type 3 guests"
```

### Task 8: Construct and own two exact SiFive U QEMU processes

**Files:**
- Create: `scripts/legofs_type3_2node.py`
- Create: `tests/test_legofs_runtime.py`

- [ ] **Step 1: Write failing command/topology tests**

The tests call `build_qemu_command(paths, node, coherence_port,
legofs_port)` for nodes 0 and 1 and assert:

```python
self.assertEqual(cmd[:3], ["qemu-system-riscv64", "-M", "sifive_u"])
self.assertEqual(sum("cxl-type3" in arg for arg in cmd), 1)
self.assertIn("coherence-v2=on", " ".join(cmd))
self.assertIn(f"coherence-v2-host-id={node}", " ".join(cmd))
self.assertNotIn("-M virt", " ".join(cmd))
```

Assert node0 alone has one `hostfwd=tcp:127.0.0.1:<port>-:3345`, every QEMU
object/device ID is node-qualified, and both commands point to the same
coherence port but different host IDs.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
python3 -m unittest -v tests/test_legofs_runtime.py
```

Expected: import fails because the runner does not exist.

- [ ] **Step 3: Implement exact command construction**

Each returned list begins exactly:

```python
["qemu-system-riscv64", "-M", "sifive_u"]
```

It then includes the existing SiFive U CXL firmware/FMW settings, one
256 MiB volatile Type-3 backend, one 2 MiB LSA, one `pxb-cxl`, one `cxl-rp`,
one `cxl-type3`, the read-only Legofs ext2 disk, and one `virtio-net-pci` user
network. The Type-3 argument contains:

```text
coherence-v2=on,cxlmemsim-addr=127.0.0.1,cxlmemsim-port=<port>,
coherence-v2-host-id=<0-or-1>,coherence-v2-cache-capacity=1024,
coherence-v2-cache-ways=8,coherence-v2-timeout-ms=5000,
coherence-v2-write-through=off
```

Do not set legacy `CXL_TRANSPORT_MODE`, `CXL_PGAS_SHM`, or
`CXL_MEMSIM_SERVER` environment variables.

- [ ] **Step 4: Implement run-scoped process ownership**

Create `out/legofs-type3/runs/<UTC timestamp>-<pid>/` with mode 0700. Reserve
two loopback TCP ports by binding sockets before process launch; release the
coherence reservation immediately before starting the server and the Legofs
reservation immediately before node0. Record PID, command array, start
monotonic time, exit time, and owner token for all three processes.

For each console, timestamp every complete received line with host
`time.monotonic_ns()` and append it to `node0-events.jsonl` or
`node1-events.jsonl` as `{host_capture_ns, line}`. This sidecar supplies one
host-clock ordering for the two guest consoles without treating either
guest's local monotonic clock as cross-VM comparable.

Cleanup sends SIGTERM only to recorded live PIDs whose `/proc/<pid>/cmdline`
still matches the recorded executable and run directory. Wait five seconds,
then SIGKILL only those remaining owned PIDs. Never use `pkill`, `killall`, or
a process-name match.

- [ ] **Step 5: Implement U-Boot boot sequencing and overlap gates**

Reuse the behavior of the existing `Console` class: wait for `=>`, run
`cxl list`, `cxl info 41.00.0`, and `cxl init`, then send role-specific
bootargs and:

```text
bootefi 90000000:<linux-image-size-hex> ${fdtcontroladdr}
```

Boot node0, wait for `LEG_OFS_CXL_READY` and `LEG_OFS_SERVER_READY`, then boot
node1. Before releasing node1's benchmark gate, assert both QEMU PIDs are
alive. After `LEG_OFS_BENCHMARK_PASS`, assert node0 is still alive. Store both
lifetime intervals and require their intersection to be non-empty.

- [ ] **Step 6: Run runtime unit tests**

Run:

```bash
python3 -m unittest -v tests/test_legofs_runtime.py
```

Expected: all command, port, PID ownership, overlap, and cleanup tests pass
using fake subprocesses; no QEMU starts in this test.

- [ ] **Step 7: Commit the two-node runtime**

Run:

```bash
git add scripts/legofs_type3_2node.py tests/test_legofs_runtime.py
git commit -m "run: orchestrate two SiFive U Type 3 nodes"
```

### Task 9: Enforce benchmark-scoped dirty back-invalidation evidence

**Files:**
- Modify: `scripts/legofs_type3_2node.py`
- Create: `tests/test_legofs_evidence.py`

- [ ] **Step 1: Write failing positive and negative evidence fixtures**

Construct minimal in-memory records for one successful operation:

```python
direct = {
    "event": "unmap", "access": "write", "op_id": 17,
    "offset": 0x4000, "length": 0x1000, "monotonic_ns": 10,
}
lifecycle_begin = {
    "event": "store_direct_begin", "op_id": 17,
    "mapping_offset": 0x4000, "mapping_length": 0x1000,
}
snoop = {
    "event": "snoop_sent", "opcode": "SNP_DATA_INV",
    "src_host": 0, "dst_host": 1, "snoop_id": 91,
    "line_address": 0x4080, "monotonic_ns": 20,
}
ack = {
    "event": "snoop_ack", "opcode": "SNP_DATA_INV",
    "src_host": 1, "dst_host": 0xffff, "snoop_id": 91,
    "line_address": 0x4080, "monotonic_ns": 21,
    "ack_strength": "MODEL", "payload_len": 64,
    "status": "OK", "dirty_data": True,
}
lifecycle_success = {
    "event": "store_direct_success", "op_id": 17,
    "mapping_offset": 0x4000, "mapping_length": 0x1000,
}
```

The positive fixture passes. Separate tests must reject zero invalidations,
clean ACK, native ACK, mismatched `snoop_id`, line outside the grant, event
before the pre-benchmark trace offset, missing host registration, duplicate
host ID, non-overlapping QEMU lifetimes, nonzero Blob/staging/legacy counters,
checksum mismatch, active lease, quarantine, timeout, protocol error, and
server-copy failure.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
python3 -m unittest -v tests/test_legofs_evidence.py
```

Expected: parser/validator imports fail.

- [ ] **Step 3: Implement strict parsers**

Parse only prefixed console lines and complete server JSONL records:

```text
BADFS_DIRECT_MAP_TRACE_JSON <json>
BADFS_LIFECYCLE_TRACE_JSON <json>
COHERENCE_V2_STATS_JSON <json>
```

Reject duplicate JSON keys, non-integer numeric fields, unknown schema
versions, truncated final JSONL records, and timestamps that go backwards in
the server trace. Save the server trace byte offset only after node0/node1 CXL
ready markers and both registration events have appeared. Only records after
that offset count toward benchmark deltas.

- [ ] **Step 4: Implement correlation and acceptance**

`correlate_dirty_backinvalidations()` returns records only when:

```python
grant_start <= snoop["line_address"]
and snoop["line_address"] + 64 <= grant_start + grant_length
and snoop["dst_host"] == 1
and snoop["snoop_id"] == ack["snoop_id"]
and ack["ack_strength"] == "MODEL"
and ack["payload_len"] == 64
and ack["dirty_data"] is True
and snoop["monotonic_ns"] <= ack["monotonic_ns"]
```

The direct and lifecycle traces bind the address to the same `op_id`; the
server trace orders snoop send/ACK. Host-capture sidecars must show the flushed
client direct-unmap line before node0's flushed `store_direct_success` line.
The synchronous call chain supplies the remaining causal edge:
`store_direct_begin -> candidate_checksum -> Type-3 GETS -> snoop/ACK -> RPC
return -> store_direct_success`. Require at least one correlation record and
include these matched event records in the result.

Parse Legofs's printed `badfs fabric stats` and require direct read/write ops
and bytes greater than zero; Blob, staging, and all four legacy op counters
equal zero; checksum failures, lease rejections, quarantine events, active
leases, and quarantined slots equal zero. Require benchmark byte count and
checksum to match the deterministic `badfs-bench` pattern.

- [ ] **Step 5: Publish a complete atomic result**

Write `result.json.tmp`, fsync it, and rename it to `result.json`. Include:

```text
status,first_failure,functional_model_only,run_id,component_commits,
artifact_sha256,qemu_commands,process_lifetimes,overlap_ns,topology,
registrations,pre_benchmark_trace_offset,coherence_delta,legofs_counters,
benchmark,correlations,logs,cleanup
```

On any exception, preserve all logs, set `status` to `failed`, record the
first exception string, run scoped cleanup, and still atomically publish the
failed result.

- [ ] **Step 6: Run all evidence tests**

Run:

```bash
python3 -m unittest -v tests/test_legofs_evidence.py \
  tests/test_legofs_runtime.py
```

Expected: the positive fixture passes and every negative fixture fails for
its named first reason.

- [ ] **Step 7: Commit the proof gate**

Run:

```bash
git add scripts/legofs_type3_2node.py tests/test_legofs_evidence.py
git commit -m "test: require Legofs-triggered dirty back-invalidation"
```

### Task 10: Build, run, debug, and document the end-to-end proof

**Files:**
- Modify: `README.md`
- Modify gitlinks: `components/qemu`, `components/cxlmemsim`, `components/legofs`
- Generated: `out/legofs-type3/results/build-manifest.json`
- Generated: `out/legofs-type3/runs/<run-id>/result.json`
- Generated: `out/legofs-type3/runs/<run-id>/{node0.log,node1.log,cxlmemsim.log,coherence.jsonl}`

- [ ] **Step 1: Run every offline superproject test**

Run:

```bash
python3 -m unittest discover -s tests -v
bash -n run.sh run-legofs-type3.sh scripts/*.sh
```

Expected: all tests and shell syntax checks pass.

- [ ] **Step 2: Build the complete stack**

Run:

```bash
./run-legofs-type3.sh --build-only --jobs "$(nproc)" --bytes 65536
```

Expected: exit 0; manifest hashes verify; static RISC-V `badfs-server`,
`badfs-bench`, and PID 1 exist; QEMU, OpenSBI, U-Boot, Linux, ext2, and
CXLMemSim artifacts are nonempty.

- [ ] **Step 3: Run the bounded end-to-end test**

Run:

```bash
./run-legofs-type3.sh --run-only --bytes 65536 --timeout 1200
```

Expected: exit 0 and the latest `result.json` reports `status: "passed"`,
exactly two registrations with host IDs 0/1, two overlapping QEMU intervals,
strict direct reads/writes, zero fallback counters, correct checksum, nonzero
benchmark-scoped `SNP_DATA_INV`, nonzero dirty model ACK completion, and at
least one address-correlated `op_id` record.

- [ ] **Step 4: Debug failures from the first failed invariant**

If Step 3 fails, inspect in this order and rerun only after the first failure
is understood:

```bash
jq . out/legofs-type3/runs/*/result.json | tail -n 120
rg -n 'error|fail|timeout|LEG_OFS_|BADFS_|CXL|dax|region' \
  out/legofs-type3/runs/*/{node0.log,node1.log,cxlmemsim.log}
rg -n 'SNP_DATA_INV|snoop_ack|server_copy' \
  out/legofs-type3/runs/*/coherence.jsonl
```

The implementation is not complete while `result.json` is failed, while the
two QEMU processes did not overlap, or while the accepted invalidation came
from boot/preflight traffic.

- [ ] **Step 5: Document the exact user workflow and claim boundary**

Add a README section with:

```bash
git clone --recurse-submodules git@github.com:SlugLab/CXLMemSim-riscv.git
cd CXLMemSim-riscv
./run-legofs-type3.sh --bytes 65536
```

State explicitly that the command uses two concurrent
`qemu-system-riscv64 -M sifive_u` machines, one Type-3 endpoint per guest,
U-Boot CXL discovery, Linux devdax, Legofs strict lifecycle-direct I/O, and
CXLMemSim model-level MESI back-invalidation. State that this is functional
QEMU/TCG evidence, not physical-link, CPU-cache, CXL.cache, or performance
evidence.

- [ ] **Step 6: Push component branches only after green evidence**

Run:

```bash
git -C components/qemu push -u origin codex/sifive-u-type3-mesi-v2
git -C components/cxlmemsim push -u origin \
  codex/riscv-legofs-coherence-trace
git -C components/legofs push -u origin codex/riscv-type3-coherence-proof
```

Expected: all pushes succeed. If the Legofs remote rejects writes because
`Zettai-US/legofs` is not writable, add a SlugLab fork as `sluglab`, push the
same branch there, and update `.gitmodules` to that exact fork URL before the
superproject commit.

- [ ] **Step 7: Record gitlinks, docs, and final verification**

Run:

```bash
git add components/qemu components/cxlmemsim components/legofs README.md
git commit -m "feat: prove Legofs Type 3 MESI back-invalidation"
git status --short
python3 -m unittest discover -s tests -v
jq -e '.status == "passed" and .functional_model_only == true and \
  (.correlations | length) > 0 and .coherence_delta.snp_data_inv > 0 and \
  .coherence_delta.dirty_data_completions > 0' \
  out/legofs-type3/runs/*/result.json
```

Expected: the worktree is clean, all tests pass, and `jq` exits zero for the
latest result.

- [ ] **Step 8: Push the integration branch**

Run:

```bash
git push -u origin codex/legofs-type3-mesi-proof
```

Expected: GitHub contains the superproject branch and its three reachable
component commits. Do not merge to `main` until the user reviews the result
JSON and logs.

## Final acceptance checklist

- [ ] Both command arrays begin exactly with
  `qemu-system-riscv64 -M sifive_u`.
- [ ] Two QEMU lifetimes overlap and each VM owns exactly one Type-3 endpoint.
- [ ] U-Boot and Linux evidence exists for host bridge, Type-3 decoder,
  region, and CXL devdax in both guests.
- [ ] Host IDs 0 and 1 have distinct live protocol-v2 sessions.
- [ ] Legofs direct read/write counters and byte counts are nonzero.
- [ ] Blob, staging, and legacy payload counters are zero.
- [ ] Benchmark checksum and byte count match the requested workload.
- [ ] Benchmark-scoped `SNP_DATA_INV`, dirty-data completion, and model ACK
  deltas are each greater than zero.
- [ ] At least one snoop line lies inside the exact lifecycle direct grant for
  the same `op_id` and completes before `store_direct_success`.
- [ ] Timeout, protocol, delivery, server-copy, checksum, lease, quarantine,
  and cleanup error counters are zero.
- [ ] `result.json` says `functional_model_only: true` and preserves all
  commands, commits, hashes, logs, and cleanup evidence.
