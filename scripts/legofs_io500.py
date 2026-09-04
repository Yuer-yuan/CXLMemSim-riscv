#!/usr/bin/env python3
"""Run one or two LegoFS servers and independent RISC-V IO500 clients."""

import argparse
import base64
import binascii
import configparser
import hashlib
import concurrent.futures
import datetime
import json
import os
import pathlib
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
import uuid
import zlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from legofs_type3_2node import Console, OwnedProcess, qemu_environment


DEFAULT_CLIENTS = 10
DEFAULT_COHERENCE_CACHE_MIB = 32
DEFAULT_SSD_CACHE_MIB = 512
DEFAULT_OBSERVATION_SAMPLE_SHIFT = 4
DEFAULT_OBSERVATION_ARENA_MIB = 12
OBSERVATION_SCHEMA_DIGEST = "942b9f1d769746ec"
OBSERVATION_SCHEMA_MAJOR = 1
OBSERVATION_SCHEMA_MINOR = 2
OBSERVATION_EVENT_BYTES = 64
OBSERVATION_MAX_BYTES = 64 * 1024**2
ENDPOINT_BYTES = 64 * 1024**3
FMW_SIZE = "64G"
SERVING_MAX_LANES = 64
RECOVERY_CONTROL_LANE_BASE = SERVING_MAX_LANES - 4
DECODER_SIZE = "0000001000000000"
HOST_DECODER = (
    "CXL host decoder0: HPA 0000001000000000 "
    f"size {DECODER_SIZE} target 0 ctrl 00000600"
)
TYPE3_DECODER = (
    "41.00.0 Type 3 decoder0: HPA 0000001000000000 "
    f"size {DECODER_SIZE} target 0 ctrl 00001600"
)
MPI_LAUNCH_RE = re.compile(r"^HYDRA_LAUNCH: (.+)$", re.MULTILINE)
PROXY_ID_RE = re.compile(r"(?:^|\s)--proxy-id\s+(\d+)(?:\s|$)")
TIME_SYNC_RE = re.compile(
    r"LEGOFS_IO500_TIME_SYNC role=(server|client) index=(\d+) "
    r"requested=(\d+) observed=(\d+)(?=[^0-9])"
)
HELLO_RE = re.compile(
    r"LEGOFS_MPI_HELLO rank=(\d+) size=(\d+) host=(\S+) "
    r"begin_ns=(\d+) end_ns=(\d+)"
)
RANK_RE = re.compile(
    r"LEGOFS_IO500_RANK_EXEC stage=(\S+) rank=(\d+) size=(\d+) "
    r"endpoint=(\d+) dax=(\S+)"
)
SUMMARY_RE = re.compile(
    r"LEGOFS_IO500_POSIX_SUMMARY index=(\d+) file=(\S+) (\{[^\n]+\})"
)
CLEAN_RESTART_CONTROL_RE = re.compile(
    r"badfs_clean_restart_control (\{[^\n]+\})"
)
EXPORT_SUMMARY_RE = re.compile(
    r"LEGOFS_IO500_EXPORT_POSIX_SUMMARY endpoint=(\d+) file=(\S+) "
    r"(\{[^\n]+\})"
)
SYSTEM_SYNC_PROBE_RE = re.compile(
    r"LEGOFS_SYSTEM_SYNC_PROBE_RESULT label=(\S+) raw_status=(-?\d+) "
    r"errno=(\d+) child_reached=([01]) exited=([01]) exit_status=(-?\d+) "
    r"signaled=([01]) term_signal=(-?\d+)"
)
PROXY_EXIT_RE = re.compile(r"LEGOFS_IO500_PROXY_EXIT index=(\d+) rc=(\d+)")
OBSERVATION_BEGIN_RE = re.compile(
    r"^LEGOFS_OBSERVATION_BEGIN role=(client-rank|server|diagnostic-client) "
    r"endpoint=(\d+) file=([^\s/]+) bytes=(\d+) sha256=([0-9a-f]{64})"
    r"(?: encoding=(gzip-base64-v1) compressed_bytes=(\d+) "
    r"compressed_sha256=([0-9a-f]{64}) chunks=(\d+))?$"
)
OBSERVATION_CHUNK_RE = re.compile(
    r"^LEGOFS_OBSERVATION_CHUNK seq=(\d+) data=([A-Za-z0-9+/]+={0,2})$"
)
OBSERVATION_END_RE = re.compile(
    r"^LEGOFS_OBSERVATION_END role=(client-rank|server|diagnostic-client) "
    r"endpoint=(\d+) file=([^\s/]+)$"
)
MPI_FATAL_MARKERS = (
    "BADFS_STRICT_LIFECYCLE_DIRECT_INIT_FAILED",
    "LEGOFS_IO500_FATAL",
)
# TCG oversubscription can delay an otherwise live guest long enough for RCU
# or kthread watchdog diagnostics.  Those lines are measurement-noise evidence,
# not proof that MPI or the filesystem stopped making progress.  The bounded
# workload deadline, guest exit, and explicit fatal markers remain the liveness
# authorities; only unrecoverable kernel conditions fail immediately here.
PERFORMANCE_INVALIDATING_GUEST_MARKERS = (
    "BUG: soft lockup",
    "Out of memory:",
)
PERFORMANCE_NOISE_GUEST_MARKERS = (
    "rcu_sched detected stalls",
    "rcu_preempt detected stalls",
    "kthread starved for",
)
GUEST_PROCESS_FAULT_MARKERS = (
    "Segmentation fault",
    "Bus error",
    "Illegal instruction",
    # RISC-V synchronous load and store/AMO access faults.  Linux prints the
    # register frame even when QEMU itself remains alive, so process polling
    # alone cannot detect this otherwise terminal MPI failure.
    "cause: 0000000000000005",
    "cause: 0000000000000007",
)
SEMANTIC_SMOKE_STAGES = (
    "tiny",
    "easy-smoke",
    "hard-smoke",
    "metadata-smoke",
    "rnd4k",
)

# Packed-small segments serve small-file payloads. Large IOR-only stages must
# still expose the counters, but zero activity is expected and is not evidence
# that the configured layout failed to initialize.
PACKED_SMALL_WORKLOAD_STAGES = (
    "tiny",
    "metadata-smoke",
    "scc",
    "standard",
)

CLIENT_TIMING_GROUPS = {
    "read_control_rpc_wait_ns": (
        "direct_read_acquire_ns",
        "direct_read_release_ns",
        "direct_read_snapshot_acquire_ns",
        "direct_read_snapshot_release_ns",
    ),
    "metadata_rpc_wait_ns": (
        "lifecycle_metadata_lookup_ns",
        "lifecycle_metadata_update_ns",
        "lifecycle_metadata_readback_ns",
        "lifecycle_file_info_ns",
        "lifecycle_operation_id_grant_ns",
    ),
    "write_control_rpc_wait_ns": (
        "lifecycle_blob_read_ns",
        "lifecycle_blob_write_ns",
        "lifecycle_direct_write_prepare_ns",
        "lifecycle_direct_write_finish_ns",
        "lifecycle_write_arena_acquire_ns",
        "lifecycle_write_arena_commit_ns",
        "lifecycle_write_arena_release_ns",
    ),
}

# Must match badfs-common serving_transport::bootstrap. Generation 13 adds the
# directory catalog, lane-owned mutation arenas, and authority-owned D/R
# cursors; accepting generation 12 would let an old process alias these ranges.
DIRECT_METADATA_FORMAT_GENERATION = 13
DIRECT_METADATA_CLIENT_COUNTERS = (
    "direct_metadata_attempts",
    "direct_metadata_hits",
    "direct_metadata_not_found_hits",
    "direct_metadata_no_hint",
    "direct_metadata_stale",
    "direct_metadata_cold_attempts",
    "direct_metadata_cold_hits",
    "direct_metadata_cold_not_found_hits",
    "direct_metadata_cold_fallbacks",
    "direct_metadata_fallback_commands",
    "direct_metadata_commands_elided",
    "direct_metadata_read_ns",
    "direct_metadata_gate_loads",
    "direct_metadata_root_loads",
    "direct_metadata_dentry_cell_loads",
    "direct_metadata_inode_record_loads",
    "direct_metadata_unstable_read_retries",
    "direct_metadata_unstable_read_recoveries",
    "direct_metadata_unstable_read_exhaustions",
    "direct_metadata_unstable_root_unavailable",
    "direct_metadata_unstable_dentry_decode",
    "direct_metadata_unstable_inode_decode",
    "direct_metadata_unstable_snapshot_changed",
)

DIRECT_MUTATION_CLIENT_COUNTERS = (
    "direct_mutation_lease_installs",
    "direct_mutation_lease_replacements",
    "direct_mutation_generic_close_handoffs",
    "direct_create_attempts",
    "direct_create_hits",
    "direct_create_fallbacks",
    "direct_create_commands_elided",
    "direct_create_post_grant_retry_hits",
    "direct_create_fallback_ineligible",
    "direct_create_fallback_no_parent_writer",
    "direct_create_fallback_existing",
    "direct_create_fallback_inode_exhausted",
    "direct_create_errors",
    "direct_unlink_attempts",
    "direct_unlink_commands_elided",
    "direct_unlink_base_commands_elided",
    "direct_unlink_peer_applied_commands_elided",
    "direct_unlink_fallbacks",
    "direct_unlink_fallback_ineligible",
    "direct_unlink_fallback_no_parent_writer",
    "direct_unlink_fallback_unapplied_peer",
    "direct_unlink_fallback_base_unavailable",
    "direct_unlink_fallback_overlay_changed",
    "direct_unlink_fallback_unsupported_target",
    "direct_unlink_fallback_live_ofd",
    "direct_unlink_not_found",
    "direct_unlink_membership_changing",
    "direct_unlink_lease_revoked",
)

DIRECT_RO_POSIX_COUNTERS = (
    "direct_ro_open_attempts",
    "direct_ro_open_hits",
    "direct_ro_open_fallbacks",
    "direct_ro_layout_roots_loaded",
    "direct_ro_layout_entries_loaded",
    "direct_ro_open_commands_elided",
    "direct_ro_close_commands_elided",
    "direct_ro_epoch_acquires",
    "direct_ro_epoch_releases",
)

PACKED_SMALL_SEGMENT_SERVER_COUNTERS = (
    "backend_fresh_format_scan_bytes",
    "backend_publication_slots_reset",
    "startup_small_runtime_segments",
    "startup_small_runtime_cells",
    "small_segment_claims",
    "small_segment_claim_persist_ns",
    "small_cell_grants",
    "small_cell_commits",
    "small_cell_payload_bytes",
    "small_cell_payload_persist_bytes",
    "small_cell_allocator_persist_barriers",
    "legacy_small_arena_reserve_calls",
    "legacy_small_arena_reserve_ns",
)

PACKED_SMALL_SEGMENT_CLIENT_COUNTERS = (
    "small_segment_cache_hits",
    "small_segment_dynamic_mmap_calls",
    "small_segment_dynamic_mmap_ns",
    "small_segment_mapped_owner_segments",
    "small_segment_protection_calls",
    "small_segment_protection_ns",
)
DIRECT_METADATA_AUTHORITY_COUNTERS = (
    "publication_batches",
    "dentry_writes",
    "inode_writes",
    "record_prepare_ns",
    "root_odd_ns",
    "root_odd_max_ns",
    "registered_hints",
    "capacity_failures",
    "publication_failures",
    "dentry_high_water",
    "dentry_capacity",
    "inode_capacity",
)


class Paths:
    def __init__(self, root: pathlib.Path, stage: str, result_label: str | None = None):
        self.root = root.resolve()
        self.stage = stage
        self.result_label = result_label or stage
        self.build = self.root / "target/build/riscv-io500"
        self.platform = self.build / "platform"
        self.payload = self.build / "images/io500-payload.ext2"
        self.manifest = self.root / "target/results/legofs-io500/build-manifest.json"
        self.dependency_versions = (
            self.root / "target/results/legofs-io500/dependency-versions.txt"
        )
        self.isa_gate = self.root / "target/results/legofs-io500/isa-gate.txt"
        self.run = self.root / "target/run/legofs-io500" / self.result_label
        self.bundle = self.root / "target/results/legofs-io500" / self.result_label
        self.qemu = self.platform / "qemu-system-riscv64"
        self.cxlmemsim = self.platform / "cxlmemsim_server"
        self.opensbi = self.platform / "fw_dynamic.bin"
        self.uboot = self.platform / "u-boot.bin"
        self.linux = self.platform / "linux-io500-Image"
        self.server_log = self.bundle / "cxlmemsim.log"
        self.coherence = self.bundle / "coherence.jsonl"
        self.central_ssd = self.run / "cxlmemsim-cxl-ssd.raw"
        self.device_dram = self.run / "cxlmemsim-device-dram.raw"
        self.client_result = self.run / "client0-results.ext2"
        self.result = self.bundle / "result.json"
        self.observation = self.bundle / "observation"
        self.observation_raw = self.observation / "raw"

    def lsa(self, host_id: int) -> pathlib.Path:
        return self.run / f"endpoint-{host_id}-lsa.raw"

    def server_state(self, server_index: int) -> pathlib.Path:
        return self.run / f"server{server_index}-state.ext2"

    def console_log(self, role: str) -> pathlib.Path:
        return self.bundle / f"{role}.log"

    def event_log(self, role: str) -> pathlib.Path:
        return self.bundle / f"{role}-events.jsonl"


class MpiStageError(RuntimeError):
    def __init__(self, stage: str, returncode: int):
        super().__init__(f"MPI stage {stage} exited with rc={returncode}")
        self.stage = stage
        self.returncode = returncode


class UdpPortReservation:
    def __init__(self):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(("127.0.0.1", 0))
        self.port = self.socket.getsockname()[1]

    def release(self):
        if self.socket is not None:
            self.socket.close()
            self.socket = None


def atomic_json(path: pathlib.Path, value) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(value, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def _canonical_json_bytes(value: dict) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def observation_profile_digest(profile: dict) -> str:
    unsigned = dict(profile)
    unsigned.pop("profile_sha256", None)
    return hashlib.sha256(_canonical_json_bytes(unsigned)).hexdigest()


def load_observation_profile(path: pathlib.Path) -> dict:
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read observation profile {path}: {error}") from error
    if not isinstance(profile, dict):
        raise ValueError("observation profile must be a JSON object")
    if profile.get("schema_version") != "legofs.observation.profile.v1":
        raise ValueError("unsupported observation profile schema")
    recorded_hash = profile.get("profile_sha256")
    if not isinstance(recorded_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", recorded_hash):
        raise ValueError("observation profile lacks a valid profile_sha256")
    if observation_profile_digest(profile) != recorded_hash:
        raise ValueError("observation profile hash mismatch")
    schema = profile.get("observation_schema")
    if schema != {
        "major": OBSERVATION_SCHEMA_MAJOR,
        "minor": OBSERVATION_SCHEMA_MINOR,
        "event_record_bytes": OBSERVATION_EVENT_BYTES,
        "digest": OBSERVATION_SCHEMA_DIGEST,
    }:
        raise ValueError("observation profile schema digest/layout mismatch")
    mode = profile.get("mode")
    shift = profile.get("sample_shift")
    arena_mib = profile.get("arena_mib")
    producer_slots = profile.get("producer_slots")
    if mode not in ("off", "aggregate", "sampled"):
        raise ValueError("observation profile mode must be off, aggregate, or sampled")
    if type(shift) is not int or not 0 <= shift <= 20:
        raise ValueError("observation profile sample_shift must be in 0..20")
    if type(arena_mib) is not int or not 8 <= arena_mib <= 64:
        raise ValueError("observation profile arena_mib must be in 8..64")
    if type(producer_slots) is not int or not 1 <= producer_slots <= 8:
        raise ValueError("observation profile producer_slots must be in 1..8")
    return profile


def resolve_observation_config(args, root: pathlib.Path, run_id: str) -> dict:
    manual = (
        args.observation_mode is not None
        or args.observation_sample_shift is not None
        or args.observation_arena_mib is not None
    )
    if args.observation_profile_manifest is not None and manual:
        raise ValueError(
            "--observation-profile-manifest is mutually exclusive with manual observation options"
        )
    profile = None
    profile_path = None
    if args.observation_profile_manifest is not None:
        profile_path = pathlib.Path(args.observation_profile_manifest)
        if not profile_path.is_absolute():
            profile_path = root / profile_path
        profile_path = profile_path.resolve()
        profile = load_observation_profile(profile_path)
        mode = profile["mode"]
        sample_shift = profile["sample_shift"]
        arena_mib = profile["arena_mib"]
        producer_slots = profile["producer_slots"]
    else:
        mode = args.observation_mode or "default"
        sample_shift = (
            DEFAULT_OBSERVATION_SAMPLE_SHIFT
            if args.observation_sample_shift is None
            else args.observation_sample_shift
        )
        arena_mib = (
            DEFAULT_OBSERVATION_ARENA_MIB
            if args.observation_arena_mib is None
            else args.observation_arena_mib
        )
        producer_slots = 8
    if mode not in ("default", "off", "aggregate", "sampled"):
        raise ValueError("invalid observation mode")
    if not 0 <= sample_shift <= 20:
        raise ValueError("observation-sample-shift must be between 0 and 20")
    if not 8 <= arena_mib <= 64:
        raise ValueError("observation-arena-mib must be between 8 and 64")
    return {
        "mode": mode,
        "sample_shift": sample_shift,
        "arena_mib": arena_mib,
        "arena_bytes": arena_mib * 1024**2,
        "producer_slots": producer_slots,
        "run_id": run_id,
        "profile_manifest": str(profile_path) if profile_path is not None else None,
        "profile_sha256": profile.get("profile_sha256") if profile is not None else None,
        "schema_digest": OBSERVATION_SCHEMA_DIGEST,
    }


def process_resource_sample(process: subprocess.Popen) -> dict | None:
    """Read host-side CPU and I/O counters without changing the measured process."""
    pid = process.pid
    try:
        stat_text = pathlib.Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields = stat_text[stat_text.rfind(")") + 2:].split()
        ticks = os.sysconf("SC_CLK_TCK")
        user_ns = int(fields[11]) * 1_000_000_000 // ticks
        system_ns = int(fields[12]) * 1_000_000_000 // ticks
        io_fields = {}
        for line in pathlib.Path(f"/proc/{pid}/io").read_text(encoding="utf-8").splitlines():
            name, value = line.split(":", 1)
            if name in ("read_bytes", "write_bytes"):
                io_fields[name] = int(value)
    except (FileNotFoundError, PermissionError, OSError, ValueError, IndexError):
        return None
    return {
        "pid": pid,
        "user_ns": user_ns,
        "system_ns": system_ns,
        "cpu_ns": user_ns + system_ns,
        "read_bytes": io_fields.get("read_bytes", 0),
        "write_bytes": io_fields.get("write_bytes", 0),
    }


def sample_named_processes(processes: dict[str, subprocess.Popen]) -> dict[str, dict]:
    return {
        name: sample
        for name, process in processes.items()
        if (sample := process_resource_sample(process)) is not None
    }


def host_process_cost_window(
    before: dict[str, dict],
    after: dict[str, dict],
    started_ns: int,
    completed_ns: int,
) -> dict:
    wall_ns = max(0, completed_ns - started_ns)
    deltas = {}
    for name in sorted(before.keys() & after.keys()):
        if before[name]["pid"] != after[name]["pid"]:
            continue
        deltas[name] = {
            "pid": after[name]["pid"],
            **{
                field: max(0, after[name][field] - before[name][field])
                for field in (
                    "user_ns", "system_ns", "cpu_ns", "read_bytes", "write_bytes"
                )
            },
        }

    def aggregate(names: list[str]) -> dict:
        return {
            field: sum(deltas[name][field] for name in names)
            for field in ("user_ns", "system_ns", "cpu_ns", "read_bytes", "write_bytes")
        }

    qemu_names = [name for name in deltas if name.endswith("_qemu")]
    groups = {
        "qemu_inclusive": aggregate(qemu_names),
        "cxlmemsim": aggregate(["cxlmemsim"] if "cxlmemsim" in deltas else []),
    }
    sampled_cpu_ns = sum(group["cpu_ns"] for group in groups.values())
    for group in groups.values():
        group["sampled_cpu_share"] = (
            group["cpu_ns"] / sampled_cpu_ns if sampled_cpu_ns else 0.0
        )
        group["cpu_equivalents"] = group["cpu_ns"] / wall_ns if wall_ns else 0.0
    return {
        "schema_version": "legofs.host-process-cost.v1",
        "started_monotonic_ns": started_ns,
        "completed_monotonic_ns": completed_ns,
        "wall_ns": wall_ns,
        "processes": deltas,
        "groups": groups,
        "sampled_cpu_ns": sampled_cpu_ns,
        "missing_after": sorted(before.keys() - after.keys()),
        "interpretation": (
            "QEMU is inclusive of guest IO500, LegoFS, Linux and emulation; "
            "sampled CPU shares are not exclusive wall-time shares"
        ),
    }


def record_host_process_cost_window(
    result: dict,
    before: dict,
    after: dict,
    started_ns: int,
    completed_ns: int,
) -> bool:
    """Close the MPI cost window once; post-run evidence export is excluded."""
    if "host_process_cost" in result:
        return False
    result["host_process_cost"] = host_process_cost_window(
        before, after, started_ns, completed_ns
    )
    return True


def legofs_timing_breakdown(
    summaries: list[dict], inspection: dict, wall_ns: int, client_count: int
) -> dict:
    groups = {
        group: sum(
            int(record.get("stats", {}).get(field, 0))
            for record in summaries
            for field in fields
        )
        for group, fields in CLIENT_TIMING_GROUPS.items()
    }
    client_wait_ns = sum(groups.values())
    rank_wall_ns = wall_ns * client_count
    server_commit_ns = int(
        inspection.get("audit", {}).get("direct_write_total_ns", 0)
    )
    server_state_validation_ns = int(
        inspection.get("audit", {}).get("state_validation_ns", 0)
    )
    transport_client = {
        field: sum(
            int(item.get("client_timing", {}).get(field, 0))
            for record in summaries
            for item in record.get("cxl_serving_evidence", [])
        )
        for field in (
            "calls",
            "call_gate_wait_ns",
            "syscall_prepare_ns",
            "sq_credit_wait_ns",
            "sq_publish_ns",
            "cq_wait_ns",
        )
    }
    transport_authority = {
        field: sum(
            int(item.get("authority_timing", {}).get(field, 0))
            for record in summaries
            for item in record.get("cxl_serving_evidence", [])
        )
        for field in (
            "sqe_consumed",
            "cqe_published",
            "authority_queue_wait_ns",
            "successful_request_poll_ns",
            "dispatcher_backend_ns",
            "completion_publication_wait_ns",
            "cqe_publish_ns",
        )
    }
    cq_wait_ns = transport_client["cq_wait_ns"]
    cq_wait_ns_by_opcode: dict[str, int] = {}
    dispatches_by_opcode: dict[str, int] = {}
    for record in summaries:
        for item in record.get("cxl_serving_evidence", []):
            for opcode, value in item.get("cq_wait_ns_by_opcode", {}).items():
                cq_wait_ns_by_opcode[str(opcode)] = (
                    cq_wait_ns_by_opcode.get(str(opcode), 0) + int(value)
                )
            for opcode, value in item.get("dispatches_by_opcode", {}).items():
                dispatches_by_opcode[str(opcode)] = (
                    dispatches_by_opcode.get(str(opcode), 0) + int(value)
                )
    cq_inner_fields = (
        "successful_request_poll_ns",
        "authority_queue_wait_ns",
        "dispatcher_backend_ns",
        "completion_publication_wait_ns",
        "cqe_publish_ns",
    )
    cq_inner_ns = sum(transport_authority[field] for field in cq_inner_fields)
    cq_residual_ns = cq_wait_ns - cq_inner_ns
    cq_attribution = {
        "outer_cq_wait_ns": cq_wait_ns,
        "inner_authority_ns": cq_inner_ns,
        "residual_ns": cq_residual_ns,
        "calls": transport_client["calls"],
        "cq_wait_ns_per_call": (
            cq_wait_ns / transport_client["calls"]
            if transport_client["calls"]
            else 0.0
        ),
        "components": {
            field: {
                "ns": transport_authority[field],
                "share_of_outer": (
                    transport_authority[field] / cq_wait_ns if cq_wait_ns else 0.0
                ),
            }
            for field in cq_inner_fields
        },
        "residual_share_of_outer": (
            cq_residual_ns / cq_wait_ns if cq_wait_ns else 0.0
        ),
        "by_opcode": {
            opcode: {
                "calls": dispatches_by_opcode.get(opcode, 0),
                "cq_wait_ns": wait_ns,
                "cq_wait_ns_per_call": (
                    wait_ns / dispatches_by_opcode.get(opcode, 0)
                    if dispatches_by_opcode.get(opcode, 0)
                    else 0.0
                ),
                "share_of_outer": wait_ns / cq_wait_ns if cq_wait_ns else 0.0,
            }
            for opcode, wait_ns in sorted(
                cq_wait_ns_by_opcode.items(), key=lambda item: int(item[0])
            )
        },
        "interpretation": (
            "CQ wait is the outer SQE-visible to CQE-consumed envelope. Authority "
            "components are nested inside it; residual contains request/completion "
            "discovery, scheduling and uninstrumented serialization. SQ publish is "
            "outside this envelope. Values summed across ranks are not IO500 wall time."
        ),
    }
    audit = inspection.get("audit", {})
    metadata_wal_persist_ns = int(audit.get("metadata_wal_persist_ns", 0))
    lifecycle_payload_persist_ns = int(
        audit.get("direct_write_payload_persist_ns", 0)
    )
    lifecycle_state_persist_ns = int(audit.get("direct_write_state_persist_ns", 0))
    visibility_durability = audit.get("visibility_durability", {})
    background_lifecycle_persist_ns = int(
        visibility_durability.get("persistence_worker_lifecycle_ns", 0)
    )
    arena_acquire = {
        field: int(audit.get(field, 0))
        for field in (
            "arena_acquire_calls",
            "arena_acquire_slots",
            "arena_acquire_lock_wait_ns",
            "arena_acquire_state_clone_ns",
            "arena_acquire_allocation_plan_ns",
            "arena_acquire_backend_reserve_ns",
            "arena_acquire_state_build_ns",
            "arena_acquire_state_validate_ns",
            "arena_acquire_grant_install_ns",
            "arena_acquire_state_persist_ns",
            "arena_acquire_trace_ns",
            "arena_acquire_total_ns",
        )
    }
    direct_write = {
        field: int(audit.get(field, 0))
        for field in (
            "direct_write_commit_groups",
            "direct_write_commit_items",
            "direct_write_prepare_ns",
            "direct_write_payload_persist_ns",
            "direct_write_state_build_ns",
            "direct_write_state_persist_ns",
            "direct_write_publish_ns",
            "direct_write_trace_ns",
            "direct_write_total_ns",
        )
    }
    packed_small_segment = {
        field: int(audit.get(field, 0))
        for field in PACKED_SMALL_SEGMENT_SERVER_COUNTERS
    }
    packed_client_names = {
        "small_segment_cache_hits": "client_cache_hits",
        "small_segment_dynamic_mmap_calls": "client_dynamic_mmap_calls",
        "small_segment_dynamic_mmap_ns": "client_dynamic_mmap_ns",
        "small_segment_mapped_owner_segments": "client_mapped_owner_segments",
        "small_segment_protection_calls": "client_protection_calls",
        "small_segment_protection_ns": "client_protection_ns",
    }
    packed_small_segment.update({
        output_name: sum(
            int(record.get("stats", {}).get(input_name, 0))
            for record in summaries
        )
        for input_name, output_name in packed_client_names.items()
    })
    writer_host_persistence = {
        field: sum(
            int(record.get("stats", {}).get(field, 0)) for record in summaries
        )
        for field in (
            "writer_host_persist_jobs_queued",
            "writer_host_persist_jobs_completed",
            "writer_host_persist_jobs_failed",
            "writer_host_local_persist_ns",
            "writer_host_receipt_submit_ns",
            "writer_host_queue_wait_ns",
            "writer_host_msync_persist_jobs",
            "writer_host_zicbom_persist_jobs",
            "writer_host_local_persist_ranges",
            "writer_host_local_persist_bytes",
            "writer_host_receipt_batches_submitted",
            "writer_host_receipt_items_submitted",
            "writer_host_quiescent_handoffs",
            "writer_host_authority_apply_wait_polls",
            "writer_host_authority_apply_wait_ns",
        )
    }
    io_envelope = {
        field: sum(
            int(record.get("stats", {}).get(field, 0)) for record in summaries
        )
        for field in (
            "read_ops",
            "read_ns",
            "read_bytes",
            "write_ops",
            "write_ns",
            "write_bytes",
        )
    }
    io_envelope["read_share_of_rank_wall"] = (
        io_envelope["read_ns"] / rank_wall_ns if rank_wall_ns else 0.0
    )
    io_envelope["write_share_of_rank_wall"] = (
        io_envelope["write_ns"] / rank_wall_ns if rank_wall_ns else 0.0
    )
    return {
        "schema_version": "legofs.timing-breakdown.v7",
        "client_io_envelope": io_envelope,
        "client_timed_intervals": groups,
        "client_timed_intervals_ns": client_wait_ns,
        "aggregate_rank_wall_ns": rank_wall_ns,
        "client_timed_share_of_rank_wall": (
            client_wait_ns / rank_wall_ns if rank_wall_ns else 0.0
        ),
        "server_direct_write_commit_ns": server_commit_ns,
        "server_commit_share_of_rank_wall": (
            server_commit_ns / rank_wall_ns if rank_wall_ns else 0.0
        ),
        "server_state_deferred_updates": int(
            inspection.get("audit", {}).get("state_deferred_updates", 0)
        ),
        "server_state_validation_ops": int(
            inspection.get("audit", {}).get("state_validation_ops", 0)
        ),
        "server_state_validation_ns": server_state_validation_ns,
        "server_state_validation_share_of_rank_wall": (
            server_state_validation_ns / rank_wall_ns if rank_wall_ns else 0.0
        ),
        "transport_client": transport_client,
        "transport_authority": transport_authority,
        "transport_cq_attribution": cq_attribution,
        "arena_acquire": arena_acquire,
        "direct_write": direct_write,
        "packed_small_segment": packed_small_segment,
        "persistence": {
            "payload_persistence_owner": audit.get("payload_persistence_owner"),
            "writer_host": writer_host_persistence,
            "metadata_wal_persist_ns": metadata_wal_persist_ns,
            "metadata_wal_persist_barriers": int(
                audit.get("metadata_wal_persist_barriers", 0)
            ),
            "metadata_wal_persist_records": int(
                audit.get("metadata_wal_persist_records", 0)
            ),
            "metadata_wal_persist_bytes": int(
                audit.get("metadata_wal_persist_bytes", 0)
            ),
            "metadata_wal_deferred_appends": int(
                audit.get("metadata_wal_deferred_appends", 0)
            ),
            "metadata_wal_deferred_records": int(
                audit.get("metadata_wal_deferred_records", 0)
            ),
            "metadata_wal_deferred_bytes": int(
                audit.get("metadata_wal_deferred_bytes", 0)
            ),
            "metadata_wal_pending_records": int(
                audit.get("metadata_wal_pending_records", 0)
            ),
            "metadata_wal_durable_lsn": int(
                audit.get("metadata_wal_durable_lsn", 0)
            ),
            "lifecycle_payload_persist_ns": lifecycle_payload_persist_ns,
            "lifecycle_state_persist_ns": lifecycle_state_persist_ns,
            "background_lifecycle_persist_ns": background_lifecycle_persist_ns,
            "deferred_state_dependency_close_ns": int(
                audit.get("deferred_state_dependency_close_ns", 0)
            ),
            "deferred_state_not_ready_cuts": int(
                audit.get("deferred_state_not_ready_cuts", 0)
            ),
            "deferred_state_resumed_cuts": int(
                audit.get("deferred_state_resumed_cuts", 0)
            ),
            "authority_coherent_acquire_jobs": int(
                audit.get("authority_coherent_acquire_jobs", 0)
            ),
            "authority_coherent_acquire_ranges": int(
                audit.get("authority_coherent_acquire_ranges", 0)
            ),
            "authority_coherent_acquire_bytes": int(
                audit.get("authority_coherent_acquire_bytes", 0)
            ),
            "authority_coherent_acquire_ns": int(
                audit.get("authority_coherent_acquire_ns", 0)
            ),
            "authority_payload_persist_jobs": int(
                audit.get("authority_payload_persist_jobs", 0)
            ),
            "authority_payload_persist_ranges": int(
                audit.get("authority_payload_persist_ranges", 0)
            ),
            "authority_payload_persist_bytes": int(
                audit.get("authority_payload_persist_bytes", 0)
            ),
            "authority_payload_persist_ns": int(
                audit.get("authority_payload_persist_ns", 0)
            ),
            "persistence_wait_ns": metadata_wal_persist_ns
            + lifecycle_payload_persist_ns
            + lifecycle_state_persist_ns
            + background_lifecycle_persist_ns,
            "provider_persist_barriers": int(
                audit.get("provider_persist_barriers", 0)
            ),
            "provider_persist_bytes": int(audit.get("provider_persist_bytes", 0)),
            "provider_persist_ns": int(audit.get("provider_persist_ns", 0)),
            "provider_payload_barriers": int(
                audit.get("provider_payload_barriers", 0)
            ),
            "provider_payload_bytes": int(
                audit.get("provider_payload_bytes", 0)
            ),
            "writer_persisted_direct_items": int(
                audit.get("writer_persisted_direct_items", 0)
            ),
            "writer_persisted_direct_bytes": int(
                audit.get("writer_persisted_direct_bytes", 0)
            ),
            "host_persist_receipts_accepted": int(
                audit.get("host_persist_receipts_accepted", 0)
            ),
            "host_persist_receipts_duplicate": int(
                audit.get("host_persist_receipts_duplicate", 0)
            ),
            "host_persist_receipts_rejected": int(
                audit.get("host_persist_receipts_rejected", 0)
            ),
            "provider_allocator_barriers": int(
                audit.get("provider_allocator_barriers", 0)
            ),
            "provider_allocator_bytes": int(
                audit.get("provider_allocator_bytes", 0)
            ),
            "foreground_checkpoint_waits": int(
                audit.get("foreground_checkpoint_waits", 0)
            ),
            "foreground_checkpoint_wait_ns": int(
                audit.get("foreground_checkpoint_wait_ns", 0)
            ),
        },
        "visibility_durability": visibility_durability,
        "interpretation": (
            "read_ns and write_ns are unsampled outer syscall envelopes summed across "
            "ranks; client RPC/control values are nested within those envelopes; "
            "transport and persistence intervals are separately accumulated and may be "
            "nested or overlap across ranks, so they are not additive wall-time shares; "
            "none of these counters is CPU time"
        ),
    }


def packed_small_validation_expectation(
    stage: str, packed_small_segments: str, fault_profile: str
) -> bool | None:
    if fault_profile != "none" or stage not in PACKED_SMALL_WORKLOAD_STAGES:
        return None
    return packed_small_segments == "on"


def direct_metadata_breakdown(
    summaries: list[dict], inspection: dict, mode: str
) -> dict:
    client = {
        field: sum(
            int(item.get(field, 0))
            for record in summaries
            for item in record.get("cxl_serving_evidence", [])
        )
        for field in DIRECT_METADATA_CLIENT_COUNTERS
    }
    capability_lanes = sum(
        item.get("direct_metadata_capability") is True
        for record in summaries
        for item in record.get("cxl_serving_evidence", [])
    )
    lane_count = sum(
        len(record.get("cxl_serving_evidence", [])) for record in summaries
    )
    read_metadata_commands = sum(
        int(item.get("dispatches_by_opcode", {}).get("2", 0))
        for record in summaries
        for item in record.get("cxl_serving_evidence", [])
    )
    sqe_submitted = sum(
        int(item.get("sqe_submitted", 0))
        for record in summaries
        for item in record.get("cxl_serving_evidence", [])
    )
    cq_wait_ns = sum(
        int(item.get("client_timing", {}).get("cq_wait_ns", 0))
        for record in summaries
        for item in record.get("cxl_serving_evidence", [])
    )
    authority = {
        field: int(
            inspection.get("audit", {}).get("direct_metadata", {}).get(field, 0)
        )
        for field in DIRECT_METADATA_AUTHORITY_COUNTERS
    }
    attempts = client["direct_metadata_attempts"]
    commands_elided = client["direct_metadata_commands_elided"]
    return {
        "schema_version": "legofs.direct-metadata.v1",
        "mode": mode,
        "client": client,
        "authority": authority,
        "lane_count": lane_count,
        "capability_lanes": capability_lanes,
        "hit_ratio": commands_elided / attempts if attempts else 0.0,
        "read_metadata_commands": read_metadata_commands,
        "baseline_equivalent_read_metadata_demand": (
            read_metadata_commands + commands_elided
        ),
        "all_sqe_submitted": sqe_submitted,
        "cq_wait_ns": cq_wait_ns,
        "cq_wait_ns_per_submitted_command": (
            cq_wait_ns / sqe_submitted if sqe_submitted else 0.0
        ),
        "interpretation": (
            "A direct hit consumes no request id, SQE or CQE. Baseline-equivalent "
            "demand is READ_METADATA commands plus proven commands elided; it is "
            "not a workload-normalization substitute for paired IO500 results."
        ),
    }


def direct_mutation_breakdown(summaries: list[dict]) -> dict:
    client = {
        field: sum(
            int(item.get(field, 0))
            for record in summaries
            for item in record.get("cxl_serving_evidence", [])
        )
        for field in DIRECT_MUTATION_CLIENT_COUNTERS
    }
    open_or_create_commands = sum(
        int(item.get("dispatches_by_opcode", {}).get("61", 0))
        for record in summaries
        for item in record.get("cxl_serving_evidence", [])
    )
    baseline_equivalent_create_demand = (
        open_or_create_commands + client["direct_create_commands_elided"]
    )
    return {
        "schema_version": "legofs.direct-mutation.v1",
        "client": client,
        "open_or_create_commands": open_or_create_commands,
        "baseline_equivalent_create_demand": baseline_equivalent_create_demand,
        "hit_ratio": (
            client["direct_create_commands_elided"] / baseline_equivalent_create_demand
            if baseline_equivalent_create_demand
            else 0.0
        ),
        "attempt_resolution_hit_ratio": (
            client["direct_create_hits"] / client["direct_create_attempts"]
            if client["direct_create_attempts"]
            else 0.0
        ),
    }


def prepare_paths(paths: Paths) -> None:
    if paths.run.exists() or paths.bundle.exists():
        raise FileExistsError(
            f"fixed stage state already exists; preserve or explicitly move it before rerun: "
            f"{paths.run} {paths.bundle}"
        )
    paths.run.mkdir(parents=True, mode=0o700)
    paths.bundle.mkdir(parents=True, mode=0o700)


def verify_manifest(paths: Paths) -> dict:
    manifest = json.loads(paths.manifest.read_text(encoding="utf-8"))
    expected = {
        "qemu": paths.qemu,
        "cxlmemsim_server": paths.cxlmemsim,
        "opensbi": paths.opensbi,
        "u_boot": paths.uboot,
        "linux": paths.linux,
        "payload": paths.payload,
        "dependency_versions": paths.dependency_versions,
        "isa_gate": paths.isa_gate,
    }
    if manifest.get("schema_version") != 2:
        raise ValueError("unsupported build manifest schema")
    for name, path in expected.items():
        entry = manifest.get("artifacts", {}).get(name)
        if not path.is_file() or not isinstance(entry, dict):
            raise FileNotFoundError(f"missing manifested artifact: {name}")
        size = path.stat().st_size
        if size <= 0:
            raise ValueError(f"empty build artifact: {name}")
        recorded_path = pathlib.Path(str(entry.get("path", "")))
        if not recorded_path.is_absolute():
            recorded_path = paths.root / recorded_path
        if recorded_path.resolve() != path.resolve():
            raise ValueError(f"build artifact path mismatch: {name}")
        if entry.get("size") != size:
            raise ValueError(f"build artifact size mismatch: {name}")
        recorded_digest = entry.get("sha256")
        if recorded_digest is not None:
            digest = hashlib.sha256()
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != recorded_digest:
                raise ValueError(f"build artifact hash mismatch: {name}")
    return {"manifest": manifest}


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _u64(data: bytes, offset: int) -> int:
    return struct.unpack_from("<Q", data, offset)[0]


def _mix64(value: int) -> int:
    mask = (1 << 64) - 1
    value &= mask
    value ^= value >> 30
    value = (value * 0xBF58476D1CE4E5B9) & mask
    value ^= value >> 27
    value = (value * 0x94D049BB133111EB) & mask
    return (value ^ (value >> 31)) & mask


def observation_run_hash(run_id: str) -> int:
    mask = (1 << 64) - 1
    value = 0x32BD89A94F0CD619
    for byte in run_id.encode("ascii"):
        value ^= byte
        value = (value * 0x100000001B3) & mask
    return _mix64(value)


def observation_run_id_for_label(result_label: str) -> str:
    # The arena stores a 64-bit hash of this deterministic token. Carrying more
    # than 64 bits through U-Boot only consumes its fixed 255-byte command line
    # without strengthening the on-disk observation identity.
    return "r" + hashlib.sha256(result_label.encode("ascii")).hexdigest()[:16]


def observation_boot_token(config: dict) -> str:
    mode = {"default": "d", "off": "o", "aggregate": "a", "sampled": "s"}[
        config["mode"]
    ]
    token = (
        f"o={mode},{config['sample_shift']},{config['arena_mib']},"
        f"{config['run_id']}"
    )
    if not re.fullmatch(r"o=[doas],[0-9]{1,2},[0-9]{1,2},r[0-9a-f]{16}", token):
        raise ValueError("observation boot token is not in its fixed bounded format")
    return token


def guest_bootargs(
    role: str,
    index: int,
    stage: str,
    server_count: int,
    client_count: int,
    serving_transport: str,
    cursor_mode: str,
    cq_wait_mode: str,
    durability_profile: str,
    payload_persistence_owner: str,
    writer_persist_provider: str,
    packed_small_segments: str,
    small_segment_count: int,
    close_batch_mode: str,
    observation: dict,
) -> str:
    cursor_token = {"legacy_shared": "l", "owned": "o"}[cursor_mode]
    cq_wait_token = {
        "timer_sleep": "s",
        "cooperative_yield": "y",
    }[cq_wait_mode]
    durability_token = {
        "d-before-v": "d",
        "coherent-seal-no-writeback": "n",
        "coherent-seal-needs-writeback": "w",
    }[durability_profile]
    payload_owner_token = {
        ("authority-bi-acquire", "msync"): "a",
        ("writer-receipt", "msync"): "r",
        ("writer-receipt", "riscv-zicbom-dax"): "R",
        ("placement-routed", "msync"): "h",
        ("placement-routed", "riscv-zicbom-dax"): "H",
        ("writer-before-visibility", "msync"): "v",
    }[(payload_persistence_owner, writer_persist_provider)]
    packed_token = 0 if packed_small_segments == "off" else small_segment_count
    close_batch_token = {"immediate": 0, "batched": 1}[close_batch_mode]
    value = (
        "earlycon=sbi console=hvc0 cxl_core.pmem_as_dax=1 "
        f"io500.role={role} io500.index={index} io500.stage={stage} "
        f"io500.server_count={server_count} io500.client_count={client_count} "
        f"io500.serving_transport={serving_transport} "
        f"c={cursor_token} "
        f"w={cq_wait_token} "
        f"d={durability_token} "
        f"u={payload_owner_token} "
        f"p={packed_token} "
        f"b={close_batch_token} "
        f"{observation_boot_token(observation)}"
    )
    command_bytes = len("setenv bootargs ''") + len(value)
    if command_bytes > 255:
        raise ValueError(
            f"U-Boot bootargs command exceeds its 255-byte input bound: {command_bytes}"
        )
    return value


def decode_observation_header(data: bytes) -> dict:
    if len(data) < 4096:
        raise ValueError("observation arena is shorter than its fixed header")
    if data[:8] != b"LEGOFSO1":
        raise ValueError("observation arena magic mismatch")
    header_bytes = _u16(data, 12)
    event_record_bytes = _u16(data, 14)
    if _u16(data, 8) != OBSERVATION_SCHEMA_MAJOR:
        raise ValueError("observation arena schema major mismatch")
    if _u16(data, 10) > OBSERVATION_SCHEMA_MINOR:
        if event_record_bytes < OBSERVATION_EVENT_BYTES:
            raise ValueError("new observation minor has an unskippable event record")
    if header_bytes != 4096 or event_record_bytes < OBSERVATION_EVENT_BYTES:
        raise ValueError("observation arena record layout mismatch")
    identity = 4096
    return {
        "schema_major": _u16(data, 8),
        "schema_minor": _u16(data, 10),
        "header_bytes": header_bytes,
        "event_record_bytes": event_record_bytes,
        "arena_bytes": _u64(data, 16),
        "requested_mode": data[24],
        "effective_mode": data[25],
        "role": data[26],
        "sample_shift": data[27],
        "producer_slots": _u16(data, 28),
        "metric_slots": _u16(data, 30),
        "endpoint_id": _u32(data, 32),
        "pid": _u32(data, 36),
        "exec_incarnation": _u64(data, 40),
        "serving_generation": _u64(data, 48),
        "workload_endpoint_mask": _u64(data, 56),
        "run_hash": _u64(data, 64),
        "realtime_anchor_ns": _u64(data, 72),
        "monotonic_anchor_ns": _u64(data, 80),
        "anchor_span_ns": _u64(data, 88),
        "clock_p50_ns": _u64(data, 96),
        "clock_p99_ns": _u64(data, 104),
        "clock_backend": _u32(data, 112),
        "producer_stride": _u32(data, 116),
        "producer_base": _u64(data, 120),
        "event_base": _u64(data, 128),
        "event_capacity_per_producer": _u64(data, 136),
        "flags": _u64(data, 144),
        "event_count": _u64(data, 152),
        "producer_slots_claimed": _u64(data, 160),
        "producer_overflow": _u64(data, 168),
        "identity_overflow": _u64(data, 176),
        "event_overflow": _u64(data, 184),
        "histogram_saturation": _u64(data, 192),
        "active_scopes": _u64(data, 200),
        "active_requests": _u64(data, 208),
        "pending_persistence": _u64(data, 216),
        "abandoned_scopes": _u64(data, 224),
        "initialization_error": _u64(data, 232),
        "export_error": _u64(data, 240),
        "clock_reads": _u64(data, 248),
        "schema_digest": f"{_u64(data, 416):016x}",
        "identity": {
            "role": data[identity],
            "endpoint_id": _u32(data, identity + 4),
            "pid": _u32(data, identity + 8),
            "mpi_rank": _u32(data, identity + 12),
            "exec_incarnation": _u64(data, identity + 16),
            "serving_generation": _u64(data, identity + 24),
        },
    }


def parse_observation_frames(output: str) -> list[dict]:
    frames = []
    current = None
    encoded_chars = 0
    max_encoded_chars = ((OBSERVATION_MAX_BYTES + 2) // 3) * 4
    for raw_line in output.replace("\r", "").splitlines():
        line = raw_line.strip()
        begin = OBSERVATION_BEGIN_RE.fullmatch(line)
        end = OBSERVATION_END_RE.fullmatch(line)
        if begin is not None:
            if current is not None:
                raise ValueError("nested observation begin marker")
            size = int(begin.group(4))
            if size > OBSERVATION_MAX_BYTES:
                raise ValueError("observation marker declares an oversized arena")
            current = {
                "role": begin.group(1),
                "endpoint": int(begin.group(2)),
                "file": begin.group(3),
                "bytes": size,
                "sha256": begin.group(5),
                "encoding": begin.group(6) or "raw-base64-v1",
            }
            if current["encoding"] == "gzip-base64-v1":
                compressed_bytes = int(begin.group(7))
                chunks = int(begin.group(9))
                max_compressed_bytes = OBSERVATION_MAX_BYTES + 1024 * 1024
                if compressed_bytes > max_compressed_bytes:
                    raise ValueError("observation marker declares oversized compressed data")
                expected_chars = ((compressed_bytes + 2) // 3) * 4
                expected_chunks = (expected_chars + 1023) // 1024
                if chunks == 0 or chunks != expected_chunks:
                    raise ValueError("observation marker chunk count mismatch")
                current.update({
                    "compressed_bytes": compressed_bytes,
                    "compressed_sha256": begin.group(8),
                    "chunks": chunks,
                    "chunk_data": {},
                    "chunk_copies": {},
                })
            else:
                current["base64"] = []
            encoded_chars = 0
            continue
        if end is not None:
            if current is None:
                raise ValueError("observation end marker lacks a begin marker")
            identity = (end.group(1), int(end.group(2)), end.group(3))
            if identity != (current["role"], current["endpoint"], current["file"]):
                raise ValueError("observation begin/end identity mismatch")
            if current["encoding"] == "gzip-base64-v1":
                chunks = current.pop("chunk_data")
                copies = current.pop("chunk_copies")
                expected_sequences = set(range(current["chunks"]))
                if set(chunks) != expected_sequences:
                    raise ValueError("observation compressed frame has missing chunks")
                if any(count > 2 for count in copies.values()):
                    raise ValueError("observation compressed frame has excess chunk copies")
                encoded = "".join(chunks[sequence] for sequence in range(current["chunks"]))
                expected_chars = ((current["compressed_bytes"] + 2) // 3) * 4
                if len(encoded) != expected_chars:
                    raise ValueError("observation compressed base64 length mismatch")
                try:
                    compressed = base64.b64decode(encoded, validate=True)
                except (binascii.Error, ValueError) as error:
                    raise ValueError(f"invalid observation compressed base64: {error}") from error
                if len(compressed) != current["compressed_bytes"]:
                    raise ValueError("observation compressed length mismatch")
                if hashlib.sha256(compressed).hexdigest() != current["compressed_sha256"]:
                    raise ValueError("observation compressed digest mismatch")
                try:
                    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
                    payload = decompressor.decompress(compressed, current["bytes"] + 1)
                    if decompressor.unconsumed_tail or len(payload) > current["bytes"]:
                        raise ValueError("observation gzip expands beyond declared arena")
                    payload += decompressor.flush()
                    if not decompressor.eof or decompressor.unused_data:
                        raise ValueError("observation gzip framing mismatch")
                except zlib.error as error:
                    raise ValueError(f"invalid observation gzip: {error}") from error
            else:
                try:
                    payload = base64.b64decode(
                        "".join(current.pop("base64")), validate=True
                    )
                except (binascii.Error, ValueError) as error:
                    raise ValueError(f"invalid observation base64: {error}") from error
            if len(payload) != current["bytes"]:
                raise ValueError("observation decoded length mismatch")
            if hashlib.sha256(payload).hexdigest() != current["sha256"]:
                raise ValueError("observation payload digest mismatch")
            current["payload"] = payload
            frames.append(current)
            current = None
            continue
        if current is not None:
            if current["encoding"] == "gzip-base64-v1":
                chunk = OBSERVATION_CHUNK_RE.fullmatch(line)
                # Kernel and console diagnostics may arrive asynchronously.  A
                # chunk is accepted only when its complete framed line parses;
                # the guest emits two identical copies so one interrupted copy
                # cannot invalidate an otherwise complete export.
                if chunk is None:
                    continue
                sequence = int(chunk.group(1))
                data = chunk.group(2)
                if sequence >= current["chunks"] or len(data) > 1024:
                    raise ValueError("observation compressed chunk is out of bounds")
                previous = current["chunk_data"].get(sequence)
                if previous is not None and previous != data:
                    raise ValueError("observation compressed chunk copies disagree")
                current["chunk_data"][sequence] = data
                current["chunk_copies"][sequence] = (
                    current["chunk_copies"].get(sequence, 0) + 1
                )
                continue
            if not line:
                continue
            encoded_chars += len(line)
            if encoded_chars > max_encoded_chars:
                raise ValueError("observation base64 exceeds the bounded arena size")
            current["base64"].append(line)
    if current is not None:
        raise ValueError("truncated observation frame")
    return frames


def validate_observation_frame(
    frame: dict,
    *,
    expected_role: str,
    expected_endpoint: int,
    config: dict,
    client_count: int,
) -> dict:
    if frame["role"] != expected_role or frame["endpoint"] != expected_endpoint:
        raise ValueError("observation marker role/endpoint mismatch")
    filename_pattern = re.compile(
        rf"observation-v1-{re.escape(config['run_id'])}-"
        rf"{re.escape(expected_role)}-{expected_endpoint}-(\d+)\.bin"
    )
    filename = filename_pattern.fullmatch(frame["file"])
    if filename is None:
        raise ValueError("observation filename identity mismatch")
    header = decode_observation_header(frame["payload"])
    role_id = {"client-rank": 1, "server": 2, "diagnostic-client": 3}[expected_role]
    mode_id = {"aggregate": 1, "sampled": 2}.get(config["mode"])
    if mode_id is None:
        raise ValueError("an arena was exported while observation was off")
    expected_mask = (1 << client_count) - 1
    if header["arena_bytes"] != len(frame["payload"]):
        raise ValueError("observation header/file arena length mismatch")
    if len(frame["payload"]) != config["arena_bytes"]:
        raise ValueError("observation arena does not match runner configuration")
    if header["requested_mode"] != mode_id or header["effective_mode"] != mode_id:
        raise ValueError("observation requested/effective mode mismatch")
    if header["sample_shift"] != config["sample_shift"]:
        raise ValueError("observation sample shift mismatch")
    if header["producer_slots"] != config["producer_slots"]:
        raise ValueError("observation producer slot count mismatch")
    if header["role"] != role_id or header["endpoint_id"] != expected_endpoint:
        raise ValueError("observation header process role/endpoint mismatch")
    if header["pid"] != int(filename.group(1)):
        raise ValueError("observation filename/header pid mismatch")
    if header["run_hash"] != observation_run_hash(config["run_id"]):
        raise ValueError("observation run identity hash mismatch")
    if header["workload_endpoint_mask"] != expected_mask:
        raise ValueError("observation workload endpoint mask mismatch")
    if header["schema_digest"] != OBSERVATION_SCHEMA_DIGEST:
        raise ValueError("observation arena schema digest mismatch")
    identity = header["identity"]
    if (
        identity["role"] != role_id
        or identity["endpoint_id"] != expected_endpoint
        or identity["pid"] != header["pid"]
        or identity["exec_incarnation"] != header["exec_incarnation"]
        or identity["serving_generation"] != header["serving_generation"]
    ):
        raise ValueError("observation identity table/header mismatch")
    expected_rank = expected_endpoint if expected_role == "client-rank" else 0xFFFFFFFF
    if identity["mpi_rank"] != expected_rank:
        raise ValueError("observation MPI rank identity mismatch")
    incomplete = any(
        header[name] != 0
        for name in ("active_scopes", "active_requests", "pending_persistence")
    )
    runtime_invalid = any(
        header[name] != 0
        for name in (
            "flags",
            "producer_overflow",
            "identity_overflow",
            "event_overflow",
            "histogram_saturation",
            "abandoned_scopes",
            "initialization_error",
            "export_error",
        )
    )
    header["snapshot_complete"] = not incomplete
    header["runtime_valid"] = not runtime_invalid
    return header


def dump_observations(
    paths: Paths,
    server_consoles: list[Console],
    client_consoles: list[Console],
    config: dict,
    client_count: int,
    stage: str,
    timeout: int,
) -> dict:
    paths.observation_raw.mkdir(parents=True, exist_ok=False)
    participants = [
        ("client-rank", index, console)
        for index, console in enumerate(client_consoles)
    ] + [
        ("server", index, console)
        for index, console in enumerate(server_consoles)
    ]
    starts = [len(console.output) for _, _, console in participants]
    for _, _, console in participants:
        console.send("LEGOFS_DUMP_OBSERVATION")
    records = []
    enabled = config["mode"] in ("aggregate", "sampled")
    for (role, endpoint, console), start in zip(participants, starts):
        console.wait(
            f"LEGOFS_IO500_OBSERVATION_DONE role={role} index={endpoint}",
            min(timeout, 1800),
            start,
        )
        frames = parse_observation_frames(console.output[start:])
        expected_files = 1 if enabled and (role == "server" or stage != "hello") else 0
        if len(frames) != expected_files:
            raise ValueError(
                f"expected {expected_files} observation arena(s) for {role}{endpoint}, "
                f"got {len(frames)}"
            )
        for frame in frames:
            header = validate_observation_frame(
                frame,
                expected_role=role,
                expected_endpoint=endpoint,
                config=config,
                client_count=client_count,
            )
            destination = paths.observation_raw / frame["file"]
            with destination.open("xb") as output:
                output.write(frame["payload"])
            records.append({
                "role": role,
                "endpoint": endpoint,
                "file": str(destination.relative_to(paths.bundle)),
                "bytes": frame["bytes"],
                "sha256": frame["sha256"],
                "header": header,
            })
    valid = all(
        record["header"]["snapshot_complete"]
        and record["header"]["runtime_valid"]
        for record in records
    )
    if not enabled:
        valid = not records
    from legofs_observe import analyze_observation_files

    analysis = analyze_observation_files(
        [paths.bundle / record["file"] for record in records],
        paths.observation,
        config=config,
    )
    return {
        "schema_version": "legofs.observation.export.v1",
        "config": config,
        "files": records,
        "analysis": analysis,
        "observation_valid": valid and analysis["observation_valid"],
        "observation_error": None,
        "export_after_mpi_completion": True,
    }


def dump_observations_safely(*args, **kwargs) -> dict:
    config = kwargs.get("config")
    try:
        return dump_observations(*args, **kwargs)
    except BaseException as error:
        return {
            "schema_version": "legofs.observation.export.v1",
            "config": config,
            "files": [],
            "analysis": None,
            "observation_valid": False,
            "observation_error": str(error),
            "export_after_mpi_completion": True,
        }


def sparse_file(path: pathlib.Path, size: int) -> None:
    with path.open("xb") as output:
        output.truncate(size)


def ext2_image(path: pathlib.Path, size: int) -> None:
    sparse_file(path, size)
    subprocess.run(["mke2fs", "-q", "-t", "ext2", "-F", str(path)], check=True)


def server_command(
    paths: Paths,
    port: int,
    trace: bool,
    ssd_cache_mib: int = DEFAULT_SSD_CACHE_MIB,
    full_trace: bool = False,
) -> list[str]:
    command = [
        str(paths.cxlmemsim),
        "--comm-mode=tcp",
        f"--port={port}",
        "--capacity=65536",
        "--default_latency=100",
        f"--topology={paths.root / 'components/cxlmemsim/qemu_integration/topology_simple.txt'}",
        "--coherence-v2=true",
        "--coherence-v2-snoop-timeout-ms=30000",
        "--backing-mode=ssd-stream",
        f"--ssd-backing-file={paths.central_ssd}",
        "--ssd-page-size=4096",
        "--ssd-io-chunk-size=65536",
        f"--ssd-cache-mb={ssd_cache_mib}",
        "--ssd-read-ahead-pages=16",
        "--ssd-io-uring=false",
        "--ssd-odirect=false",
    ]
    if full_trace or trace:
        command.append(f"--coherence-v2-trace={paths.coherence}")
    # The audited author baseline always prints aggregate MESI counters during
    # shutdown.  It has no counter-enable CLI switch, so the normal score path
    # deliberately adds no non-standard simulator control here.
    return command


def qemu_command(
    paths: Paths,
    *,
    role: str,
    server_index: int | None,
    client_index: int | None,
    host_id: int,
    coherence_port: int,
    multicast_port: int,
    coherence_cache_bytes: int = DEFAULT_COHERENCE_CACHE_MIB * 1024**2,
    read_exclusive: bool = False,
) -> list[str]:
    if role == "server":
        if server_index is None or client_index is not None:
            raise ValueError("server QEMU requires only server_index")
        label = f"server{server_index}"
    else:
        if client_index is None or server_index is not None:
            raise ValueError("client QEMU requires only client_index")
        label = f"client{client_index}"
    machine_id = f"cxl-{label}"
    command = [
        "qemu-system-riscv64",
        "-M", "sifive_u",
        # device-DAX persistence uses the standard RISC-V Zicbom cache-block
        # operations.  sifive_u's default u54 CPU omits that extension.
        "-cpu", "rv64,h=false,sstc=false,svadu=false,zicboz=false,"
                "zicbom=true,cbom_blocksize=64",
        "-machine", "cxl=on",
        "-machine",
        f"cxl-fmw.0.targets.0={machine_id},cxl-fmw.0.size={FMW_SIZE},"
        # Device-coherent, persistent HDM-DB: DEVMEM | PMEM | BI.
        "cxl-fmw.0.restrictions=0x29",
        "-smp", "5",
        "-m", "2G",
        "-display", "none",
        "-serial", "stdio",
        "-monitor", "none",
        "-no-reboot",
        "-bios", str(paths.opensbi),
        "-kernel", str(paths.uboot),
        "-device", f"loader,file={paths.linux},addr=0x90000000,force-raw=on",
        "-object",
        f"memory-backend-file,id=t3ssd-{label},mem-path={paths.device_dram},"
        "size=64G,share=on",
        "-object",
        f"memory-backend-file,id=t3lsa-{label},mem-path={paths.lsa(host_id)},"
        "size=2M,share=on",
        "-device",
        f"pxb-cxl,bus=pcie.0,bus_nr=64,id={machine_id},hdm_for_passthrough=on",
        "-device",
        f"cxl-rp,bus={machine_id},port=0,id=rp-{label},chassis=0,slot=0,"
        "x-256b-flit=on",
        "-device",
        f"cxl-type3,bus=rp-{label},persistent-memdev=t3ssd-{label},"
        f"lsa=t3lsa-{label},id=t3-{label},coherence-v2=on,"
        "x-256b-flit=on,hdm-db=on,"
        f"cxlmemsim-addr=127.0.0.1,cxlmemsim-port={coherence_port},"
        f"coherence-v2-host-id={host_id},"
        f"coherence-v2-cache-capacity={coherence_cache_bytes},"
        "coherence-v2-cache-ways=4,coherence-v2-timeout-ms=30000,"
        "coherence-v2-write-through=off,"
        f"coherence-v2-read-exclusive={'on' if read_exclusive else 'off'}",
        "-drive",
        f"file={paths.payload},if=none,format=raw,readonly=on,id=payload-{label}",
        "-device",
        f"virtio-blk-pci,drive=payload-{label},bus=pcie.0,id=payload-dev-{label},romfile=",
    ]
    state = paths.server_state(server_index) if role == "server" else paths.client_result
    if role == "server" or client_index == 0:
        command.extend([
            "-drive", f"file={state},if=none,format=raw,id=state-{label}",
            "-device",
            f"virtio-blk-pci,drive=state-{label},bus=pcie.0,id=state-dev-{label},romfile=",
        ])
    command.extend([
        "-netdev", f"socket,id=net-{label},mcast=230.77.0.1:{multicast_port}",
        "-device",
        f"virtio-net-pci,netdev=net-{label},bus=pcie.0,id=nic-{label},"
        f"mac=52:54:00:77:00:{host_id:02x},romfile=",
    ])
    return command


def wait_log(path: pathlib.Path, marker: str, process, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"CXLMemSim exited before readiness rc={process.returncode}")
        if path.is_file() and marker in path.read_text(encoding="utf-8", errors="replace"):
            return
        time.sleep(0.05)
    raise TimeoutError(f"timed out waiting for {marker!r}")


def wait_guest_marker(
    console: Console, marker: str, timeout: int, start: int = 0
) -> None:
    """Wait for guest readiness while treating a guest fatal as terminal."""
    deadline = time.monotonic() + timeout
    with console.condition:
        while True:
            stage_output = console.output[start:]
            for fatal in MPI_FATAL_MARKERS:
                if fatal in stage_output:
                    line = stage_output[stage_output.index(fatal):].splitlines()[0]
                    raise RuntimeError(
                        f"guest reported fatal marker {line!r} while waiting "
                        f"for {marker!r}"
                    )
            if marker in stage_output:
                return
            if console.process.poll() is not None:
                raise RuntimeError(
                    f"QEMU exited with {console.process.returncode} while waiting "
                    f"for {marker!r}"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"timed out waiting for {marker!r}")
            console.condition.wait(min(remaining, 0.5))


def boot_guest(
    console: Console,
    paths: Paths,
    role: str,
    index: int,
    stage: str,
    server_count: int,
    client_count: int,
    serving_transport: str,
    cursor_mode: str,
    cq_wait_mode: str,
    durability_profile: str,
    payload_persistence_owner: str,
    writer_persist_provider: str,
    packed_small_segments: str,
    small_segment_count: int,
    close_batch_mode: str,
    observation: dict,
    timeout: int,
) -> None:
    console.wait("Hit any key to stop autoboot", timeout)
    console.send("")
    console.wait("=> ", timeout)
    if HOST_DECODER not in console.output or TYPE3_DECODER not in console.output:
        raise ValueError(f"{role}{index} preboot decoder proof is missing")
    listing = console.command_until_prompt("cxl list", timeout)
    if "41.00.0" not in listing or "Type 3" not in listing:
        raise ValueError(f"{role}{index} Type 3 enumeration is missing")
    information = console.command_until_prompt("cxl info 41.00.0", timeout)
    if DECODER_SIZE not in information:
        raise ValueError(f"{role}{index} Type 3 size is not 64 GiB")
    initialized = console.command_until_prompt("cxl init", timeout)
    if HOST_DECODER not in initialized or TYPE3_DECODER not in initialized:
        raise ValueError(f"{role}{index} decoder initialization is incomplete")
    bootargs = guest_bootargs(
        role,
        index,
        stage,
        server_count,
        client_count,
        serving_transport,
        cursor_mode,
        cq_wait_mode,
        durability_profile,
        payload_persistence_owner,
        writer_persist_provider,
        packed_small_segments,
        small_segment_count,
        close_batch_mode,
        observation,
    )
    console.command_until_prompt(f"setenv bootargs '{bootargs}'", timeout)
    console.send(f"bootefi 90000000:{paths.linux.stat().st_size:x} ${{fdtcontroladdr}}")


def parse_trace(path: pathlib.Path) -> list[dict]:
    # The tiny-stage persistence proof is collected while CXLMemSim is still
    # appending to this file.  Read one byte snapshot and discard only its
    # unterminated tail; iterating the live file can otherwise observe the
    # writer between the body and newline of an otherwise valid JSON record.
    snapshot = path.read_bytes()
    complete_end = snapshot.rfind(b"\n")
    if complete_end < 0:
        return []
    records = []
    complete = snapshot[:complete_end].decode("utf-8")
    for number, line in enumerate(complete.splitlines(), 1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid coherence JSONL record {number}: {error}") from error
        if record.get("schema_version") != 1:
            raise ValueError("unsupported coherence trace schema")
        records.append(record)
    return records


def registrations(records: list[dict], host_count: int) -> list[dict]:
    matches = [
        record for record in records
        if record.get("event") == "registration" and record.get("status") == "OK"
    ]
    by_host = {}
    sessions = set()
    for record in matches:
        host = record.get("src_host")
        session = record.get("session_id")
        if not isinstance(host, int) or not isinstance(session, int):
            raise ValueError("registration identity is not integral")
        if host in by_host or session == 0 or session in sessions:
            raise ValueError("duplicate coherence host or session identity")
        by_host[host] = record
        sessions.add(session)
    if set(by_host) != set(range(host_count)):
        raise ValueError(
            f"expected host IDs 0..{host_count - 1}, got {sorted(by_host)}"
        )
    return [by_host[index] for index in range(host_count)]


def wait_mpi_exit(
    console: Console,
    stage: str,
    timeout: int,
    start: int,
    monitored_guests: list[tuple[str, Console, int]] | None = None,
) -> int:
    pattern = re.compile(
        rf"LEGOFS_IO500_MPI_EXIT stage={re.escape(stage)} rc=(-?\d+)(?=[^0-9])"
    )
    deadline = time.monotonic() + timeout
    if monitored_guests is None:
        monitored_guests = [("coordinator", console, start)]
    with console.condition:
        while True:
            for guest, monitored, guest_start in monitored_guests:
                stage_output = monitored.output[guest_start:]
                for marker in MPI_FATAL_MARKERS:
                    if marker in stage_output:
                        raise RuntimeError(
                            f"MPI stage {stage} guest {guest} reported fatal marker: "
                            f"{marker}"
                        )
                for marker in GUEST_PROCESS_FAULT_MARKERS:
                    if marker in stage_output:
                        raise RuntimeError(
                            f"MPI stage {stage} guest {guest} reported fatal "
                            f"process fault: {marker}"
                        )
                for marker in PERFORMANCE_INVALIDATING_GUEST_MARKERS:
                    if marker in stage_output:
                        raise RuntimeError(
                            f"INCONCLUSIVE_RACE: MPI stage {stage} guest {guest} "
                            f"reported kernel scheduling stall: {marker}"
                        )
            match = pattern.search(console.output, start)
            if match is not None:
                rc = int(match.group(1))
                if rc != 0:
                    raise MpiStageError(stage, rc)
                return rc
            if console.process.poll() is not None:
                raise RuntimeError(
                    f"QEMU exited with {console.process.returncode} while waiting for MPI exit"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"timed out waiting for MPI stage {stage} exit")
            console.condition.wait(min(remaining, 0.5))


def wait_verify_exit(console: Console, stage: str, timeout: int, start: int) -> int:
    pattern = re.compile(
        rf"LEGOFS_IO500_VERIFY_EXIT stage={re.escape(stage)} rc=(-?\d+)(?=[^0-9])"
    )
    deadline = time.monotonic() + timeout
    with console.condition:
        while True:
            match = pattern.search(console.output, start)
            if match is not None:
                return int(match.group(1))
            if console.process.poll() is not None:
                raise RuntimeError(
                    f"QEMU exited with {console.process.returncode} while waiting for verifier"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"timed out waiting for IO500 verifier for {stage}")
            console.condition.wait(min(remaining, 0.5))


def parse_hydra_proxy_commands(
    output: str, client_count: int = DEFAULT_CLIENTS
) -> dict[int, str]:
    commands = [command.rstrip("\r") for command in MPI_LAUNCH_RE.findall(output)]
    if len(commands) != client_count:
        raise ValueError(
            f"Hydra emitted {len(commands)} proxy commands, expected {client_count}"
        )
    by_proxy = {}
    for command in commands:
        match = PROXY_ID_RE.search(command)
        if not match:
            raise ValueError("Hydra proxy command lacks --proxy-id")
        proxy_id = int(match.group(1))
        if proxy_id in by_proxy:
            raise ValueError(f"duplicate Hydra proxy ID: {proxy_id}")
        by_proxy[proxy_id] = command
    if set(by_proxy) != set(range(client_count)):
        raise ValueError(
            f"Hydra proxy IDs are not 0..{client_count - 1}: {sorted(by_proxy)}"
        )
    return by_proxy


def synchronize_guest_clocks(
    server_consoles: list[Console],
    client_consoles: list[Console],
    *,
    target_epoch: int | None = None,
    client_count: int = DEFAULT_CLIENTS,
    allowed_receipt_lag_seconds: int = 5,
) -> dict:
    if len(client_consoles) != client_count:
        raise ValueError(f"clock sync requires {client_count} client guests")
    if target_epoch is None:
        target_epoch = int(time.time())
    if target_epoch <= 0:
        raise ValueError("clock sync target must be a positive Unix epoch")

    participants = [
        ("server", index, console)
        for index, console in enumerate(server_consoles)
    ] + [
        ("client", index, console)
        for index, console in enumerate(client_consoles)
    ]
    starts = [len(console.output) for _, _, console in participants]
    dispatch_ns = time.monotonic_ns()
    for _, _, console in participants:
        console.send(f"LEGOFS_SET_TIME {target_epoch}")

    records = []
    for (role, index, console), start in zip(participants, starts):
        deadline = time.monotonic() + 30
        with console.condition:
            while True:
                matches = [
                    match
                    for match in TIME_SYNC_RE.finditer(console.output[start:])
                    if match.group(1) == role
                    and int(match.group(2)) == index
                    and int(match.group(3)) == target_epoch
                ]
                if len(matches) > 1:
                    raise ValueError(
                        f"expected one clock-sync receipt for {role}{index}"
                    )
                if matches:
                    observed = int(matches[0].group(4))
                    break
                if console.process.poll() is not None:
                    raise RuntimeError(
                        f"QEMU exited while waiting for clock-sync receipt "
                        f"from {role}{index}"
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"timed out waiting for clock-sync receipt from {role}{index}"
                    )
                console.condition.wait(min(remaining, 0.5))
        if not target_epoch <= observed <= target_epoch + allowed_receipt_lag_seconds:
            raise ValueError(
                f"clock-sync receipt for {role}{index} is outside the allowed window"
            )
        records.append({
            "role": role,
            "index": index,
            "requested_epoch": target_epoch,
            "observed_epoch": observed,
        })
    return {
        "method": "PID-1 console command using BusyBox date and one shared host epoch",
        "target_epoch": target_epoch,
        "host_dispatch_monotonic_ns": dispatch_ns,
        "host_complete_monotonic_ns": time.monotonic_ns(),
        "allowed_receipt_lag_seconds": allowed_receipt_lag_seconds,
        "records": records,
    }


def _send_and_wait(
    console: Console, command: str, marker: str, timeout: int
) -> dict:
    start = len(console.output)
    started_ns = time.monotonic_ns()
    console.send(command)
    console.wait(marker, timeout, start)
    completed_ns = time.monotonic_ns()
    return {
        "command": command,
        "marker": marker,
        "started_monotonic_ns": started_ns,
        "completed_monotonic_ns": completed_ns,
        "elapsed_ns": completed_ns - started_ns,
        "output": console.output[start:],
    }


def parse_system_sync_probe(output: str, label: str) -> dict:
    matches = list(SYSTEM_SYNC_PROBE_RE.finditer(output))
    exits = list(PROXY_EXIT_RE.finditer(output))
    if len(matches) != 1:
        return {
            "label": label,
            "program_reached": False,
            "proxy_rc": int(exits[-1].group(2)) if exits else None,
            "raw_output": output,
        }
    match = matches[0]
    if match.group(1) != label:
        raise ValueError(
            f"system-sync probe label mismatch: expected {label}, got {match.group(1)}"
        )
    if len(exits) != 1 or int(exits[0].group(1)) != 0:
        raise ValueError("system-sync probe lacks one client0 proxy exit record")
    names = (
        "raw_status",
        "errno",
        "child_reached",
        "exited",
        "exit_status",
        "signaled",
        "term_signal",
    )
    values = [int(value) for value in match.groups()[1:]]
    return {
        "label": label,
        "program_reached": True,
        **dict(zip(names, values)),
        "proxy_rc": int(exits[0].group(2)),
    }


def run_system_sync_probe(
    console: Console, client_count: int, timeout: int
) -> dict:
    """Compare system(3) with no preload, libc-only patching, and all DSOs."""
    first_endpoint = 2 * client_count + 2
    if first_endpoint + 1 >= RECOVERY_CONTROL_LANE_BASE:
        raise ValueError("system-sync diagnostic endpoints overlap reserved lanes")
    commands = (
        (
            "control",
            "LEGOFS_PROXY LD_PRELOAD= /payload/bin/system-sync-probe control",
            None,
        ),
        (
            "preload-libc",
            "LEGOFS_PROXY "
            f"BADFS_CLIENT_ENDPOINT_ID={first_endpoint} "
            "BADFS_OBSERVATION_MODE=off "
            "INTERCEPT_LOG=/tmp/system-sync-preload-libc.log "
            "LD_PRELOAD=/payload/lib/libbadfs_intercept.so "
            "/payload/bin/system-sync-probe preload-libc",
            "/tmp/system-sync-preload-libc.log",
        ),
        (
            "preload-all",
            "LEGOFS_PROXY "
            f"BADFS_CLIENT_ENDPOINT_ID={first_endpoint + 1} "
            "BADFS_OBSERVATION_MODE=off INTERCEPT_ALL_OBJS=1 "
            "INTERCEPT_LOG=/tmp/system-sync-preload-all.log "
            "LD_PRELOAD=/payload/lib/libbadfs_intercept.so "
            "/payload/bin/system-sync-probe preload-all",
            "/tmp/system-sync-preload-all.log",
        ),
    )
    records = []
    for label, command, log_path in commands:
        event = _send_and_wait(
            console,
            command,
            "LEGOFS_IO500_PROXY_EXIT index=0 rc=",
            timeout,
        )
        record = parse_system_sync_probe(event["output"], label)
        record["elapsed_ns"] = event["elapsed_ns"]
        if log_path is not None:
            log_event = _send_and_wait(
                console,
                "LEGOFS_PROXY LD_PRELOAD= /bin/busybox grep -E "
                "' -- (clone|execve|pipe2|wait4|fcntl)' "
                + log_path,
                "LEGOFS_IO500_PROXY_EXIT index=0 rc=0",
                timeout,
            )
            record["syscall_log_tail"] = [
                line.rstrip("\r")
                for line in log_event["output"].splitlines()
                if " -- " in line
            ]
        records.append(record)
    control = records[0]
    if not (
        control.get("program_reached")
        and control.get("raw_status") == 0
        and control.get("child_reached") == 1
        and control.get("proxy_rc") == 0
    ):
        raise ValueError(f"unpreloaded system-sync control failed: {control}")
    return {
        "schema_version": "legofs.system-sync-probe.v1",
        "profile": "diagnose-system-sync",
        "outside_io500_timing": True,
        "records": records,
    }


def run_clean_restart_fault(
    server_console: Console,
    client_consoles: list[Console],
    timeout: int,
) -> dict:
    """Exercise one cooperative process restart without preserving open OFDs."""
    if len(client_consoles) != 2:
        raise ValueError("clean-server-restart requires exactly two client guests")
    events = []
    events.append(_send_and_wait(
        client_consoles[0],
        "LEGOFS_CLEAN_RESTART_COHORT produce 42",
        "LEGOFS_IO500_CLEAN_RESTART_COHORT_EXIT index=0 action=produce endpoint=42 generation=1 rc=0",
        timeout,
    ))
    events.append(_send_and_wait(
        client_consoles[1],
        "LEGOFS_CLEAN_RESTART_COHORT observe 43",
        "LEGOFS_IO500_CLEAN_RESTART_COHORT_EXIT index=1 action=observe endpoint=43 generation=1 rc=0",
        timeout,
    ))
    control = _send_and_wait(
        client_consoles[0],
        "LEGOFS_CLEAN_RESTART_CONTROL 2 7640891576956012809",
        "LEGOFS_IO500_CLEAN_RESTART_CONTROL_EXIT index=0 target_generation=2 rc=0",
        timeout,
    )
    events.append(control)
    control_records = [
        json.loads(match.group(1).rstrip("\r"))
        for match in CLEAN_RESTART_CONTROL_RE.finditer(control["output"])
    ]
    if len(control_records) != 1:
        raise ValueError(
            f"clean restart emitted {len(control_records)} control records, expected one"
        )
    transport = control_records[0].get("transport", {})
    if transport.get("filesystem_tcp_requests_after_cxl_ready") != 0:
        raise ValueError("clean restart used filesystem TCP after CXL ready")
    if transport.get("legacy_tarpc_calls_after_cxl_ready") != 0:
        raise ValueError("clean restart used legacy tarpc after CXL ready")
    if transport.get("blob_tcp_bytes_after_cxl_ready") != 0:
        raise ValueError("clean restart used Blob TCP after CXL ready")
    if transport.get("transport_fallbacks_after_cxl_ready") != 0:
        raise ValueError("clean restart used transport fallback after CXL ready")
    opcodes = transport.get("dispatches_by_opcode", {})
    if {str(key): int(value) for key, value in opcodes.items()} != {
        "2": 1,
        "3": 1,
        "4": 1,
    }:
        raise ValueError(f"unexpected clean-restart opcode counts: {opcodes}")

    events.append(_send_and_wait(
        server_console,
        "LEGOFS_SERVER_CLEAN_RESTART 2",
        "LEGOFS_IO500_SERVER_RESTARTED index=0",
        timeout,
    ))
    for index, console in enumerate(client_consoles):
        events.append(_send_and_wait(
            console,
            "LEGOFS_CLIENT_GENERATION 2",
            f"LEGOFS_IO500_CLIENT_GENERATION_SET index={index} generation=2",
            timeout,
        ))
    events.append(_send_and_wait(
        client_consoles[0],
        "LEGOFS_CLEAN_RESTART_COHORT recover 42",
        "LEGOFS_IO500_CLEAN_RESTART_COHORT_EXIT index=0 action=recover endpoint=42 generation=2 rc=0",
        timeout,
    ))
    events.append(_send_and_wait(
        client_consoles[1],
        "LEGOFS_CLEAN_RESTART_COHORT delete 43",
        "LEGOFS_IO500_CLEAN_RESTART_COHORT_EXIT index=1 action=delete endpoint=43 generation=2 rc=0",
        timeout,
    ))
    return {
        "schema_version": "legofs.clean-server-restart.v1",
        "profile": "clean-server-restart",
        "functional_model_only": True,
        "physical_hardware_evidence": False,
        "cohort_a": ["produce", "observe"],
        "cohort_b": ["recover", "delete"],
        "control": control_records,
        "events": [
            {key: value for key, value in event.items() if key != "output"}
            for event in events
        ],
    }


def run_unauthorized_clean_restart_probe(
    server_console: Console,
    timeout: int,
) -> dict:
    """Prove that a successor without clean authorization dies pre-listener."""
    event = _send_and_wait(
        server_console,
        "LEGOFS_SERVER_UNAUTHORIZED_RESTART_PROBE 2",
        (
            "LEGOFS_IO500_UNAUTHORIZED_RESTART_REJECTED index=0 "
            "target_generation=2 listener_open=0"
        ),
        timeout,
    )
    return {
        "schema_version": "legofs.unauthorized-clean-restart.v1",
        "profile": "reject-unauthorized-clean-restart",
        "functional_model_only": True,
        "physical_hardware_evidence": False,
        "event": event,
    }


def run_active_lane_retirement_probe(
    client_consoles: list[Console],
    timeout: int,
) -> dict:
    """Prove that recovery control reports, but never retires, a live lane."""
    if len(client_consoles) != 2:
        raise ValueError("reject-active-clean-retirement requires two clients")
    holder = client_consoles[0]
    inspector = client_consoles[1]
    holder_start = len(holder.output)
    holder.send("LEGOFS_CLEAN_RESTART_COHORT hold 42")
    holder.wait("badfs_clean_restart_hold_ready hold_ms=30000", timeout, holder_start)
    inspection = _send_and_wait(
        inspector,
        "LEGOFS_INSPECT_ENDPOINT 41",
        "LEGOFS_IO500_INSPECT_ENDPOINT_EXIT index=1 endpoint=41 rc=0",
        timeout,
    )
    lifecycle_inspection = parse_server_inspection(
        inspection["output"],
        1,
        expected_active_client_lanes=1,
    )
    recovery = lifecycle_inspection["recovery_control"]
    retirement = recovery.get("retirement", {})
    if retirement.get("active_client_lanes") != 1:
        raise ValueError(f"active lane was not preserved: {retirement}")
    transport = recovery.get("transport", {})
    for forbidden in (
        "filesystem_tcp_requests_after_cxl_ready",
        "legacy_tarpc_calls_after_cxl_ready",
        "blob_tcp_bytes_after_cxl_ready",
        "transport_fallbacks_after_cxl_ready",
    ):
        if transport.get(forbidden) != 0:
            raise ValueError(f"active-lane probe used forbidden path: {forbidden}")
    holder.wait(
        (
            "LEGOFS_IO500_CLEAN_RESTART_COHORT_EXIT index=0 action=hold "
            "endpoint=42 generation=1 rc=0"
        ),
        timeout,
        holder_start,
    )
    return {
        "schema_version": "legofs.active-clean-retirement.v1",
        "profile": "reject-active-clean-retirement",
        "functional_model_only": True,
        "physical_hardware_evidence": False,
        "lifecycle_inspection": lifecycle_inspection,
    }


def launch_mpi(
    server_consoles: list[Console],
    client_consoles: list[Console],
    stage: str,
    timeout: int,
    client_count: int = DEFAULT_CLIENTS,
    serving_transport: str = "legacy",
) -> dict:
    coordinator = client_consoles[0]
    monitored_guests = [
        (f"server{index}", console, len(console.output))
        for index, console in enumerate(server_consoles)
    ] + [
        (f"client{index}", console, len(console.output))
        for index, console in enumerate(client_consoles)
    ]
    start = len(coordinator.output)
    coordinator.send(f"LEGOFS_MPI {stage}")
    coordinator.wait(f"LEGOFS_IO500_MPI_STARTED stage={stage}", 30, start)
    coordinator.wait("HYDRA_LAUNCH_END", 120, start)
    launch_text = coordinator.output[start:]
    by_proxy = parse_hydra_proxy_commands(launch_text, client_count)
    for proxy_id in range(client_count):
        client_consoles[proxy_id].send(f"LEGOFS_PROXY {by_proxy[proxy_id]}")
    for proxy_id, console in enumerate(client_consoles):
        console.wait(f"LEGOFS_IO500_PROXY_STARTED index={proxy_id}", 30)
    post_preflight_clock_sync = None
    if stage == "tiny" and serving_transport == "cxl":
        # Hydra forwards every rank's stdout through the coordinator console,
        # even though each rank executes in a different client guest.
        for endpoint in range(client_count):
            coordinator.wait(
                f"LEGOFS_IO500_PREFLIGHT_READY endpoint={endpoint}", timeout
            )
        post_preflight_clock_sync = synchronize_guest_clocks(
            server_consoles, client_consoles, client_count=client_count
        )
        for endpoint in range(client_count):
            coordinator.wait(
                f"LEGOFS_IO500_PREFLIGHT_RELEASED endpoint={endpoint}", 30
            )
    # Never step CLOCK_REALTIME after the measured workload is released.
    # MPI/IO500 implementations may derive cross-rank phase boundaries from
    # clocks that are not monotonic across `date -s`; periodic console-driven
    # correction therefore corrupts elapsed time and also perturbs TCG.  The
    # topology-wide synchronization above is the final clock mutation for this
    # run.  Any later filesystem timestamp-coherence failure is a product bug
    # to diagnose, not something the measurement harness may hide.
    rc = wait_mpi_exit(
        coordinator,
        stage,
        timeout,
        start,
        monitored_guests=monitored_guests,
    )
    return {
        "launcher": "MPICH Hydra manual",
        "coordinator": "client0",
        "returncode": rc,
        "proxy_commands": [by_proxy[index] for index in range(client_count)],
        "post_preflight_clock_sync": post_preflight_clock_sync,
        "clock_maintenance": {
            "enabled": False,
            "interval_seconds": None,
            "synchronizations": [],
            "errors": [],
            "reason": "measurement-window clock mutation is forbidden",
        },
        "output_start": start,
        "output_end": len(coordinator.output),
    }


def parse_hello(
    consoles: list[Console], client_count: int = DEFAULT_CLIENTS
) -> dict:
    records = []
    for console in consoles:
        for match in HELLO_RE.finditer(console.output):
            records.append({
                "rank": int(match.group(1)),
                "size": int(match.group(2)),
                "host": match.group(3),
                "begin_ns": int(match.group(4)),
                "end_ns": int(match.group(5)),
            })
    by_rank = {record["rank"]: record for record in records}
    if len(records) != client_count or set(by_rank) != set(range(client_count)):
        raise ValueError("MPI hello did not produce exactly one record for every rank")
    for rank, record in by_rank.items():
        if record["size"] != client_count or record["host"] != f"client{rank}":
            raise ValueError(f"rank {rank} placement mismatch: {record}")
        if record["begin_ns"] >= record["end_ns"]:
            raise ValueError("MPI hello interval is empty")
    overlap = min(item["end_ns"] for item in records) - max(item["begin_ns"] for item in records)
    if overlap <= 0:
        raise ValueError("MPI rank intervals did not overlap")
    return {
        "records": [by_rank[i] for i in range(client_count)],
        "overlap_ns": overlap,
    }


def parse_rank_markers(
    consoles: list[Console],
    stage: str,
    client_count: int = DEFAULT_CLIENTS,
) -> list[dict]:
    records = []
    for console in consoles:
        for match in RANK_RE.finditer(console.output):
            if match.group(1) == stage:
                records.append({
                    "stage": stage,
                    "rank": int(match.group(2)),
                    "size": int(match.group(3)),
                    "endpoint": int(match.group(4)),
                    "dax": match.group(5),
                })
    by_rank = {record["rank"]: record for record in records}
    if len(records) != client_count or set(by_rank) != set(range(client_count)):
        raise ValueError("IO500 did not execute exactly one rank on every client")
    for rank, record in by_rank.items():
        if record["size"] != client_count or record["endpoint"] != rank:
            raise ValueError(f"IO500 rank {rank} endpoint mismatch")
        if not record["dax"].startswith("/dev/dax"):
            raise ValueError(f"IO500 rank {rank} did not resolve device-DAX")
    return [by_rank[index] for index in range(client_count)]


def validate_cxl_path_summary(
    record: dict,
    endpoint: int,
    authority_count: int | None = None,
    expected_cursor_mode: str | None = None,
    expected_cq_wait_mode: str | None = None,
    expected_direct_metadata_mode: str | None = None,
) -> int:
    if record.get("schema_version") != "badfs.posix.path-summary.v4":
        raise ValueError("unexpected POSIX summary schema")
    if record.get("intercept_enabled") is not True:
        raise ValueError("POSIX summary does not prove syscall interception")
    classification = record.get("syscall_classification", {})
    totals = classification.get("totals", {})
    if totals.get("forbidden_badfs_forward") != 0:
        raise ValueError("BadFS-owned syscall escaped to the guest kernel")

    evidence = record.get("cxl_serving_evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("POSIX summary lacks CXL serving evidence")
    if authority_count is not None and len(evidence) != authority_count:
        raise ValueError("CXL authority evidence count differs between processes")
    if {item.get("authority_id") for item in evidence} != set(range(len(evidence))):
        raise ValueError("CXL serving evidence does not cover contiguous authorities")
    for item in evidence:
        authority_id = item["authority_id"]
        if item.get("lane_id") != endpoint:
            raise ValueError(
                f"authority {authority_id} lane does not match endpoint {endpoint}"
            )
        if item.get("lane_role") != "client_fs":
            raise ValueError(
                f"authority {authority_id} IO500 lane is not CLIENT_FS"
            )
        if any(
            not isinstance(item.get(name), int) or item[name] <= 0
            for name in (
                "format_generation",
                "session_generation",
                "lane_generation",
            )
        ):
            raise ValueError("CXL serving evidence has an invalid generation")
        if (
            expected_direct_metadata_mode is not None
            and item["format_generation"] != DIRECT_METADATA_FORMAT_GENERATION
        ):
            raise ValueError("direct metadata layout/format generation mismatch")
        if item.get("bootstrap_tcp_exchanges") != 1:
            raise ValueError("CXL bootstrap exchange count is not exactly one")
        if item.get("bootstrap_tcp_connections") != 1:
            raise ValueError("CXL bootstrap connection count is not exactly one")
        if not isinstance(item.get("bootstrap_tcp_bytes"), int) or item[
            "bootstrap_tcp_bytes"
        ] <= 0:
            raise ValueError("CXL bootstrap byte evidence is missing")
        submitted = item.get("sqe_submitted")
        consumed = item.get("cqe_consumed")
        if not isinstance(submitted, int) or submitted <= 0 or consumed != submitted:
            raise ValueError("local CXL SQ/CQ counters are not balanced")
        handoff = item.get("persistence_handoff")
        handoff_fields = (
            "request_generation",
            "release_generation",
            "grant_generation",
        )
        if not isinstance(handoff, dict) or any(
            not isinstance(handoff.get(name), int) or handoff[name] < 0
            for name in handoff_fields
        ):
            raise ValueError("CXL persistence-handoff snapshot is incomplete")
        handoff_requests = item.get("persistence_handoff_requests")
        handoff_grants = item.get("persistence_handoff_grants")
        handoff_releases = item.get("persistence_handoff_releases")
        handoff_wait_ns = item.get("persistence_handoff_wait_ns")
        if any(
            not isinstance(value, int) or value < 0
            for value in (
                handoff_requests,
                handoff_grants,
                handoff_releases,
                handoff_wait_ns,
            )
        ):
            raise ValueError("CXL persistence-handoff counters are incomplete")
        if not (
            handoff_requests == handoff_grants == handoff_releases
            and handoff["request_generation"]
            == handoff["grant_generation"]
            == handoff["release_generation"]
            == handoff_requests
        ):
            raise ValueError("CXL persistence-handoff generations are not balanced")
        if (handoff_requests > 0) != (handoff_wait_ns > 0):
            raise ValueError("CXL persistence-handoff wait evidence is inconsistent")
        sequences = item.get("shared_sequences")
        if not isinstance(sequences, dict) or any(
            sequences.get(name) != submitted
            for name in (
                "sq_produced",
                "sq_consumed",
                "cq_produced",
                "cq_consumed",
            )
        ):
            raise ValueError("shared CXL SQ/CQ lane sequences are not balanced")
        authority_timing = item.get("authority_timing")
        if not isinstance(authority_timing, dict) or any(
            not isinstance(authority_timing.get(name), int)
            or authority_timing[name] < 0
            for name in (
                "sqe_consumed",
                "cqe_published",
                "authority_queue_wait_ns",
                "successful_request_poll_ns",
                "dispatcher_backend_ns",
                "completion_publication_wait_ns",
                "cqe_publish_ns",
            )
        ):
            raise ValueError("CXL authority timing evidence is incomplete")
        if (
            authority_timing["sqe_consumed"] != submitted
            or authority_timing["cqe_published"] != submitted
            or authority_timing["successful_request_poll_ns"] <= 0
            or authority_timing["dispatcher_backend_ns"] <= 0
        ):
            raise ValueError("CXL authority evidence does not cover every request")
        client_timing = item.get("client_timing")
        if not isinstance(client_timing, dict) or any(
            not isinstance(client_timing.get(name), int) or client_timing[name] < 0
            for name in (
                "calls",
                "call_gate_wait_ns",
                "syscall_prepare_ns",
                "sq_credit_wait_ns",
                "sq_publish_ns",
                "cq_wait_ns",
                "cq_empty_polls",
                "cq_spin_polls",
                "cq_cooperative_yields",
                "cq_timer_sleeps",
            )
        ):
            raise ValueError("CXL client timing evidence is incomplete")
        if (
            client_timing["calls"] != submitted
            or client_timing["sq_publish_ns"] <= 0
            or client_timing["cq_wait_ns"] <= 0
        ):
            raise ValueError("CXL client timing evidence does not cover every request")
        cq_wait_by_opcode = item.get("cq_wait_ns_by_opcode")
        if (
            not isinstance(cq_wait_by_opcode, dict)
            or not cq_wait_by_opcode
            or any(
                not isinstance(opcode, str)
                or not opcode.isdigit()
                or int(opcode) < 0
                or int(opcode) > 255
                or not isinstance(wait_ns, int)
                or wait_ns <= 0
                for opcode, wait_ns in cq_wait_by_opcode.items()
            )
            or sum(cq_wait_by_opcode.values()) != client_timing["cq_wait_ns"]
            or not set(cq_wait_by_opcode).issubset(item.get("dispatches_by_opcode", {}))
        ):
            raise ValueError("per-opcode CQ wait evidence is inconsistent")
        cursor_mode = item.get("cursor_mode")
        if cursor_mode not in ("legacy_shared", "owned"):
            raise ValueError("CXL serving evidence has an invalid cursor mode")
        if expected_cursor_mode is not None and cursor_mode != expected_cursor_mode:
            raise ValueError(
                "CXL serving evidence cursor mode differs from the requested mode"
            )
        cq_wait_mode = item.get("cq_wait_mode")
        if cq_wait_mode not in ("timer_sleep", "cooperative_yield"):
            raise ValueError("CXL serving evidence has an invalid CQ wait mode")
        if (
            expected_cq_wait_mode is not None
            and cq_wait_mode != expected_cq_wait_mode
        ):
            raise ValueError(
                "CXL serving evidence CQ wait mode differs from the requested mode"
            )
        open_mode = item.get("open_mode")
        if open_mode != "fused_pin":
            raise ValueError("retired split-open selector reappeared")
        fused_attempts = item.get("fused_open_attempts")
        fused_pins = item.get("fused_open_pins")
        fused_fallbacks = item.get("fused_open_fallbacks")
        fused_close_attempts = item.get("fused_close_attempts")
        fused_close_snapshot_releases = item.get(
            "fused_close_snapshot_releases"
        )
        batched_close_commands = item.get("batched_close_commands")
        batched_close_items = item.get("batched_close_items")
        if any(
            not isinstance(value, int) or value < 0
            for value in (
                fused_attempts,
                fused_pins,
                fused_fallbacks,
                fused_close_attempts,
                fused_close_snapshot_releases,
                batched_close_commands,
                batched_close_items,
            )
        ) or fused_pins + fused_fallbacks > fused_attempts:
            raise ValueError("CXL fused-open evidence is inconsistent")
        if fused_close_snapshot_releases > fused_close_attempts:
            raise ValueError("CXL fused-close evidence is inconsistent")
        if (
            batched_close_commands > batched_close_items
            or batched_close_items > fused_close_snapshot_releases
        ):
            raise ValueError("CXL batched-close evidence is inconsistent")
        lane_access = item.get("client_lane_access")
        access_fields = (
            "submit_calls",
            "completion_polls",
            "request_polls",
            "completions",
            "ready_loads",
            "peer_publication_loads",
            "self_cursor_shared_loads",
            "capacity_refreshes",
            "shared_identity_rereads",
        )
        if not isinstance(lane_access, dict) or any(
            not isinstance(lane_access.get(name), int) or lane_access[name] < 0
            for name in access_fields
        ):
            raise ValueError("CXL client lane-access evidence is incomplete")
        if (
            lane_access["submit_calls"] != submitted
            or lane_access["completion_polls"] < submitted
            or lane_access["peer_publication_loads"]
            < lane_access["completion_polls"]
            or lane_access["completion_polls"]
            != client_timing["cq_empty_polls"] + submitted
            or client_timing["cq_empty_polls"]
            != client_timing["cq_spin_polls"]
            + client_timing["cq_cooperative_yields"]
            + client_timing["cq_timer_sleeps"]
        ):
            raise ValueError("CXL client lane-access evidence is inconsistent")
        if cq_wait_mode == "cooperative_yield" and client_timing["cq_timer_sleeps"] != 0:
            raise ValueError("cooperative CQ wait performed a timer sleep")
        if cq_wait_mode == "timer_sleep" and client_timing["cq_cooperative_yields"] != 0:
            raise ValueError("timer CQ wait performed a cooperative yield")
        if cursor_mode == "owned" and (
            lane_access["self_cursor_shared_loads"] != 0
            or lane_access["shared_identity_rereads"] != 0
        ):
            raise ValueError("owned cursor mode performed a forbidden shared reread")
        if cursor_mode == "legacy_shared" and (
            lane_access["self_cursor_shared_loads"] < 2 * submitted
            or lane_access["shared_identity_rereads"] != submitted
        ):
            raise ValueError("legacy cursor evidence does not cover shared rereads")
        direct_capability = item.get("direct_metadata_capability")
        if not isinstance(direct_capability, bool):
            raise ValueError("CXL serving evidence lacks direct metadata capability")
        if any(
            not isinstance(item.get(name), int) or item[name] < 0
            for name in DIRECT_METADATA_CLIENT_COUNTERS
        ):
            raise ValueError("CXL direct metadata evidence is incomplete")
        direct_attempts = item["direct_metadata_attempts"]
        direct_hits = item["direct_metadata_hits"]
        direct_not_found = item["direct_metadata_not_found_hits"]
        direct_fallbacks = item["direct_metadata_fallback_commands"]
        if direct_attempts != direct_hits + direct_not_found + direct_fallbacks:
            raise ValueError("direct metadata attempt accounting is inconsistent")
        if item["direct_metadata_commands_elided"] != direct_hits + direct_not_found:
            raise ValueError("direct metadata command-elision accounting is inconsistent")
        cold_attempts = item["direct_metadata_cold_attempts"]
        cold_hits = item["direct_metadata_cold_hits"]
        cold_not_found = item["direct_metadata_cold_not_found_hits"]
        cold_fallbacks = item["direct_metadata_cold_fallbacks"]
        if cold_attempts != (
            item["direct_metadata_no_hint"] + item["direct_metadata_stale"]
        ):
            raise ValueError("direct metadata cold-admission accounting is inconsistent")
        if cold_attempts != cold_hits + cold_not_found + cold_fallbacks:
            raise ValueError("direct metadata cold-outcome accounting is inconsistent")
        if cold_hits > direct_hits or cold_not_found > direct_not_found:
            raise ValueError("direct metadata cold outcomes exceed logical outcomes")
        # Cold admission is one source of a real READ_METADATA command, but
        # not the only one once direct-mutation overlays are enabled. A
        # reader with an existing hint may correctly fail closed while the
        # relevant catalog generation is odd or changes during joint
        # validation. The total logical outcome and opcode checks below still
        # account for every such command exactly; the cold subset may only be
        # smaller than that total.
        if cold_fallbacks > direct_fallbacks:
            raise ValueError("direct metadata fallback accounting is inconsistent")
        unstable_retries = item["direct_metadata_unstable_read_retries"]
        unstable_outcomes = (
            item["direct_metadata_unstable_read_recoveries"]
            + item["direct_metadata_unstable_read_exhaustions"]
        )
        if unstable_outcomes > unstable_retries:
            raise ValueError("direct metadata unstable-read accounting is inconsistent")
        unstable_reasons = sum(
            item[name]
            for name in (
                "direct_metadata_unstable_root_unavailable",
                "direct_metadata_unstable_dentry_decode",
                "direct_metadata_unstable_inode_decode",
                "direct_metadata_unstable_snapshot_changed",
            )
        )
        if unstable_reasons != unstable_retries:
            raise ValueError("direct metadata unstable-read reasons do not close")
        if direct_attempts == 0 and any(
            item[name] != 0
            for name in (
                "direct_metadata_read_ns",
                "direct_metadata_gate_loads",
                "direct_metadata_root_loads",
                "direct_metadata_dentry_cell_loads",
                "direct_metadata_inode_record_loads",
                "direct_metadata_unstable_read_retries",
                "direct_metadata_unstable_read_recoveries",
                "direct_metadata_unstable_read_exhaustions",
                "direct_metadata_unstable_root_unavailable",
                "direct_metadata_unstable_dentry_decode",
                "direct_metadata_unstable_inode_decode",
                "direct_metadata_unstable_snapshot_changed",
            )
        ):
            raise ValueError("idle direct metadata reader reports memory accesses")
        if expected_direct_metadata_mode == "rpc":
            if direct_capability or any(
                item[name] != 0 for name in DIRECT_METADATA_CLIENT_COUNTERS
            ):
                raise ValueError("rpc metadata mode executed the direct CXL reader")
        elif expected_direct_metadata_mode == "cxl" and not direct_capability:
            raise ValueError("direct metadata capability absent in cxl mode")
        if any(
            not isinstance(item.get(name), int) or item[name] < 0
            for name in DIRECT_MUTATION_CLIENT_COUNTERS
        ):
            raise ValueError("CXL direct mutation evidence is incomplete")
        mutation_attempts = item["direct_create_attempts"]
        mutation_hits = item["direct_create_hits"]
        mutation_fallbacks = item["direct_create_fallbacks"]
        mutation_errors = item["direct_create_errors"]
        mutation_installs = item["direct_mutation_lease_installs"]
        mutation_replacements = item["direct_mutation_lease_replacements"]
        mutation_handoffs = item["direct_mutation_generic_close_handoffs"]
        if mutation_attempts != mutation_hits + mutation_fallbacks + mutation_errors:
            raise ValueError("direct create attempt accounting is inconsistent")
        if mutation_hits != (
            item["direct_create_commands_elided"]
            + item["direct_create_post_grant_retry_hits"]
        ):
            raise ValueError("direct create hit-origin accounting is inconsistent")
        if mutation_fallbacks != (
            item["direct_create_fallback_ineligible"]
            + item["direct_create_fallback_no_parent_writer"]
            + item["direct_create_fallback_existing"]
            + item["direct_create_fallback_inode_exhausted"]
        ):
            raise ValueError("direct create fallback-reason accounting is inconsistent")
        if mutation_installs > mutation_attempts or (mutation_hits > 0 and mutation_installs == 0):
            raise ValueError("direct mutation lease accounting is inconsistent")
        if mutation_replacements > max(0, mutation_installs - 1):
            raise ValueError("direct mutation lease replacement accounting is inconsistent")
        if mutation_handoffs > mutation_hits:
            raise ValueError("direct mutation generic-close handoff accounting is inconsistent")
        unlink_attempts = item["direct_unlink_attempts"]
        unlink_elided = item["direct_unlink_commands_elided"]
        unlink_fallbacks = item["direct_unlink_fallbacks"]
        if unlink_attempts != (
            unlink_elided
            + unlink_fallbacks
            + item["direct_unlink_not_found"]
            + item["direct_unlink_membership_changing"]
            + item["direct_unlink_lease_revoked"]
        ):
            raise ValueError("direct unlink outcome accounting is inconsistent")
        if unlink_fallbacks != sum(
            item[name]
            for name in (
                "direct_unlink_fallback_ineligible",
                "direct_unlink_fallback_no_parent_writer",
                "direct_unlink_fallback_unapplied_peer",
                "direct_unlink_fallback_base_unavailable",
                "direct_unlink_fallback_overlay_changed",
                "direct_unlink_fallback_unsupported_target",
                "direct_unlink_fallback_live_ofd",
            )
        ):
            raise ValueError("direct unlink fallback-reason accounting is inconsistent")
        dispatches = item.get("dispatches_by_opcode")
        if not isinstance(dispatches, dict) or not dispatches:
            raise ValueError("CXL serving evidence lacks opcode dispatches")
        try:
            valid_dispatches = [
                (int(opcode), count)
                for opcode, count in dispatches.items()
                if 0 < int(opcode) < 256 and isinstance(count, int) and count > 0
            ]
        except (TypeError, ValueError):
            raise ValueError("CXL opcode dispatch evidence is malformed") from None
        if (
            len(valid_dispatches) != len(dispatches)
            or sum(count for _, count in valid_dispatches) != submitted
        ):
            raise ValueError("CXL opcode dispatch total does not match submitted SQEs")
        if dispatches.get("35") != 1:
            raise ValueError("CXL evidence was captured before owner release completed")
        if (
            expected_direct_metadata_mode == "cxl"
            and dispatches.get("2", 0) != direct_fallbacks
        ):
            raise ValueError(
                "READ_METADATA opcode count differs from direct fallback commands"
            )
        for forbidden in (
            "unsupported_serving_calls_after_cxl_ready",
            "filesystem_tcp_requests_after_cxl_ready",
            "legacy_tarpc_calls_after_cxl_ready",
            "blob_tcp_bytes_after_cxl_ready",
            "transport_fallbacks_after_cxl_ready",
        ):
            if item.get(forbidden) != 0:
                raise ValueError(f"nonzero forbidden serving path counter: {forbidden}")
    return len(evidence)


def validate_posix_summaries(
    records: list[dict],
    client_count: int = DEFAULT_CLIENTS,
    expected_cursor_mode: str | None = None,
    expected_cq_wait_mode: str | None = None,
    expected_direct_metadata_mode: str | None = None,
    expected_close_mode: str = "batched",
    expected_payload_persistence_owner: str | None = None,
) -> None:
    if expected_close_mode not in ("immediate", "batched"):
        raise ValueError("invalid expected lifecycle close mode")
    active_ranks = set()
    authority_count = None
    fused_open_attempts = 0
    fused_open_pins = 0
    fused_close_attempts = 0
    fused_close_snapshot_releases = 0
    batched_close_commands = 0
    batched_close_items = 0
    direct_attempts = 0
    direct_hits = 0
    handoff_requests_by_rank = {rank: 0 for rank in range(client_count)}
    direct_ro_totals = {name: 0 for name in DIRECT_RO_POSIX_COUNTERS}
    for record in records:
        rank = record.get("mpi_rank")
        endpoint = record.get("endpoint")
        if rank not in range(client_count) or endpoint != rank:
            raise ValueError(f"POSIX summary rank/endpoint mismatch: {record}")
        authority_count = validate_cxl_path_summary(
            record,
            endpoint,
            authority_count,
            expected_cursor_mode,
            expected_cq_wait_mode,
            expected_direct_metadata_mode,
        )
        for evidence in record["cxl_serving_evidence"]:
            fused_open_attempts += evidence["fused_open_attempts"]
            fused_open_pins += evidence["fused_open_pins"]
            fused_close_attempts += evidence["fused_close_attempts"]
            fused_close_snapshot_releases += evidence[
                "fused_close_snapshot_releases"
            ]
            batched_close_commands += evidence["batched_close_commands"]
            batched_close_items += evidence["batched_close_items"]
            direct_attempts += evidence["direct_metadata_attempts"]
            direct_hits += evidence["direct_metadata_hits"]
            direct_hits += evidence["direct_metadata_not_found_hits"]
            handoff_requests_by_rank[rank] += evidence[
                "persistence_handoff_requests"
            ]
        stats = record.get("stats", {})
        for name in DIRECT_RO_POSIX_COUNTERS:
            value = stats.get(name, 0)
            if not isinstance(value, int) or value < 0:
                raise ValueError("direct RO POSIX evidence is incomplete")
            direct_ro_totals[name] += value
        if (
            stats.get("direct_ro_open_hits", 0)
            + stats.get("direct_ro_open_fallbacks", 0)
            > stats.get("direct_ro_open_attempts", 0)
            or stats.get("direct_ro_open_commands_elided", 0)
            != stats.get("direct_ro_open_hits", 0)
            or stats.get("direct_ro_layout_roots_loaded", 0)
            != stats.get("direct_ro_open_hits", 0)
            or stats.get("direct_ro_epoch_acquires", 0)
            != stats.get("direct_ro_open_hits", 0)
            or stats.get("direct_ro_epoch_releases", 0)
            > stats.get("direct_ro_epoch_acquires", 0)
            or stats.get("direct_ro_close_commands_elided", 0)
            > stats.get("direct_ro_epoch_releases", 0)
        ):
            raise ValueError("direct RO POSIX evidence is inconsistent")
        totals = record.get("syscall_classification", {}).get("totals", {})
        if sum(stats.get(name, 0) for name in ("open_ops", "read_ops", "write_ops")) > 0:
            if totals.get("handled", 0) <= 0:
                raise ValueError("active POSIX summary handled no BadFS syscall")
            active_ranks.add(rank)
    if active_ranks != set(range(client_count)):
        raise ValueError(
            f"active POSIX summaries do not cover ranks 0..{client_count - 1}: "
            f"{sorted(active_ranks)}"
        )
    direct_ro_hits = direct_ro_totals["direct_ro_open_hits"]
    direct_ro_releases = direct_ro_totals["direct_ro_epoch_releases"]
    direct_ro_close_elided = direct_ro_totals[
        "direct_ro_close_commands_elided"
    ]
    if (
        fused_open_attempts + direct_ro_totals["direct_ro_open_attempts"] == 0
        or fused_open_pins + direct_ro_hits == 0
        or fused_close_attempts + direct_ro_close_elided == 0
        or fused_close_snapshot_releases + direct_ro_releases == 0
    ):
        raise ValueError(
            "lifecycle mode did not retain and retire an existing regular file"
        )
    if expected_close_mode == "batched" and (
        batched_close_commands == 0 or batched_close_items == 0
    ) and direct_ro_close_elided == 0:
        raise ValueError("batched lifecycle close mode produced no bounded close batch")
    if expected_close_mode == "immediate" and (
        batched_close_commands != 0 or batched_close_items != 0
    ):
        raise ValueError("immediate lifecycle close mode dispatched a close batch")
    if expected_direct_metadata_mode == "cxl" and (
        direct_attempts <= 0 or direct_hits <= 0
    ):
        raise ValueError("cxl metadata mode produced no direct metadata hit")
    if expected_payload_persistence_owner == "WRITER_RECEIPT":
        missing = [
            rank
            for rank in sorted(active_ranks)
            if handoff_requests_by_rank[rank] <= 0
        ]
        if missing:
            raise ValueError(
                "writer-receipt run lacks completed CXL persistence handoff "
                f"for active ranks {missing}"
            )


def validate_post_run_exporter_summary(
    record: dict,
    client_count: int,
    expected_cursor_mode: str | None = None,
    expected_cq_wait_mode: str | None = None,
    expected_direct_metadata_mode: str | None = None,
) -> None:
    expected_endpoint = 2 * client_count + 1
    if record.get("mpi_rank") is not None:
        raise ValueError("post-run exporter retained an MPI rank identity")
    if record.get("endpoint") != expected_endpoint:
        raise ValueError(
            "post-run exporter endpoint mismatch: "
            f"expected {expected_endpoint}, got {record.get('endpoint')}"
        )
    validate_cxl_path_summary(
        record,
        expected_endpoint,
        expected_cursor_mode=expected_cursor_mode,
        expected_cq_wait_mode=expected_cq_wait_mode,
        expected_direct_metadata_mode=expected_direct_metadata_mode,
    )
    stats = record.get("stats", {})
    totals = record.get("syscall_classification", {}).get("totals", {})
    if stats.get("open_ops", 0) <= 0 or stats.get("read_ops", 0) <= 0:
        raise ValueError("post-run exporter did not read the LegoFS result directory")
    if totals.get("handled", 0) <= 0:
        raise ValueError("post-run exporter handled no BadFS syscall")


def parse_post_run_exporter_summary(
    output: str,
    client_count: int,
    expected_cursor_mode: str | None = None,
    expected_cq_wait_mode: str | None = None,
    expected_direct_metadata_mode: str | None = None,
) -> dict:
    matches = list(EXPORT_SUMMARY_RE.finditer(output))
    if len(matches) != 1:
        raise ValueError(
            "IO500 did not produce exactly one exporter CXL path summary"
        )
    match = matches[0]
    record = json.loads(match.group(3))
    marker_endpoint = int(match.group(1))
    embedded_endpoint = record.get("endpoint")
    if embedded_endpoint is not None and embedded_endpoint != marker_endpoint:
        raise ValueError("post-run exporter marker/record endpoint mismatch")
    record["endpoint"] = marker_endpoint
    record["file"] = match.group(2)
    validate_post_run_exporter_summary(
        record,
        client_count,
        expected_cursor_mode,
        expected_cq_wait_mode,
        expected_direct_metadata_mode,
    )
    return record


def dump_summaries(
    consoles: list[Console],
    client_count: int = DEFAULT_CLIENTS,
    expected_cursor_mode: str | None = None,
    expected_cq_wait_mode: str | None = None,
    expected_direct_metadata_mode: str | None = None,
    expected_close_mode: str = "batched",
    expected_payload_persistence_owner: str | None = None,
) -> list[dict]:
    starts = [len(console.output) for console in consoles]
    for console in consoles:
        console.send("LEGOFS_DUMP_SUMMARIES")
    records = []
    for index, console in enumerate(consoles):
        console.wait(f"LEGOFS_IO500_SUMMARIES_DONE index={index}", 60, starts[index])
        for match in SUMMARY_RE.finditer(console.output[starts[index]:]):
            record = json.loads(match.group(3))
            record["endpoint"] = int(match.group(1))
            record["file"] = match.group(2)
            records.append(record)
    validate_posix_summaries(
        records,
        client_count,
        expected_cursor_mode,
        expected_cq_wait_mode,
        expected_direct_metadata_mode,
        expected_close_mode,
        expected_payload_persistence_owner,
    )
    return records


def validate_authority_internal_fabric(fabric: dict, server_count: int) -> None:
    if server_count <= 1:
        return
    submitted = fabric.get("authority_internal_submitted", 0)
    completed = fabric.get("authority_internal_completed", 0)
    if submitted <= 0 or completed != submitted:
        raise ValueError(
            "lifecycle inspection lacks completed authority-internal CXL traffic"
        )
    if fabric.get("authority_prepare_receipts", 0) <= 0:
        raise ValueError(
            "lifecycle inspection lacks durable cross-authority PREPARE receipts"
        )
    committed = fabric.get("authority_committed_transactions", 0)
    if committed <= 0:
        raise ValueError(
            "lifecycle inspection lacks committed cross-authority transactions"
        )
    if fabric.get("authority_marker_publications", 0) < committed * 2:
        raise ValueError(
            "lifecycle inspection lacks PREPARED/COMMITTED marker publications"
        )
    if fabric.get("authority_durable_prefix", 0) < committed * 2:
        raise ValueError(
            "lifecycle inspection durable prefix does not cover committed transactions"
        )
    durable_prefix = fabric.get("authority_durable_prefix", 0)
    record_persists = fabric.get("authority_journal_record_persists", 0)
    anchor_persists = fabric.get("authority_journal_anchor_persists", 0)
    if record_persists < durable_prefix:
        raise ValueError(
            "lifecycle inspection lacks CXL transaction-journal record persists"
        )
    if anchor_persists < record_persists + server_count:
        raise ValueError(
            "lifecycle inspection lacks CXL transaction-journal anchor persists"
        )


def validate_recovery_control_checkpoints(
    records: list[dict], server_count: int, *, expected_active_client_lanes: int = 0
) -> None:
    if len(records) != server_count or {
        record.get("server") for record in records
    } != set(range(server_count)):
        raise ValueError("recovery-control checkpoints do not cover every authority")
    for record in records:
        server = record["server"]
        if record.get("schema_version") != "badfs.recovery-control.inspection.v1":
            raise ValueError("unexpected recovery-control checkpoint schema")
        retirement = record.get("retirement")
        if not isinstance(retirement, dict):
            raise ValueError("recovery-control clean retirement is missing")
        retirement_identity = retirement.get("identity")
        if (
            not isinstance(retirement_identity, dict)
            or retirement_identity.get("authority") != server
            or not isinstance(retirement.get("retired_lanes"), int)
            or retirement["retired_lanes"] <= 0
            or retirement.get("active_client_lanes") != expected_active_client_lanes
        ):
            raise ValueError("recovery-control clean retirement is incomplete")
        checkpoint = record.get("checkpoint")
        if not isinstance(checkpoint, dict):
            raise ValueError("recovery-control checkpoint body is missing")
        identity = checkpoint.get("identity")
        if (
            not isinstance(identity, dict)
            or identity.get("authority") != server
            or not isinstance(identity.get("serving_incarnation"), int)
            or identity["serving_incarnation"] <= 0
        ):
            raise ValueError("recovery-control checkpoint identity is invalid")
        if retirement_identity != identity:
            raise ValueError(
                "recovery-control retirement/checkpoint identities do not match"
            )
        metadata_lsn = checkpoint.get("metadata_lsn")
        lifecycle_lsn = checkpoint.get("lifecycle_lsn")
        if (
            checkpoint.get("data_domain") != "lifecycle"
            or checkpoint.get("data_lsn") != 0
            or not isinstance(metadata_lsn, int)
            or metadata_lsn < 0
            or not isinstance(lifecycle_lsn, int)
            or lifecycle_lsn < metadata_lsn
        ):
            raise ValueError("recovery-control durable cursor set is inconsistent")

        transport = record.get("transport")
        if not isinstance(transport, dict):
            raise ValueError("recovery-control transport evidence is missing")
        if (
            transport.get("authority_id") != server
            or transport.get("lane_id") != RECOVERY_CONTROL_LANE_BASE + server
            or transport.get("lane_role") != "recovery_control"
        ):
            raise ValueError("recovery-control lane identity is invalid")
        if any(
            not isinstance(transport.get(name), int) or transport[name] <= 0
            for name in ("format_generation", "session_generation", "lane_generation")
        ):
            raise ValueError("recovery-control generation evidence is invalid")
        if (
            transport.get("bootstrap_tcp_connections") != 1
            or transport.get("bootstrap_tcp_exchanges") != 1
            or not isinstance(transport.get("bootstrap_tcp_bytes"), int)
            or transport["bootstrap_tcp_bytes"] <= 0
        ):
            raise ValueError("recovery-control bootstrap evidence is invalid")
        if transport.get("sqe_submitted") != 2 or transport.get("cqe_consumed") != 2:
            raise ValueError("recovery-control local SQ/CQ counters are not balanced")
        sequences = transport.get("shared_sequences")
        if not isinstance(sequences, dict) or any(
            sequences.get(name) != 2
            for name in ("sq_produced", "sq_consumed", "cq_produced", "cq_consumed")
        ):
            raise ValueError("recovery-control shared SQ/CQ counters are not balanced")
        authority_timing = transport.get("authority_timing")
        if (
            not isinstance(authority_timing, dict)
            or authority_timing.get("sqe_consumed") != 2
            or authority_timing.get("cqe_published") != 2
            or not isinstance(authority_timing.get("successful_request_poll_ns"), int)
            or authority_timing["successful_request_poll_ns"] <= 0
            or not isinstance(authority_timing.get("dispatcher_backend_ns"), int)
            or authority_timing["dispatcher_backend_ns"] <= 0
            or not isinstance(
                authority_timing.get("completion_publication_wait_ns"), int
            )
            or authority_timing["completion_publication_wait_ns"] < 0
        ):
            raise ValueError("recovery-control authority evidence is incomplete")
        client_timing = transport.get("client_timing")
        if (
            not isinstance(client_timing, dict)
            or client_timing.get("calls") != 2
            or not isinstance(client_timing.get("sq_publish_ns"), int)
            or client_timing["sq_publish_ns"] <= 0
            or not isinstance(client_timing.get("cq_wait_ns"), int)
            or client_timing["cq_wait_ns"] <= 0
        ):
            raise ValueError("recovery-control client evidence is incomplete")
        cq_wait_by_opcode = transport.get("cq_wait_ns_by_opcode")
        if (
            not isinstance(cq_wait_by_opcode, dict)
            or not cq_wait_by_opcode
            or sum(cq_wait_by_opcode.values()) != client_timing["cq_wait_ns"]
            or not set(cq_wait_by_opcode).issubset(
                transport.get("dispatches_by_opcode", {})
            )
        ):
            raise ValueError("recovery-control per-opcode CQ wait evidence is inconsistent")
        if transport.get("dispatches_by_opcode") != {"1": 1, "2": 1}:
            raise ValueError(
                "recovery-control checkpoint/retirement opcodes were not dispatched exactly once"
            )
        for forbidden in (
            "unsupported_serving_calls_after_cxl_ready",
            "filesystem_tcp_requests_after_cxl_ready",
            "legacy_tarpc_calls_after_cxl_ready",
            "blob_tcp_bytes_after_cxl_ready",
            "transport_fallbacks_after_cxl_ready",
        ):
            if transport.get(forbidden) != 0:
                raise ValueError(
                    f"nonzero recovery-control forbidden path counter: {forbidden}"
                )


def inspect_servers(
    console: Console,
    server_count: int,
    *,
    require_direct_read: bool = True,
    command_free_direct_read_ops: int = 0,
    require_recovery_control: bool = True,
    expected_direct_metadata_mode: str | None = None,
    expected_packed_small_segments: bool | None = None,
    expected_durability_profile: str = "D_BEFORE_V",
    expected_payload_persistence_owner: str | None = None,
) -> dict:
    start = len(console.output)
    console.send("LEGOFS_INSPECT")
    console.wait("LEGOFS_IO500_INSPECT_EXIT index=0 rc=0", 120, start)
    return parse_server_inspection(
        console.output[start:],
        server_count,
        require_direct_read=require_direct_read,
        command_free_direct_read_ops=command_free_direct_read_ops,
        require_recovery_control=require_recovery_control,
        expected_direct_metadata_mode=expected_direct_metadata_mode,
        expected_packed_small_segments=expected_packed_small_segments,
        expected_durability_profile=expected_durability_profile,
        expected_payload_persistence_owner=expected_payload_persistence_owner,
    )


def lifecycle_payload_persistence_evidence(audit: dict, server: int) -> dict:
    """Validate each legal payload-durability path without conflating them."""
    owner = audit.get("payload_persistence_owner")
    if owner not in (
        "AUTHORITY_BI_ACQUIRE",
        "WRITER_RECEIPT",
        "PLACEMENT_ROUTED",
        "WRITER_BEFORE_VISIBILITY",
    ):
        raise ValueError(
            f"lifecycle server {server} lacks a valid payload persistence owner"
        )
    direct_items = audit["direct_write_commit_items"]
    writer_items = audit["writer_persisted_direct_items"]
    writer_bytes = audit["writer_persisted_direct_bytes"]
    receipt_items = audit.get("host_persist_receipts_accepted", 0)
    receipt_duplicates = audit.get("host_persist_receipts_duplicate", 0)
    receipt_rejections = audit.get("host_persist_receipts_rejected", 0)
    pending_receipts = audit.get("pending_payload_dependencies", 0)
    authority = {
        name: audit.get(name, 0)
        for name in (
            "authority_coherent_acquire_jobs",
            "authority_coherent_acquire_ranges",
            "authority_coherent_acquire_bytes",
            "authority_coherent_acquire_ns",
            "authority_payload_persist_jobs",
            "authority_payload_persist_ranges",
            "authority_payload_persist_bytes",
            "authority_payload_persist_ns",
        )
    }
    for name, value in (
        ("host_persist_receipts_accepted", receipt_items),
        ("host_persist_receipts_duplicate", receipt_duplicates),
        ("host_persist_receipts_rejected", receipt_rejections),
        ("pending_payload_dependencies", pending_receipts),
    ):
        if not isinstance(value, int) or value < 0:
            raise ValueError(
                f"lifecycle server {server} lacks valid {name} evidence"
            )
    for name, value in authority.items():
        if not isinstance(value, int) or value < 0:
            raise ValueError(
                f"lifecycle server {server} lacks valid {name} evidence"
            )
    if writer_items > direct_items:
        raise ValueError(
            f"lifecycle server {server} reports more writer-persisted items "
            "than committed direct-write items"
        )
    if writer_items == 0 and writer_bytes != 0:
        raise ValueError(
            f"lifecycle server {server} reports writer-persisted bytes without items"
        )
    if writer_items > 0 and writer_bytes <= 0:
        raise ValueError(
            f"lifecycle server {server} lacks writer-persisted byte evidence"
        )

    provider = (
        audit["provider_payload_barriers"] > 0
        and audit["provider_payload_bytes"] > 0
    )
    writer_complete = (
        direct_items > 0
        and writer_items == direct_items
        and writer_bytes > 0
    )
    writer_host_receipt_complete = (
        direct_items > 0
        and writer_items + receipt_items == direct_items
        and receipt_items > 0
        and receipt_rejections == 0
        and pending_receipts == 0
    )
    authority_jobs = authority["authority_coherent_acquire_jobs"]
    authority_complete = (
        authority_jobs > 0
        and authority["authority_payload_persist_jobs"] == authority_jobs
        and authority["authority_coherent_acquire_ranges"]
        == authority["authority_payload_persist_ranges"]
        > 0
        and authority["authority_coherent_acquire_bytes"]
        == authority["authority_payload_persist_bytes"]
        > 0
        and authority["authority_coherent_acquire_ns"] > 0
        and authority["authority_payload_persist_ns"] > 0
    )
    if authority_jobs > 0 and not authority_complete:
        raise ValueError(
            f"lifecycle server {server} has incomplete authority payload evidence"
        )
    if receipt_items > direct_items - writer_items:
        raise ValueError(
            f"lifecycle server {server} reports receipts beyond deferred direct items"
        )
    authority_dependency_items = direct_items - writer_items
    if owner == "PLACEMENT_ROUTED":
        authority_dependency_items -= receipt_items
    if owner == "AUTHORITY_BI_ACQUIRE":
        if receipt_items != 0 or receipt_duplicates != 0 or receipt_rejections != 0:
            raise ValueError(
                f"lifecycle server {server} mixed writer receipts into authority ownership"
            )
        if pending_receipts != 0:
            raise ValueError(
                f"lifecycle server {server} has pending authority payload dependencies"
            )
        if authority_dependency_items > 0 and not (
            provider
            and authority_complete
            and authority["authority_coherent_acquire_ranges"]
            >= authority_dependency_items
        ):
            raise ValueError(
                f"lifecycle server {server} lacks complete authority-owned payload evidence"
            )
    elif owner not in ("PLACEMENT_ROUTED",) and any(authority.values()):
        raise ValueError(
            f"lifecycle server {server} used authority persistence under owner {owner}"
        )
    if owner == "WRITER_RECEIPT":
        if direct_items > 0 and not writer_host_receipt_complete:
            raise ValueError(
                f"lifecycle server {server} lacks payload persistence evidence"
            )
    elif owner == "PLACEMENT_ROUTED":
        if pending_receipts != 0 or receipt_rejections != 0:
            raise ValueError(
                f"lifecycle server {server} has incomplete placement-routed receipts"
            )
        if authority_dependency_items > 0 and not (
            provider
            and authority_complete
            and authority["authority_coherent_acquire_ranges"]
            >= authority_dependency_items
        ):
            raise ValueError(
                f"lifecycle server {server} lacks placement-routed authority evidence"
            )
        if receipt_items == 0 and authority_dependency_items == 0 and direct_items > writer_items:
            raise ValueError(
                f"lifecycle server {server} did not close placement-routed dependencies"
            )
    elif owner == "WRITER_BEFORE_VISIBILITY":
        if direct_items > 0 and not (provider or writer_complete):
            raise ValueError(
                f"lifecycle server {server} has direct-write items without payload "
                "persistence evidence"
            )
    return {
        "owner": owner,
        "provider_payload_barrier": provider,
        "authority_complete": authority_complete,
        **authority,
        "writer_persisted_complete": writer_complete,
        "writer_host_receipt_complete": writer_host_receipt_complete,
        "placement_routed_complete": owner == "PLACEMENT_ROUTED"
        and pending_receipts == 0
        and receipt_rejections == 0
        and (authority_dependency_items == 0 or authority_complete),
        "direct_write_commit_items": direct_items,
        "writer_persisted_direct_items": writer_items,
        "writer_persisted_direct_bytes": writer_bytes,
        "host_persist_receipts_accepted": receipt_items,
        "host_persist_receipts_duplicate": receipt_duplicates,
        "host_persist_receipts_rejected": receipt_rejections,
        "pending_payload_dependencies": pending_receipts,
        "authority_dependency_items": authority_dependency_items,
    }


def validate_packed_small_segment_audit(
    audit: dict,
    server: int,
    *,
    expected_packed_small_segments: bool | None,
) -> None:
    """Fail closed unless the selected small-object allocator actually ran."""
    for name in PACKED_SMALL_SEGMENT_SERVER_COUNTERS:
        if not isinstance(audit.get(name), int) or audit[name] < 0:
            raise ValueError(
                f"lifecycle server {server} lacks packed-small-segment {name} evidence"
            )
    if expected_packed_small_segments is None:
        return

    packed_values = {
        name: audit[name]
        for name in PACKED_SMALL_SEGMENT_SERVER_COUNTERS
        if not name.startswith("legacy_small_arena_")
    }
    if expected_packed_small_segments:
        for name in (
            "backend_fresh_format_scan_bytes",
            "backend_publication_slots_reset",
            "startup_small_runtime_segments",
            "startup_small_runtime_cells",
        ):
            if audit[name] != 0:
                raise ValueError(
                    f"lifecycle server {server} fresh layout-v7 startup reports {name}={audit[name]}"
                )
        if audit["small_segment_claims"] <= 0:
            raise ValueError(
                f"lifecycle server {server} observed no packed segment claim"
            )
        if audit["small_segment_claim_persist_ns"] <= 0:
            raise ValueError(
                f"lifecycle server {server} lacks packed segment claim persistence timing"
            )
        if audit["small_cell_grants"] <= 0:
            raise ValueError(
                f"lifecycle server {server} observed no packed small-cell grants"
            )
        if (
            audit["small_cell_commits"] <= 0
            or audit["small_cell_commits"] > audit["small_cell_grants"]
        ):
            raise ValueError(
                f"lifecycle server {server} has inconsistent packed small-cell commits"
            )
        if audit["small_cell_payload_bytes"] <= 0:
            raise ValueError(
                f"lifecycle server {server} observed no packed small-cell payload"
            )
        if (
            audit["small_cell_payload_persist_bytes"]
            > audit["small_cell_payload_bytes"]
        ):
            raise ValueError(
                f"lifecycle server {server} over-counted packed payload persistence"
            )
        durable_payload_bytes = (
            audit["small_cell_payload_persist_bytes"]
            + int(audit.get("writer_persisted_direct_bytes", 0))
        )
        if durable_payload_bytes < audit["small_cell_payload_bytes"]:
            raise ValueError(
                f"lifecycle server {server} lacks packed payload durability evidence"
            )
        if audit["small_cell_allocator_persist_barriers"] != 0:
            raise ValueError(
                f"lifecycle server {server} persisted allocator metadata per small cell"
            )
        if (
            audit["legacy_small_arena_reserve_calls"] != 0
            or audit["legacy_small_arena_reserve_ns"] != 0
        ):
            raise ValueError(
                f"lifecycle server {server} reached the legacy 4 KiB reserve path"
            )
    else:
        if any(packed_values.values()):
            raise ValueError(
                f"lifecycle server {server} used packed-small-segment work in v6 mode"
            )
        if (
            audit["legacy_small_arena_reserve_calls"] <= 0
            or audit["legacy_small_arena_reserve_ns"] <= 0
        ):
            raise ValueError(
                f"lifecycle server {server} observed no legacy 4 KiB reserve"
            )


def validate_packed_small_segment_client_evidence(
    summaries: list[dict], *, expected_packed_small_segments: bool | None
) -> None:
    totals = {
        name: sum(
            int(record.get("stats", {}).get(name, 0)) for record in summaries
        )
        for name in PACKED_SMALL_SEGMENT_CLIENT_COUNTERS
    }
    if expected_packed_small_segments is None:
        return
    if expected_packed_small_segments:
        if totals["small_segment_dynamic_mmap_calls"] <= 0:
            raise ValueError("packed client observed no dynamic mmap")
        if (
            totals["small_segment_dynamic_mmap_calls"]
            != totals["small_segment_mapped_owner_segments"]
        ):
            raise ValueError("packed client observed a duplicate dynamic mmap")
        if totals["small_segment_dynamic_mmap_ns"] <= 0:
            raise ValueError("packed client lacks dynamic mmap elapsed time")
        if totals["small_segment_cache_hits"] <= 0:
            raise ValueError("packed client observed no segment-cache reuse")
        if (
            totals["small_segment_protection_calls"] <= 0
            or totals["small_segment_protection_ns"] <= 0
        ):
            raise ValueError("packed client lacks protection-transition evidence")
    elif any(totals.values()):
        raise ValueError("v6 client unexpectedly used packed-small-segment mapping")


def parse_server_inspection(
    output: str,
    server_count: int,
    *,
    require_direct_read: bool = True,
    command_free_direct_read_ops: int = 0,
    expected_active_client_lanes: int = 0,
    require_recovery_control: bool = True,
    expected_direct_metadata_mode: str | None = None,
    expected_packed_small_segments: bool | None = None,
    expected_durability_profile: str = "D_BEFORE_V",
    expected_payload_persistence_owner: str | None = None,
) -> dict:
    marker = "badfs_lifecycle_inspection "
    recovery_marker = "badfs_recovery_control_checkpoint "
    records = []
    recovery_records = []
    for line in output.splitlines():
        if line.startswith(marker):
            records.append(json.loads(line[len(marker):]))
        elif line.startswith(recovery_marker):
            recovery_records.append(json.loads(line[len(recovery_marker):]))
    records.sort(key=lambda record: record.get("server", -1))
    if len(records) != server_count or [
        record.get("server") for record in records
    ] != list(range(server_count)):
        raise ValueError("lifecycle inspection does not cover every configured server")
    if any(
        record.get("schema_version") != "badfs.lifecycle.inspection.v1"
        for record in records
    ):
        raise ValueError("unexpected lifecycle inspection schema")
    recovery_records.sort(key=lambda record: record.get("server", -1))
    if require_recovery_control:
        validate_recovery_control_checkpoints(
            recovery_records,
            server_count,
            expected_active_client_lanes=expected_active_client_lanes,
        )
    elif recovery_records:
        raise ValueError(
            "single-authority minimal inspection unexpectedly created recovery-control state"
        )
    fabric = {
        key: sum(record.get("fabric", {}).get(key, 0) for record in records)
        for key in records[0].get("fabric", {})
    }
    if (
        not isinstance(command_free_direct_read_ops, int)
        or command_free_direct_read_ops < 0
    ):
        raise ValueError("invalid command-free direct-read evidence")
    if fabric.get("trusted_direct_write_ops", 0) == 0 or (
        require_direct_read
        and fabric.get("trusted_direct_read_ops", 0) == 0
        and command_free_direct_read_ops == 0
    ):
        raise ValueError("lifecycle inspection lacks direct write/read operations")
    forbidden_fabric = (
        "staged_read_ops", "staged_read_bytes", "staged_write_ops", "staged_write_bytes",
        "blob_read_ops", "blob_read_bytes", "blob_write_ops", "blob_write_bytes",
        "legacy_read_file_block_ops", "legacy_write_file_block_ops",
        "legacy_read_fabric_block_ops", "legacy_write_fabric_block_ops",
        "stale_ref_rejections", "epoch_rejections", "checksum_failures", "lease_rejections",
        "quarantine_events", "active_leases", "quarantined_slots",
    )
    for name in forbidden_fabric:
        if fabric.get(name, 0) != 0:
            raise ValueError(f"lifecycle inspection reports {name}")
    validate_authority_internal_fabric(fabric, server_count)
    for record in records:
        audit = record.get("audit", {})
        if (
            expected_payload_persistence_owner is not None
            and audit.get("payload_persistence_owner")
            != expected_payload_persistence_owner
        ):
            raise ValueError(
                f"lifecycle server {record['server']} reported unexpected "
                "payload persistence owner"
            )
        for name in ("pending_operations", "quarantined_extents", "active_read_leases"):
            if audit.get(name, 0) != 0:
                raise ValueError(
                    f"lifecycle server {record['server']} audit reports {name}"
                )
        direct_metadata = audit.get("direct_metadata")
        if not isinstance(direct_metadata, dict) or any(
            not isinstance(direct_metadata.get(name), int)
            or direct_metadata[name] < 0
            for name in DIRECT_METADATA_AUTHORITY_COUNTERS
        ):
            raise ValueError(
                f"lifecycle server {record['server']} lacks direct metadata audit"
            )
        if direct_metadata["dentry_high_water"] > direct_metadata["dentry_capacity"]:
            raise ValueError(
                f"lifecycle server {record['server']} direct metadata high-water exceeds capacity"
            )
        if expected_direct_metadata_mode == "rpc":
            if any(direct_metadata[name] != 0 for name in DIRECT_METADATA_AUTHORITY_COUNTERS):
                raise ValueError("rpc metadata mode published direct CXL metadata")
        elif expected_direct_metadata_mode == "cxl":
            if (
                direct_metadata["publication_batches"] <= 0
                or direct_metadata["dentry_writes"] <= 0
                or direct_metadata["inode_writes"] <= 0
                or direct_metadata["registered_hints"] <= 0
                or direct_metadata["dentry_capacity"] <= 0
                or direct_metadata["inode_capacity"] <= 0
            ):
                raise ValueError("cxl metadata mode lacks direct publication activity")
            if (
                direct_metadata["capacity_failures"] != 0
                or direct_metadata["publication_failures"] != 0
            ):
                raise ValueError("cxl metadata publication reported a fail-closed error")
        for name in (
            "metadata_wal_persist_barriers",
            "metadata_wal_persist_records",
            "metadata_wal_persist_bytes",
            "metadata_wal_persist_ns",
            "provider_persist_barriers",
            "provider_persist_bytes",
            "provider_persist_ns",
            "provider_payload_barriers",
            "provider_payload_bytes",
            "direct_write_commit_items",
            "writer_persisted_direct_items",
            "writer_persisted_direct_bytes",
            "host_persist_receipts_accepted",
            "host_persist_receipts_duplicate",
            "host_persist_receipts_rejected",
            "pending_payload_dependencies",
            "pending_payload_bytes",
            "authority_coherent_acquire_jobs",
            "authority_coherent_acquire_ranges",
            "authority_coherent_acquire_bytes",
            "authority_coherent_acquire_ns",
            "authority_payload_persist_jobs",
            "authority_payload_persist_ranges",
            "authority_payload_persist_bytes",
            "authority_payload_persist_ns",
            "state_log_lock_wait_ns",
            "state_log_delta_ns",
            "state_log_encode_ns",
            "state_log_metadata_ns",
            "state_log_write_ns",
            "state_log_fsync_ns",
            "deferred_state_capture_lock_wait_ns",
            "deferred_state_clone_ns",
            "deferred_state_dependency_close_ns",
            "deferred_state_receipt_wait_ns",
            "deferred_state_append_ns",
            "deferred_state_not_ready_cuts",
            "deferred_state_resumed_cuts",
            "provider_allocator_barriers",
            "provider_allocator_bytes",
            "foreground_checkpoint_waits",
            "foreground_checkpoint_wait_ns",
            "arena_acquire_calls",
            "arena_acquire_slots",
            "arena_acquire_lock_wait_ns",
            "arena_acquire_state_clone_ns",
            "arena_acquire_allocation_plan_ns",
            "arena_acquire_backend_reserve_ns",
            "arena_acquire_state_build_ns",
            "arena_acquire_state_validate_ns",
            "arena_acquire_grant_install_ns",
            "arena_acquire_state_persist_ns",
            "arena_acquire_trace_ns",
            "arena_acquire_total_ns",
            "backend_fresh_format_scan_bytes",
            "backend_publication_slots_reset",
            "startup_small_runtime_segments",
            "startup_small_runtime_cells",
            "small_segment_claims",
            "small_segment_claim_persist_ns",
            "small_cell_grants",
            "small_cell_commits",
            "small_cell_payload_bytes",
            "small_cell_payload_persist_bytes",
            "small_cell_allocator_persist_barriers",
            "legacy_small_arena_reserve_calls",
            "legacy_small_arena_reserve_ns",
        ):
            if not isinstance(audit.get(name), int) or audit[name] < 0:
                raise ValueError(
                    f"lifecycle server {record['server']} lacks {name} evidence"
                )
        validate_packed_small_segment_audit(
            audit,
            record["server"],
            expected_packed_small_segments=expected_packed_small_segments,
        )
        if audit["metadata_wal_persist_barriers"] <= 0:
            raise ValueError(
                f"lifecycle server {record['server']} observed no metadata WAL barrier"
            )
        if audit["metadata_wal_persist_records"] < audit["metadata_wal_persist_barriers"]:
            raise ValueError(
                f"lifecycle server {record['server']} has fewer WAL records than barriers"
            )
        for name in (
            "metadata_wal_deferred_appends",
            "metadata_wal_deferred_records",
            "metadata_wal_deferred_bytes",
            "metadata_wal_pending_records",
            "metadata_wal_durable_lsn",
        ):
            if not isinstance(audit.get(name), int) or audit[name] < 0:
                raise ValueError(
                    f"lifecycle server {record['server']} lacks metadata WAL field {name}"
                )
        if (
            audit["provider_persist_barriers"] <= 0
            or audit["provider_persist_bytes"] <= 0
            or audit["provider_allocator_barriers"] <= 0
            or audit["provider_allocator_bytes"] <= 0
        ):
            raise ValueError(
                f"lifecycle server {record['server']} lacks provider persistence evidence"
            )
        record["payload_persistence_evidence"] = (
            lifecycle_payload_persistence_evidence(audit, record["server"])
        )
        vd = audit.get("visibility_durability", {})
        for name in (
            "logical_mutations",
            "published_v",
            "durable_d",
            "visible_sequence",
            "durable_sequence",
            "v_to_d_lag",
            "max_v_to_d_lag",
            "foreground_d_waits",
            "foreground_d_wait_ns",
            "semantic_batches",
            "semantic_batch_operations",
            "semantic_batch_bytes",
            "persistence_batches",
            "persistence_batch_operations",
            "persistence_metadata_records",
            "persistence_metadata_bytes",
            "persistence_worker_ns",
            "persistence_worker_metadata_ns",
            "persistence_worker_lifecycle_ns",
            "dependency_closure_waits",
            "dependency_closure_wait_ns",
        ):
            if not isinstance(vd.get(name), int) or vd[name] < 0:
                raise ValueError(
                    f"lifecycle server {record['server']} lacks V/D field {name}"
                )
        if vd.get("provider_profile") != expected_durability_profile:
            raise ValueError(
                f"lifecycle server {record['server']} reported unexpected durability profile"
            )
        logical = vd["logical_mutations"]
        if logical <= 0 or vd["semantic_batches"] <= 0:
            raise ValueError(
                f"lifecycle server {record['server']} observed no syscall-first V/D evidence"
            )
        if expected_durability_profile == "D_BEFORE_V":
            if vd["foreground_d_waits"] <= 0 or not (
                vd["published_v"]
                == vd["durable_d"]
                == vd["visible_sequence"]
                == vd["durable_sequence"]
                == vd["semantic_batch_operations"]
                == logical
            ):
                raise ValueError(
                    f"lifecycle server {record['server']} has inconsistent D-before-V counters"
                )
            if vd["v_to_d_lag"] != 0 or vd["max_v_to_d_lag"] != 0:
                raise ValueError(
                    f"lifecycle server {record['server']} accumulated V/D lag in D_BEFORE_V"
                )
        else:
            if not (
                vd["published_v"]
                == vd["visible_sequence"]
                == vd["semantic_batch_operations"]
                == logical
                and vd["durable_d"] == vd["durable_sequence"] <= vd["visible_sequence"]
                and vd["v_to_d_lag"]
                == vd["visible_sequence"] - vd["durable_sequence"]
                and vd["max_v_to_d_lag"] >= vd["v_to_d_lag"]
                and vd["max_v_to_d_lag"] > 0
                and vd["durable_d"] > 0
            ):
                raise ValueError(
                    f"lifecycle server {record['server']} has inconsistent V-before-D counters"
                )
            if (
                vd["persistence_batches"] <= 0
                or vd["persistence_batch_operations"] != vd["durable_sequence"]
                or vd["persistence_worker_ns"] <= 0
                or vd["persistence_worker_metadata_ns"] <= 0
                or vd["persistence_worker_lifecycle_ns"] <= 0
                or audit["metadata_wal_deferred_records"] <= 0
            ):
                raise ValueError(
                    f"lifecycle server {record['server']} lacks batched authority D evidence"
                )
        if (
            vd["dependency_closure_waits"] != vd["foreground_d_waits"]
            or vd["dependency_closure_wait_ns"] != vd["foreground_d_wait_ns"]
        ):
            raise ValueError(
                f"lifecycle server {record['server']} has inconsistent dependency waits"
            )
        if audit["arena_acquire_calls"] <= 0 or audit["arena_acquire_slots"] <= 0:
            raise ValueError(
                f"lifecycle server {record['server']} observed no direct arena refill"
            )
        arena_stage_ns = sum(
            audit[name]
            for name in (
                "arena_acquire_lock_wait_ns",
                "arena_acquire_state_clone_ns",
                "arena_acquire_allocation_plan_ns",
                "arena_acquire_backend_reserve_ns",
                "arena_acquire_state_build_ns",
                "arena_acquire_state_validate_ns",
                "arena_acquire_grant_install_ns",
                "arena_acquire_state_persist_ns",
                "arena_acquire_trace_ns",
            )
        )
        if audit["arena_acquire_total_ns"] <= 0 or arena_stage_ns > audit["arena_acquire_total_ns"]:
            raise ValueError(
                f"lifecycle server {record['server']} has inconsistent arena timing evidence"
            )
    if server_count == 1:
        records[0]["command_free_direct_read_ops"] = command_free_direct_read_ops
        records[0]["recovery_control"] = (
            recovery_records[0] if require_recovery_control else None
        )
        if not require_recovery_control:
            records[0]["serving_profile"] = "single_authority_minimal"
        return records[0]
    audit = {}
    extent_states = {}
    epoch_fields = {"server_epoch", "layout_epoch", "namespace_epoch", "metadata_lsn"}
    for record in records:
        for key, value in record.get("audit", {}).items():
            if key == "extent_states":
                extent_states.update(
                    {
                        f"{record['server']}:{extent}": state
                        for extent, state in value.items()
                    }
                )
            elif isinstance(value, bool):
                audit[key] = bool(audit.get(key, False) or value)
            elif isinstance(value, int):
                if key in epoch_fields:
                    audit[key] = max(audit.get(key, 0), value)
                else:
                    audit[key] = audit.get(key, 0) + value
    audit["direct_metadata"] = {
        key: sum(
            record["audit"]["direct_metadata"][key]
            for record in records
        )
        for key in DIRECT_METADATA_AUTHORITY_COUNTERS
    }
    audit["extent_states"] = extent_states
    return {
        "schema_version": "badfs.lifecycle.inspection.aggregate.v1",
        "server": "aggregate",
        "fabric": fabric,
        "audit": audit,
        "servers": records,
        "command_free_direct_read_ops": command_free_direct_read_ops,
        "recovery_control": recovery_records,
    }


def read_event_records(path: pathlib.Path) -> list[dict]:
    records = []
    previous = None
    with path.open("r", encoding="utf-8") as source:
        for line in source:
            record = json.loads(line)
            capture = record.get("host_capture_ns")
            if not isinstance(capture, int) or not isinstance(record.get("line"), str):
                raise ValueError("invalid console event sidecar record")
            if previous is not None and capture < previous:
                raise ValueError("console capture timestamps went backwards")
            previous = capture
            records.append(record)
    return records


def decode_selected_trace_event(
    line: str, marker: str, selected_events: tuple[str, ...]
) -> dict | None:
    if marker not in line:
        return None
    payload = line.split(marker, 1)[1]
    try:
        # The functional platform multiplexes several guest processes through
        # one hvc console. A complete proof record may therefore have unrelated
        # serial text appended before the translated newline, or an individual
        # record may be truncated. Accept only a complete JSON prefix and drop
        # damaged samples; the proof below still fails closed unless other
        # independently correlated samples establish the required ordering.
        record, _ = json.JSONDecoder().raw_decode(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(record, dict):
        return None
    if record.get("event") not in selected_events:
        return None
    return record


def strict_persistency_proof(paths: Paths, summaries: list[dict], server_count: int) -> dict:
    owner_endpoint = {
        record["owner"]: record["endpoint"]
        for record in summaries
        if isinstance(record.get("owner"), int) and isinstance(record.get("endpoint"), int)
    }
    client_events = read_event_records(paths.event_log("client0"))
    server_events = []
    for server_index in range(server_count):
        server_events.extend(
            read_event_records(paths.event_log(f"server{server_index}"))
        )
    server_events.sort(key=lambda event: event["host_capture_ns"])
    direct = []
    for event in client_events:
        marker = "BADFS_DIRECT_MAP_TRACE_JSON "
        record = decode_selected_trace_event(
            event["line"],
            marker,
            (
                "persisted",
                "coherent_sealed",
                "writer_host_persisted",
                "drop",
                "unmap",
            ),
        )
        if record is None:
            continue
        if (
            record.get("schema_version") == "badfs.direct-map-trace.v1"
            and record.get("access") in ("write", "read_write")
            and record.get("rc") == 0
        ):
            direct.append((record, event["host_capture_ns"]))
    lifecycle = []
    for event in server_events:
        marker = "BADFS_LIFECYCLE_TRACE_JSON "
        record = decode_selected_trace_event(
            event["line"],
            marker,
            (
                "store_direct_begin",
                "store_direct_success",
                "payload_dependency_acquired",
                "payload_dependency_persisted",
            ),
        )
        if record is None:
            continue
        if (
            record.get("schema_version") == "badfs.lifecycle.v1"
        ):
            lifecycle.append((record, event["host_capture_ns"]))
    coherence = parse_trace(paths.coherence)
    acks = {
        record.get("snoop_id"): record
        for record in coherence if record.get("event") == "snoop_ack"
    }
    completions = {
        record.get("snoop_id"): record
        for record in coherence if record.get("event") == "dirty_completion"
    }
    fence_completions = {
        (record.get("src_host"), record.get("session_id"), record.get("request_id")): record
        for record in coherence
        if record.get("event") == "persistence_fence_completion"
        and record.get("opcode") == "FENCE"
    }
    fence_requests = [
        record for record in coherence
        if record.get("event") == "request" and record.get("opcode") == "FENCE"
    ]
    gets_by_line = {}
    snoops_by_line = {}
    for record in coherence:
        line_address = record.get("line_address")
        if not isinstance(line_address, int):
            continue
        if record.get("event") == "request" and record.get("opcode") == "GETS":
            gets_by_line.setdefault(line_address, []).append(record)
        elif record.get("event") == "snoop_send":
            snoops_by_line.setdefault(line_address, []).append(record)
    begin = {
        (record.get("owner"), record.get("op_id")): (record, capture)
        for record, capture in lifecycle if record.get("event") == "store_direct_begin"
    }
    success = {
        (record.get("owner"), record.get("op_id")): (record, capture)
        for record, capture in lifecycle if record.get("event") == "store_direct_success"
    }
    writer_proofs = []
    authority_proofs = []
    bi_proofs = []

    writer_host_persisted = {}
    for record, capture in direct:
        if record.get("event") != "writer_host_persisted":
            continue
        key = (record.get("owner"), record.get("op_id"))
        if key in writer_host_persisted:
            # More than one success event for one operation cannot establish
            # an unambiguous one-shot provider completion.
            writer_host_persisted[key] = None
        else:
            writer_host_persisted[key] = (record, capture)

    def dependency_key(record):
        numeric_fields = (
            "owner",
            "op_id",
            "extent_id",
            "extent_generation",
            "layout_epoch",
            "mapping_offset",
            "mapping_length",
            "dependency_range_index",
            "dependency_range_count",
            "dependency_range_bytes",
        )
        if any(not isinstance(record.get(field), int) for field in numeric_fields):
            return None
        if (
            record["owner"] <= 0
            or record["op_id"] <= 0
            or record["extent_id"] <= 0
            or record["extent_generation"] <= 0
            or record["layout_epoch"] <= 0
            or record["mapping_length"] <= 0
            or record["dependency_range_count"] <= 0
            or record["dependency_range_index"] >= record["dependency_range_count"]
            or record["dependency_range_bytes"] <= 0
        ):
            return None
        digest = record.get("dependency_range_set_digest")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            return None
        return tuple(record[field] for field in numeric_fields) + (digest,)

    acquired_dependencies = {}
    persisted_dependencies = {}
    for record, capture in lifecycle:
        if record.get("event") not in (
            "payload_dependency_acquired",
            "payload_dependency_persisted",
        ):
            continue
        key = dependency_key(record)
        if key is None:
            continue
        target = (
            acquired_dependencies
            if record.get("event") == "payload_dependency_acquired"
            else persisted_dependencies
        )
        if key in target:
            # A duplicate exact stage makes the sample ambiguous and therefore
            # cannot establish a one-shot acquire/persist chain.
            target[key] = None
        else:
            target[key] = (record, capture)

    for direct_record, unmap_capture in direct:
        key = (direct_record.get("owner"), direct_record.get("op_id"))
        if key not in begin or key not in success or key[0] not in owner_endpoint:
            continue
        begin_record, _ = begin[key]
        success_record, success_capture = success[key]
        offset = direct_record.get("offset")
        length = direct_record.get("length")
        if (
            not isinstance(offset, int) or not isinstance(length, int) or length <= 0
            or begin_record.get("mapping_offset") != offset
            or begin_record.get("mapping_length") != length
            or success_record.get("mapping_offset") != offset
            or success_record.get("mapping_length") != length
        ):
            continue
        if (
            direct_record.get("event") == "persisted"
            and begin_record.get("fault_point") == "writer_persisted"
            and unmap_capture < begin[key][1] <= success_capture
        ):
            writer_proofs.append({
                "owner": key[0],
                "op_id": key[1],
                "endpoint": owner_endpoint[key[0]],
                "mapping_offset": offset,
                "mapping_length": length,
                "persisted_capture_ns": unmap_capture,
                "store_begin_capture_ns": begin[key][1],
                "store_success_capture_ns": success_capture,
            })
            continue
        writer_host_value = writer_host_persisted.get(key)
        if (
            direct_record.get("event") == "coherent_sealed"
            and begin_record.get("fault_point") == "coherent_seal"
            and unmap_capture < begin[key][1] <= success_capture
            and writer_host_value is not None
        ):
            writer_host_record, writer_host_capture = writer_host_value
            if (
                writer_host_record.get("offset") == offset
                and writer_host_record.get("length") == length
                and success_capture < writer_host_capture
            ):
                writer_proofs.append({
                    "owner": key[0],
                    "op_id": key[1],
                    "endpoint": owner_endpoint[key[0]],
                    "mapping_offset": offset,
                    "mapping_length": length,
                    "coherent_seal_capture_ns": unmap_capture,
                    "store_begin_capture_ns": begin[key][1],
                    "store_success_capture_ns": success_capture,
                    "persisted_capture_ns": writer_host_capture,
                    "proof_kind": "writer_host_receipt",
                })
                continue
        if (
            direct_record.get("event") == "coherent_sealed"
            and begin_record.get("fault_point") == "coherent_seal"
            and unmap_capture < begin[key][1] <= success_capture
        ):
            for dependency_key_value, acquired_value in acquired_dependencies.items():
                if acquired_value is None:
                    continue
                acquired_record, acquired_capture = acquired_value
                if (
                    acquired_record.get("owner") != key[0]
                    or acquired_record.get("op_id") != key[1]
                    or acquired_record.get("fault_point") != "authority_coherent_acquire"
                    or acquired_record.get("persist_state") != "not_persisted"
                    or acquired_capture <= success_capture
                ):
                    continue
                persisted_value = persisted_dependencies.get(dependency_key_value)
                if persisted_value is None:
                    continue
                persisted_record, persisted_capture = persisted_value
                if (
                    persisted_record.get("fault_point") != "authority_payload_persist"
                    or persisted_record.get("persist_state") != "persisted"
                    or persisted_capture <= acquired_capture
                ):
                    continue
                dependency_offset = acquired_record["mapping_offset"]
                dependency_length = acquired_record["mapping_length"]
                dependency_end = dependency_offset + dependency_length
                if (
                    dependency_offset < offset
                    or dependency_end > offset + length
                    or dependency_end <= dependency_offset
                ):
                    continue

                authority_gets = None
                line = dependency_offset // 64 * 64
                while line < dependency_end and authority_gets is None:
                    for request in gets_by_line.get(line, ()):
                        request_ns = request.get("monotonic_ns")
                        request_host = request.get("src_host")
                        if (
                            isinstance(request_ns, int)
                            and isinstance(request_host, int)
                            and 0 <= request_host < server_count
                            and request.get("status") == "OK"
                            and success_capture < request_ns < acquired_capture
                        ):
                            authority_gets = request
                            break
                    line += 64
                if authority_gets is None:
                    continue

                fence_pair = None
                authority_host = authority_gets["src_host"]
                for request in fence_requests:
                    request_ns = request.get("monotonic_ns")
                    if (
                        request.get("src_host") != authority_host
                        or request.get("status") != "OK"
                        or not isinstance(request_ns, int)
                        or not (acquired_capture < request_ns < persisted_capture)
                    ):
                        continue
                    completion = fence_completions.get((
                        request.get("src_host"),
                        request.get("session_id"),
                        request.get("request_id"),
                    ))
                    completion_ns = completion.get("monotonic_ns") if completion else None
                    if (
                        completion is not None
                        and completion.get("status") == "OK"
                        and isinstance(completion_ns, int)
                        and request_ns <= completion_ns < persisted_capture
                    ):
                        fence_pair = (request, completion)
                        break
                if fence_pair is None:
                    continue

                dirty_transfer = None
                cxl_host = owner_endpoint[key[0]] + server_count
                gets_ns = authority_gets["monotonic_ns"]
                for snoop in snoops_by_line.get(authority_gets["line_address"], ()):
                    snoop_ns = snoop.get("monotonic_ns")
                    if (
                        snoop.get("opcode") not in ("SNP_DATA_DOWNGRADE", "SNP_DATA_INV")
                        or snoop.get("dst_host") != cxl_host
                        or not isinstance(snoop_ns, int)
                        or not (gets_ns <= snoop_ns < acquired_capture)
                    ):
                        continue
                    snoop_id = snoop.get("snoop_id")
                    ack = acks.get(snoop_id)
                    completion = completions.get(snoop_id)
                    if not ack or not completion:
                        continue
                    ack_ns = ack.get("monotonic_ns")
                    completion_ns = completion.get("monotonic_ns")
                    if (
                        ack.get("opcode") == "SNOOP_ACK"
                        and ack.get("src_host") == cxl_host
                        and ack.get("ack_strength") == "MODEL"
                        and ack.get("dirty_data") is True
                        and ack.get("payload_len") == 64
                        and ack.get("status") == "OK"
                        and completion.get("opcode") == "SNOOP_ACK"
                        and completion.get("src_host") == cxl_host
                        and completion.get("ack_strength") == "MODEL"
                        and completion.get("dirty_data") is True
                        and completion.get("payload_len") == 64
                        and completion.get("status") == "OK"
                        and ack.get("session_id") == snoop.get("session_id")
                        and completion.get("session_id") == snoop.get("session_id")
                        and ack.get("epoch") == snoop.get("epoch")
                        and completion.get("epoch") == snoop.get("epoch")
                        and isinstance(ack_ns, int)
                        and isinstance(completion_ns, int)
                        and snoop_ns <= ack_ns <= completion_ns < acquired_capture
                    ):
                        dirty_transfer = {
                            "opcode": snoop.get("opcode"),
                            "snoop_id": snoop_id,
                            "snoop_send_ns": snoop_ns,
                            "snoop_ack_ns": ack_ns,
                            "dirty_completion_ns": completion_ns,
                        }
                        break

                fence_request, fence_completion = fence_pair
                authority_proofs.append({
                    "owner": key[0],
                    "op_id": key[1],
                    "endpoint": owner_endpoint[key[0]],
                    "cxl_host_id": cxl_host,
                    "authority_host_id": authority_host,
                    "mapping_offset": offset,
                    "mapping_length": length,
                    "dependency_offset": dependency_offset,
                    "dependency_length": dependency_length,
                    "dependency_range_index": acquired_record["dependency_range_index"],
                    "dependency_range_count": acquired_record["dependency_range_count"],
                    "dependency_range_bytes": acquired_record["dependency_range_bytes"],
                    "dependency_range_set_digest": acquired_record[
                        "dependency_range_set_digest"
                    ],
                    "sealed_capture_ns": unmap_capture,
                    "store_begin_capture_ns": begin[key][1],
                    "store_success_capture_ns": success_capture,
                    "gets_ns": authority_gets["monotonic_ns"],
                    "gets_line_address": authority_gets["line_address"],
                    "acquired_capture_ns": acquired_capture,
                    "fence_request_ns": fence_request["monotonic_ns"],
                    "fence_completion_ns": fence_completion["monotonic_ns"],
                    "persisted_capture_ns": persisted_capture,
                    "dirty_transfer": dirty_transfer,
                })
                break
            if authority_proofs and authority_proofs[-1]["owner"] == key[0] \
                    and authority_proofs[-1]["op_id"] == key[1]:
                continue
        if begin_record.get("fault_point") != "dirty_range_ownership":
            continue
        cxl_host = owner_endpoint[key[0]] + server_count
        for snoop in coherence:
            if (
                snoop.get("event") != "snoop_send"
                or snoop.get("opcode") != "SNP_DATA_INV"
                or snoop.get("dst_host") != cxl_host
            ):
                continue
            line_address = snoop.get("line_address")
            if not isinstance(line_address, int) or not (offset <= line_address < offset + length):
                continue
            snoop_id = snoop.get("snoop_id")
            ack = acks.get(snoop_id)
            completion = completions.get(snoop_id)
            if not ack or not completion:
                continue
            if not (
                ack.get("opcode") == "SNOOP_ACK"
                and ack.get("src_host") == cxl_host
                and ack.get("ack_strength") == "MODEL"
                and ack.get("dirty_data") is True
                and ack.get("payload_len") == 64
                and ack.get("status") == "OK"
                and completion.get("opcode") == "SNOOP_ACK"
                and completion.get("src_host") == cxl_host
                and completion.get("ack_strength") == "MODEL"
                and completion.get("dirty_data") is True
                and completion.get("payload_len") == 64
                and completion.get("status") == "OK"
                and ack.get("session_id") == snoop.get("session_id")
                and completion.get("session_id") == snoop.get("session_id")
                and ack.get("epoch") == snoop.get("epoch")
                and completion.get("epoch") == snoop.get("epoch")
            ):
                continue
            snoop_ns = snoop.get("monotonic_ns")
            ack_ns = ack.get("monotonic_ns")
            completion_ns = completion.get("monotonic_ns")
            if not (
                isinstance(snoop_ns, int) and isinstance(ack_ns, int)
                and isinstance(completion_ns, int)
                and unmap_capture < snoop_ns <= ack_ns <= completion_ns < success_capture
            ):
                continue
            bi_proofs.append({
                "owner": key[0],
                "op_id": key[1],
                "endpoint": owner_endpoint[key[0]],
                "cxl_host_id": cxl_host,
                "mapping_offset": offset,
                "mapping_length": length,
                "line_address": line_address,
                "snoop_id": snoop_id,
                "unmap_capture_ns": unmap_capture,
                "snoop_send_ns": snoop_ns,
                "snoop_ack_ns": ack_ns,
                "dirty_completion_ns": completion_ns,
                "store_success_capture_ns": success_capture,
            })
            break
    legacy_begins = [
        record for record, _ in lifecycle
        if record.get("event") == "store_direct_begin"
        and record.get("fault_point") == "dirty_range_ownership"
    ]
    if legacy_begins and not bi_proofs:
        raise ValueError(
            "legacy direct writes lack exact unmap < SNP_DATA_INV < dirty ACK < completion < store success proof"
        )
    if not writer_proofs and not authority_proofs and not bi_proofs:
        raise ValueError(
            "no exact writer-persisted, authority-acquired, or "
            "back-invalidation persistency proof"
        )
    return {
        "count": len(writer_proofs) + len(authority_proofs) + len(bi_proofs),
        "writer_persisted": {
            "count": len(writer_proofs),
            "first": writer_proofs[0] if writer_proofs else None,
        },
        "authority_acquired": {
            "count": len(authority_proofs),
            "dirty_transfer_count": sum(
                proof["dirty_transfer"] is not None for proof in authority_proofs
            ),
            "first": authority_proofs[0] if authority_proofs else None,
        },
        "backinvalidation": {
            "count": len(bi_proofs),
            "first": bi_proofs[0] if bi_proofs else None,
        },
    }


def validate_tiny_provider_counters(final_stats: dict, proof: dict) -> None:
    writer_count = proof.get("writer_persisted", {}).get("count", 0)
    authority = proof.get("authority_acquired", {})
    authority_count = authority.get("count", 0)
    authority_dirty_transfers = authority.get("dirty_transfer_count", 0)
    bi_count = proof.get("backinvalidation", {}).get("count", 0)
    if writer_count > 0:
        for name in ("request_fence", "persistence_fence_completions"):
            if final_stats.get(name, 0) <= 0:
                raise ValueError(
                    f"writer-persisted proof lacks provider counter: {name}"
                )
        if final_stats.get("putm", 0) <= 0:
            raise ValueError("writer-persisted proof lacks a line PUTM")
    if authority_count > 0:
        for name in ("gets", "request_fence", "persistence_fence_completions"):
            if final_stats.get(name, 0) <= 0:
                raise ValueError(
                    f"authority-acquired proof lacks provider counter: {name}"
                )
        if authority_dirty_transfers > 0:
            if (
                final_stats.get("snp_data_downgrade", 0)
                + final_stats.get("snp_data_inv", 0)
                <= 0
            ):
                raise ValueError(
                    "authority-acquired proof lacks a dirty-data snoop counter"
                )
            for name in ("model_acks", "dirty_data_completions"):
                if final_stats.get(name, 0) <= 0:
                    raise ValueError(
                        f"authority-acquired proof lacks provider counter: {name}"
                    )
    if bi_count > 0:
        for name in ("snp_data_inv", "model_acks", "dirty_data_completions"):
            if final_stats.get(name, 0) <= 0:
                raise ValueError(
                    f"back-invalidation proof lacks provider counter: {name}"
                )


def validate_inspection_provider_counters(final_stats: dict, inspection: dict) -> None:
    writer_items = int(
        inspection.get("audit", {}).get("writer_persisted_direct_items", 0)
    )
    if writer_items <= 0:
        return
    for name in ("request_fence", "persistence_fence_completions"):
        if final_stats.get(name, 0) <= 0:
            raise ValueError(
                f"writer-persisted inspection lacks provider counter: {name}"
            )


def classify_io500_verifier(stage: str, rc: int, output: str) -> str:
    # The QEMU serial console may expand one guest newline to `\r\r\n`.
    # Strip carriage returns before matching whole verifier lines; keep the
    # exact `[OK]` line requirement so an embedded or invalid-run marker is
    # never accepted as a clean result.
    normalized = output.replace("\r", "")
    invalid_ok = (
        "[OK] But this is an invalid run!" in normalized
        and "ERROR:" not in normalized
    )
    clean_ok = re.search(r"(?m)^\[OK\]$", normalized) is not None
    if stage in SEMANTIC_SMOKE_STAGES:
        if rc == 1 and invalid_ok:
            return "PASS: integrity verified; expected INVALID smoke (verifier rc=1)"
        raise RuntimeError(
            f"IO500 verifier for {stage} did not report the expected verified INVALID result (rc={rc})"
        )
    if rc == 0 and clean_ok:
        return "PASS: exact RISC-V io500-verify rc=0"
    raise RuntimeError(f"IO500 verifier for {stage} failed clean verification (rc={rc})")


def io500_verifier_record(stage: str, rc: int, output: str) -> dict:
    failure = None
    try:
        verdict = classify_io500_verifier(stage, rc, output)
    except RuntimeError as error:
        failure = str(error)
        verdict = f"FAIL: {failure}"
    return {
        "command": ["io500-verify", "config.ini", "result.txt", "1"],
        "rc": rc,
        "verdict": verdict,
        "output": output,
        "failure": failure,
    }


def verify_io500(console: Console, stage: str) -> dict:
    start = len(console.output)
    console.send(f"LEGOFS_VERIFY {stage}")
    rc = wait_verify_exit(console, stage, 600, start)
    output = console.output[start:]
    return io500_verifier_record(stage, rc, output)


def parse_io500_metrics(text: str) -> dict:
    sections = {}
    current = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        header = re.fullmatch(r"\[([^]]+)\]", line)
        if header:
            current = header.group(1)
            sections.setdefault(current, {})
            continue
        if current is None or "=" not in line:
            continue
        name, value = (part.strip() for part in line.split("=", 1))
        sections[current][name] = value

    phases = []
    for name, values in sections.items():
        if name in ("SCORE", "SCOREX") or "score" not in values:
            continue
        try:
            score = float(values["score"])
            seconds = float(values["t_delta"])
        except (KeyError, ValueError):
            continue
        phases.append({
            "name": name,
            "score": score,
            "unit": "GiB/s" if name.startswith("ior-") else "kIOPS",
            "seconds": seconds,
            "t_start": values.get("t_start"),
            "t_end": values.get("t_end"),
            "start_realtime_ns": parse_io500_utc_timestamp(values.get("t_start")),
            "end_realtime_ns": parse_io500_utc_timestamp(values.get("t_end")),
        })

    def score(name: str) -> dict | None:
        values = sections.get(name)
        if not values:
            return None
        try:
            return {
                "md_kiops": float(values["MD"]),
                "bw_gib_s": float(values["BW"]),
                "score": float(values["SCORE"]),
                "hash": values["hash"],
            }
        except (KeyError, ValueError):
            return None

    return {
        "phases": phases,
        "official": score("SCORE"),
        "extended": score("SCOREX"),
    }


def parse_io500_utc_timestamp(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        timestamp = datetime.datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    timestamp = timestamp.replace(tzinfo=datetime.timezone.utc)
    return int(timestamp.timestamp()) * 1_000_000_000


def project_observation_timestamp_to_phase(
    event_monotonic_ns: int,
    header: dict,
    phases: list[dict],
    *,
    guest_sync_error_ns: int,
    io500_timestamp_resolution_ns: int = 1_000_000_000,
) -> dict:
    """Project a rank-local monotonic event without inventing boundary precision."""
    estimate = (
        int(header["realtime_anchor_ns"])
        + int(event_monotonic_ns)
        - int(header["monotonic_anchor_ns"])
    )
    error = (
        max(0, int(header.get("anchor_span_ns", 0)))
        + max(0, int(guest_sync_error_ns))
        + max(0, int(io500_timestamp_resolution_ns))
    )
    lower = estimate - error
    upper = estimate + error
    overlaps = []
    contained = []
    for phase in phases:
        start = phase.get("start_realtime_ns")
        end = phase.get("end_realtime_ns")
        if not isinstance(start, int) or not isinstance(end, int) or end < start:
            continue
        if lower <= end and upper >= start:
            overlaps.append(phase["name"])
        if start <= lower and upper <= end:
            contained.append(phase["name"])
    if len(contained) == 1 and overlaps == contained:
        assignment = contained[0]
        ambiguous = False
    else:
        assignment = None
        ambiguous = bool(overlaps)
    return {
        "estimated_realtime_ns": estimate,
        "error_bound_ns": error,
        "interval_start_ns": lower,
        "interval_end_ns": upper,
        "phase": assignment,
        "phase_ambiguous": ambiguous,
        "overlapping_phases": overlaps,
    }


def io500_phase_enabled(config_text: str, phase: str) -> bool:
    config = configparser.ConfigParser(interpolation=None)
    config.read_string(config_text)
    if not config.has_section(phase):
        return False
    try:
        return config.getboolean(phase, "run", fallback=False)
    except ValueError as error:
        raise ValueError(f"IO500 phase {phase} has an invalid run value") from error


def extract_results(paths: Paths) -> dict:
    extracted = paths.bundle / "result-disk"
    extracted.mkdir()
    subprocess.run(
        ["debugfs", "-R", f"rdump / {extracted}", str(paths.client_result)],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    stage_dir = extracted / paths.stage
    result_path = stage_dir / "result.txt"
    config_path = stage_dir / "config.ini"
    if not result_path.is_file() or not config_path.is_file():
        raise FileNotFoundError("IO500 result disk lacks result.txt or config.ini")
    shutil.copy2(result_path, paths.bundle / "result.txt")
    shutil.copy2(config_path, paths.bundle / "config.ini")
    text = result_path.read_text(encoding="utf-8", errors="replace")
    config_text = config_path.read_text(encoding="utf-8", errors="strict")
    find_enabled = io500_phase_enabled(config_text, "find")
    find_section = re.search(
        r"(?ms)^\[find\]\s*$.*?^found\s*=\s*(\d+)\s*$", text
    )
    if find_enabled and (
        find_section is None or int(find_section.group(1)) <= 0
    ):
        raise ValueError("IO500 find phase did not match any file")
    invalid_lines = [line for line in text.splitlines() if "[INVALID]" in line]
    return {
        "result_path": str(paths.bundle / "result.txt"),
        "config_path": str(paths.bundle / "config.ini"),
        "invalid": bool(invalid_lines),
        "invalid_lines": invalid_lines,
        "metrics": parse_io500_metrics(text),
    }


def stop_process(item: OwnedProcess) -> None:
    try:
        item.terminate_owned()
    except (OSError, subprocess.SubprocessError):
        pass


def finish_guest_processes(consoles: list[Console], grace_seconds: float = 5) -> None:
    """Give every guest the same shutdown grace period, then stop its owned PID."""
    def finish_one(console: Console) -> None:
        try:
            console.process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            stop_process(console.owned)

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(consoles)) as pool:
        futures = [pool.submit(finish_one, console) for console in consoles]
        for future in futures:
            future.result()


def functional_model_evidence() -> dict:
    return {
        "functional_model_only": True,
        "guest_visible_cxl_evidence": False,
        "physical_hardware_evidence": False,
        # Deprecated compatibility field. Functional-model traffic is not
        # physical hardware evidence; new consumers use the two fields above.
        "physical_cxl_evidence": False,
    }


def resolve_payload_persistence_owner(
    durability_profile: str, requested_owner: str
) -> str:
    if requested_owner == "auto":
        requested_owner = (
            "placement-routed"
            if durability_profile == "coherent-seal-no-writeback"
            else "writer-before-visibility"
        )
    legal_payload_owners = {
        "d-before-v": {"writer-before-visibility"},
        "coherent-seal-needs-writeback": {"writer-before-visibility"},
        "coherent-seal-no-writeback": {
            "authority-bi-acquire",
            "writer-receipt",
            "placement-routed",
        },
    }
    if requested_owner not in legal_payload_owners[durability_profile]:
        raise ValueError(
            "payload persistence owner is incompatible with durability profile"
        )
    return requested_owner


def execute(
    paths: Paths,
    timeout: int,
    server_count: int,
    client_count: int,
    coherence_cache_bytes: int = DEFAULT_COHERENCE_CACHE_MIB * 1024**2,
    ssd_cache_mib: int = DEFAULT_SSD_CACHE_MIB,
    server_read_exclusive: bool = False,
    full_coherence_trace: bool = False,
    serving_transport: str = "legacy",
    cursor_mode: str = "owned",
    cq_wait_mode: str = "timer_sleep",
    durability_profile: str = "d-before-v",
    payload_persistence_owner: str = "auto",
    writer_persist_provider: str = "msync",
    packed_small_segments: str = "on",
    small_segment_count: int = 2048,
    fault_profile: str = "none",
    observation_config: dict | None = None,
    host_profiler: str = "off",
    close_batch_mode: str = "batched",
) -> dict:
    direct_metadata_validation_mode = "cxl" if serving_transport == "cxl" else None
    durability_validation_profile = {
        "d-before-v": "D_BEFORE_V",
        "coherent-seal-no-writeback": "COHERENT_SEAL_NO_WRITEBACK",
        "coherent-seal-needs-writeback": "COHERENT_SEAL_NEEDS_WRITEBACK",
    }[durability_profile]
    payload_persistence_owner = resolve_payload_persistence_owner(
        durability_profile, payload_persistence_owner
    )
    if writer_persist_provider == "riscv-zicbom-dax" and (
        durability_profile != "coherent-seal-no-writeback"
        or payload_persistence_owner not in {"writer-receipt", "placement-routed"}
        or serving_transport != "cxl"
    ):
        raise ValueError(
            "riscv-zicbom-dax requires CXL serving, coherent-seal-no-writeback, "
            "and a writer-receipt payload owner"
        )
    payload_owner_validation = {
        "authority-bi-acquire": "AUTHORITY_BI_ACQUIRE",
        "writer-receipt": "WRITER_RECEIPT",
        "placement-routed": "PLACEMENT_ROUTED",
        "writer-before-visibility": "WRITER_BEFORE_VISIBILITY",
    }[payload_persistence_owner]
    packed_small_segment_validation_mode = packed_small_validation_expectation(
        paths.stage, packed_small_segments, fault_profile
    )
    if observation_config is None:
        observation_config = {
            "mode": "default",
            "sample_shift": DEFAULT_OBSERVATION_SAMPLE_SHIFT,
            "arena_mib": DEFAULT_OBSERVATION_ARENA_MIB,
            "arena_bytes": DEFAULT_OBSERVATION_ARENA_MIB * 1024**2,
            "producer_slots": 8,
            "run_id": observation_run_id_for_label(paths.result_label),
            "profile_manifest": None,
            "profile_sha256": None,
            "schema_digest": OBSERVATION_SCHEMA_DIGEST,
        }
    build = verify_manifest(paths)
    prepare_paths(paths)
    owner = str(uuid.uuid4())
    host_count = client_count + server_count
    result = {
        "schema_version": "legofs.riscv.io500.v2",
        "status": "failed",
        "stage": paths.stage,
        "first_failure": None,
        "filesystem_valid": False,
        "io500_validity": None,
        "observation_valid": False,
        "observation_error": None,
        **functional_model_evidence(),
        "owner_token": owner,
        "build": build,
        "topology": {
            "physical_hosts": 1,
            "server_guests": server_count,
            "client_guests": client_count,
            "mpi_ranks": client_count,
            "qemu_machine": "sifive_u",
            "type3_endpoints": host_count,
            "type3_bytes_per_endpoint": ENDPOINT_BYTES,
            "shared_cxlmemsim_region_bytes": ENDPOINT_BYTES,
            "backend": "CXLMemSim ssd-stream over QEMU file-backed persistent-memdev",
            "data_path": (
                "private 64-byte TCP MESI functional adapter gated by "
                "guest-visible CXL Type 3 HDM-DB"
            ),
            "coherence_cache_bytes_per_endpoint": coherence_cache_bytes,
            "ssd_cache_bytes": ssd_cache_mib * 1024**2,
            "server_read_exclusive": server_read_exclusive,
            "server_scope": f"{server_count} LegoFS server allocation partitions",
            "legofs_serving_transport": serving_transport,
            "legofs_direct_metadata_mode": (
                "cxl" if serving_transport == "cxl" else "unavailable"
            ),
            "legofs_cursor_mode": cursor_mode,
            "legofs_cq_wait_mode": cq_wait_mode,
            "legofs_durability_profile": durability_profile,
            "legofs_payload_persistence_owner": payload_persistence_owner,
            "legofs_writer_persist_provider": writer_persist_provider,
            "legofs_close_mode": close_batch_mode,
            "lifecycle_pool_layout": (
                "v7-packed-small-segments"
                if packed_small_segments == "on"
                else "v6-variable-extents"
            ),
            "small_segment_count_per_authority": (
                small_segment_count if packed_small_segments == "on" else 0
            ),
            "legofs_open_mode": "fused_pin",
            "fault_profile": fault_profile,
            "allocator_partitions": [
                {
                    "server": server,
                    "offset": server * (ENDPOINT_BYTES // server_count),
                    "length": ENDPOINT_BYTES // server_count,
                }
                for server in range(server_count)
            ],
        },
        "validity": {},
        "commands": {},
        "observation_config": observation_config,
        "host_profiler": {
            "requested": host_profiler,
            "effective": "off" if host_profiler == "off" else "deferred_until_o5",
        },
        "processes": {},
        "cleanup": {},
    }
    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # CXLMemSim listens on INADDR_ANY. Reserve against the same address scope;
    # probing only 127.0.0.1 can select a port already owned on 127.0.1.1.
    tcp.bind(("0.0.0.0", 0))
    coherence_port = tcp.getsockname()[1]
    multicast = UdpPortReservation()
    server_owned = None
    server_log_handle = None
    server_consoles = []
    client_consoles = []
    all_owned = []
    try:
        sparse_file(paths.central_ssd, ENDPOINT_BYTES)
        sparse_file(paths.device_dram, ENDPOINT_BYTES)
        for host_id in range(host_count):
            sparse_file(paths.lsa(host_id), 2 * 1024**2)
        for server_index in range(server_count):
            ext2_image(paths.server_state(server_index), 4 * 1024**3)
        ext2_image(paths.client_result, 2 * 1024**3)

        # Hello/tiny retain only registration and BI ordering evidence.  All
        # other stages use lock-free counters with no JSON formatting or I/O.
        use_proof_trace = paths.stage in ("hello", "tiny")
        cxl_command = server_command(
            paths,
            coherence_port,
            use_proof_trace,
            ssd_cache_mib,
            full_coherence_trace,
        )
        result["commands"]["cxlmemsim"] = cxl_command
        server_log_handle = paths.server_log.open("w", encoding="utf-8")
        tcp.close()
        tcp = None
        server_process = subprocess.Popen(
            cxl_command,
            stdout=server_log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        server_owned = OwnedProcess(server_process, cxl_command, paths.run, owner, time.monotonic_ns())
        all_owned.append(server_owned)
        wait_log(paths.server_log, "Server listening on TCP port", server_process, timeout)
        multicast_port = multicast.port
        multicast.release()

        environment = qemu_environment(paths)
        environment["PATH"] = str(paths.qemu.parent) + os.pathsep + environment.get("PATH", "")
        server_boot_futures = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=server_count) as pool:
            for server_index in range(server_count):
                command = qemu_command(
                    paths,
                    role="server",
                    server_index=server_index,
                    client_index=None,
                    host_id=server_index,
                    coherence_port=coherence_port,
                    multicast_port=multicast_port,
                    coherence_cache_bytes=coherence_cache_bytes,
                    read_exclusive=server_read_exclusive,
                )
                result["commands"][f"server{server_index}_qemu"] = command
                console = Console(
                    command,
                    environment,
                    paths.console_log(f"server{server_index}"),
                    paths.event_log(f"server{server_index}"),
                    paths.run,
                    owner,
                )
                server_consoles.append(console)
                all_owned.append(console.owned)
                server_boot_futures.append(
                    pool.submit(
                        boot_guest,
                        console,
                        paths,
                        "server",
                        server_index,
                        paths.stage,
                        server_count,
                        client_count,
                        serving_transport,
                        cursor_mode,
                        cq_wait_mode,
                        durability_profile,
                        payload_persistence_owner,
                        writer_persist_provider,
                        packed_small_segments,
                        small_segment_count,
                        close_batch_mode,
                        observation_config,
                        timeout,
                    )
                )
            for future in server_boot_futures:
                future.result()
        for server_index, console in enumerate(server_consoles):
            wait_guest_marker(
                console,
                f"LEGOFS_IO500_CXL_READY role=server index={server_index}", timeout
            )
            wait_guest_marker(
                console,
                f"LEGOFS_IO500_SERVER_READY index={server_index}", timeout
            )

        boot_futures = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=client_count) as pool:
            for index in range(client_count):
                command = qemu_command(
                    paths,
                    role="client",
                    server_index=None,
                    client_index=index,
                    host_id=server_count + index,
                    coherence_port=coherence_port, multicast_port=multicast_port,
                    coherence_cache_bytes=coherence_cache_bytes,
                )
                result["commands"][f"client{index}_qemu"] = command
                console = Console(
                    command, environment, paths.console_log(f"client{index}"),
                    paths.event_log(f"client{index}"), paths.run, owner,
                )
                client_consoles.append(console)
                all_owned.append(console.owned)
                boot_futures.append(
                    pool.submit(
                        boot_guest,
                        console,
                        paths,
                        "client",
                        index,
                        paths.stage,
                        server_count,
                        client_count,
                        serving_transport,
                        cursor_mode,
                        cq_wait_mode,
                        durability_profile,
                        payload_persistence_owner,
                        writer_persist_provider,
                        packed_small_segments,
                        small_segment_count,
                        close_batch_mode,
                        observation_config,
                        timeout,
                    )
                )
            for future in boot_futures:
                future.result()
        for index, console in enumerate(client_consoles):
            wait_guest_marker(
                console, f"LEGOFS_IO500_CXL_READY role=client index={index}", timeout
            )
            wait_guest_marker(
                console, f"LEGOFS_IO500_CLIENT_READY index={index}", timeout
            )

        result["clock_sync"] = synchronize_guest_clocks(
            server_consoles, client_consoles, client_count=client_count
        )

        trace_records = parse_trace(paths.coherence) if use_proof_trace else []
        registration_records = (
            registrations(trace_records, host_count) if use_proof_trace else None
        )
        result["coherence_registrations"] = registration_records
        metered_processes = {"cxlmemsim": server_process}
        metered_processes.update({
            f"server{index}_qemu": console.process
            for index, console in enumerate(server_consoles)
        })
        metered_processes.update({
            f"client{index}_qemu": console.process
            for index, console in enumerate(client_consoles)
        })
        cost_started_ns = time.monotonic_ns()
        cost_before = sample_named_processes(metered_processes)
        try:
            if fault_profile == "clean-server-restart":
                result["fault_injection"] = run_clean_restart_fault(
                    server_consoles[0], client_consoles, timeout
                )
            elif fault_profile == "reject-unauthorized-clean-restart":
                result["fault_injection"] = run_unauthorized_clean_restart_probe(
                    server_consoles[0], timeout
                )
            elif fault_profile == "diagnose-system-sync":
                result["system_sync_probe"] = run_system_sync_probe(
                    client_consoles[0], client_count, timeout
                )
            result["commands"]["mpi"] = launch_mpi(
                server_consoles,
                client_consoles,
                paths.stage,
                timeout,
                client_count,
                serving_transport,
            )
            record_host_process_cost_window(
                result,
                cost_before,
                sample_named_processes(metered_processes),
                cost_started_ns,
                time.monotonic_ns(),
            )
            if paths.stage in ("scc", "standard") and serving_transport == "cxl":
                mpi_command = result["commands"]["mpi"]
                mpi_output = client_consoles[0].output[
                    mpi_command["output_start"]:mpi_command["output_end"]
                ]
                result["post_run_exporter_summary"] = (
                    parse_post_run_exporter_summary(
                        mpi_output,
                        client_count,
                        cursor_mode,
                        cq_wait_mode,
                        "cxl",
                    )
                )
            result["observation"] = dump_observations_safely(
                paths,
                server_consoles,
                client_consoles,
                config=observation_config,
                client_count=client_count,
                stage=paths.stage,
                timeout=timeout,
            )
            result["observation_valid"] = result["observation"]["observation_valid"]
            result["observation_error"] = result["observation"]["observation_error"]
            if fault_profile == "reject-active-clean-retirement":
                result["fault_injection"] = run_active_lane_retirement_probe(
                    client_consoles, timeout
                )
        except MpiStageError:
            record_host_process_cost_window(
                result,
                cost_before,
                sample_named_processes(metered_processes),
                cost_started_ns,
                time.monotonic_ns(),
            )
            diagnostics = {}
            if "observation" not in result:
                result["observation"] = dump_observations_safely(
                    paths,
                    server_consoles,
                    client_consoles,
                    config=observation_config,
                    client_count=client_count,
                    stage=paths.stage,
                    timeout=timeout,
                )
                result["observation_valid"] = result["observation"]["observation_valid"]
                result["observation_error"] = result["observation"]["observation_error"]
            try:
                diagnostics["rank_placement"] = parse_rank_markers(
                    client_consoles, paths.stage, client_count
                )
            except Exception as error:
                diagnostics["rank_placement_error"] = str(error)
            try:
                diagnostics["posix_path_summaries"] = dump_summaries(
                    client_consoles,
                    client_count,
                    cursor_mode,
                    cq_wait_mode,
                    direct_metadata_validation_mode,
                    close_batch_mode,
                    payload_owner_validation,
                )
            except Exception as error:
                diagnostics["posix_path_summary_error"] = str(error)
            try:
                diagnostics["lifecycle_inspection"] = inspect_servers(
                    client_consoles[0],
                    server_count,
                    require_direct_read=False,
                    require_recovery_control=not (
                        serving_transport == "cxl" and server_count == 1
                    ),
                    expected_direct_metadata_mode=direct_metadata_validation_mode,
                    expected_durability_profile=durability_validation_profile,
                    expected_payload_persistence_owner=payload_owner_validation,
                )
            except Exception as error:
                diagnostics["lifecycle_inspection_error"] = str(error)
            result["failed_mpi_diagnostics"] = diagnostics
            raise
        finally:
            record_host_process_cost_window(
                result,
                cost_before,
                sample_named_processes(metered_processes),
                cost_started_ns,
                time.monotonic_ns(),
            )

        if paths.stage == "hello":
            hello = parse_hello(client_consoles, client_count)
            result["mpi_hello"] = hello
            result["validity"] = {
                "program_path": "real MPICH Hydra/PMI hello",
                "completion": "PASS",
                "no_invalid": "NOT_APPLICABLE",
                "verifier": "NOT_APPLICABLE",
                "scale": f"{client_count} ranks / {client_count} independent client guests",
                "backend_scope": (
                    f"one simulated 64-GiB CXL region / {server_count} disjoint "
                    "LegoFS allocator partitions"
                ),
                "concurrency": f"PASS: all rank intervals overlap by {hello['overlap_ns']} ns",
                "correctness": "MPI placement/topology only; not a distributed filesystem proof",
            }
        else:
            rank_records = parse_rank_markers(
                client_consoles, paths.stage, client_count
            )
            summaries = dump_summaries(
                client_consoles,
                client_count,
                cursor_mode,
                cq_wait_mode,
                direct_metadata_validation_mode,
                close_batch_mode,
                payload_owner_validation,
            )
            validate_packed_small_segment_client_evidence(
                summaries,
                expected_packed_small_segments=packed_small_segment_validation_mode,
            )
            command_free_direct_read_ops = sum(
                min(
                    int(record.get("stats", {}).get("direct_ro_open_hits", 0)),
                    int(record.get("stats", {}).get("direct_read_data_maps", 0)),
                )
                for record in summaries
            )
            if fault_profile == "reject-active-clean-retirement":
                inspection = result["fault_injection"]["lifecycle_inspection"]
            else:
                inspection = inspect_servers(
                    client_consoles[0],
                    server_count,
                    command_free_direct_read_ops=command_free_direct_read_ops,
                    require_recovery_control=not (
                        serving_transport == "cxl" and server_count == 1
                    ),
                    expected_direct_metadata_mode=direct_metadata_validation_mode,
                    expected_packed_small_segments=(
                        packed_small_segment_validation_mode
                    ),
                    expected_durability_profile=durability_validation_profile,
                    expected_payload_persistence_owner=payload_owner_validation,
                )
            verifier = verify_io500(client_consoles[0], paths.stage)
            result["rank_placement"] = rank_records
            result["posix_path_summaries"] = summaries
            result["lifecycle_inspection"] = inspection
            if direct_metadata_validation_mode is not None:
                result["direct_metadata"] = direct_metadata_breakdown(
                    summaries, inspection, direct_metadata_validation_mode
                )
                result["direct_mutation"] = direct_mutation_breakdown(summaries)
            result["verifier"] = verifier
            result["legofs_timing"] = legofs_timing_breakdown(
                summaries,
                inspection,
                result["host_process_cost"]["wall_ns"],
                client_count,
            )
            writer_host = result["legofs_timing"]["persistence"]["writer_host"]
            if writer_host["writer_host_persist_jobs_completed"]:
                if writer_persist_provider == "riscv-zicbom-dax" and (
                    writer_host["writer_host_zicbom_persist_jobs"] == 0
                    or writer_host["writer_host_msync_persist_jobs"] != 0
                ):
                    raise ValueError(
                        "selected riscv-zicbom-dax writer provider was not used exclusively"
                    )
                if writer_persist_provider == "msync" and (
                    writer_host["writer_host_msync_persist_jobs"] == 0
                    or writer_host["writer_host_zicbom_persist_jobs"] != 0
                ):
                    raise ValueError(
                        "selected msync writer provider was not used exclusively"
                    )
        for console in client_consoles:
            console.send("LEGOFS_POWEROFF")
        for console in server_consoles:
            console.send("LEGOFS_POWEROFF")
        finish_guest_processes(client_consoles + server_consoles)
        server_process.send_signal(signal.SIGINT)
        try:
            server_process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            stop_process(server_owned)
        server_owned.record_exit()
        server_log_handle.flush()

        if paths.stage != "hello":
            io500 = extract_results(paths)
            result["io500"] = io500
            no_invalid = not io500["invalid"]
            expected_no_invalid = paths.stage in ("scc", "standard")
            result["validity"] = {
                "program_path": (
                    f"PASS: {client_count} rank summaries from "
                    "libbadfs_intercept lifecycle route"
                ),
                "completion": "PASS: mpiexec rc=0 and result.txt preserved",
                "no_invalid": (
                    "PASS" if no_invalid else
                    ("EXPECTED_INVALID_SEMANTIC_SMOKE" if paths.stage in SEMANTIC_SMOKE_STAGES else "FAIL")
                ),
                "verifier": verifier["verdict"],
                "scale": (
                    f"{client_count} ranks / {client_count} independent client QEMU guests / "
                    f"{server_count} server guests"
                ),
                "backend_scope": "one LegoFS namespace and one simulated 64-GiB CXL region",
                "concurrency": (
                    f"{client_count} simultaneous Hydra proxies; storage-operation "
                    "overlap is only separately traced for tiny"
                ),
                "correctness": "IO500 verifier only; no Jepsen/Elle distributed-consistency claim",
            }

        if paths.server_log.is_file():
            server_text = paths.server_log.read_text(encoding="utf-8", errors="replace")
            stats_lines = [
                line.split("COHERENCE_V2_STATS_JSON ", 1)[1]
                for line in server_text.splitlines()
                if "COHERENCE_V2_STATS_JSON " in line
            ]
            if len(stats_lines) != 1:
                raise ValueError("CXLMemSim did not emit exactly one final stats record")
            result["coherence_final_stats"] = json.loads(stats_lines[0])
            final_stats = result["coherence_final_stats"]
            if final_stats.get("registrations") != host_count:
                raise ValueError(
                    f"CXLMemSim final registration count is not {host_count}"
                )
            for name in (
                "timeouts", "protocol_errors", "delivery_failures",
                "server_copy_failures", "active_bindings",
            ):
                if final_stats.get(name) != 0:
                    raise ValueError(f"CXLMemSim final error counter is nonzero: {name}")
            if final_stats.get("gets", 0) + final_stats.get("getm", 0) <= 0:
                raise ValueError("CXLMemSim final stats lack coherent data traffic")
            if paths.stage != "hello":
                validate_inspection_provider_counters(final_stats, inspection)
            if paths.stage == "tiny":
                result["strict_persistency_proof"] = strict_persistency_proof(
                    paths, summaries, server_count
                )
                validate_tiny_provider_counters(
                    final_stats, result["strict_persistency_proof"]
                )
            result["guest_visible_cxl_evidence"] = True
        if paths.stage != "hello":
            if verifier["failure"] is not None:
                raise RuntimeError(verifier["failure"])
            if expected_no_invalid and not no_invalid:
                raise ValueError(f"{paths.stage} result contains [INVALID]")
        result["filesystem_valid"] = True
        result["io500_validity"] = result["validity"]
        result["status"] = "passed"
        return result
    except BaseException as error:
        result["first_failure"] = str(error)
        raise
    finally:
        if tcp is not None:
            tcp.close()
        multicast.release()
        for item in reversed(all_owned):
            stop_process(item)
        if server_log_handle is not None and not server_log_handle.closed:
            server_log_handle.close()
        for console in client_consoles + server_consoles:
            console.close_files()
        result["processes"] = {
            f"process{index}": item.as_json() for index, item in enumerate(all_owned)
        }
        remaining = [item.process.pid for item in all_owned if item.matches_live_pid()]
        result["cleanup"] = {"owned_processes_remaining": remaining}
        atomic_json(paths.result, result)


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--stage",
        choices=(
            "hello",
            "tiny",
            "easy-smoke",
            "hard-smoke",
            "metadata-smoke",
            "rnd4k",
            "scc",
            "standard",
        ),
        required=True,
    )
    parser.add_argument("--server-count", type=int, choices=(1, 2), default=1)
    parser.add_argument("--client-count", type=int, default=DEFAULT_CLIENTS)
    parser.add_argument("--result-label")
    parser.add_argument("--timeout", type=int, default=7200)
    parser.add_argument(
        "--coherence-cache-mib",
        type=int,
        default=DEFAULT_COHERENCE_CACHE_MIB,
        help="per-endpoint QEMU coherence directory capacity in MiB",
    )
    parser.add_argument(
        "--ssd-cache-mib",
        type=int,
        default=DEFAULT_SSD_CACHE_MIB,
        help="CXLMemSim SSD-stream residency capacity in MiB",
    )
    parser.add_argument(
        "--server-read-exclusive",
        action="store_true",
        help="negative-control mode: force server reads to request M state",
    )
    parser.add_argument(
        "--full-coherence-trace",
        action="store_true",
        help="diagnostic mode: record every coherence event instead of counters",
    )
    parser.add_argument(
        "--serving-transport",
        choices=("legacy", "cxl"),
        default="legacy",
    )
    parser.add_argument(
        "--cursor-mode",
        choices=("legacy_shared", "owned"),
        default="owned",
    )
    parser.add_argument(
        "--cq-wait-mode",
        choices=("timer_sleep", "cooperative_yield"),
        default="timer_sleep",
    )
    parser.add_argument(
        "--durability-profile",
        choices=(
            "d-before-v",
            "coherent-seal-no-writeback",
            "coherent-seal-needs-writeback",
        ),
        default="d-before-v",
    )
    parser.add_argument(
        "--payload-persistence-owner",
        choices=(
            "auto",
            "authority-bi-acquire",
            "writer-receipt",
            "placement-routed",
            "writer-before-visibility",
        ),
        default="auto",
        help="select the host that closes immutable payload durability dependencies",
    )
    parser.add_argument(
        "--writer-persist-provider",
        choices=("msync", "riscv-zicbom-dax"),
        default="msync",
        help="select the writer-host persistent-DAX cache-clean provider",
    )
    parser.add_argument(
        "--packed-small-segments",
        choices=("off", "on"),
        default="on",
        help="select the v7 product layout or explicit v6 diagnostic layout",
    )
    parser.add_argument(
        "--close-batch-mode",
        choices=("immediate", "batched"),
        default="batched",
        help="select immediate or bounded-batch clean read-only close commands",
    )
    parser.add_argument(
        "--small-segment-count",
        type=int,
        default=2048,
        help="number of 2 MiB packed segments per authority when v7 is enabled",
    )
    parser.add_argument(
        "--fault-profile",
        choices=(
            "none",
            "clean-server-restart",
            "reject-unauthorized-clean-restart",
            "reject-active-clean-retirement",
            "diagnose-system-sync",
        ),
        default="none",
    )
    parser.add_argument(
        "--observation-mode",
        choices=("default", "off", "aggregate", "sampled"),
        default=None,
    )
    parser.add_argument("--observation-sample-shift", type=int, default=None)
    parser.add_argument("--observation-arena-mib", type=int, default=None)
    parser.add_argument("--observation-profile-manifest")
    parser.add_argument(
        "--host-profiler", choices=("off", "stat", "record"), default="off"
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.timeout <= 0:
        raise ValueError("timeout must be positive")
    if not 1 <= args.client_count <= DEFAULT_CLIENTS:
        raise ValueError(f"client-count must be between 1 and {DEFAULT_CLIENTS}")
    if not 1 <= args.coherence_cache_mib <= 4095:
        raise ValueError("coherence-cache-mib must be between 1 and 4095")
    if not 1 <= args.ssd_cache_mib <= 65536:
        raise ValueError("ssd-cache-mib must be between 1 and 65536")
    partition_bytes = ENDPOINT_BYTES // args.server_count
    max_small_segments = partition_bytes // (2 * 1024**2) - 1
    if not 1 <= args.small_segment_count <= max_small_segments:
        raise ValueError(
            f"small-segment-count must be between 1 and {max_small_segments}"
        )
    if (
        args.observation_sample_shift is not None
        and not 0 <= args.observation_sample_shift <= 20
    ):
        raise ValueError("observation-sample-shift must be between 0 and 20")
    if (
        args.observation_arena_mib is not None
        and not 8 <= args.observation_arena_mib <= 64
    ):
        raise ValueError("observation-arena-mib must be between 8 and 64")
    if args.fault_profile in (
        "clean-server-restart",
        "reject-unauthorized-clean-restart",
        "reject-active-clean-retirement",
    ) and (
        args.stage != "tiny"
        or args.server_count != 1
        or args.client_count != 2
        or args.serving_transport != "cxl"
    ):
        raise ValueError(
            f"{args.fault_profile} requires --stage tiny --server-count 1 "
            "--client-count 2 --serving-transport cxl"
        )
    if args.fault_profile == "diagnose-system-sync" and (
        args.stage != "metadata-smoke"
        or args.server_count != 1
        or args.client_count != 2
        or args.serving_transport != "cxl"
    ):
        raise ValueError(
            "diagnose-system-sync requires --stage metadata-smoke "
            "--server-count 1 --client-count 2 --serving-transport cxl"
        )
    if args.durability_profile != "d-before-v" and args.serving_transport != "cxl":
        raise ValueError("coherent durability profiles require --serving-transport cxl")
    if args.durability_profile != "d-before-v" and args.fault_profile != "none":
        raise ValueError(
            "coherent durability profiles currently require --fault-profile none"
        )
    if args.result_label is not None and not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]*", args.result_label
    ):
        raise ValueError("result-label contains unsupported characters")
    if args.result_label is not None and len(args.result_label) > 96:
        raise ValueError("result-label is longer than 96 characters")
    root = pathlib.Path(__file__).resolve().parents[1]
    paths = Paths(root, args.stage, args.result_label)
    observation_config = resolve_observation_config(
        args, root, observation_run_id_for_label(paths.result_label)
    )
    try:
        result = execute(
            paths,
            args.timeout,
            args.server_count,
            args.client_count,
            args.coherence_cache_mib * 1024**2,
            args.ssd_cache_mib,
            args.server_read_exclusive,
            args.full_coherence_trace,
            args.serving_transport,
            args.cursor_mode,
            args.cq_wait_mode,
            args.durability_profile,
            args.payload_persistence_owner,
            args.writer_persist_provider,
            args.packed_small_segments,
            args.small_segment_count,
            args.fault_profile,
            observation_config,
            args.host_profiler,
            args.close_batch_mode,
        )
    except BaseException as error:
        print(f"error: {error}", file=sys.stderr)
        print(f"result: {paths.result}", file=sys.stderr)
        return 1
    print(f"LEGOFS_IO500_COMPLETE stage={args.stage} result={paths.result}")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
