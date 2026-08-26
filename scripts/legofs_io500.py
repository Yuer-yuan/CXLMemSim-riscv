#!/usr/bin/env python3
"""Run one or two LegoFS servers and independent RISC-V IO500 clients."""

import argparse
import concurrent.futures
import hashlib
import json
import os
import pathlib
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from legofs_type3_2node import Console, OwnedProcess, qemu_environment
from legofs_local_candidate_gate import evaluate_local_run


DEFAULT_CLIENTS = 10
FILESYSTEM_MODES = ("legacy-cxl-reference", "rdwo-candidate")
EVALUATION_MANIFEST_SCHEMA = "legofs.evaluation-run-manifest.v1"
PHASE_EVIDENCE_MANIFEST_SCHEMA = "legofs.phase-evidence-manifest.v1"
DEFAULT_COHERENCE_CACHE_MIB = 32
DEFAULT_SSD_CACHE_MIB = 512
POSIX_CAPABILITY_MATRIX = (
    pathlib.Path(__file__).resolve().parents[1]
    / "components/legofs/docs/superpowers/specs/legofs-v2-posix-capability-matrix.toml"
)
ENDPOINT_BYTES = 64 * 1024**3
CXL_CONTROL_RING_BYTES = 256 * 1024
CXL_CONTROL_DEFAULT_CLIENTS = 16
FMW_SIZE = "64G"
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
    r"LEGOFS_IO500_RANK_EXEC mode=(\S+) stage=(\S+) rank=(\d+) size=(\d+) "
    r"endpoint=(\d+) dax=(\S+)"
)
SUMMARY_RE = re.compile(
    r"LEGOFS_IO500_POSIX_SUMMARY index=(\d+) file=(\S+) (\{[^\n]+\})"
)
MPI_FATAL_MARKERS = (
    "BADFS_STRICT_LIFECYCLE_DIRECT_INIT_FAILED",
    "LEGOFS_IO500_FATAL",
)
KERNEL_PRINTK_INTERLEAVE_RE = re.compile(
    r"\[\s*\d+(?:\.\d+)?\]\s+[A-Za-z0-9_.:-]+:"
)
KERNEL_PRINTK_SPLIT_PREFIX_RE = re.compile(
    r"\[\s*\d+(?:\.\d+)?\]\s+[A-Za-z0-9_.:-]*"
)
KERNEL_PRINTK_INTERRUPT_TAIL_RE = re.compile(
    r"[A-Za-z0-9_.:-]*timer:\s+interrupt took\s+\d+\s+ns"
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


def cxl_control_layout(server_count: int, client_count: int) -> dict:
    align = lambda value, unit: (value + unit - 1) // unit * unit
    max_clients = max(CXL_CONTROL_DEFAULT_CLIENTS, client_count)
    slot_stride = align(4096 + 2 * CXL_CONTROL_RING_BYTES, 4096)
    server_stride = align(4096 + slot_stride * max_clients, 2 * 1024**2)
    control_size = align(4096 + server_stride * server_count, 2 * 1024**2)
    partition_size = (
        (ENDPOINT_BYTES - control_size) // server_count // (2 * 1024**2)
    ) * (2 * 1024**2)
    return {
        "protocol_version": 1,
        "transport": "cxl-dax-ring",
        "max_clients": max_clients,
        "ring_bytes_per_direction": CXL_CONTROL_RING_BYTES,
        "server_stride": server_stride,
        "control_region_bytes": control_size,
        "allocator_partitions": [
            {
                "server": server,
                "offset": control_size + server * partition_size,
                "length": partition_size,
            }
            for server in range(server_count)
        ],
    }


class Paths:
    def __init__(
        self,
        root: pathlib.Path,
        stage: str,
        result_label: str | None = None,
        filesystem_mode: str = "legacy-cxl-reference",
    ):
        self.root = root.resolve()
        self.stage = stage
        self.filesystem_mode = filesystem_mode
        self.result_label = result_label or stage
        self.build = self.root / "target/build/riscv-io500"
        self.platform = self.build / "platform"
        self.payload = self.build / "images/io500-payload.ext2"
        self.manifest = self.root / "target/results/legofs-io500/build-manifest.json"
        self.dependency_versions = (
            self.root / "target/results/legofs-io500/dependency-versions.txt"
        )
        self.isa_gate = self.root / "target/results/legofs-io500/isa-gate.txt"
        self.run = (
            self.root
            / "target/run/legofs-io500"
            / self.filesystem_mode
            / self.result_label
        )
        self.bundle = (
            self.root
            / "target/results/legofs-io500"
            / self.filesystem_mode
            / self.result_label
        )
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
        self.payload_root = self.build / "payload-root"
        self.payload_config = self.payload_root / "etc" / f"io500-{stage}.ini"
        self.legacy_server = self.payload_root / "bin" / "badfs-server"
        self.legacy_bench = self.payload_root / "bin" / "badfs-bench"
        self.legacy_intercept = self.payload_root / "lib" / "libbadfs_intercept.so"
        self.syscall_intercept = (
            self.payload_root / "lib" / "libsyscall_intercept.so.0.1.0"
        )
        self.rdwo_client = self.payload_root / "bin" / "badfs-rdwo-client"
        self.rdwo_server = self.payload_root / "bin" / "badfs-rdwo-server"
        self.rdwo_host_agent = self.payload_root / "bin" / "badfs-rdwo-host-agent"
        self.rdwo_intercept = (
            self.payload_root / "lib" / "libbadfs_rdwo_intercept.so"
        )
        self.product_engine_manifest = (
            self.payload_root / "etc" / "legofs-rdwo-engine.manifest"
        )
        self.product_capability_manifest = (
            self.payload_root / "etc" / "legofs-rdwo-capabilities.manifest"
        )

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


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_digest(value: dict) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_evidence_mode(record: dict) -> None:
    if bool(record.get("functional_model_only")) == bool(
        record.get("physical_hardware_evidence")
    ):
        raise ValueError(
            "functional_model_only and physical_hardware_evidence must be exclusive"
        )


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
    return {
        "schema_version": "legofs.timing-breakdown.v1",
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
        "interpretation": (
            "client values are summed elapsed RPC/control intervals across ranks; "
            "server commit and validation are nested inside some client intervals and "
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


def expected_mode_artifacts(paths: Paths, filesystem_mode: str) -> dict:
    if filesystem_mode == "legacy-cxl-reference":
        return {
            "badfs_server": paths.legacy_server,
            "badfs_bench": paths.legacy_bench,
            "badfs_intercept": paths.legacy_intercept,
            "syscall_intercept": paths.syscall_intercept,
        }
    if filesystem_mode == "rdwo-candidate":
        return {
            "rdwo_client": paths.rdwo_client,
            "rdwo_server": paths.rdwo_server,
            "rdwo_host_agent": paths.rdwo_host_agent,
            "rdwo_intercept": paths.rdwo_intercept,
            "product_engine_manifest": paths.product_engine_manifest,
            "product_capability_manifest": paths.product_capability_manifest,
        }
    raise ValueError(f"unsupported filesystem mode: {filesystem_mode}")


def verify_manifest(
    paths: Paths,
    filesystem_mode: str = "legacy-cxl-reference",
) -> dict:
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
    expected.update(expected_mode_artifacts(paths, filesystem_mode))
    if manifest.get("schema_version") != 2:
        raise ValueError("unsupported build manifest schema")
    source = manifest.get("sources", {}).get("legofs")
    if not isinstance(source, dict):
        raise ValueError("build manifest lacks the LegoFS component source")
    required_source_fields = ("commit", "tree", "dirty_diff_sha256", "status_sha256")
    for field in required_source_fields:
        value = source.get(field)
        expected_length = 40 if field in ("commit", "tree") else 64
        if not isinstance(value, str) or len(value) != expected_length:
            raise ValueError(f"invalid LegoFS source provenance field: {field}")
    for name, path in expected.items():
        entry = manifest.get("artifacts", {}).get(name)
        if not path.is_file() or not isinstance(entry, dict):
            raise FileNotFoundError(f"missing manifested artifact: {name}")
        if path.stat().st_size <= 0:
            raise ValueError(f"empty build artifact: {name}")
        expected_sha256 = entry.get("sha256")
        if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
            raise ValueError(f"manifested artifact lacks sha256: {name}")
        if sha256_file(path) != expected_sha256:
            raise ValueError(f"manifested artifact hash mismatch: {name}")
    return {"manifest": manifest}


def validate_evaluation_manifest(
    manifest_path: pathlib.Path,
    paths: Paths,
    build: dict,
    filesystem_mode: str,
    server_count: int,
    client_count: int,
) -> dict:
    record = json.loads(manifest_path.read_text(encoding="utf-8"))
    if record.get("schema_version") != EVALUATION_MANIFEST_SCHEMA:
        raise ValueError("unsupported evaluation manifest schema")
    closed = dict(record)
    digest = closed.pop("manifest_digest", None)
    if not isinstance(digest, str) or digest != canonical_digest(closed):
        raise ValueError("evaluation manifest digest mismatch")
    if record.get("filesystem_mode") != filesystem_mode:
        raise ValueError("evaluation manifest filesystem mode mismatch")
    if record.get("io500_mode") != "standard":
        raise ValueError("current IO500 harness requires standard mode")
    if record.get("product_consumes_this_manifest") is not False:
        raise ValueError("evaluation manifest must remain external to the product")
    if record.get("official_candidate") is not False:
        raise ValueError("evaluation shell cannot predeclare an official candidate")
    topology = record.get("topology", {})
    if (
        topology.get("server_guests") != server_count
        or topology.get("client_guests") != client_count
        or topology.get("mpi_ranks") != client_count
        or topology.get("shared_cxl_type3_region_bytes") != ENDPOINT_BYTES
        or topology.get("hdm_db_bi_required") is not True
    ):
        raise ValueError("evaluation manifest topology mismatch")
    evidence = record.get("phase_evidence", {})
    if (
        evidence.get("schema_version") != PHASE_EVIDENCE_MANIFEST_SCHEMA
        or evidence.get("external_only") is not True
        or evidence.get("product_visible_phase_identity") is not False
    ):
        raise ValueError("evaluation phase evidence is not external-only")
    records = evidence.get("records")
    if not isinstance(records, list) or not records:
        raise ValueError("evaluation manifest contains no phase evidence records")
    for phase in records:
        units = phase.get("required_metric_units")
        expected_units = ["GiB/s", "kIOPS"] if str(phase.get("phase", "")).startswith("ior-") else ["kIOPS"]
        if units != expected_units:
            raise ValueError("evaluation phase metric units are incomplete")
    expected_build_digest = record.get("build_identity", {}).get(
        "build_manifest_sha256"
    )
    if expected_build_digest != sha256_file(paths.manifest):
        raise ValueError("evaluation manifest build identity is stale")
    artifacts = record.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        raise ValueError("evaluation manifest artifact table is empty")
    for name, artifact in artifacts.items():
        if not isinstance(artifact, dict):
            raise ValueError(f"invalid evaluation artifact record: {name}")
        artifact_path = pathlib.Path(str(artifact.get("path", "")))
        if not artifact_path.is_absolute():
            artifact_path = paths.root / artifact_path
        if not artifact_path.is_file():
            raise FileNotFoundError(f"evaluation artifact is missing: {name}")
        if (
            artifact.get("size") != artifact_path.stat().st_size
            or artifact.get("sha256") != sha256_file(artifact_path)
        ):
            raise ValueError(f"evaluation artifact changed after preflight: {name}")
    effective = record.get("effective_config", {})
    config_path = pathlib.Path(str(effective.get("path", "")))
    if not config_path.is_file():
        raise FileNotFoundError("evaluation effective config is missing")
    config_digest = effective.get("sha256")
    if config_digest != sha256_file(config_path):
        raise ValueError("evaluation effective config digest mismatch")
    if not paths.payload_config.is_file():
        raise FileNotFoundError("payload IO500 config is missing")
    if config_digest != sha256_file(paths.payload_config):
        raise ValueError("evaluation config is not the config embedded in the payload")
    if build.get("manifest") is None:
        raise ValueError("verified build manifest is unavailable")
    return record


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
    filesystem_mode: str,
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
        f"io500.filesystem_mode={filesystem_mode}'",
        timeout,
    )
    console.send(f"bootefi 90000000:{paths.linux.stat().st_size:x} ${{fdtcontroladdr}}")


def parse_trace(path: pathlib.Path) -> list[dict]:
    records = []
    with path.open("r", encoding="utf-8") as source:
        for number, line in enumerate(source, 1):
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
        if not target_epoch <= observed <= target_epoch + 5:
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
        "records": records,
    }


def launch_mpi(
    client_consoles: list[Console],
    stage: str,
    timeout: int,
    client_count: int = DEFAULT_CLIENTS,
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
    rc = wait_mpi_exit(coordinator, stage, timeout, start)
    return {
        "launcher": "MPICH Hydra manual",
        "coordinator": "client0",
        "returncode": rc,
        "proxy_commands": [by_proxy[index] for index in range(client_count)],
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
    filesystem_mode: str = "legacy-cxl-reference",
) -> list[dict]:
    records = []
    for console in consoles:
        for match in RANK_RE.finditer(console.output):
            if match.group(1) == filesystem_mode and match.group(2) == stage:
                records.append({
                    "filesystem_mode": filesystem_mode,
                    "stage": stage,
                    "rank": int(match.group(3)),
                    "size": int(match.group(4)),
                    "endpoint": int(match.group(5)),
                    "dax": match.group(6),
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


def validate_posix_summaries(
    records: list[dict], client_count: int = DEFAULT_CLIENTS
) -> None:
    active_ranks = set()
    for record in records:
        if record.get("schema_version") != "badfs.posix.path-summary.v2":
            raise ValueError("unexpected POSIX summary schema")
        if record.get("intercept_enabled") is not True:
            raise ValueError("POSIX summary does not prove syscall interception")
        if record.get("control_transport") != "cxl-dax-ring":
            raise ValueError("POSIX summary did not use the CXL DAX control transport")
        rank = record.get("mpi_rank")
        endpoint = record.get("endpoint")
        if rank not in range(client_count) or endpoint != rank:
            raise ValueError(f"POSIX summary rank/endpoint mismatch: {record}")
        stats = record.get("stats", {})
        classification = record.get("syscall_classification", {})
        totals = classification.get("totals", {})
        if totals.get("forbidden_badfs_forward") != 0:
            raise ValueError("BadFS-owned syscall escaped to the guest kernel")
        if sum(stats.get(name, 0) for name in ("open_ops", "read_ops", "write_ops")) > 0:
            if totals.get("handled", 0) <= 0:
                raise ValueError("active POSIX summary handled no BadFS syscall")
            active_ranks.add(rank)
    if active_ranks != set(range(client_count)):
        raise ValueError(
            f"active POSIX summaries do not cover ranks 0..{client_count - 1}: "
            f"{sorted(active_ranks)}"
        )


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


def inspect_servers(
    console: Console,
    server_count: int,
    *,
    require_direct_read: bool = True,
) -> dict:
    start = len(console.output)
    console.send("LEGOFS_INSPECT")
    console.wait("LEGOFS_IO500_INSPECT_EXIT index=0 rc=0", 120, start)
    marker = "badfs_lifecycle_inspection "
    records = []
    for line in console.output[start:].splitlines():
        if line.startswith(marker):
            records.append(json.loads(line[len(marker):]))
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
    for record in records:
        for name in ("pending_operations", "quarantined_extents", "active_read_leases"):
            if record.get("audit", {}).get(name, 0) != 0:
                raise ValueError(
                    f"lifecycle server {record['server']} audit reports {name}"
                )
    if server_count == 1:
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


def parse_marked_console_json(
    events: list[dict], marker: str, source: pathlib.Path
) -> tuple[list[tuple[dict, int]], list[dict]]:
    """Parse app JSON from a shared UART without inventing interleaved bytes."""
    records = []
    discarded = []
    for event_number, event in enumerate(events, 1):
        line = event["line"]
        if marker not in line:
            continue
        prefix, payload = line.split(marker, 1)
        try:
            record = json.loads(payload)
        except json.JSONDecodeError as error:
            printk = KERNEL_PRINTK_INTERLEAVE_RE.search(payload)
            split_tail = KERNEL_PRINTK_INTERRUPT_TAIL_RE.search(
                payload[error.pos:]
            )
            split_printk = (
                KERNEL_PRINTK_SPLIT_PREFIX_RE.fullmatch(prefix) is not None
                and split_tail is not None
            )
            if printk is None and not split_printk:
                raise ValueError(
                    f"invalid {marker.rstrip()} record in {source.name} "
                    f"event {event_number}: {error}"
                ) from error
            discarded.append({
                "classification": "guest-uart-kernel-printk-interleave",
                "source": source.name,
                "event_record": event_number,
                "host_capture_ns": event["host_capture_ns"],
                "json_error_offset": error.pos,
                "kernel_printk_prefix": (
                    printk.group(0)
                    if printk is not None
                    else prefix + split_tail.group(0)
                ),
            })
            continue
        records.append((record, event["host_capture_ns"]))
    return records, discarded


def strict_persistency_proof(paths: Paths, summaries: list[dict], server_count: int) -> dict:
    owner_endpoint = {
        record["owner"]: record["endpoint"]
        for record in summaries
        if isinstance(record.get("owner"), int) and isinstance(record.get("endpoint"), int)
    }
    client_events = read_event_records(paths.event_log("client0"))
    server_event_sources = []
    for server_index in range(server_count):
        source = paths.event_log(f"server{server_index}")
        server_event_sources.append((source, read_event_records(source)))
    direct_records, discarded = parse_marked_console_json(
        client_events, "BADFS_DIRECT_MAP_TRACE_JSON ", paths.event_log("client0")
    )
    direct = []
    for record, capture in direct_records:
        if (
            record.get("schema_version") == "badfs.direct-map-trace.v1"
            and record.get("event") in ("persisted", "drop", "unmap")
            and record.get("access") in ("write", "read_write")
            and record.get("rc") == 0
        ):
            direct.append((record, capture))
    lifecycle_records = []
    for source, server_events in server_event_sources:
        parsed, lifecycle_discarded = parse_marked_console_json(
            server_events, "BADFS_LIFECYCLE_TRACE_JSON ", source
        )
        lifecycle_records.extend(parsed)
        discarded.extend(lifecycle_discarded)
    lifecycle_records.sort(key=lambda item: item[1])
    lifecycle = []
    for record, capture in lifecycle_records:
        if (
            record.get("schema_version") == "badfs.lifecycle.v1"
            and record.get("event") in ("store_direct_begin", "store_direct_success")
        ):
            lifecycle.append((record, capture))
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
        "serial_trace_diagnostics": {
            "discarded_uart_interleaved_records": len(discarded),
            "discarded_records": discarded,
            "policy": (
                "discard only malformed app JSON with a complete Linux printk prefix, "
                "or a timestamp/interrupt-tail split around the app marker; never "
                "reconstruct interleaved fields"
            ),
        },
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
    invalid_ok = "[OK] But this is an invalid run!" in output and "ERROR:" not in output
    clean_ok = re.search(r"(?m)^\[OK\]\r?$", output) is not None
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


def execute(
    paths: Paths,
    timeout: int,
    server_count: int,
    client_count: int,
    filesystem_mode: str,
    evaluation_manifest: pathlib.Path | None,
    coherence_cache_bytes: int = DEFAULT_COHERENCE_CACHE_MIB * 1024**2,
    ssd_cache_mib: int = DEFAULT_SSD_CACHE_MIB,
    server_read_exclusive: bool = False,
    full_coherence_trace: bool = False,
) -> dict:
    build = verify_manifest(paths, filesystem_mode)
    if paths.stage == "hello":
        if evaluation_manifest is not None:
            raise ValueError("MPI hello must not consume an IO500 evaluation manifest")
        evaluation = None
    else:
        if evaluation_manifest is None:
            raise ValueError("IO500 stages require an external evaluation manifest")
        evaluation = validate_evaluation_manifest(
            evaluation_manifest,
            paths,
            build,
            filesystem_mode,
            server_count,
            client_count,
        )
    if filesystem_mode == "rdwo-candidate" and paths.stage != "hello":
        raise RuntimeError(
            "rdwo-candidate IO500 evidence collection is unavailable until V0.3"
        )
    prepare_paths(paths)
    owner = str(uuid.uuid4())
    host_count = client_count + server_count
    control_layout = cxl_control_layout(server_count, client_count)
    result = {
        "schema_version": "legofs.riscv.io500.v2",
        "status": "failed",
        "stage": paths.stage,
        "filesystem_mode": filesystem_mode,
        "first_failure": None,
        "functional_model_only": True,
        "physical_hardware_evidence": False,
        "functional_model_semantics_evidence": False,
        "owner_token": owner,
        "build": build,
        "evaluation_manifest": evaluation,
        "topology": {
            "physical_hosts": 1,
            "server_guests": server_count,
            "client_guests": client_count,
            "mpi_ranks": client_count,
            "filesystem_mode": filesystem_mode,
            "qemu_machine": "sifive_u",
            "type3_endpoints": host_count,
            "type3_bytes_per_endpoint": ENDPOINT_BYTES,
            "shared_cxlmemsim_region_bytes": ENDPOINT_BYTES,
            "backend": "CXLMemSim ssd-stream over QEMU file-backed persistent-memdev",
            "data_path": (
                "private 64-byte TCP MESI functional adapter gated by "
                "guest-visible CXL Type 3 HDM-DB"
            ),
            "legofs_control_path": "CXL DAX request/completion rings; no LegoFS TCP",
            "cxl_control": control_layout,
            "coherence_cache_bytes_per_endpoint": coherence_cache_bytes,
            "ssd_cache_bytes": ssd_cache_mib * 1024**2,
            "server_read_exclusive": server_read_exclusive,
            "server_scope": f"{server_count} LegoFS server allocation partitions",
            "allocator_partitions": control_layout["allocator_partitions"],
        },
        "validity": {},
        "commands": {},
        "processes": {},
        "cleanup": {},
    }
    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    tcp.bind(("127.0.0.1", 0))
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
                        filesystem_mode,
                        timeout,
                    )
                )
            for future in server_boot_futures:
                future.result()
        for server_index, console in enumerate(server_consoles):
            console.wait(
                f"LEGOFS_IO500_CXL_READY role=server index={server_index}", timeout
            )
            console.wait(f"mode={filesystem_mode} dax=", timeout)
            console.wait(
                f"LEGOFS_IO500_SERVER_READY index={server_index}", timeout
            )
            # `Console.wait` can return as soon as the index substring arrives,
            # before the rest of the serial line has been read. Wait for the
            # CXL-specific suffix as well so validation cannot race a partial
            # readiness marker.
            console.wait("transport=cxl-dax-ring control_bytes=", timeout)
            ready_marker = re.search(
                rf"LEGOFS_IO500_SERVER_READY index={server_index} [^\n]+",
                console.output,
            )
            if (
                ready_marker is None
                or f"mode={filesystem_mode}" not in ready_marker.group(0)
                or "transport=cxl-dax-ring" not in ready_marker.group(0)
            ):
                raise ValueError(
                    f"server{server_index} did not prove the CXL DAX control transport"
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
                        filesystem_mode,
                        timeout,
                    )
                )
            for future in boot_futures:
                future.result()
        for index, console in enumerate(client_consoles):
            console.wait(f"LEGOFS_IO500_CXL_READY role=client index={index}", timeout)
            console.wait(f"LEGOFS_IO500_CLIENT_READY index={index}", timeout)
            console.wait(f"mode={filesystem_mode}", timeout)

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
            result["commands"]["mpi"] = launch_mpi(
                client_consoles, paths.stage, timeout, client_count
            )
        except MpiStageError:
            diagnostics = {}
            try:
                diagnostics["rank_placement"] = parse_rank_markers(
                    client_consoles,
                    paths.stage,
                    client_count,
                    filesystem_mode,
                )
            except Exception as error:
                diagnostics["rank_placement_error"] = str(error)
            if filesystem_mode == "legacy-cxl-reference":
                try:
                    diagnostics["posix_path_summaries"] = dump_summaries(
                        client_consoles, client_count
                    )
                except Exception as error:
                    diagnostics["posix_path_summary_error"] = str(error)
                try:
                    diagnostics["lifecycle_inspection"] = inspect_servers(
                        client_consoles[0], server_count, require_direct_read=False
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
                client_consoles,
                paths.stage,
                client_count,
                filesystem_mode,
            )
            if filesystem_mode != "legacy-cxl-reference":
                raise RuntimeError("candidate evidence collector was not selected")
            summaries = dump_summaries(client_consoles, client_count)
            inspection = inspect_servers(client_consoles[0], server_count)
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
            result["functional_model_semantics_evidence"] = True
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
        validate_evidence_mode(result)
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
    parser.add_argument(
        "--filesystem-mode",
        choices=FILESYSTEM_MODES,
        default="legacy-cxl-reference",
    )
    parser.add_argument("--evaluation-manifest", type=pathlib.Path)
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
        "--local-candidate-gate",
        action="store_true",
        help=(
            "fail closed unless local timed counter windows and the implemented "
            "POSIX subset pass; never grants C3/C4 or official eligibility"
        ),
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
    if args.result_label is not None and not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]*", args.result_label
    ):
        raise ValueError("result-label contains unsupported characters")
    root = pathlib.Path(__file__).resolve().parents[1]
    paths = Paths(
        root,
        args.stage,
        args.result_label,
        filesystem_mode=args.filesystem_mode,
    )
    try:
        result = execute(
            paths,
            args.timeout,
            args.server_count,
            args.client_count,
            args.filesystem_mode,
            args.evaluation_manifest,
            args.coherence_cache_mib * 1024**2,
            args.ssd_cache_mib,
            args.server_read_exclusive,
            args.full_coherence_trace,
        )
    except BaseException as error:
        print(f"error: {error}", file=sys.stderr)
        print(f"result: {paths.result}", file=sys.stderr)
        return 1
    if args.local_candidate_gate:
        gate = evaluate_local_run(result, POSIX_CAPABILITY_MATRIX)
        result["local_candidate_gate"] = gate
        atomic_json(paths.result, result)
        if gate["decision"] != "local_functional_pass":
            print(
                "error: local candidate gate blocked; no C3/C4 or official claim was made",
                file=sys.stderr,
            )
            print(f"result: {paths.result}", file=sys.stderr)
            return 2
    print(f"LEGOFS_IO500_COMPLETE stage={args.stage} result={paths.result}")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
