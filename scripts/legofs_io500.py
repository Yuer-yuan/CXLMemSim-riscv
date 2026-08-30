#!/usr/bin/env python3
"""Run one or two LegoFS servers and independent RISC-V IO500 clients."""

import argparse
import hashlib
import concurrent.futures
import json
import os
import pathlib
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from legofs_type3_2node import Console, OwnedProcess, qemu_environment


DEFAULT_CLIENTS = 10
DEFAULT_COHERENCE_CACHE_MIB = 32
DEFAULT_SSD_CACHE_MIB = 512
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
MPI_FATAL_MARKERS = (
    "BADFS_STRICT_LIFECYCLE_DIRECT_INIT_FAILED",
    "LEGOFS_IO500_FATAL",
)
SEMANTIC_SMOKE_STAGES = (
    "tiny",
    "easy-smoke",
    "hard-smoke",
    "metadata-smoke",
    "rnd4k",
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
            "dispatcher_backend_ns",
            "cqe_publish_ns",
        )
    }
    audit = inspection.get("audit", {})
    metadata_wal_persist_ns = int(audit.get("metadata_wal_persist_ns", 0))
    lifecycle_payload_persist_ns = int(
        audit.get("direct_write_payload_persist_ns", 0)
    )
    lifecycle_state_persist_ns = int(audit.get("direct_write_state_persist_ns", 0))
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
    return {
        "schema_version": "legofs.timing-breakdown.v4",
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
        "arena_acquire": arena_acquire,
        "persistence": {
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
            "lifecycle_payload_persist_ns": lifecycle_payload_persist_ns,
            "lifecycle_state_persist_ns": lifecycle_state_persist_ns,
            "persistence_wait_ns": metadata_wal_persist_ns
            + lifecycle_payload_persist_ns
            + lifecycle_state_persist_ns,
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
        "visibility_durability": audit.get("visibility_durability", {}),
        "interpretation": (
            "client values are summed elapsed RPC/control intervals across ranks; "
            "transport and persistence intervals are separately accumulated and may be "
            "nested or overlap across ranks, so they are not additive wall-time shares; "
            "none of these counters is CPU time"
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


def boot_guest(
    console: Console,
    paths: Paths,
    role: str,
    index: int,
    stage: str,
    server_count: int,
    client_count: int,
    serving_transport: str,
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
    console.command_until_prompt(
        "setenv bootargs 'earlycon=sbi console=hvc0 loglevel=5 "
        "cxl_core.pmem_as_dax=1 "
        f"io500.role={role} io500.index={index} io500.stage={stage} "
        f"io500.server_count={server_count} io500.client_count={client_count} "
        f"io500.serving_transport={serving_transport}'",
        timeout,
    )
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


def wait_mpi_exit(console: Console, stage: str, timeout: int, start: int) -> int:
    pattern = re.compile(
        rf"LEGOFS_IO500_MPI_EXIT stage={re.escape(stage)} rc=(-?\d+)(?=[^0-9])"
    )
    deadline = time.monotonic() + timeout
    with console.condition:
        while True:
            stage_output = console.output[start:]
            for marker in MPI_FATAL_MARKERS:
                if marker in stage_output:
                    raise RuntimeError(
                        f"MPI stage {stage} reported fatal marker: {marker}"
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
    # Oversubscribed TCG advances each VM's wall clock according to the host
    # time that VM receives.  IO500's official find phase compares ctime from
    # client0's result disk with ctime assigned by other guests, so a one-shot
    # boot synchronization is insufficient.  Maintain a common wall-clock
    # epoch while MPI runs; IO500 elapsed time remains MPI monotonic time.
    maintain_clocks = stage == "tiny" and serving_transport == "cxl"
    clock_stop = threading.Event()
    clock_maintenance = []
    clock_errors = []

    def maintain_guest_clocks() -> None:
        while not clock_stop.wait(1.0):
            try:
                clock_maintenance.append(
                    synchronize_guest_clocks(
                        server_consoles,
                        client_consoles,
                        client_count=client_count,
                        # Under saturated TCG the date command can be queued
                        # for several host seconds after all consoles receive
                        # it.  The filesystem-level find predicate remains the
                        # authoritative correctness check; this larger bound
                        # only prevents that queueing delay from aborting the
                        # diagnostic helper itself.
                        allowed_receipt_lag_seconds=30,
                    )
                )
            except BaseException as error:
                clock_errors.append(error)
                return

    clock_thread = None
    if maintain_clocks:
        clock_thread = threading.Thread(
            target=maintain_guest_clocks,
            name="legofs-io500-clock-maintenance",
            daemon=True,
        )
        clock_thread.start()
    try:
        rc = wait_mpi_exit(coordinator, stage, timeout, start)
    finally:
        if clock_thread is not None:
            clock_stop.set()
            clock_thread.join(timeout=35)
    if clock_thread is not None and clock_thread.is_alive():
        raise TimeoutError("guest clock maintenance did not stop")
    return {
        "launcher": "MPICH Hydra manual",
        "coordinator": "client0",
        "returncode": rc,
        "proxy_commands": [by_proxy[index] for index in range(client_count)],
        "post_preflight_clock_sync": post_preflight_clock_sync,
        "clock_maintenance": {
            "enabled": maintain_clocks,
            "interval_seconds": 1.0 if maintain_clocks else None,
            "synchronizations": clock_maintenance,
            "errors": [str(error) for error in clock_errors],
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
                "dispatcher_backend_ns",
                "cqe_publish_ns",
            )
        ):
            raise ValueError("CXL authority timing evidence is incomplete")
        if (
            authority_timing["sqe_consumed"] != submitted
            or authority_timing["cqe_published"] != submitted
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
            )
        ):
            raise ValueError("CXL client timing evidence is incomplete")
        if (
            client_timing["calls"] != submitted
            or client_timing["sq_publish_ns"] <= 0
            or client_timing["cq_wait_ns"] <= 0
        ):
            raise ValueError("CXL client timing evidence does not cover every request")
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
    records: list[dict], client_count: int = DEFAULT_CLIENTS
) -> None:
    active_ranks = set()
    authority_count = None
    for record in records:
        rank = record.get("mpi_rank")
        endpoint = record.get("endpoint")
        if rank not in range(client_count) or endpoint != rank:
            raise ValueError(f"POSIX summary rank/endpoint mismatch: {record}")
        authority_count = validate_cxl_path_summary(
            record, endpoint, authority_count
        )
        stats = record.get("stats", {})
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


def validate_post_run_exporter_summary(record: dict, client_count: int) -> None:
    expected_endpoint = 2 * client_count + 1
    if record.get("mpi_rank") is not None:
        raise ValueError("post-run exporter retained an MPI rank identity")
    if record.get("endpoint") != expected_endpoint:
        raise ValueError(
            "post-run exporter endpoint mismatch: "
            f"expected {expected_endpoint}, got {record.get('endpoint')}"
        )
    validate_cxl_path_summary(record, expected_endpoint)
    stats = record.get("stats", {})
    totals = record.get("syscall_classification", {}).get("totals", {})
    if stats.get("open_ops", 0) <= 0 or stats.get("read_ops", 0) <= 0:
        raise ValueError("post-run exporter did not read the LegoFS result directory")
    if totals.get("handled", 0) <= 0:
        raise ValueError("post-run exporter handled no BadFS syscall")


def parse_post_run_exporter_summary(output: str, client_count: int) -> dict:
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
    validate_post_run_exporter_summary(record, client_count)
    return record


def dump_summaries(
    consoles: list[Console], client_count: int = DEFAULT_CLIENTS
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
    validate_posix_summaries(records, client_count)
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
            or not isinstance(authority_timing.get("dispatcher_backend_ns"), int)
            or authority_timing["dispatcher_backend_ns"] <= 0
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
    require_recovery_control: bool = True,
) -> dict:
    start = len(console.output)
    console.send("LEGOFS_INSPECT")
    console.wait("LEGOFS_IO500_INSPECT_EXIT index=0 rc=0", 120, start)
    return parse_server_inspection(
        console.output[start:],
        server_count,
        require_direct_read=require_direct_read,
        require_recovery_control=require_recovery_control,
    )


def parse_server_inspection(
    output: str,
    server_count: int,
    *,
    require_direct_read: bool = True,
    expected_active_client_lanes: int = 0,
    require_recovery_control: bool = True,
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
    if fabric.get("trusted_direct_write_ops", 0) == 0 or (
        require_direct_read and fabric.get("trusted_direct_read_ops", 0) == 0
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
        for name in ("pending_operations", "quarantined_extents", "active_read_leases"):
            if audit.get(name, 0) != 0:
                raise ValueError(
                    f"lifecycle server {record['server']} audit reports {name}"
                )
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
        ):
            if not isinstance(audit.get(name), int) or audit[name] < 0:
                raise ValueError(
                    f"lifecycle server {record['server']} lacks {name} evidence"
                )
        if audit["metadata_wal_persist_barriers"] <= 0:
            raise ValueError(
                f"lifecycle server {record['server']} observed no metadata WAL barrier"
            )
        if audit["metadata_wal_persist_records"] < audit["metadata_wal_persist_barriers"]:
            raise ValueError(
                f"lifecycle server {record['server']} has fewer WAL records than barriers"
            )
        if (
            audit["provider_persist_barriers"] <= 0
            or audit["provider_payload_barriers"] <= 0
            or audit["provider_allocator_barriers"] <= 0
        ):
            raise ValueError(
                f"lifecycle server {record['server']} lacks provider persistence evidence"
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
            "dependency_closure_waits",
            "dependency_closure_wait_ns",
        ):
            if not isinstance(vd.get(name), int) or vd[name] < 0:
                raise ValueError(
                    f"lifecycle server {record['server']} lacks V/D field {name}"
                )
        if vd.get("provider_profile") != "D_BEFORE_V":
            raise ValueError(
                f"lifecycle server {record['server']} did not retain D_BEFORE_V"
            )
        logical = vd["logical_mutations"]
        if logical <= 0 or vd["semantic_batches"] <= 0 or vd["foreground_d_waits"] <= 0:
            raise ValueError(
                f"lifecycle server {record['server']} observed no syscall-first V/D evidence"
            )
        if not (
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
    audit["extent_states"] = extent_states
    return {
        "schema_version": "badfs.lifecycle.inspection.aggregate.v1",
        "server": "aggregate",
        "fabric": fabric,
        "audit": audit,
        "servers": records,
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
            event["line"], marker, ("persisted", "drop", "unmap")
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
            event["line"], marker, ("store_direct_begin", "store_direct_success")
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
    begin = {
        (record.get("owner"), record.get("op_id")): (record, capture)
        for record, capture in lifecycle if record.get("event") == "store_direct_begin"
    }
    success = {
        (record.get("owner"), record.get("op_id")): (record, capture)
        for record, capture in lifecycle if record.get("event") == "store_direct_success"
    }
    writer_proofs = []
    bi_proofs = []
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
    if not writer_proofs and not bi_proofs:
        raise ValueError("no exact writer-persisted or back-invalidation persistency proof")
    return {
        "count": len(writer_proofs) + len(bi_proofs),
        "writer_persisted": {
            "count": len(writer_proofs),
            "first": writer_proofs[0] if writer_proofs else None,
        },
        "backinvalidation": {
            "count": len(bi_proofs),
            "first": bi_proofs[0] if bi_proofs else None,
        },
    }


def validate_tiny_provider_counters(final_stats: dict, proof: dict) -> None:
    writer_count = proof.get("writer_persisted", {}).get("count", 0)
    bi_count = proof.get("backinvalidation", {}).get("count", 0)
    if writer_count > 0:
        for name in ("request_fence", "persistence_fence_completions"):
            if final_stats.get(name, 0) <= 0:
                raise ValueError(
                    f"writer-persisted proof lacks provider counter: {name}"
                )
        if final_stats.get("putm", 0) <= 0:
            raise ValueError("writer-persisted proof lacks a line PUTM")
    if bi_count > 0:
        for name in ("snp_data_inv", "model_acks", "dirty_data_completions"):
            if final_stats.get(name, 0) <= 0:
                raise ValueError(
                    f"back-invalidation proof lacks provider counter: {name}"
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
    find_section = re.search(
        r"(?ms)^\[find\]\s*$.*?^found\s*=\s*(\d+)\s*$", text
    )
    if "[find]" in text and (
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
    fault_profile: str = "none",
) -> dict:
    build = verify_manifest(paths)
    prepare_paths(paths)
    owner = str(uuid.uuid4())
    host_count = client_count + server_count
    result = {
        "schema_version": "legofs.riscv.io500.v2",
        "status": "failed",
        "stage": paths.stage,
        "first_failure": None,
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
                        timeout,
                    )
                )
            for future in server_boot_futures:
                future.result()
        for server_index, console in enumerate(server_consoles):
            console.wait(
                f"LEGOFS_IO500_CXL_READY role=server index={server_index}", timeout
            )
            console.wait(
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
                        timeout,
                    )
                )
            for future in boot_futures:
                future.result()
        for index, console in enumerate(client_consoles):
            console.wait(f"LEGOFS_IO500_CXL_READY role=client index={index}", timeout)
            console.wait(f"LEGOFS_IO500_CLIENT_READY index={index}", timeout)

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
            result["commands"]["mpi"] = launch_mpi(
                server_consoles,
                client_consoles,
                paths.stage,
                timeout,
                client_count,
                serving_transport,
            )
            if paths.stage in ("scc", "standard") and serving_transport == "cxl":
                mpi_command = result["commands"]["mpi"]
                mpi_output = client_consoles[0].output[
                    mpi_command["output_start"]:mpi_command["output_end"]
                ]
                result["post_run_exporter_summary"] = (
                    parse_post_run_exporter_summary(mpi_output, client_count)
                )
            if fault_profile == "reject-active-clean-retirement":
                result["fault_injection"] = run_active_lane_retirement_probe(
                    client_consoles, timeout
                )
        except MpiStageError:
            diagnostics = {}
            try:
                diagnostics["rank_placement"] = parse_rank_markers(
                    client_consoles, paths.stage, client_count
                )
            except Exception as error:
                diagnostics["rank_placement_error"] = str(error)
            try:
                diagnostics["posix_path_summaries"] = dump_summaries(
                    client_consoles, client_count
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
                )
            except Exception as error:
                diagnostics["lifecycle_inspection_error"] = str(error)
            result["failed_mpi_diagnostics"] = diagnostics
            raise
        finally:
            cost_after = sample_named_processes(metered_processes)
            result["host_process_cost"] = host_process_cost_window(
                cost_before, cost_after, cost_started_ns, time.monotonic_ns()
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
            summaries = dump_summaries(client_consoles, client_count)
            if fault_profile == "reject-active-clean-retirement":
                inspection = result["fault_injection"]["lifecycle_inspection"]
            else:
                inspection = inspect_servers(
                    client_consoles[0],
                    server_count,
                    require_recovery_control=not (
                        serving_transport == "cxl" and server_count == 1
                    ),
                )
            verifier = verify_io500(client_consoles[0], paths.stage)
            result["rank_placement"] = rank_records
            result["posix_path_summaries"] = summaries
            result["lifecycle_inspection"] = inspection
            result["verifier"] = verifier
            result["legofs_timing"] = legofs_timing_breakdown(
                summaries,
                inspection,
                result["host_process_cost"]["wall_ns"],
                client_count,
            )
            if paths.stage == "tiny":
                result["strict_persistency_proof"] = strict_persistency_proof(
                    paths, summaries, server_count
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
            if paths.stage == "tiny":
                validate_tiny_provider_counters(
                    final_stats, result["strict_persistency_proof"]
                )
            result["guest_visible_cxl_evidence"] = True
        if paths.stage != "hello":
            if verifier["failure"] is not None:
                raise RuntimeError(verifier["failure"])
            if expected_no_invalid and not no_invalid:
                raise ValueError(f"{paths.stage} result contains [INVALID]")
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
        "--fault-profile",
        choices=(
            "none",
            "clean-server-restart",
            "reject-unauthorized-clean-restart",
            "reject-active-clean-retirement",
        ),
        default="none",
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
    if args.result_label is not None and not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]*", args.result_label
    ):
        raise ValueError("result-label contains unsupported characters")
    root = pathlib.Path(__file__).resolve().parents[1]
    paths = Paths(root, args.stage, args.result_label)
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
            args.fault_profile,
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
