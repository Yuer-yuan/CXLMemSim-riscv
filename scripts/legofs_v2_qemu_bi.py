#!/usr/bin/env python3
"""Run the real non-MPI LegoFS v2 path on QEMU CXL Type-3 guests.

The QEMU/CXLMemSim HDM-DB path is FunctionalModelOnly.  The private host-side
MESI TCP adapter is recorded as implementation evidence and is never described
as LegoFS transport or physical hardware evidence.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import os
import pathlib
import re
import signal
import socket
import statistics
import subprocess
import sys
import time
import uuid
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from legofs_type3_2node import Console, OwnedProcess, qemu_environment


DEFAULT_GUEST_DRAM_MIB = 2048
SCHEMA = "legofs.v2.qemu-hdm-db-bi.v1"
PUBLICATION_TRANSFER_MODES = ("exact-prefix", "full-slot-baseline")
DURABLE_PROGRESS_MODES = (
    "anchor",
    "anchor-eager-baseline",
    "full-root-baseline",
)
METADATA_INDEX_MODES = ("inode-ref", "redundant-file-version-baseline")
DEFAULT_WORKLOAD_ITERATION_ENVELOPE = 8
WORKLOAD_SCHEMA = "legofs.v2.io500-interface.v2"
WORKLOAD_PHASES = (
    "create",
    "create_cleanup",
    "full_create",
    "write_close",
    "lookup_stat",
    "read_verify",
    "unlink",
    "full",
)
WORKLOAD_PHASE_BYTES_PER_ITERATION = {
    "create": 0,
    "create_cleanup": 0,
    "full_create": 0,
    "write_close": 3901,
    "lookup_stat": 0,
    "read_verify": 3901,
    "unlink": 0,
    "full": 7802,
}


def bounded_start_batches(client_count: int, parallelism: int) -> list[range]:
    if client_count <= 0 or parallelism <= 0:
        raise ValueError("bounded client start geometry must be positive")
    return [
        range(begin, min(begin + parallelism, client_count))
        for begin in range(0, client_count, parallelism)
    ]


def dispatch_bounded_client_commands(
    consoles: list[Console],
    client_count: int,
    admission_only: bool,
    iterations: int,
    vd_bi_calibration: bool = False,
) -> tuple[str, list[int]]:
    if len(consoles) != client_count + 1:
        raise ValueError("client dispatch requires one server and the exact cohort")
    if admission_only and vd_bi_calibration:
        raise ValueError("admission-only and V/D calibration modes are exclusive")
    command = (
        "bounded-client-admit"
        if admission_only
        else "bounded-client-calibrate"
        if vd_bi_calibration
        else "bounded-client-start"
    )
    starts = []
    for slot in range(client_count):
        console = consoles[slot + 1]
        starts.append(len(console.output))
        suffix = f"{slot}" if admission_only else f"{slot} {iterations}"
        console.send(f"/payload/bin/legofs-v2-qemu-node {command} {suffix}")
    return command, starts


def read_unique_marker_capture_ns(path: pathlib.Path, marker: str) -> int:
    matches = []
    with path.open("r", encoding="utf-8") as source:
        for line in source:
            event = json.loads(line)
            if marker in str(event.get("line", "")):
                matches.append(int(event["host_capture_ns"]))
    if len(matches) != 1:
        raise ValueError(f"expected one host event for {marker!r}, found {len(matches)}")
    return matches[0]


def build_startup_pipeline_evidence(
    paths: "Paths", client_count: int, admission_only: bool, vd_bi_calibration: bool = False
) -> dict[str, Any]:
    server_events = paths.event_log("server")
    server_begin = read_unique_marker_capture_ns(
        server_events, "/payload/bin/legofs-v2-qemu-node server-start"
    )
    layout_ready = read_unique_marker_capture_ns(
        server_events, "LEGOFS_V2_SERVER_LAYOUT_READY"
    )
    recovering_ready = read_unique_marker_capture_ns(
        server_events, "LEGOFS_V2_SERVER_RECOVERING_READY"
    )
    admission_begin = read_unique_marker_capture_ns(
        server_events, "/payload/bin/legofs-v2-qemu-node server-admit"
    )
    serving_ready = read_unique_marker_capture_ns(
        server_events, "LEGOFS_V2_SERVER_READY provider_pid="
    )
    command = (
        "bounded-client-admit"
        if admission_only
        else "bounded-client-calibrate"
        if vd_bi_calibration
        else "bounded-client-start"
    )
    ready_marker = (
        "LEGOFS_V2_BOUNDED_CLIENT_ADMITTED"
        if admission_only
        else "LEGOFS_VD_BI_CALIBRATION_WAITING"
        if vd_bi_calibration
        else "LEGOFS_V2_BOUNDED_WORKLOAD_WAITING"
    )
    client_begin = []
    client_ready = []
    for slot in range(client_count):
        events = paths.event_log(f"client{slot}")
        client_begin.append(
            read_unique_marker_capture_ns(
                events, f"/payload/bin/legofs-v2-qemu-node {command} {slot}"
            )
        )
        client_ready.append(
            read_unique_marker_capture_ns(events, f"{ready_marker} slot={slot}")
        )

    if not (
        server_begin <= layout_ready <= recovering_ready <= admission_begin <= serving_ready
    ):
        raise ValueError("server cold-start milestones are not forward-only")
    if any(begin < layout_ready for begin in client_begin):
        raise ValueError("client provisioning started before CXL layout publication")
    if any(ready < begin for begin, ready in zip(client_begin, client_ready)):
        raise ValueError("client READY preceded its provisioning command")
    if admission_begin < max(client_ready):
        raise ValueError("exact-cohort admission began before every client READY")

    client_interval_begin = min(client_begin)
    client_interval_end = max(client_ready)
    overlap_begin = max(layout_ready, client_interval_begin)
    overlap_end = min(recovering_ready, client_interval_end)
    overlap_ns = max(0, overlap_end - overlap_begin)
    return {
        "schema": "legofs.v2.startup-pipeline.v1",
        "server_command_begin_ns": server_begin,
        "layout_ready_ns": layout_ready,
        "provider_recovering_ready_ns": recovering_ready,
        "client_command_begin_ns": client_begin,
        "client_ready_ns": client_ready,
        "admission_begin_ns": admission_begin,
        "serving_ready_ns": serving_ready,
        "layout_initialization_ns": layout_ready - server_begin,
        "provider_recovery_after_layout_ns": recovering_ready - layout_ready,
        "client_prepare_makespan_ns": client_interval_end - client_interval_begin,
        "provider_client_overlap_ns": overlap_ns,
        "final_exact_cohort_admission_ns": serving_ready - admission_begin,
        "server_start_to_serving_ns": serving_ready - server_begin,
        "client_started_before_provider_recovering_ready": all(
            begin < recovering_ready for begin in client_begin
        ),
    }


class Paths:
    def __init__(
        self,
        root: pathlib.Path,
        label: str,
        publication_transfer_mode: str = "exact-prefix",
        durable_progress_mode: str = "anchor",
        workload_iteration_envelope: int = DEFAULT_WORKLOAD_ITERATION_ENVELOPE,
        metadata_index_mode: str = "inode-ref",
        vd_bi_calibration: bool = False,
    ):
        if publication_transfer_mode not in PUBLICATION_TRANSFER_MODES:
            raise ValueError("unsupported publication transfer mode")
        if durable_progress_mode not in DURABLE_PROGRESS_MODES:
            raise ValueError("unsupported durable progress mode")
        if metadata_index_mode not in METADATA_INDEX_MODES:
            raise ValueError("unsupported metadata index mode")
        if not isinstance(workload_iteration_envelope, int) or workload_iteration_envelope <= 0:
            raise ValueError("workload iteration envelope must be a positive integer")
        self.root = root.resolve()
        self.label = label
        self.publication_transfer_mode = publication_transfer_mode
        self.durable_progress_mode = durable_progress_mode
        self.metadata_index_mode = metadata_index_mode
        self.vd_bi_calibration = vd_bi_calibration
        self.workload_iteration_envelope = workload_iteration_envelope
        publication_suffix = (
            "" if publication_transfer_mode == "exact-prefix" else "-full-slot-baseline"
        )
        durable_suffix = {
            "anchor": "",
            "anchor-eager-baseline": "-durable-anchor-eager-baseline",
            "full-root-baseline": "-durable-full-root-baseline",
        }[durable_progress_mode]
        metadata_suffix = {
            "inode-ref": "",
            "redundant-file-version-baseline": "-redundant-file-version-baseline",
        }[metadata_index_mode]
        workload_suffix = (
            ""
            if workload_iteration_envelope == DEFAULT_WORKLOAD_ITERATION_ENVELOPE
            else f"-workload-i{workload_iteration_envelope}"
        )
        calibration_suffix = "-vd-bi-calibration" if vd_bi_calibration else ""
        guest_build_suffix = (
            publication_suffix + durable_suffix + metadata_suffix + calibration_suffix
        )
        payload_build_suffix = guest_build_suffix + workload_suffix
        self.platform = self.root / "target/build/riscv-io500/platform"
        self.v2_build = (
            self.root / f"target/build/riscv-v2-qemu{payload_build_suffix}/payload"
        )
        self.v2_platform = self.root / "target/build/riscv-v2-qemu/platform"
        self.qemu = self.platform / "qemu-system-riscv64"
        self.cxlmemsim = self.platform / "cxlmemsim_server"
        self.opensbi = self.platform / "fw_dynamic.bin"
        self.uboot = self.platform / "u-boot.bin"
        self.linux = self.v2_platform / "linux-v2-Image"
        self.payload = self.v2_build / "v2-payload.ext2"
        self.fixture_layout = self.v2_build / "fixture/layout.json"
        self.platform_manifest = self.root / "target/results/legofs-io500/build-manifest.json"
        self.bundle_root = (
            self.root / f"target/build/riscv-v2-guest{guest_build_suffix}/bundle"
        )
        self.bundle_manifest = self.bundle_root / "evidence/build-manifest.json"
        self.bundle_transfer_mode = (
            self.bundle_root / "evidence/publication-transfer-mode.txt"
        )
        self.bundle_durable_progress_mode = (
            self.bundle_root / "evidence/durable-progress-mode.txt"
        )
        self.bundle_metadata_index_mode = (
            self.bundle_root / "evidence/metadata-index-mode.txt"
        )
        self.bundle_vd_bi_calibration_mode = (
            self.bundle_root / "evidence/vd-bi-calibration-mode.txt"
        )
        self.payload_manifest = self.v2_build / "evidence/payload-manifest.json"
        self.payload_transfer_mode = (
            self.v2_build / "evidence/publication-transfer-mode.txt"
        )
        self.payload_durable_progress_mode = (
            self.v2_build / "evidence/durable-progress-mode.txt"
        )
        self.payload_metadata_index_mode = (
            self.v2_build / "evidence/metadata-index-mode.txt"
        )
        self.payload_vd_bi_calibration_mode = (
            self.v2_build / "evidence/vd-bi-calibration-mode.txt"
        )
        self.payload_workload_iteration_envelope = (
            self.v2_build / "evidence/workload-iteration-envelope.txt"
        )
        self.kernel_manifest = self.v2_platform / "evidence/kernel-manifest.json"
        self.run = self.root / "target/run/legofs-v2-qemu-bi" / label
        self.output = self.root / "target/results/legofs-v2-qemu-bi" / label
        self.device_dram = self.run / "device-dram.raw"
        self.central_ssd = self.run / "central-ssd.raw"
        self.coherence = self.output / "coherence.jsonl"
        self.cxlmemsim_log = self.output / "cxlmemsim.log"
        self.result = self.output / "result.json"

    def lsa(self, host_id: int) -> pathlib.Path:
        return self.run / f"endpoint-{host_id}-lsa.raw"

    def console_log(self, role: str) -> pathlib.Path:
        return self.output / f"{role}.log"

    def event_log(self, role: str) -> pathlib.Path:
        return self.output / f"{role}-events.jsonl"


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: pathlib.Path, value: Any) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(value, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def verify_manifest_artifact(
    root: pathlib.Path,
    manifest: dict[str, Any],
    name: str,
    expected_path: pathlib.Path,
) -> dict[str, Any]:
    entry = manifest.get("artifacts", {}).get(name)
    if not isinstance(entry, dict):
        raise ValueError(f"manifest lacks artifact {name}")
    recorded = pathlib.Path(entry.get("path", ""))
    if not recorded.is_absolute():
        recorded = root / recorded
    if recorded.resolve() != expected_path.resolve():
        raise ValueError(f"manifest path changed for {name}")
    expected_digest = entry.get("sha256")
    if not isinstance(expected_digest, str) or sha256_file(expected_path) != expected_digest:
        raise ValueError(f"manifest hash changed for {name}")
    return {"path": str(expected_path), "sha256": expected_digest, "size": expected_path.stat().st_size}


def verify_publication_transfer_mode(path: pathlib.Path, expected: str) -> None:
    if expected not in PUBLICATION_TRANSFER_MODES:
        raise ValueError("unsupported expected publication transfer mode")
    if path.read_bytes() != (expected + "\n").encode("ascii"):
        raise ValueError("publication transfer mode evidence mismatch")


def verify_durable_progress_mode(path: pathlib.Path, expected: str) -> None:
    if expected not in DURABLE_PROGRESS_MODES:
        raise ValueError("unsupported expected durable progress mode")
    if path.read_bytes() != (expected + "\n").encode("ascii"):
        raise ValueError("durable progress mode evidence mismatch")


def verify_metadata_index_mode(path: pathlib.Path, expected: str) -> None:
    if expected not in METADATA_INDEX_MODES:
        raise ValueError("unsupported expected metadata index mode")
    if path.read_bytes() != (expected + "\n").encode("ascii"):
        raise ValueError("metadata index mode evidence mismatch")


def verify_vd_bi_calibration_mode(path: pathlib.Path, expected: bool) -> None:
    if path.read_bytes() != ("1\n" if expected else "0\n").encode("ascii"):
        raise ValueError("V/D BI calibration mode evidence mismatch")


def verify_workload_iteration_envelope(path: pathlib.Path, expected: int) -> None:
    if expected <= 0:
        raise ValueError("expected workload iteration envelope must be positive")
    if path.read_bytes() != (str(expected) + "\n").encode("ascii"):
        raise ValueError("workload iteration envelope evidence mismatch")


def verify_build(paths: Paths) -> dict[str, Any]:
    manifests = {}
    for name, path in {
        "platform": paths.platform_manifest,
        "v2_bundle": paths.bundle_manifest,
        "v2_payload": paths.payload_manifest,
        "v2_kernel": paths.kernel_manifest,
    }.items():
        if not path.is_file():
            raise FileNotFoundError(f"missing build manifest: {path}")
        manifests[name] = json.loads(path.read_text(encoding="utf-8"))
        if manifests[name].get("schema_version") != 2:
            raise ValueError(f"unsupported {name} manifest schema")

    platform = manifests["platform"]
    platform_artifacts = {
        name: verify_manifest_artifact(paths.root, platform, name, path)
        for name, path in {
            "qemu": paths.qemu,
            "cxlmemsim_server": paths.cxlmemsim,
            "opensbi": paths.opensbi,
            "u_boot": paths.uboot,
        }.items()
    }
    kernel_entry = verify_manifest_artifact(
        paths.root, manifests["v2_kernel"], "linux_v2", paths.linux
    )
    payload_entry = verify_manifest_artifact(
        paths.root, manifests["v2_payload"], "payload_image", paths.payload
    )
    bundle_mode_entry = verify_manifest_artifact(
        paths.root,
        manifests["v2_bundle"],
        "publication_transfer_mode",
        paths.bundle_transfer_mode,
    )
    payload_bundle_entry = verify_manifest_artifact(
        paths.root,
        manifests["v2_payload"],
        "rv64_bundle_manifest",
        paths.bundle_manifest,
    )
    payload_mode_entry = verify_manifest_artifact(
        paths.root,
        manifests["v2_payload"],
        "publication_transfer_mode",
        paths.payload_transfer_mode,
    )
    bundle_durable_progress_entry = verify_manifest_artifact(
        paths.root,
        manifests["v2_bundle"],
        "durable_progress_mode",
        paths.bundle_durable_progress_mode,
    )
    payload_durable_progress_entry = verify_manifest_artifact(
        paths.root,
        manifests["v2_payload"],
        "durable_progress_mode",
        paths.payload_durable_progress_mode,
    )
    bundle_metadata_index_entry = verify_manifest_artifact(
        paths.root,
        manifests["v2_bundle"],
        "metadata_index_mode",
        paths.bundle_metadata_index_mode,
    )
    payload_metadata_index_entry = verify_manifest_artifact(
        paths.root,
        manifests["v2_payload"],
        "metadata_index_mode",
        paths.payload_metadata_index_mode,
    )
    bundle_vd_bi_calibration_entry = verify_manifest_artifact(
        paths.root,
        manifests["v2_bundle"],
        "vd_bi_calibration_mode",
        paths.bundle_vd_bi_calibration_mode,
    )
    payload_vd_bi_calibration_entry = verify_manifest_artifact(
        paths.root,
        manifests["v2_payload"],
        "vd_bi_calibration_mode",
        paths.payload_vd_bi_calibration_mode,
    )
    payload_workload_iteration_entry = verify_manifest_artifact(
        paths.root,
        manifests["v2_payload"],
        "workload_iteration_envelope",
        paths.payload_workload_iteration_envelope,
    )
    verify_publication_transfer_mode(
        paths.bundle_transfer_mode, paths.publication_transfer_mode
    )
    verify_publication_transfer_mode(
        paths.payload_transfer_mode, paths.publication_transfer_mode
    )
    verify_durable_progress_mode(
        paths.bundle_durable_progress_mode, paths.durable_progress_mode
    )
    verify_durable_progress_mode(
        paths.payload_durable_progress_mode, paths.durable_progress_mode
    )
    verify_metadata_index_mode(
        paths.bundle_metadata_index_mode, paths.metadata_index_mode
    )
    verify_metadata_index_mode(
        paths.payload_metadata_index_mode, paths.metadata_index_mode
    )
    verify_vd_bi_calibration_mode(
        paths.bundle_vd_bi_calibration_mode, paths.vd_bi_calibration
    )
    verify_vd_bi_calibration_mode(
        paths.payload_vd_bi_calibration_mode, paths.vd_bi_calibration
    )
    verify_workload_iteration_envelope(
        paths.payload_workload_iteration_envelope,
        paths.workload_iteration_envelope,
    )
    if not paths.fixture_layout.is_file():
        raise FileNotFoundError("v2 QEMU payload lacks host-side fixture layout evidence")
    return {
        "platform_artifacts": platform_artifacts,
        "payload": payload_entry,
        "publication_transfer_mode": paths.publication_transfer_mode,
        "publication_transfer_mode_artifacts": {
            "bundle": bundle_mode_entry,
            "payload": payload_mode_entry,
            "payload_bundle_manifest": payload_bundle_entry,
        },
        "durable_progress_mode": paths.durable_progress_mode,
        "durable_progress_mode_artifacts": {
            "bundle": bundle_durable_progress_entry,
            "payload": payload_durable_progress_entry,
        },
        "metadata_index_mode": paths.metadata_index_mode,
        "metadata_index_mode_artifacts": {
            "bundle": bundle_metadata_index_entry,
            "payload": payload_metadata_index_entry,
        },
        "vd_bi_calibration": paths.vd_bi_calibration,
        "vd_bi_calibration_mode_artifacts": {
            "bundle": bundle_vd_bi_calibration_entry,
            "payload": payload_vd_bi_calibration_entry,
        },
        "workload_iteration_envelope": paths.workload_iteration_envelope,
        "workload_iteration_envelope_artifact": payload_workload_iteration_entry,
        "kernel": kernel_entry,
        "platform_manifest_sha256": sha256_file(paths.platform_manifest),
        "v2_bundle_manifest_sha256": sha256_file(paths.bundle_manifest),
        "v2_payload_manifest_sha256": sha256_file(paths.payload_manifest),
        "v2_kernel_manifest_sha256": sha256_file(paths.kernel_manifest),
        "v2_source": manifests["v2_bundle"].get("sources", {}).get("legofs"),
    }


def reserve_tcp_port() -> tuple[socket.socket, int]:
    reservation = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    reservation.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    reservation.bind(("127.0.0.1", 0))
    return reservation, reservation.getsockname()[1]


def sparse_file(path: pathlib.Path, size: int) -> None:
    with path.open("xb") as output:
        output.truncate(size)


def cxlmemsim_command(
    paths: Paths, port: int, ssd_cache_mib: int, endpoint_gib: int
) -> list[str]:
    return [
        str(paths.cxlmemsim),
        "--comm-mode=tcp",
        f"--port={port}",
        f"--capacity={endpoint_gib * 1024}",
        "--default_latency=100",
        f"--topology={paths.root / 'components/cxlmemsim/qemu_integration/topology_simple.txt'}",
        "--coherence-v2=true",
        "--coherence-v2-snoop-timeout-ms=30000",
        f"--coherence-v2-trace={paths.coherence}",
        "--backing-mode=ssd-stream",
        f"--ssd-backing-file={paths.central_ssd}",
        "--ssd-page-size=4096",
        "--ssd-io-chunk-size=65536",
        f"--ssd-cache-mb={ssd_cache_mib}",
        "--ssd-read-ahead-pages=16",
        "--ssd-io-uring=false",
        "--ssd-odirect=false",
    ]


def qemu_command(
    paths: Paths,
    role: str,
    host_id: int,
    port: int,
    cache_mib: int,
    guest_dram_mib: int,
    endpoint_gib: int,
) -> list[str]:
    machine_id = f"cxl-{role}"
    command = [
        str(paths.qemu),
        "-M", "sifive_u",
        "-cpu", "rv64,h=false,sstc=false,svadu=false,zicboz=false,zicbom=true,cbom_blocksize=64",
        "-machine", "cxl=on",
        "-machine",
        f"cxl-fmw.0.targets.0={machine_id},cxl-fmw.0.size={endpoint_gib}G,cxl-fmw.0.restrictions=0x29",
        "-smp", "5",
        "-m", f"{guest_dram_mib}M",
        "-display", "none",
        "-serial", "stdio",
        "-monitor", "none",
        "-no-reboot",
        "-bios", str(paths.opensbi),
        "-kernel", str(paths.uboot),
        "-device", f"loader,file={paths.linux},addr=0x90000000,force-raw=on",
        "-object",
        f"memory-backend-file,id=t3ssd-{role},mem-path={paths.device_dram},size={endpoint_gib}G,share=on",
        "-object",
        f"memory-backend-file,id=t3lsa-{role},mem-path={paths.lsa(host_id)},size=2M,share=on",
        "-device",
        f"pxb-cxl,bus=pcie.0,bus_nr=64,id={machine_id},hdm_for_passthrough=on",
        "-device",
        f"cxl-rp,bus={machine_id},port=0,id=rp-{role},chassis=0,slot=0,x-256b-flit=on",
        "-device",
        f"cxl-type3,bus=rp-{role},persistent-memdev=t3ssd-{role},"
        f"lsa=t3lsa-{role},id=t3-{role},coherence-v2=on,x-256b-flit=on,hdm-db=on,"
        f"cxlmemsim-addr=127.0.0.1,cxlmemsim-port={port},coherence-v2-host-id={host_id},"
        f"coherence-v2-cache-capacity={cache_mib * 1024**2},coherence-v2-cache-ways=4,"
        "coherence-v2-timeout-ms=30000,coherence-v2-write-through=off,"
        # Normal loads must enter Shared state. Forcing the server's polling
        # loads into Modified state manufactures BI traffic and turns shared
        # Gate/SQ/CQ cache lines into an unrepresentative ownership ping-pong.
        # The evidence gate below still requires naturally occurring dirty BI
        # on workload-correlated ranges.
        "coherence-v2-read-exclusive=off",
        "-drive",
        f"file={paths.payload},if=none,format=raw,readonly=on,id=payload-{role}",
        "-device",
        f"virtio-blk-pci,drive=payload-{role},bus=pcie.0,id=payload-dev-{role},romfile=",
        "-nic", "none",
    ]
    if "-netdev" in command or command[-2:] != ["-nic", "none"]:
        raise AssertionError("v2 QEMU command does not explicitly disable guest networking")
    return command


def wait_log(path: pathlib.Path, marker: str, process: subprocess.Popen[Any], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"CXLMemSim exited before readiness rc={process.returncode}")
        if path.is_file() and marker in path.read_text(encoding="utf-8", errors="replace"):
            return
        time.sleep(0.05)
    raise TimeoutError(f"timed out waiting for CXLMemSim marker {marker!r}")


def wait_console(console: Console, marker: str, timeout: float, start: int = 0) -> None:
    deadline = time.monotonic() + timeout
    with console.condition:
        while marker not in console.output[start:]:
            failure = console.output.find("LEGOFS_V2_FATAL", start)
            if failure >= 0:
                line_end = console.output.find("\n", failure)
                if line_end >= 0:
                    line = console.output[failure:line_end].rstrip("\r")
                    raise RuntimeError(
                        f"guest reported {line!r} while waiting for {marker!r}"
                    )
            if "WORKLOAD_EXIT" in marker:
                exit_prefix = (
                    "LEGOFS_V2_BOUNDED_WORKLOAD_EXIT slot="
                    if "BOUNDED_WORKLOAD_EXIT" in marker
                    else "LEGOFS_V2_WORKLOAD_EXIT rc="
                )
                workload_exit = console.output.find(exit_prefix, start)
                if workload_exit >= 0:
                    line_end = console.output.find("\n", workload_exit)
                    if line_end >= 0:
                        line = console.output[workload_exit:line_end].rstrip("\r")
                        raise RuntimeError(
                            f"guest reported {line!r} while waiting for {marker!r}"
                        )
            if console.process.poll() is not None:
                raise RuntimeError(f"QEMU exited with rc={console.process.returncode}")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"timed out waiting for guest marker {marker!r}")
            console.condition.wait(min(remaining, 0.5))


def wait_recorded_console(
    console: Console, marker: str, timeout: float, start: int = 0
) -> None:
    """Wait for console visibility and the matching flushed event record."""
    deadline = time.monotonic() + timeout
    wait_console(console, marker, timeout, start)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f"timed out closing event watermark for {marker!r}")
    console.wait_event(marker, remaining)


def boot_shell(
    console: Console, paths: Paths, timeout: float, endpoint_bytes: int
) -> None:
    decoder_size = f"{endpoint_bytes:016x}"
    host_decoder = (
        "CXL host decoder0: HPA 0000001000000000 "
        f"size {decoder_size} target 0 ctrl 00000600"
    )
    type3_decoder = (
        "41.00.0 Type 3 decoder0: HPA 0000001000000000 "
        f"size {decoder_size} target 0 ctrl 00001600"
    )
    console.wait("Hit any key to stop autoboot", timeout)
    console.send("")
    console.wait("=> ", timeout)
    if host_decoder not in console.output or type3_decoder not in console.output:
        raise ValueError("preboot CXL decoder proof is missing")
    listing = console.command_until_prompt("cxl list", timeout)
    if "41.00.0" not in listing or "Type 3" not in listing:
        raise ValueError("U-Boot did not enumerate the Type-3 device")
    information = console.command_until_prompt("cxl info 41.00.0", timeout)
    if decoder_size not in information:
        raise ValueError("U-Boot Type-3 decoder size changed")
    initialized = console.command_until_prompt("cxl init", timeout)
    if host_decoder not in initialized or type3_decoder not in initialized:
        raise ValueError("U-Boot CXL decoder initialization is incomplete")
    removed_network = console.command_until_prompt(
        "fdt rm /soc/ethernet@10090000", timeout
    )
    if "libfdt" in removed_network.lower() or "error" in removed_network.lower():
        raise ValueError("U-Boot could not remove the built-in Ethernet node")
    console.command_until_prompt(
        "setenv bootargs 'earlycon=sbi console=hvc0 loglevel=3 cxl_core.pmem_as_dax=1 "
        "initcall_blacklist=macb_driver_init,sit_init ipv6.disable=1 legofs.v2=1'",
        timeout,
    )
    console.send(f"bootefi 90000000:{paths.linux.stat().st_size:x} ${{fdtcontroladdr}}")
    wait_console(console, "LEGOFS_V2_CONTROL_READY", timeout)


def prepare_guest(
    console: Console, timeout: float, endpoint_bytes: int
) -> dict[str, Any]:
    start = len(console.output)
    console.send("/payload/bin/legofs-v2-qemu-node prepare")
    wait_console(console, "network_devices=0", timeout, start)
    output = console.output[start:]
    match = re.search(
        r"LEGOFS_V2_GUEST_READY dax=(\S+) path=(\S+) size=(\d+) "
        r"initial_align=(\d+) align=(\d+) alignment_mechanism=(\S+) "
        r"driver=(\S+) network_devices=(\d+)",
        output,
    )
    if not match:
        raise ValueError("guest readiness record is malformed")
    record = {
        "dax": match.group(1),
        "path": match.group(2),
        "size": int(match.group(3)),
        "initial_align": int(match.group(4)),
        "align": int(match.group(5)),
        "alignment_mechanism": match.group(6),
        "driver": match.group(7),
        "network_devices": int(match.group(8)),
    }
    if (
        record["size"] != endpoint_bytes
        or record["align"] != 4096
        or record["driver"] != "device_dax"
    ):
        raise ValueError("guest device-DAX identity changed")
    if record["network_devices"] != 0:
        raise ValueError("guest unexpectedly has a network device")
    return record


def extract_marked_json(output: str, begin: str, end: str) -> dict[str, Any]:
    begin_index = output.find(begin)
    end_index = output.find(end, begin_index + len(begin))
    if begin_index < 0 or end_index < 0:
        raise ValueError(f"missing marked JSON block: {begin}")
    body = output[begin_index + len(begin):end_index]
    for line in body.splitlines():
        line = line.strip().rstrip("\r")
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
    raise ValueError(f"marked block contains no JSON object: {begin}")


def _percentile_ns(values: list[int], numerator: int) -> int:
    if not values:
        raise ValueError("latency sample set is empty")
    ordered = sorted(values)
    index = ((len(ordered) - 1) * numerator + 99) // 100
    return ordered[min(index, len(ordered) - 1)]


def _derived_rates(successful: int, logical_bytes: int, elapsed_ns: int) -> dict[str, float]:
    if elapsed_ns <= 0:
        raise ValueError("workload elapsed time must be positive")
    iops = successful * 1_000_000_000.0 / elapsed_ns
    bytes_per_second = logical_bytes * 1_000_000_000.0 / elapsed_ns
    return {
        "iops": iops,
        "kiops": iops / 1000.0,
        "bandwidth_bytes_per_second": bytes_per_second,
        "bandwidth_mib_per_second": bytes_per_second / (1024.0 * 1024.0),
        "bandwidth_gib_per_second": bytes_per_second / (1024.0 * 1024.0 * 1024.0),
    }


def _require_close(actual: Any, expected: float, label: str, *, absolute: float = 1e-6) -> None:
    try:
        converted = float(actual)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} is not numeric") from error
    if not math.isfinite(converted) or not math.isclose(
        converted, expected, rel_tol=1e-8, abs_tol=absolute
    ):
        raise ValueError(f"{label}={converted!r}, independently derived {expected!r}")


def validate_workload_v2(
    workload: dict[str, Any], iterations: int, expected_slot: int
) -> dict[str, dict[str, Any]]:
    if workload.get("schema") != WORKLOAD_SCHEMA:
        raise ValueError("unexpected workload result schema")
    if workload.get("guest_client_slot") != expected_slot:
        raise ValueError("workload client slot changed")
    config = workload.get("config")
    if not isinstance(config, dict) or config != {
        "iterations": iterations,
        "payload_bytes": 3901,
        "first_write_bytes": 2048,
    }:
        raise ValueError("workload config changed")
    records = workload.get("records")
    if not isinstance(records, list):
        raise ValueError("workload records are absent")
    by_phase: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("workload phase record is not an object")
        phase = record.get("phase_name")
        if phase not in WORKLOAD_PHASES or phase in by_phase:
            raise ValueError("workload phase set is invalid or duplicated")
        if record.get("workload_name") != "v2-io500-interface":
            raise ValueError("workload phase name changed")
        attempted = record.get("attempted_operations")
        successful = record.get("successful_operations")
        failed = record.get("failed_operations")
        if (attempted, successful, failed) != (iterations, iterations, 0):
            raise ValueError(f"workload phase {phase} operation counts changed")
        logical_bytes = record.get("logical_bytes_successfully_transferred")
        expected_bytes = WORKLOAD_PHASE_BYTES_PER_ITERATION[phase] * iterations
        if logical_bytes != expected_bytes:
            raise ValueError(f"workload phase {phase} logical bytes changed")
        begin = record.get("phase_begin_ns")
        end = record.get("phase_end_ns")
        wall = record.get("wall_elapsed_ns")
        measured = record.get("measured_elapsed_ns")
        if not all(isinstance(value, int) for value in (begin, end, wall, measured)):
            raise ValueError(f"workload phase {phase} raw time is malformed")
        if begin <= 0 or end <= begin or wall != end - begin or measured != wall:
            raise ValueError(f"workload phase {phase} elapsed time is inconsistent")
        latencies = record.get("latencies_ns")
        if (
            not isinstance(latencies, list)
            or len(latencies) != iterations
            or any(not isinstance(value, int) or value <= 0 for value in latencies)
        ):
            raise ValueError(f"workload phase {phase} latency samples are malformed")
        rates = _derived_rates(successful, logical_bytes, measured)
        for name, expected in rates.items():
            _require_close(record.get(name), expected, f"{phase}.{name}")
        for name, numerator in (("p50_us", 50), ("p95_us", 95), ("p99_us", 99)):
            _require_close(
                record.get(name),
                _percentile_ns(latencies, numerator) / 1000.0,
                f"{phase}.{name}",
                absolute=0.001,
            )
        _require_close(
            record.get("max_us"),
            max(latencies) / 1000.0,
            f"{phase}.max_us",
            absolute=0.001,
        )
        if record.get("measurement_source") != "guest-clock-monotonic-raw":
            raise ValueError(f"workload phase {phase} measurement source changed")
        if record.get("tool_reported_value") is not None or record.get("tool_reported_unit") is not None:
            raise ValueError("interface workload must not invent a tool-reported value")
        by_phase[phase] = record
    if tuple(phase for phase in WORKLOAD_PHASES if phase in by_phase) != WORKLOAD_PHASES:
        raise ValueError("workload phase set is incomplete")
    return by_phase


def read_phase_timeline(
    path: pathlib.Path, slot: int, workload: dict[str, Any]
) -> dict[str, dict[str, int]]:
    phase_records = validate_workload_v2(
        workload, int(workload.get("config", {}).get("iterations", 0)), slot
    )
    observed: dict[tuple[str, str], dict[str, int]] = {}
    pattern = re.compile(
        rf"LEGOFS_V2_PHASE_(BEGIN|END) slot={slot} "
        r"phase=([a-z_]+) guest_ns=([0-9]+)"
    )
    with path.open("r", encoding="utf-8") as source:
        for number, line in enumerate(source, 1):
            event = json.loads(line)
            match = pattern.search(str(event.get("line", "")))
            if match is None:
                continue
            boundary, phase, guest_ns = match.groups()
            if phase not in WORKLOAD_PHASES:
                raise ValueError(f"unknown phase marker at event line {number}")
            key = (phase, boundary.lower())
            if key in observed:
                raise ValueError(f"duplicate phase marker for {phase}.{boundary.lower()}")
            host_ns = event.get("host_capture_ns")
            if not isinstance(host_ns, int) or host_ns <= 0:
                raise ValueError("phase marker host timestamp is malformed")
            observed[key] = {"host_ns": host_ns, "guest_ns": int(guest_ns)}
    timeline = {}
    for phase in WORKLOAD_PHASES:
        begin = observed.get((phase, "begin"))
        end = observed.get((phase, "end"))
        if begin is None or end is None:
            raise ValueError(f"phase marker pair is incomplete for {phase}")
        if begin["host_ns"] >= end["host_ns"] or begin["guest_ns"] >= end["guest_ns"]:
            raise ValueError(f"phase marker ordering is invalid for {phase}")
        record = phase_records[phase]
        if (
            begin["guest_ns"] != record["phase_begin_ns"]
            or end["guest_ns"] != record["phase_end_ns"]
        ):
            raise ValueError(f"phase marker and workload record disagree for {phase}")
        timeline[phase] = {
            "begin_ns": begin["host_ns"],
            "end_ns": end["host_ns"],
        }
    return timeline


def build_performance_records(
    run_id: str,
    workloads: list[dict[str, Any]],
    timelines: dict[int, dict[str, dict[str, int]]],
    iterations: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    client_count = len(workloads)
    if client_count == 0 or set(timelines) != set(range(client_count)):
        raise ValueError("performance timeline does not cover the exact client cohort")
    phases_by_slot = {}
    for expected_slot, workload in enumerate(workloads):
        if workload.get("client_slot") != expected_slot:
            raise ValueError("workload cohort is not canonical and dense")
        phases_by_slot[expected_slot] = validate_workload_v2(
            workload, iterations, expected_slot
        )

    output = []
    aggregate_by_phase = {}
    cross_check = {}
    for phase in WORKLOAD_PHASES:
        begins = [timelines[slot][phase]["begin_ns"] for slot in range(client_count)]
        ends = [timelines[slot][phase]["end_ns"] for slot in range(client_count)]
        phase_begin = min(begins)
        phase_end = max(ends)
        overlap_begin = max(begins)
        overlap_end = min(ends)
        overlap_valid = overlap_end > overlap_begin
        overlap_elapsed = overlap_end - overlap_begin if overlap_valid else 0
        client_records = [phases_by_slot[slot][phase] for slot in range(client_count)]
        for slot, record in enumerate(client_records):
            enriched = dict(record)
            enriched.update(
                run_id=run_id,
                scope="per-client",
                client_count=client_count,
                client_slot=slot,
                clock_domain=f"guest-slot-{slot}-clock-monotonic-raw",
                host_phase_begin_ns=timelines[slot][phase]["begin_ns"],
                host_phase_end_ns=timelines[slot][phase]["end_ns"],
                concurrent_overlap_begin_ns=overlap_begin if overlap_valid else None,
                concurrent_overlap_end_ns=overlap_end if overlap_valid else None,
                concurrent_overlap_elapsed_ns=overlap_elapsed,
            )
            output.append(enriched)
        attempted = sum(record["attempted_operations"] for record in client_records)
        successful = sum(record["successful_operations"] for record in client_records)
        failed = sum(record["failed_operations"] for record in client_records)
        logical_bytes = sum(
            record["logical_bytes_successfully_transferred"] for record in client_records
        )
        operation_kinds = {record["operation_kind"] for record in client_records}
        requested_sizes = {
            record["requested_operation_size_bytes"] for record in client_records
        }
        if len(operation_kinds) != 1 or len(requested_sizes) != 1:
            raise ValueError(f"per-client phase contract differs for {phase}")
        latencies = [
            latency
            for record in client_records
            for latency in record["latencies_ns"]
        ]
        elapsed = phase_end - phase_begin
        rates = _derived_rates(successful, logical_bytes, elapsed)
        aggregate = {
            "run_id": run_id,
            "workload_name": "v2-io500-interface",
            "phase_name": phase,
            "scope": "aggregate",
            "client_count": client_count,
            "client_slot": None,
            "operation_kind": next(iter(operation_kinds)),
            "requested_operation_size_bytes": next(iter(requested_sizes)),
            "attempted_operations": attempted,
            "successful_operations": successful,
            "failed_operations": failed,
            "logical_bytes_successfully_transferred": logical_bytes,
            "phase_begin_ns": phase_begin,
            "phase_end_ns": phase_end,
            "concurrent_overlap_begin_ns": overlap_begin if overlap_valid else None,
            "concurrent_overlap_end_ns": overlap_end if overlap_valid else None,
            "concurrent_overlap_elapsed_ns": overlap_elapsed,
            "wall_elapsed_ns": elapsed,
            "measured_elapsed_ns": elapsed,
            **rates,
            "p50_us": _percentile_ns(latencies, 50) / 1000.0,
            "p95_us": _percentile_ns(latencies, 95) / 1000.0,
            "p99_us": _percentile_ns(latencies, 99) / 1000.0,
            "max_us": max(latencies) / 1000.0,
            "tool_reported_value": None,
            "tool_reported_unit": None,
            "measurement_source": "runner-host-clock-monotonic-phase-markers",
            "clock_domain": "runner-host-clock-monotonic",
            "latencies_ns": latencies,
        }
        output.append(aggregate)
        aggregate_by_phase[phase] = aggregate
        cross_check[phase] = {
            "per_client_iops_sum": sum(record["iops"] for record in client_records),
            "per_client_bandwidth_bytes_per_second_sum": sum(
                record["bandwidth_bytes_per_second"] for record in client_records
            ),
        }
    create = aggregate_by_phase["create"]
    full = aggregate_by_phase["full"]
    write = aggregate_by_phase["write_close"]
    read = aggregate_by_phase["read_verify"]
    summary = {
        "schema": "legofs.v2.workload-performance-summary.v2",
        "metric_scope": "authoritative-global-host-makespan",
        "create_iops": create["iops"],
        "create_kiops": create["kiops"],
        "full_transaction_iops": full["iops"],
        "full_transaction_kiops": full["kiops"],
        "payload_write_iops": write["iops"],
        "payload_write_kiops": write["kiops"],
        "payload_write_bw_bytes_s": write["bandwidth_bytes_per_second"],
        "payload_write_bw_mib_s": write["bandwidth_mib_per_second"],
        "payload_write_bw_gib_s": write["bandwidth_gib_per_second"],
        "payload_read_iops": read["iops"],
        "payload_read_kiops": read["kiops"],
        "payload_read_bw_bytes_s": read["bandwidth_bytes_per_second"],
        "payload_read_bw_mib_s": read["bandwidth_mib_per_second"],
        "payload_read_bw_gib_s": read["bandwidth_gib_per_second"],
        "per_client_rate_sum_cross_check": cross_check,
    }
    return output, summary


def validate_path_evidence(record: dict[str, Any], iterations: int) -> None:
    expected = {
        "open_ops": 4 * iterations,
        "close_ops": 4 * iterations,
        "unlink_ops": 2 * iterations,
        "write_ops": 2 * iterations,
        "write_bytes": 3901 * iterations,
        "read_ops": iterations,
        "read_bytes": 3901 * iterations,
        "sync_ops": iterations,
    }
    if record.get("schema_version") != "badfs.posix.path-summary.v2":
        raise ValueError("unexpected ProcessClient evidence schema")
    if record.get("control_transport") != "cxl-dax-ring":
        raise ValueError("ProcessClient did not use CXL SQ/CQ")
    stats = record.get("stats", {})
    for name, value in expected.items():
        if stats.get(name) != value:
            raise ValueError(f"POSIX counter {name}={stats.get(name)!r}, expected {value}")
    totals = record.get("syscall_classification", {}).get("totals", {})
    if totals.get("forbidden_badfs_forward") != 0:
        raise ValueError("BadFS-owned syscall reached host-kernel fallback")
    provider = record.get("provider_v2")
    if not isinstance(provider, dict):
        raise ValueError("provider-v2 path evidence is absent")
    profile = provider.get("profile_c")
    if not isinstance(profile, dict):
        raise ValueError("Profile-C path evidence is absent")
    for name, value in {
        "direct_open_commands": iterations,
        "profile_c_open_commands": 3 * iterations,
        "profile_c_published_writes": 2 * iterations,
        "dynamic_mmap_calls": 0,
        "slow_path_handoffs_required": 0,
    }.items():
        if provider.get(name) != value:
            raise ValueError(f"provider-v2 counter {name} changed")
    for name in ("dynamic_mmap_calls", "slow_path_handoffs_required", "writer_window_exhaustions"):
        if profile.get(name) != 0:
            raise ValueError(f"Profile-C counter {name} is nonzero")
    if profile.get("mutation_plane_sqe") != profile.get("mutation_plane_cqe"):
        raise ValueError("Mutation SQE/CQE counts differ")
    if profile.get("payload_bytes_via_cxl") != profile.get("total_payload_bytes"):
        raise ValueError("some application payload bytes bypassed CXL shared memory")
    if profile.get("metadata_bytes_via_cxl") != profile.get("total_metadata_bytes"):
        raise ValueError("some filesystem metadata bytes bypassed CXL shared memory")


def validate_authority_telemetry(
    telemetry: dict[str, Any], durable_progress_mode: str, workload_ran: bool
) -> None:
    if telemetry.get("schema") != "legofs.v2.authority-telemetry.v1":
        raise ValueError("unexpected authority telemetry schema")
    if durable_progress_mode not in DURABLE_PROGRESS_MODES:
        raise ValueError("unsupported authority durable progress mode")
    fields = (
        "durable_progress_deferred_advances",
        "durable_progress_root_piggybacks",
        "durable_progress_forced_publications",
        "persistence_stable_copy_ns",
        "persistence_runtime_publication_ns",
        "persistence_runtime_root_publications",
        "persistence_runtime_publication_elisions",
    )
    for field in fields:
        value = telemetry.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"authority telemetry field {field} is not a counter")
    if not workload_ran:
        return
    if durable_progress_mode == "anchor":
        if telemetry["persistence_runtime_root_publications"] != 0:
            raise ValueError("candidate persistence ACK rewrote a RootAnchor")
        if telemetry["persistence_runtime_publication_elisions"] <= 0:
            raise ValueError("candidate workload did not elide persistence publication")
        if telemetry["durable_progress_deferred_advances"] <= 0:
            raise ValueError("candidate workload did not defer metadata D publication")
        if telemetry["durable_progress_root_piggybacks"] <= 0:
            raise ValueError("candidate workload did not piggyback deferred D on a root")
    else:
        if telemetry["persistence_runtime_publication_elisions"] != 0:
            raise ValueError("eager baseline unexpectedly elided persistence publication")
        if telemetry["persistence_runtime_root_publications"] <= 0:
            raise ValueError("eager baseline did not publish persistence runtime roots")
        if telemetry["durable_progress_deferred_advances"] != 0:
            raise ValueError("eager baseline unexpectedly deferred metadata D")


def read_trace(path: pathlib.Path) -> list[dict[str, Any]]:
    records = []
    with path.open("r", encoding="utf-8") as source:
        for number, line in enumerate(source, 1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"invalid coherence trace line {number}: {error}") from error
            if value.get("schema_version") != 1:
                raise ValueError("unsupported coherence trace schema")
            records.append(value)
    return records


def coherence_request_completion_timing(
    records: list[dict[str, Any]], start_ns: int, end_ns: int
) -> dict[str, Any]:
    """Validate and summarize model-server execution intervals.

    These intervals deliberately exclude guest SQ/CQ transport and the trace
    writer's own synchronous JSONL flush. They are FunctionalModelOnly
    calibration inputs, never physical CXL latency evidence.
    """
    event_names = {
        "GETS": "gets",
        "GETM": "getm",
        "UPGRADE": "upgrade",
        "PUTM": "putm",
    }
    requests: dict[tuple[int, int, int], dict[str, Any]] = {}
    for record in records:
        timestamp = record.get("monotonic_ns")
        opcode = record.get("opcode")
        if (
            record.get("event") != "request"
            or opcode not in event_names
            or not isinstance(timestamp, int)
            or not start_ns <= timestamp <= end_ns
        ):
            continue
        key = (record.get("src_host"), record.get("session_id"), record.get("request_id"))
        if any(isinstance(value, bool) or not isinstance(value, int) for value in key):
            raise ValueError("coherence request identity is malformed")
        if key in requests:
            raise ValueError("coherence interval contains a duplicate request identity")
        requests[key] = record

    samples = {name: [] for name in event_names.values()}
    paired: set[tuple[int, int, int]] = set()
    completion_records = 0
    for record in records:
        timestamp = record.get("monotonic_ns")
        opcode = record.get("opcode")
        if (
            record.get("event") != "request_completion"
            or opcode not in event_names
            or not isinstance(timestamp, int)
            or not start_ns <= timestamp <= end_ns
        ):
            continue
        completion_records += 1
        key = (record.get("src_host"), record.get("session_id"), record.get("request_id"))
        request = requests.get(key)
        if request is None or request.get("opcode") != opcode:
            raise ValueError("coherence completion has no matching interval request")
        if key in paired:
            raise ValueError("coherence request has duplicate completion timing")
        duration = record.get("duration_ns")
        if isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0:
            raise ValueError("coherence completion duration is not positive")
        if record.get("status") != "OK":
            raise ValueError("coherence completion timing contains a failed operation")
        if timestamp < request["monotonic_ns"]:
            raise ValueError("coherence completion precedes its request")
        paired.add(key)
        samples[event_names[opcode]].append(duration)

    if completion_records and paired != set(requests):
        raise ValueError("coherence request/completion timing is imbalanced")

    events = {}
    for name, values in samples.items():
        if not values:
            continue
        median = statistics.median(values)
        mad = statistics.median(abs(value - median) for value in values)
        events[name] = {
            "samples_ns": values,
            "sample_count": len(values),
            "lower_ns": min(values),
            "median_ns": median,
            "upper_ns": max(values),
            "rmad": 1.4826 * mad / median,
        }
    return {
        "schema": "legofs.cxlmemsim.request-completion-timing.v1",
        "evidence_class": "FunctionalModelOnly",
        "physical_hardware_evidence": False,
        "measurement_scope": "cxlmemsim-server-execution-excluding-trace-flush",
        "instrumentation_available": completion_records > 0,
        "request_count": len(requests),
        "completion_count": completion_records,
        "events": events,
    }


def build_vd_calibration_probes(
    authority_telemetry: dict[str, Any],
    client_calibrations: list[dict[str, Any]],
    coherence_timing: dict[str, Any],
) -> dict[str, Any]:
    def after_warmup(raw: list[Any], count: int) -> list[Any]:
        if len(raw) <= count:
            return []
        return raw[count:]

    authority = authority_telemetry.get("vd_calibration_samples")
    if (
        not isinstance(authority, dict)
        or authority.get("schema") != "legofs.vd-authority-calibration.v1"
    ):
        raise ValueError("calibration artifact lacks authority raw samples")
    authority_events = authority.get("events")
    if not isinstance(authority_events, dict):
        raise ValueError("authority calibration events are malformed")
    if (
        coherence_timing.get("schema")
        != "legofs.cxlmemsim.request-completion-timing.v1"
        or not coherence_timing.get("instrumentation_available")
    ):
        raise ValueError("calibration artifact lacks coherence completion instrumentation")
    coherence_events = coherence_timing.get("events")
    if not isinstance(coherence_events, dict):
        raise ValueError("coherence calibration events are malformed")
    sq_cq = []
    for record in client_calibrations:
        if record.get("schema") != "legofs.vd-client-calibration.v1":
            raise ValueError("client calibration schema changed")
        raw = record.get("events", {}).get("sq_cq", {}).get("samples_ns")
        if not isinstance(raw, list):
            raise ValueError("client calibration lacks SQ/CQ samples")
        sq_cq.extend(raw)
    events: dict[str, dict[str, Any]] = {
        "sq_cq": {
            "samples_ns": sq_cq,
            "warmup_samples_discarded": 16,
        }
    }
    for name in ("gets", "getm", "upgrade", "putm"):
        raw = coherence_events.get(name)
        if isinstance(raw, dict) and isinstance(raw.get("samples_ns"), list):
            events[name] = {
                "samples_ns": after_warmup(raw["samples_ns"], 1),
                "warmup_samples_discarded": 1,
            }
    for name in (
        "page_persist",
        "tail_persist",
        "root_publication",
        "durable_anchor_publication",
        "cow_path",
        "append_1",
        "append_2",
        "append_4",
        "append_8",
    ):
        raw = authority_events.get(name)
        if isinstance(raw, dict) and isinstance(raw.get("samples_ns"), list):
            events[name] = {
                "samples_ns": after_warmup(raw["samples_ns"], 1),
                "warmup_samples_discarded": 1,
            }
    return {
        "schema": "legofs.vd-bi-calibration-probes.v1",
        "evidence_class": "FunctionalModelOnly",
        "physical_hardware_evidence": False,
        "isolation": "compile-time-calibration-artifact",
        "events": events,
        "provenance": {
            "sq_cq": "guest accepted-SQE to consumed-CQE steady-clock interval",
            "coherence": "CXLMemSim model request execution interval",
            "authority": "authority steady-clock around real production primitives",
            "warmup": (
                "client discards 16 explicit no-op probes; runner discards the first "
                "in-interval sample for each coherence and authority primitive"
            ),
        },
    }


def coherence_evidence(
    records: list[dict[str, Any]],
    start_ns: int,
    end_ns: int,
    layout: dict[str, Any],
    host_count: int,
) -> dict[str, Any]:
    registrations = [
        record for record in records
        if record.get("event") == "registration" and record.get("status") == "OK"
    ]
    expected_hosts = set(range(host_count))
    if {record.get("src_host") for record in registrations} != expected_hosts:
        raise ValueError("coherence trace lacks distinct server/client endpoint registration")
    sessions = {record.get("session_id") for record in registrations}
    if len(sessions) != host_count or 0 in sessions:
        raise ValueError("coherence endpoint sessions are not distinct and nonzero")

    ranges = []
    if layout.get("schema") == "legofs.v2.bounded-client-functional-fixture.v1":
        items = layout.get("regions", [])
    else:
        items = [
            {"name": name, **layout[name]}
            for name in (
                "provider", "mutation", "lifecycle", "persistence_lane",
                "persistence_coverage", "provider_provisioning", "host_provisioning",
                "client_provisioning",
            )
        ]
    for item in items:
        ranges.append((item["name"], int(item["provider_offset"]), int(item["provider_offset"]) + int(item["length"])))

    def range_name(address: Any) -> str | None:
        if not isinstance(address, int):
            return None
        for name, begin, end in ranges:
            if begin <= address < end:
                return name
        return None

    acks = {record.get("snoop_id"): record for record in records if record.get("event") == "snoop_ack"}
    completions = {
        record.get("snoop_id"): record
        for record in records if record.get("event") == "dirty_completion"
    }
    proofs = []
    for send in records:
        timestamp = send.get("monotonic_ns")
        if (
            send.get("event") != "snoop_send"
            or send.get("opcode") not in {"SNP_DATA_INV", "SNP_DATA_DOWNGRADE"}
            or not isinstance(timestamp, int)
            or not start_ns <= timestamp <= end_ns
        ):
            continue
        region = range_name(send.get("line_address"))
        if region is None:
            continue
        snoop_id = send.get("snoop_id")
        ack = acks.get(snoop_id)
        if not isinstance(ack, dict):
            continue
        ack_ns = ack.get("monotonic_ns")
        if (
            ack.get("opcode") != "SNOOP_ACK"
            or ack.get("status") != "OK"
            or ack.get("src_host") != send.get("dst_host")
            or ack.get("session_id") != send.get("session_id")
            or ack.get("epoch") != send.get("epoch")
            or not isinstance(ack_ns, int)
            or ack_ns < timestamp
        ):
            continue
        proof = {"region": region, "send": send, "ack": ack, "completion": None}
        if ack.get("dirty_data") is True:
            completion = completions.get(snoop_id)
            completion_ns = completion.get("monotonic_ns") if isinstance(completion, dict) else None
            if (
                not isinstance(completion, dict)
                or completion.get("status") != "OK"
                or completion.get("src_host") != send.get("dst_host")
                or not isinstance(completion_ns, int)
                or completion_ns < ack_ns
            ):
                continue
            proof["completion"] = completion
        proofs.append(proof)
    dirty = [proof for proof in proofs if proof["completion"] is not None]
    if not proofs:
        raise ValueError(
            "workload interval contains no range-correlated dirty-data HDM-DB BI request/ACK"
        )
    if not dirty:
        raise ValueError("workload interval contains no dirty-data BI completion ordering proof")
    return {
        "registrations": registrations,
        "bi_proof_count": len(proofs),
        "dirty_bi_proof_count": len(dirty),
        "first_bi": proofs[0],
        "first_dirty_bi": dirty[0],
    }


def parse_final_stats(log: str) -> dict[str, Any]:
    records = []
    marker = "COHERENCE_V2_STATS_JSON "
    for line in log.splitlines():
        position = line.find(marker)
        if position >= 0:
            records.append(json.loads(line[position + len(marker):].strip()))
    if len(records) != 1:
        raise ValueError(f"expected one final coherence stats record, found {len(records)}")
    stats = records[0]
    for name in ("timeouts", "protocol_errors", "delivery_failures", "server_copy_failures", "active_bindings"):
        if stats.get(name) != 0:
            raise ValueError(f"coherence error counter {name} is nonzero")
    return stats


def terminate_owned(item: OwnedProcess) -> None:
    try:
        item.terminate_owned()
    except (OSError, subprocess.SubprocessError):
        pass


def execute(paths: Paths, args: argparse.Namespace) -> dict[str, Any]:
    build = verify_build(paths)
    paths.run.mkdir(parents=True, exist_ok=False)
    paths.output.mkdir(parents=True, exist_ok=False)
    layout = json.loads(paths.fixture_layout.read_text(encoding="utf-8"))
    bounded_layout = layout.get("schema") == "legofs.v2.bounded-client-functional-fixture.v1"
    if bounded_layout and int(layout.get("client_count", 0)) != args.client_count:
        raise ValueError("bounded payload client count differs from requested QEMU cohort")
    if bounded_layout and int(layout.get("workload_iteration_envelope", 0)) != paths.workload_iteration_envelope:
        raise ValueError("bounded payload workload iteration envelope changed")
    if bounded_layout and args.iterations > paths.workload_iteration_envelope:
        raise ValueError("requested iterations exceed the sealed bounded workload envelope")
    if args.admission_only and not bounded_layout:
        raise ValueError("admission-only mode requires a bounded payload profile")
    if not bounded_layout and args.client_count != 1:
        raise ValueError("multi-client workloads require a bounded payload profile")
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "failed",
        "evidence_class": "FunctionalModelOnly",
        "functional_model_only": True,
        "physical_hardware_evidence": False,
        "official_io500": False,
        "mpi_ran": False,
        "iterations": args.iterations,
        "publication_transfer_mode": paths.publication_transfer_mode,
        "durable_progress_mode": paths.durable_progress_mode,
        "metadata_index_mode": paths.metadata_index_mode,
        "vd_bi_calibration": paths.vd_bi_calibration,
        "workload_iteration_envelope": paths.workload_iteration_envelope,
        "build": build,
        "commands": {},
        "first_failure": None,
    }
    reservation, port = reserve_tcp_port()
    server_process = None
    server_owned = None
    server_log_handle = None
    consoles: list[Console | None] = []
    try:
        endpoint_bytes = args.endpoint_gib * 1024**3
        if int(layout.get("capacity_bytes", 0)) > endpoint_bytes:
            raise ValueError("bounded fixture exceeds configured CXL endpoint capacity")
        sparse_file(paths.central_ssd, endpoint_bytes)
        sparse_file(paths.device_dram, endpoint_bytes)
        host_count = 1 + args.client_count
        for host_id in range(host_count):
            sparse_file(paths.lsa(host_id), 2 * 1024**2)

        server_argv = cxlmemsim_command(
            paths, port, args.ssd_cache_mib, args.endpoint_gib
        )
        result["commands"]["cxlmemsim"] = server_argv
        server_log_handle = paths.cxlmemsim_log.open("w", encoding="utf-8")
        reservation.close()
        reservation = None
        server_process = subprocess.Popen(
            server_argv,
            cwd=paths.run,
            stdout=server_log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        server_owned = OwnedProcess(server_process, server_argv, paths.run, str(uuid.uuid4()), time.monotonic_ns())
        wait_log(paths.cxlmemsim_log, "Server listening on TCP port", server_process, args.timeout)

        environment = qemu_environment(paths)
        guest_identity = {}
        roles = ["server"] + [f"client{index}" for index in range(args.client_count)]
        commands = []
        for host_id, role in enumerate(roles):
            argv = qemu_command(
                paths,
                role,
                host_id,
                port,
                args.coherence_cache_mib,
                args.guest_dram_mib,
                args.endpoint_gib,
            )
            result["commands"][f"{role}_qemu"] = argv
            commands.append(argv)
        consoles = [None] * host_count

        def boot_and_prepare(index: int) -> tuple[str, dict[str, Any]]:
            role = roles[index]
            console = Console(
                commands[index],
                environment,
                paths.console_log(role),
                paths.event_log(role),
                paths.run,
                str(uuid.uuid4()),
            )
            consoles[index] = console
            boot_shell(console, paths, args.timeout, endpoint_bytes)
            return role, prepare_guest(console, args.timeout, endpoint_bytes)
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(host_count, args.boot_parallelism)
        ) as pool:
            futures = [pool.submit(boot_and_prepare, index) for index in range(host_count)]
            for future in futures:
                role, identity = future.result()
                guest_identity[role] = identity
        if any(console is None for console in consoles):
            raise RuntimeError("QEMU boot worker did not create every guest")
        consoles = [console for console in consoles if console is not None]
        result["guest_identity"] = guest_identity

        server_start = len(consoles[0].output)
        consoles[0].send("/payload/bin/legofs-v2-qemu-node server-start")
        if bounded_layout:
            wait_console(
                consoles[0],
                "LEGOFS_V2_SERVER_LAYOUT_READY clients=",
                args.timeout,
                server_start,
            )
            command, client_starts = dispatch_bounded_client_commands(
                consoles,
                args.client_count,
                args.admission_only,
                args.iterations,
                paths.vd_bi_calibration,
            )
            waiting_marker = (
                "LEGOFS_V2_BOUNDED_CLIENT_ADMITTED"
                if args.admission_only
                else "LEGOFS_VD_BI_CALIBRATION_WAITING"
                if paths.vd_bi_calibration
                else "LEGOFS_V2_BOUNDED_WORKLOAD_WAITING"
            )
            # Exact-cohort admission is a barrier: delaying later client commands
            # until earlier clients report READY can deadlock the first batch at
            # its fail-closed ServingGate timeout.  Dispatch every already-booted
            # guest before waiting.  The bounded pool limits host-side console
            # waiters; it must not serialize cohort membership.
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(args.client_count, args.client_start_parallelism)
            ) as pool:
                futures = [
                    pool.submit(
                        wait_recorded_console,
                        consoles[slot + 1],
                        f"{waiting_marker} slot={slot}",
                        args.timeout,
                        client_starts[slot],
                    )
                    for slot in range(args.client_count)
                ]
                for future in futures:
                    future.result()
            # The provider has recovered concurrently with disjoint
            # client/HostAgent cold preparation. Recovery completion remains
            # mandatory before Management consumes the exact READY cohort.
            wait_console(
                consoles[0],
                "LEGOFS_V2_SERVER_RECOVERING_READY provider_pid=",
                args.timeout,
                server_start,
            )
            workload_start_ns = time.monotonic_ns()
            serving_start = len(consoles[0].output)
            consoles[0].send("/payload/bin/legofs-v2-qemu-node server-admit")
            wait_recorded_console(
                consoles[0],
                f"LEGOFS_V2_SERVER_READY provider_pid=",
                args.timeout,
                serving_start,
            )
            if paths.vd_bi_calibration:
                calibration_outputs: list[str] = []
                wait_starts = []
                for slot, console in enumerate(consoles[1:]):
                    start = len(console.output)
                    console.send(
                        f"/payload/bin/legofs-v2-qemu-node bounded-client-calibration-wait {slot}"
                    )
                    wait_starts.append(start)
                with concurrent.futures.ThreadPoolExecutor(max_workers=args.client_count) as pool:
                    futures = [
                        pool.submit(
                            wait_recorded_console,
                            console,
                            f"LEGOFS_VD_BI_CALIBRATION_EXIT slot={slot} rc=0 records=1 socket_fds=0",
                            args.timeout,
                            wait_starts[slot],
                        )
                        for slot, console in enumerate(consoles[1:])
                    ]
                    for future in futures:
                        future.result()
                calibration_outputs = [
                    console.output[wait_starts[slot]:]
                    for slot, console in enumerate(consoles[1:])
                ]
            elif not args.admission_only:
                workload_outputs: list[str] = []
                wait_starts = []
                for slot, console in enumerate(consoles[1:]):
                    start = len(console.output)
                    console.send(f"/payload/bin/legofs-v2-qemu-node bounded-client-wait {slot}")
                    wait_starts.append(start)
                with concurrent.futures.ThreadPoolExecutor(max_workers=args.client_count) as pool:
                    futures = [
                        pool.submit(
                            wait_recorded_console,
                            console,
                            f"LEGOFS_V2_BOUNDED_WORKLOAD_EXIT slot={slot} rc=0 summaries=1 socket_fds=0",
                            args.timeout,
                            wait_starts[slot],
                        )
                        for slot, console in enumerate(consoles[1:])
                    ]
                    for future in futures:
                        future.result()
                workload_outputs = [
                    console.output[wait_starts[slot]:]
                    for slot, console in enumerate(consoles[1:])
                ]
            workload_end_ns = time.monotonic_ns()
            result["bounded_admission"] = {
                "client_count": args.client_count,
                "admitted_slots": list(range(args.client_count)),
                "gate_state": "SERVING",
                "socket_fds": 0,
                "tcp_fallbacks": 0,
            }
            result["startup_pipeline"] = build_startup_pipeline_evidence(
                paths,
                args.client_count,
                args.admission_only,
                paths.vd_bi_calibration,
            )
            if args.admission_only:
                result["admission_host_interval_ns"] = {
                    "begin": workload_start_ns,
                    "end": workload_end_ns,
                }
            elif paths.vd_bi_calibration:
                calibrations = []
                path_evidence = []
                for slot, output in enumerate(calibration_outputs):
                    calibration = extract_marked_json(
                        output,
                        f"LEGOFS_VD_BI_CLIENT_CALIBRATION_BEGIN slot={slot}",
                        f"LEGOFS_VD_BI_CLIENT_CALIBRATION_END slot={slot}",
                    )
                    if calibration.get("schema") != "legofs.vd-client-calibration.v1":
                        raise ValueError("unexpected guest calibration schema")
                    evidence = calibration.get("path_evidence")
                    if not isinstance(evidence, dict):
                        raise ValueError("guest calibration lacks CXL path evidence")
                    calibrations.append({"client_slot": slot, **calibration})
                    path_evidence.append({"client_slot": slot, **evidence})
                result["client_calibrations"] = calibrations
                result["path_evidence"] = path_evidence
                result["calibration_host_interval_ns"] = {
                    "begin": workload_start_ns,
                    "end": workload_end_ns,
                }
            else:
                workloads = []
                path_evidence = []
                for slot, output in enumerate(workload_outputs):
                    exit_match = re.search(
                        rf"LEGOFS_V2_BOUNDED_WORKLOAD_EXIT slot={slot} rc=(\d+) summaries=(\d+) socket_fds=(\d+)",
                        output,
                    )
                    if not exit_match or tuple(map(int, exit_match.groups())) != (0, 1, 0):
                        raise ValueError(f"bounded workload exit gate failed for slot {slot}")
                    workload = extract_marked_json(
                        output,
                        f"LEGOFS_V2_WORKLOAD_STDOUT_BEGIN slot={slot}",
                        f"LEGOFS_V2_WORKLOAD_STDOUT_END slot={slot}",
                    )
                    evidence = extract_marked_json(
                        output,
                        f"LEGOFS_V2_PATH_SUMMARY_BEGIN slot={slot}",
                        f"LEGOFS_V2_PATH_SUMMARY_END slot={slot}",
                    )
                    validate_workload_v2(workload, args.iterations, slot)
                    validate_path_evidence(evidence, args.iterations)
                    workloads.append({"client_slot": slot, **workload})
                    path_evidence.append({"client_slot": slot, **evidence})
                timelines = {
                    slot: read_phase_timeline(
                        paths.event_log(f"client{slot}"), slot, workload
                    )
                    for slot, workload in enumerate(workloads)
                }
                performance_records, performance = build_performance_records(
                    paths.label, workloads, timelines, args.iterations
                )
                workload_start_ns = min(
                    timeline["create"]["begin_ns"] for timeline in timelines.values()
                )
                workload_end_ns = max(
                    timeline["full"]["end_ns"] for timeline in timelines.values()
                )
                result["workloads"] = workloads
                result["path_evidence"] = path_evidence
                result["performance_records"] = performance_records
                result["performance"] = performance
                result["workload_host_interval_ns"] = {
                    "begin": workload_start_ns,
                    "end": workload_end_ns,
                }
            stop_start = len(consoles[0].output)
            consoles[0].send("/payload/bin/legofs-v2-qemu-node server-stop")
            wait_console(
                consoles[0],
                "LEGOFS_V2_SERVER_STOPPED provider_pid=",
                args.timeout,
                stop_start,
            )
            stop_output = consoles[0].output[stop_start:]
            authority_telemetry = extract_marked_json(
                stop_output,
                "LEGOFS_V2_AUTHORITY_TELEMETRY_BEGIN",
                "LEGOFS_V2_AUTHORITY_TELEMETRY_END",
            )
            provider_stop = extract_marked_json(
                stop_output,
                "LEGOFS_V2_PROVIDER_STOP_BEGIN",
                "LEGOFS_V2_PROVIDER_STOP_END",
            )
            validate_authority_telemetry(
                authority_telemetry,
                paths.durable_progress_mode,
                not args.admission_only,
            )
            if provider_stop.get("schema") != "legofs.v2.bounded-provider-stop.v1":
                raise ValueError("unexpected bounded provider stop schema")
            if any(
                provider_stop.get(field) != 0
                for field in (
                    "tcp_fallbacks",
                    "host_filesystem_fallbacks",
                    "dynamic_mmap_calls",
                )
            ):
                raise ValueError("bounded provider stop violated the hot-path budget")
            result["authority_telemetry"] = authority_telemetry
            result["provider_stop"] = provider_stop
        else:
            wait_console(consoles[0], "LEGOFS_V2_SERVER_READY", args.timeout, server_start)
            writer_windows = max(32, args.iterations * 4)
            if writer_windows > 512:
                raise ValueError("requested workload exceeds the finite guest writer-window gate")
            client_start = len(consoles[1].output)
            consoles[1].send(f"/payload/bin/legofs-v2-qemu-node client-start {writer_windows}")
            wait_console(consoles[1], "LEGOFS_V2_CLIENT_READY", args.timeout, client_start)

            workload_output_start = len(consoles[1].output)
            workload_start_ns = time.monotonic_ns()
            consoles[1].send(f"/payload/bin/legofs-v2-qemu-node client-run {args.iterations}")
            wait_recorded_console(
                consoles[1],
                "LEGOFS_V2_WORKLOAD_EXIT rc=0 summaries=1 socket_fds=0",
                args.timeout,
                workload_output_start,
            )
            workload_end_ns = time.monotonic_ns()
            workload_output = consoles[1].output[workload_output_start:]
            exit_match = re.search(r"LEGOFS_V2_WORKLOAD_EXIT rc=(\d+) summaries=(\d+) socket_fds=(\d+)", workload_output)
            if not exit_match:
                raise ValueError("workload exit evidence is malformed")
            if tuple(map(int, exit_match.groups())) != (0, 1, 0):
                raise ValueError(f"workload exit gate failed: {exit_match.groups()}")
            workload = extract_marked_json(
                workload_output,
                "LEGOFS_V2_WORKLOAD_STDOUT_BEGIN",
                "LEGOFS_V2_WORKLOAD_STDOUT_END",
            )
            path_evidence = extract_marked_json(
                workload_output,
                "LEGOFS_V2_PATH_SUMMARY_BEGIN",
                "LEGOFS_V2_PATH_SUMMARY_END",
            )
            validate_workload_v2(workload, args.iterations, 0)
            validate_path_evidence(path_evidence, args.iterations)
            workload_with_slot = {"client_slot": 0, **workload}
            timeline = read_phase_timeline(paths.event_log("client0"), 0, workload)
            performance_records, performance = build_performance_records(
                paths.label,
                [workload_with_slot],
                {0: timeline},
                args.iterations,
            )
            workload_start_ns = timeline["create"]["begin_ns"]
            workload_end_ns = timeline["full"]["end_ns"]
            result["workload"] = workload
            result["path_evidence"] = path_evidence
            result["performance_records"] = performance_records
            result["performance"] = performance
            result["workload_host_interval_ns"] = {
                "begin": workload_start_ns,
                "end": workload_end_ns,
            }

        for console in reversed(consoles):
            if console is not None:
                terminate_owned(console.owned)
        if server_owned is not None:
            terminate_owned(server_owned)
        if server_log_handle is not None:
            server_log_handle.flush()
            server_log_handle.close()
            server_log_handle = None
        trace = read_trace(paths.coherence)
        result["coherence"] = coherence_evidence(
            trace, workload_start_ns, workload_end_ns, layout, host_count
        )
        request_completion_timing = coherence_request_completion_timing(
            trace, workload_start_ns, workload_end_ns
        )
        result["coherence"]["request_completion_timing"] = request_completion_timing
        if paths.vd_bi_calibration:
            result["vd_calibration_probes"] = build_vd_calibration_probes(
                result.get("authority_telemetry", {}),
                result.get("client_calibrations", []),
                request_completion_timing,
            )
        cxlmemsim_output = paths.cxlmemsim_log.read_text(encoding="utf-8", errors="replace")
        result["coherence"]["final_stats"] = parse_final_stats(cxlmemsim_output)
        result["topology"] = {
            "server_guests": 1,
            "client_guests": args.client_count,
            "dram_per_guest_mib": args.guest_dram_mib,
            "guest_vcpus": 5,
            "qemu_cpu_model": (
                "rv64,h=false,sstc=false,svadu=false,zicboz=false,"
                "zicbom=true,cbom_blocksize=64"
            ),
            "host_cpu_pinning": "none",
            "boot_parallelism": args.boot_parallelism,
            "client_start_parallelism": args.client_start_parallelism,
            "client_command_dispatch": "all-before-ready-wait",
            "guest_network_devices": 0,
            "legofs_transport": "CxlShmSqCq",
            "type3_bytes_per_endpoint": endpoint_bytes,
            "coherence_cache_mib": args.coherence_cache_mib,
            "ssd_cache_mib": args.ssd_cache_mib,
            "cxl_fmw_restrictions": "0x29 (DEVMEM|PMEM|BI)",
            "hdm_db": True,
            "flit_bytes": 256,
            "read_exclusive": False,
            "qemu_cxlmemsim_adapter": "private host-side TCP MESI functional adapter",
            "adapter_is_legofs_transport": False,
            "publication_transfer_mode": paths.publication_transfer_mode,
            "durable_progress_mode": paths.durable_progress_mode,
            "metadata_index_mode": paths.metadata_index_mode,
            "vd_bi_calibration": paths.vd_bi_calibration,
            "workload_iteration_envelope": paths.workload_iteration_envelope,
        }
        result["status"] = "passed"
        atomic_json(paths.result, result)
        return result
    except BaseException as error:
        result["first_failure"] = f"{type(error).__name__}: {error}"
        if paths.output.is_dir():
            atomic_json(paths.result, result)
        raise
    finally:
        if reservation is not None:
            reservation.close()
        for console in reversed(consoles):
            if console is not None:
                terminate_owned(console.owned)
                console.close_files()
        if server_owned is not None:
            terminate_owned(server_owned)
        if server_log_handle is not None:
            server_log_handle.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--client-count", type=int, default=1)
    parser.add_argument("--admission-only", action="store_true")
    parser.add_argument("--guest-dram-mib", type=int, default=DEFAULT_GUEST_DRAM_MIB)
    parser.add_argument("--boot-parallelism", type=int, default=2)
    parser.add_argument("--client-start-parallelism", type=int, default=2)
    parser.add_argument("--endpoint-gib", type=int, default=64)
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--coherence-cache-mib", type=int, default=32)
    parser.add_argument("--ssd-cache-mib", type=int, default=512)
    parser.add_argument(
        "--publication-transfer-mode",
        choices=PUBLICATION_TRANSFER_MODES,
        default="exact-prefix",
    )
    parser.add_argument(
        "--durable-progress-mode",
        choices=DURABLE_PROGRESS_MODES,
        default="anchor",
    )
    parser.add_argument(
        "--metadata-index-mode",
        choices=METADATA_INDEX_MODES,
        default="inode-ref",
    )
    parser.add_argument("--vd-bi-calibration", action="store_true")
    parser.add_argument("--result-label")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.iterations <= 0:
        raise ValueError("iterations must be positive")
    if not 1 <= args.client_count <= 10:
        raise ValueError("client-count must be between 1 and 10")
    if args.vd_bi_calibration and args.admission_only:
        raise ValueError("V/D calibration cannot run in admission-only mode")
    if args.vd_bi_calibration and args.client_count != 1:
        raise ValueError("isolated V/D calibration requires exactly one client guest")
    if args.vd_bi_calibration and args.iterations < 3:
        raise ValueError("V/D calibration requires at least three SQ/CQ samples")
    if not 512 <= args.guest_dram_mib <= 16384:
        raise ValueError("guest-dram-mib must be between 512 and 16384")
    if not 1 <= args.boot_parallelism <= 4:
        raise ValueError("boot-parallelism must be between 1 and 4")
    if not 1 <= args.client_start_parallelism <= 10:
        raise ValueError("client-start-parallelism must be between 1 and 10")
    if args.endpoint_gib not in (1, 2, 4, 8, 16, 32, 64):
        raise ValueError("endpoint-gib must be a power of two between 1 and 64")
    if args.timeout <= 0:
        raise ValueError("timeout must be positive")
    if not 1 <= args.coherence_cache_mib <= 4095:
        raise ValueError("coherence cache must be between 1 and 4095 MiB")
    if not 1 <= args.ssd_cache_mib <= 65536:
        raise ValueError("SSD cache must be between 1 and 65536 MiB")
    label = args.result_label or time.strftime("%Y%m%dT%H%M%S") + f"-{os.getpid()}"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", label):
        raise ValueError("result label contains unsupported characters")
    root = pathlib.Path(__file__).resolve().parents[1]
    paths = Paths(
        root,
        label,
        args.publication_transfer_mode,
        args.durable_progress_mode,
        max(DEFAULT_WORKLOAD_ITERATION_ENVELOPE, args.iterations),
        args.metadata_index_mode,
        args.vd_bi_calibration,
    )
    try:
        result = execute(paths, args)
    except BaseException as error:
        print(f"error: {error}", file=sys.stderr)
        print(f"result: {paths.result}", file=sys.stderr)
        return 1
    print(f"LEGOFS_V2_QEMU_BI_COMPLETE result={paths.result}")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
