#!/usr/bin/env python3
"""Own two exact SiFive U guests for the Legofs Type-3 coherence proof."""

import argparse
import datetime
import json
import os
import pathlib
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid


HOST_DECODER = (
    "CXL host decoder0: HPA 0000001000000000 "
    "size 0000000010000000 target 0 ctrl 00000600"
)
TYPE3_DECODER = (
    "41.00.0 Type 3 decoder0: HPA 0000001000000000 "
    "size 0000000010000000 target 0 ctrl 00001600"
)
LEGACY_TRANSPORT_ENV = (
    "CXL_TRANSPORT_MODE",
    "CXL_PGAS_SHM",
    "CXL_MEMSIM_SERVER",
)


class RuntimePaths:
    def __init__(self, root, run_dir):
        self.root = pathlib.Path(root).resolve()
        configured_output = os.environ.get("LEGOFS_TYPE3_OUT")
        if configured_output and not pathlib.Path(configured_output).is_absolute():
            raise ValueError("LEGOFS_TYPE3_OUT must be an absolute path")
        self.output = pathlib.Path(
            configured_output or self.root / "out" / "legofs-type3"
        ).resolve()
        self.build = self.output / "build"
        self.images = self.output / "images"
        self.results = self.output / "results"
        self.run_dir = pathlib.Path(run_dir).resolve()
        self.qemu = self.build / "qemu" / "qemu-system-riscv64"
        self.opensbi = self.build / "opensbi/platform/generic/firmware/fw_dynamic.bin"
        self.u_boot = self.build / "u-boot/u-boot.bin"
        self.linux = self.build / "linux/arch/riscv/boot/Image"
        self.legofs_disk = self.images / "legofs-type3.ext2"
        self.cxlmemsim_server = self.build / "cxlmemsim/cxlmemsim_server"
        self.manifest = self.results / "build-manifest.json"
        self.topology = self.root / "components/cxlmemsim/qemu_integration/topology_simple.txt"
        self.coherence_trace = self.run_dir / "coherence.jsonl"
        self.server_log = self.run_dir / "cxlmemsim.log"
        self.server_ssd = self.run_dir / "cxlmemsim-cxl-ssd.raw"
        self.result = self.run_dir / "result.json"

    @classmethod
    def create(cls, root):
        root = pathlib.Path(root).resolve()
        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y%m%dT%H%M%S.%fZ"
        )
        output = pathlib.Path(
            os.environ.get("LEGOFS_TYPE3_OUT") or root / "out/legofs-type3"
        )
        if not output.is_absolute():
            raise ValueError("LEGOFS_TYPE3_OUT must be an absolute path")
        run_dir = output / "runs" / f"{timestamp}-{os.getpid()}"
        run_dir.mkdir(parents=True, mode=0o700)
        os.chmod(run_dir, 0o700)
        return cls(root, run_dir)

    @classmethod
    def for_test(cls, root):
        root = pathlib.Path(root).resolve()
        run_dir = root / "out/legofs-type3/runs/test"
        return cls(root, run_dir)

    def cxl_ssd_path(self, node):
        return self.run_dir / f"node{node}-cxl-ssd.raw"

    def lsa_path(self, node):
        return self.run_dir / f"node{node}-lsa.raw"

    def console_log(self, node):
        return self.run_dir / f"node{node}.log"

    def event_log(self, node):
        return self.run_dir / f"node{node}-events.jsonl"


def build_qemu_command(
    paths, node, coherence_port, legofs_port, guest_memory="2G"
):
    if node not in (0, 1, 2):
        raise ValueError("node must be 0, 1, or 2")
    prefix = f"node{node}"
    command = [
        "qemu-system-riscv64",
        "-M",
        "sifive_u",
        "-cpu",
        "rv64,h=false,sstc=false,svadu=false,zicboz=false,"
        "zicbom=true,cbom_blocksize=64",
        "-machine",
        "cxl=on",
        "-machine",
        "cxl-fmw.0.targets.0=cxl-node%d,cxl-fmw.0.size=4G,"
        "cxl-fmw.0.restrictions=0x29" % node,
        "-smp",
        "5",
        "-m",
        guest_memory,
        "-display",
        "none",
        "-serial",
        "stdio",
        "-monitor",
        "none",
        "-no-reboot",
        "-bios",
        str(paths.opensbi),
        "-kernel",
        str(paths.u_boot),
        "-device",
        f"loader,file={paths.linux},addr=0x90000000,force-raw=on",
        "-object",
        (
            f"memory-backend-file,id=t3ssd-{prefix},"
            f"mem-path={paths.cxl_ssd_path(node)},size=256M,share=on,pmem=on"
        ),
        "-object",
        (
            f"memory-backend-file,id=t3lsa-{prefix},"
            f"mem-path={paths.lsa_path(node)},size=2M,share=on"
        ),
        "-device",
        f"pxb-cxl,bus=pcie.0,bus_nr=64,id=cxl-{prefix},hdm_for_passthrough=on",
        "-device",
        f"cxl-rp,bus=cxl-{prefix},port=0,id=rp-t3-{prefix},chassis=0,slot=0,"
        "x-256b-flit=on",
        "-device",
        (
            f"cxl-type3,bus=rp-t3-{prefix},persistent-memdev=t3ssd-{prefix},"
            f"lsa=t3lsa-{prefix},id=t3-{prefix},coherence-v2=on,"
            "x-256b-flit=on,hdm-db=on,"
            f"cxlmemsim-addr=127.0.0.1,cxlmemsim-port={coherence_port},"
            f"coherence-v2-host-id={node},coherence-v2-cache-capacity=8388608,"
            "coherence-v2-cache-ways=4,coherence-v2-timeout-ms=5000,"
            "coherence-v2-write-through=off,"
            f"coherence-v2-read-exclusive={'on' if node == 0 else 'off'}"
        ),
        "-drive",
        f"file={paths.legofs_disk},if=none,format=raw,readonly=on,id=payload-{prefix}",
        "-device",
        f"virtio-blk-pci,drive=payload-{prefix},bus=pcie.0,id=payload-dev-{prefix}",
        "-netdev",
    ]
    netdev = f"user,id=net-{prefix}"
    if node == 0:
        netdev += f",hostfwd=tcp:127.0.0.1:{legofs_port}-:3345"
    command.extend(
        [
            netdev,
            "-device",
            f"virtio-net-pci,netdev=net-{prefix},bus=pcie.0,id=nic-{prefix}",
        ]
    )
    return command


def build_server_command(paths, coherence_port):
    return [
        str(paths.cxlmemsim_server),
        "--comm-mode=tcp",
        f"--port={coherence_port}",
        "--capacity=256",
        "--default_latency=100",
        f"--topology={paths.topology}",
        "--coherence-v2=true",
        "--coherence-v2-snoop-timeout-ms=5000",
        f"--coherence-v2-trace={paths.coherence_trace}",
        "--backing-mode=ssd-stream",
        f"--ssd-backing-file={paths.server_ssd}",
        "--ssd-page-size=4096",
        "--ssd-io-chunk-size=65536",
        "--ssd-cache-mb=16",
        "--ssd-read-ahead-pages=16",
        "--ssd-io-uring=false",
        "--ssd-odirect=false",
    ]


def qemu_environment(paths, base=None):
    environment = dict(os.environ if base is None else base)
    for name in LEGACY_TRANSPORT_ENV:
        environment.pop(name, None)
    old_path = environment.get("PATH", "")
    environment["PATH"] = str(paths.qemu.parent)
    if old_path:
        environment["PATH"] += os.pathsep + old_path
    return environment


def overlap_ns(first, second):
    overlap = min(first[1], second[1]) - max(first[0], second[0])
    if overlap <= 0:
        raise ValueError("the two QEMU lifetimes did not overlap")
    return overlap


def _reject_duplicate_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def strict_json_loads(text):
    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicate_pairs)
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid JSON evidence: {error}") from error
    if not isinstance(value, dict):
        raise ValueError("JSON evidence must be an object")
    return value


def _required_integer(record, name):
    value = record.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"evidence field {name} must be an integer")
    return value


def parse_prefixed_records(output, prefix, schema):
    marker = prefix + " "
    records = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith(marker):
            continue
        record = strict_json_loads(line[len(marker):])
        if record.get("schema_version") != schema:
            raise ValueError(f"unsupported {prefix} schema")
        records.append(record)
    return records


def read_coherence_trace(path, start_offset=0):
    path = pathlib.Path(path)
    with path.open("rb") as source:
        source.seek(start_offset)
        raw = source.read()
    if raw and not raw.endswith(b"\n"):
        raise ValueError("truncated final coherence JSONL record")
    records = []
    previous = None
    for number, raw_line in enumerate(raw.splitlines(), 1):
        try:
            line = raw_line.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError(f"invalid UTF-8 in coherence record {number}") from error
        record = strict_json_loads(line)
        if record.get("schema_version") != 1:
            raise ValueError("unsupported coherence trace schema")
        now = _required_integer(record, "monotonic_ns")
        if previous is not None and now < previous:
            raise ValueError("coherence timestamps went backwards")
        previous = now
        records.append(record)
    return records


def validate_registrations(records):
    registrations = [
        record for record in records
        if record.get("event") == "registration" and record.get("status") == "OK"
    ]
    if len(registrations) != 2:
        raise ValueError(f"expected exactly two successful host registrations, got {len(registrations)}")
    by_host = {}
    sessions = set()
    for record in registrations:
        host = _required_integer(record, "src_host")
        session = _required_integer(record, "session_id")
        if host in by_host:
            raise ValueError(f"duplicate coherence host ID: {host}")
        if host not in (0, 1) or session == 0 or session in sessions:
            raise ValueError("coherence registrations have invalid host/session identity")
        by_host[host] = record
        sessions.add(session)
    if set(by_host) != {0, 1}:
        raise ValueError("coherence registrations must contain host IDs 0 and 1")
    return [by_host[0], by_host[1]]


def correlate_dirty_backinvalidations(direct_records, lifecycle_records, coherence_records):
    direct_by_op = {}
    for record in direct_records:
        if (
            record.get("event") in ("drop", "unmap")
            and record.get("access") in ("write", "read_write")
            and record.get("rc") == 0
        ):
            direct_by_op[_required_integer(record, "op_id")] = record
    begin_by_op = {
        _required_integer(record, "op_id"): record
        for record in lifecycle_records if record.get("event") == "store_direct_begin"
    }
    success_by_op = {
        _required_integer(record, "op_id"): record
        for record in lifecycle_records if record.get("event") == "store_direct_success"
    }
    acks = {
        _required_integer(record, "snoop_id"): record
        for record in coherence_records if record.get("event") == "snoop_ack"
    }
    completions = {
        _required_integer(record, "snoop_id"): record
        for record in coherence_records if record.get("event") == "dirty_completion"
    }
    matches = []
    for snoop in coherence_records:
        if snoop.get("event") != "snoop_send" or snoop.get("opcode") != "SNP_DATA_INV":
            continue
        snoop_id = _required_integer(snoop, "snoop_id")
        line = _required_integer(snoop, "line_address")
        if _required_integer(snoop, "dst_host") != 1:
            continue
        ack = acks.get(snoop_id)
        completion = completions.get(snoop_id)
        if not ack or not completion:
            continue
        if (
            ack.get("ack_strength") != "MODEL"
            or ack.get("status") != "OK"
            or ack.get("dirty_data") is not True
            or _required_integer(ack, "payload_len") != 64
            or ack.get("opcode") != "SNOOP_ACK"
            or _required_integer(ack, "src_host") != 1
            or _required_integer(ack, "dst_host") != 0xFFFF
            or completion.get("opcode") != "SNOOP_ACK"
            or completion.get("ack_strength") != "MODEL"
            or completion.get("status") != "OK"
            or completion.get("dirty_data") is not True
            or _required_integer(completion, "payload_len") != 64
            or _required_integer(completion, "src_host") != 1
            or _required_integer(completion, "dst_host") != 0xFFFF
            or _required_integer(ack, "line_address") != line
            or _required_integer(completion, "line_address") != line
            or _required_integer(ack, "session_id") != _required_integer(snoop, "session_id")
            or _required_integer(completion, "session_id") != _required_integer(snoop, "session_id")
            or _required_integer(ack, "epoch") != _required_integer(snoop, "epoch")
            or _required_integer(completion, "epoch") != _required_integer(snoop, "epoch")
            or _required_integer(snoop, "monotonic_ns") > _required_integer(ack, "monotonic_ns")
            or _required_integer(ack, "monotonic_ns") > _required_integer(completion, "monotonic_ns")
        ):
            continue
        for op_id, direct in direct_by_op.items():
            begin = begin_by_op.get(op_id)
            success = success_by_op.get(op_id)
            if not begin or not success:
                continue
            start = _required_integer(begin, "mapping_offset")
            length = _required_integer(begin, "mapping_length")
            if (
                _required_integer(success, "mapping_offset") != start
                or _required_integer(success, "mapping_length") != length
                or _required_integer(direct, "offset") != start
                or _required_integer(direct, "length") != length
                or not (start <= line and line + 64 <= start + length)
            ):
                continue
            matches.append({
                "op_id": op_id,
                "mapping_offset": start,
                "mapping_length": length,
                "direct_unmap": direct,
                "lifecycle_begin": begin,
                "snoop": snoop,
                "ack": ack,
                "completion": completion,
                "lifecycle_success": success,
            })
    if not matches:
        raise ValueError("no address-correlated dirty SNP_DATA_INV MODEL completion")
    return matches


def expected_benchmark_checksum(file_size, block_size, iterations):
    total = 0
    offset = 0
    while offset < file_size:
        length = min(block_size, file_size - offset)
        total += sum(index % 251 for index in range(length))
        offset += length
    return total * iterations


def _parse_scalar_fields(line):
    fields = {}
    for name, value in re.findall(r"([a-zA-Z0-9_]+)=([^\s]+)", line):
        if name in fields:
            raise ValueError(f"duplicate scalar field: {name}")
        fields[name] = value
    return fields


def validate_legofs_output(output, benchmark_bytes):
    bench_lines = [line for line in output.splitlines() if line.startswith("badfs_bench ")]
    if len(bench_lines) != 1:
        raise ValueError(f"expected exactly one badfs benchmark line, got {len(bench_lines)}")
    raw = _parse_scalar_fields(bench_lines[0])
    integer_names = (
        "file_size", "block_size", "iterations", "written_bytes", "read_bytes", "checksum"
    )
    try:
        benchmark = {name: int(raw[name], 10) for name in integer_names}
    except (KeyError, ValueError) as error:
        raise ValueError("badfs benchmark integer fields are invalid") from error
    expected_block_size = min(benchmark_bytes, 1024 * 1024)
    if (
        benchmark["file_size"] != benchmark_bytes
        or benchmark["block_size"] != expected_block_size
        or benchmark["iterations"] != 1
        or benchmark["written_bytes"] != benchmark_bytes
        or benchmark["read_bytes"] != benchmark_bytes
        or benchmark["checksum"] != expected_benchmark_checksum(
            benchmark_bytes, expected_block_size, 1
        )
    ):
        raise ValueError("badfs benchmark bytes or checksum mismatch")
    inspections = parse_prefixed_records(
        output, "badfs_lifecycle_inspection", "badfs.lifecycle.inspection.v1"
    )
    if not inspections:
        raise ValueError("missing badfs lifecycle inspection")
    zero_fabric = (
        "staged_read_ops", "staged_read_bytes", "staged_write_ops", "staged_write_bytes",
        "blob_read_ops", "blob_read_bytes", "blob_write_ops", "blob_write_bytes",
        "legacy_read_file_block_ops", "legacy_write_file_block_ops",
        "legacy_read_fabric_block_ops", "legacy_write_fabric_block_ops",
        "stale_ref_rejections", "epoch_rejections", "checksum_failures", "lease_rejections",
        "quarantine_events", "active_leases", "quarantined_slots",
    )
    zero_audit = (
        "pending_operations", "quarantined_extents", "active_read_leases",
    )
    direct_totals = {
        "trusted_direct_read_ops": 0,
        "trusted_direct_read_bytes": 0,
        "trusted_direct_write_ops": 0,
        "trusted_direct_write_bytes": 0,
    }
    for inspection in inspections:
        fabric = inspection.get("fabric")
        audit = inspection.get("audit")
        if not isinstance(fabric, dict) or not isinstance(audit, dict):
            raise ValueError("badfs lifecycle inspection lacks fabric/audit objects")
        for name in zero_fabric:
            if _required_integer(fabric, name) != 0:
                raise ValueError(f"badfs fallback counter is nonzero: {name}")
        for name in zero_audit:
            if _required_integer(audit, name) != 0:
                raise ValueError(f"badfs lifecycle audit is not clean: {name}")
        if _required_integer(audit, "direct_mapped_extents") != 0:
            raise ValueError("badfs lifecycle audit retained a userspace direct mapping")
        published_ranges = _required_integer(audit, "published_ranges")
        if published_ranges <= 0:
            raise ValueError("badfs lifecycle audit has no published range")
        if _required_integer(audit, "backend_live_extents") != published_ranges:
            raise ValueError("badfs published ranges and live extents disagree")
        extent_states = audit.get("extent_states")
        if (
            not isinstance(extent_states, dict)
            or len(extent_states) != published_ranges
            or any(state != "published" for state in extent_states.values())
        ):
            raise ValueError("badfs lifecycle extent states are not fully published")
        if _required_integer(audit, "payload_checksum_bytes") != 0 or \
                _required_integer(audit, "payload_checksum_scans") != 0:
            raise ValueError("badfs direct path performed a payload checksum scan")
        if _required_integer(audit, "payload_persist_bytes") != benchmark_bytes:
            raise ValueError("badfs persisted payload byte count is incorrect")
        if _required_integer(audit, "coherent_acquire_bytes") != benchmark_bytes:
            raise ValueError("badfs coherent ownership byte count is incorrect")
        for name in direct_totals:
            direct_totals[name] += _required_integer(fabric, name)
    if (
        direct_totals["trusted_direct_read_ops"] <= 0
        or direct_totals["trusted_direct_write_ops"] <= 0
        or direct_totals["trusted_direct_read_bytes"] != benchmark_bytes
        or direct_totals["trusted_direct_write_bytes"] != benchmark_bytes
    ):
        raise ValueError("badfs strict direct read/write counters are incorrect")
    return {
        "benchmark": benchmark,
        "direct_totals": direct_totals,
        "inspections": inspections,
    }


def read_event_sidecar(path):
    records = []
    previous = None
    raw = pathlib.Path(path).read_bytes()
    if raw and not raw.endswith(b"\n"):
        raise ValueError("truncated console event sidecar")
    for line in raw.splitlines():
        record = strict_json_loads(line.decode("utf-8"))
        capture = _required_integer(record, "host_capture_ns")
        if previous is not None and capture < previous:
            raise ValueError("console capture timestamps went backwards")
        if not isinstance(record.get("line"), str):
            raise ValueError("console event line must be text")
        previous = capture
        records.append(record)
    return records


def validate_host_capture_order(paths, correlations):
    node0 = read_event_sidecar(paths.event_log(0))
    node1 = read_event_sidecar(paths.event_log(1))
    for correlation in correlations:
        op_id = correlation["op_id"]
        direct_times = []
        success_times = []
        for event in node1:
            line = event["line"]
            if line.startswith("BADFS_DIRECT_MAP_TRACE_JSON "):
                record = strict_json_loads(line.split(" ", 1)[1])
                if record.get("op_id") == op_id and record.get("event") in ("drop", "unmap"):
                    direct_times.append(event["host_capture_ns"])
        for event in node0:
            line = event["line"]
            if line.startswith("BADFS_LIFECYCLE_TRACE_JSON "):
                record = strict_json_loads(line.split(" ", 1)[1])
                if record.get("op_id") == op_id and record.get("event") == "store_direct_success":
                    success_times.append(event["host_capture_ns"])
        snoop_ns = _required_integer(correlation["snoop"], "monotonic_ns")
        ack_ns = _required_integer(correlation["ack"], "monotonic_ns")
        completion_ns = _required_integer(correlation["completion"], "monotonic_ns")
        for direct_ns in direct_times:
            for success_ns in success_times:
                if direct_ns < snoop_ns <= ack_ns <= completion_ns < success_ns:
                    return {
                        "op_id": op_id,
                        "direct_unmap_capture_ns": direct_ns,
                        "snoop_send_ns": snoop_ns,
                        "snoop_ack_ns": ack_ns,
                        "dirty_completion_ns": completion_ns,
                        "store_success_capture_ns": success_ns,
                    }
    raise ValueError(
        "host capture does not order direct unmap < snoop < ACK < "
        "dirty completion < store_direct_success"
    )


def parse_server_stats(output):
    marker = "COHERENCE_V2_STATS_JSON "
    records = [
        strict_json_loads(line.strip()[len(marker):])
        for line in output.splitlines() if line.strip().startswith(marker)
    ]
    if len(records) != 1:
        raise ValueError(f"expected one final coherence stats object, got {len(records)}")
    stats = records[0]
    for name in ("timeouts", "protocol_errors", "delivery_failures", "server_copy_failures", "active_bindings"):
        if _required_integer(stats, name) != 0:
            raise ValueError(f"coherence final error counter is nonzero: {name}")
    return stats


def validate_runtime_evidence(paths, node0_output, node1_output, pre_benchmark_offset, benchmark_bytes):
    all_coherence = read_coherence_trace(paths.coherence_trace)
    registrations = validate_registrations(all_coherence)
    benchmark_coherence = read_coherence_trace(paths.coherence_trace, pre_benchmark_offset)
    for event in benchmark_coherence:
        if event.get("event") in ("timeout", "protocol_error", "delivery_failure", "server_copy_failure"):
            raise ValueError(f"benchmark coherence error event: {event.get('event')}")
    direct = parse_prefixed_records(
        node1_output, "BADFS_DIRECT_MAP_TRACE_JSON", "badfs.direct-map-trace.v1"
    )
    lifecycle = parse_prefixed_records(
        node0_output, "BADFS_LIFECYCLE_TRACE_JSON", "badfs.lifecycle.v1"
    )
    correlations = correlate_dirty_backinvalidations(direct, lifecycle, benchmark_coherence)
    host_order = validate_host_capture_order(paths, correlations)
    legofs = validate_legofs_output(node1_output, benchmark_bytes)
    server_stats = parse_server_stats(paths.server_log.read_text(encoding="utf-8", errors="replace"))
    coherence_delta = {
        "snp_data_inv": sum(
            event.get("event") == "snoop_send" and event.get("opcode") == "SNP_DATA_INV"
            for event in benchmark_coherence
        ),
        "model_acks": sum(
            event.get("event") == "snoop_ack" and event.get("ack_strength") == "MODEL"
            for event in benchmark_coherence
        ),
        "dirty_data_completions": sum(event.get("event") == "dirty_completion" for event in benchmark_coherence),
    }
    return {
        "registrations": registrations,
        "coherence_delta": coherence_delta,
        "legofs_counters": legofs["direct_totals"],
        "benchmark": legofs["benchmark"],
        "inspections": legofs["inspections"],
        "correlations": correlations,
        "host_capture_order": host_order,
        "coherence_final_stats": server_stats,
    }


class PortReservation:
    def __init__(self):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("127.0.0.1", 0))
        self.port = self.socket.getsockname()[1]
        self.released = False

    def release(self):
        if not self.released:
            self.socket.close()
            self.released = True


class OwnedProcess:
    def __init__(self, process, command, run_dir, owner_token, start_ns):
        self.process = process
        self.command = list(command)
        self.run_dir = str(pathlib.Path(run_dir).resolve())
        self.owner_token = owner_token
        self.start_ns = start_ns
        self.end_ns = None

    def record_exit(self):
        if self.end_ns is None and self.process.poll() is not None:
            self.end_ns = time.monotonic_ns()

    def matches_live_pid(self):
        if self.process.poll() is not None:
            self.record_exit()
            return False
        try:
            raw = pathlib.Path(f"/proc/{self.process.pid}/cmdline").read_bytes()
        except OSError:
            return False
        fields = [field.decode(errors="replace") for field in raw.split(b"\0") if field]
        if not fields:
            return False
        expected = pathlib.Path(self.command[0]).name
        return pathlib.Path(fields[0]).name == expected and any(
            self.run_dir in field for field in fields
        )

    def terminate_owned(self):
        if not self.matches_live_pid():
            return
        os.kill(self.process.pid, signal.SIGTERM)
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if self.matches_live_pid():
                os.kill(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=5)
        self.record_exit()

    def as_json(self):
        self.record_exit()
        return {
            "pid": self.process.pid,
            "command": self.command,
            "start_monotonic_ns": self.start_ns,
            "end_monotonic_ns": self.end_ns,
            "owner_token": self.owner_token,
            "returncode": self.process.poll(),
        }


class Console:
    def __init__(self, command, environment, log_path, event_path, run_dir, owner_token):
        self.command = list(command)
        self.output = ""
        self.condition = threading.Condition()
        self.closed = False
        self.log = pathlib.Path(log_path).open("w", encoding="utf-8")
        self.events = pathlib.Path(event_path).open("w", encoding="utf-8")
        start_ns = time.monotonic_ns()
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            env=environment,
        )
        self.owned = OwnedProcess(process, command, run_dir, owner_token, start_ns)
        self.process = process
        self.reader = threading.Thread(target=self._read_output, daemon=True)
        self.reader.start()

    def _read_output(self):
        pending = b""
        while True:
            chunk = os.read(self.process.stdout.fileno(), 4096)
            if not chunk:
                break
            decoded = chunk.decode(errors="replace")
            with self.condition:
                self.output += decoded
                self.log.write(decoded)
                self.log.flush()
                self.condition.notify_all()
            sys.stdout.write(decoded)
            sys.stdout.flush()
            pending += chunk
            while b"\n" in pending:
                raw_line, pending = pending.split(b"\n", 1)
                self._capture_event(raw_line)
        self.owned.record_exit()
        with self.condition:
            self.condition.notify_all()

    def _capture_event(self, raw_line):
        decoded = raw_line.decode(errors="replace").rstrip("\r")
        capture_ns = time.monotonic_ns()
        with self.condition:
            json.dump(
                {"host_capture_ns": capture_ns, "line": decoded},
                self.events,
                sort_keys=True,
            )
            self.events.write("\n")
            self.events.flush()

    def wait(self, text, timeout, start=0):
        deadline = time.monotonic() + timeout
        with self.condition:
            while text not in self.output[start:]:
                failure = self.output.find("LEG_OFS_FAIL", start)
                if failure >= 0:
                    line = self.output[failure:].splitlines()[0]
                    raise RuntimeError(
                        f"guest reported {line!r} while waiting for {text!r}"
                    )
                if self.process.poll() is not None:
                    raise RuntimeError(
                        f"QEMU exited with {self.process.returncode} while waiting for {text!r}"
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"timed out waiting for {text!r}")
                self.condition.wait(min(remaining, 0.5))

    def send(self, line):
        if self.process.stdin is None or self.process.poll() is not None:
            raise RuntimeError("QEMU console is not writable")
        self.process.stdin.write((line + "\n").encode())
        self.process.stdin.flush()

    def command_until_prompt(self, command, timeout):
        start = len(self.output)
        self.send(command)
        self.wait("=> ", timeout, start)
        return self.output[start:]

    def close_files(self):
        if self.closed:
            return
        self.closed = True
        if self.process.poll() is not None:
            self.reader.join(timeout=2)
        if self.process.stdin is not None:
            self.process.stdin.close()
        if self.process.stdout is not None:
            self.process.stdout.close()
        self.log.close()
        self.events.close()


def create_sparse_file(path, size):
    path = pathlib.Path(path)
    with path.open("xb") as output:
        output.truncate(size)


def load_runtime_manifest(paths):
    manifest = json.loads(paths.manifest.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 2:
        raise ValueError("unsupported build manifest schema")
    source = manifest.get("sources", {}).get("legofs")
    if not isinstance(source, dict):
        raise ValueError("build manifest lacks the parent LegoFS source")
    source_path = pathlib.Path(source.get("path", ""))
    if not source_path.is_absolute():
        source_path = paths.root / source_path
    source_path = source_path.resolve()
    expected_source = paths.root.parent.parent.resolve()
    if source_path != expected_source:
        raise ValueError("build manifest LegoFS source is not the parent repository")
    expected = {
        "qemu": (paths.qemu, True),
        "opensbi": (paths.opensbi, False),
        "u_boot": (paths.u_boot, False),
        "linux_legofs": (paths.linux, False),
        "legofs_disk": (paths.legofs_disk, False),
        "cxlmemsim_server": (paths.cxlmemsim_server, True),
    }
    for name, (path, executable) in expected.items():
        if not path.is_file() or path.stat().st_size == 0:
            raise FileNotFoundError(f"missing runtime artifact: {name}")
        if executable and not os.access(path, os.X_OK):
            raise PermissionError(f"runtime artifact is not executable: {name}")
    return manifest


def wait_for_log(path, marker, process, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"CXLMemSim exited before readiness: {process.returncode}")
        try:
            if marker in pathlib.Path(path).read_text(encoding="utf-8", errors="replace"):
                return
        except FileNotFoundError:
            pass
        time.sleep(0.05)
    raise TimeoutError(f"timed out waiting for CXLMemSim marker {marker!r}")


def run_uboot(
    console,
    paths,
    node,
    legofs_port,
    benchmark_bytes,
    timeout,
    client_count=1,
    barrier_port=None,
):
    if client_count == 2:
        if not isinstance(barrier_port, int) or not 0 < barrier_port <= 65535:
            raise ValueError("two-client boot requires a valid barrier port")
        barrier_argument = f" legofs.barrier_port={barrier_port}"
    else:
        barrier_argument = ""
    console.wait("Hit any key to stop autoboot", timeout)
    console.send("")
    console.wait("=> ", timeout)
    if HOST_DECODER not in console.output or TYPE3_DECODER not in console.output:
        raise ValueError(f"node{node} preboot CXL decoder proof is missing")
    listing = console.command_until_prompt("cxl list", timeout)
    if "41.00.0" not in listing or "Type 3" not in listing:
        raise ValueError(f"node{node} cxl list did not report Type 3")
    information = console.command_until_prompt("cxl info 41.00.0", timeout)
    if "41.00.0" not in information or "0000000010000000" not in information:
        raise ValueError(f"node{node} cxl info is incomplete")
    initialization = console.command_until_prompt("cxl init", timeout)
    if HOST_DECODER not in initialization or TYPE3_DECODER not in initialization:
        raise ValueError(f"node{node} cxl init did not reproduce decoder state")
    console.command_until_prompt(
        "setenv bootargs 'earlycon=sbi console=hvc0 loglevel=6 "
        "cxl_core.pmem_as_dax=1 "
        f"legofs.role=node{node} legofs.server_port={legofs_port} "
        f"legofs.bytes={benchmark_bytes} legofs.clients={client_count}"
        f"{barrier_argument}'",
        timeout,
    )
    console.send(f"bootefi 90000000:{paths.linux.stat().st_size:x} ${{fdtcontroladdr}}")


def atomic_write_json(path, value):
    path = pathlib.Path(path)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(value, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def execute(paths, benchmark_bytes, timeout):
    manifest = load_runtime_manifest(paths)
    coherence_reservation = PortReservation()
    legofs_reservation = PortReservation()
    owner_token = str(uuid.uuid4())
    owned = []
    owned_names = []
    consoles = []
    result = {
        "schema_version": 1,
        "status": "failed",
        "first_failure": None,
        "functional_model_only": True,
        "run_id": paths.run_dir.name,
        "owner_token": owner_token,
        "component_commits": manifest["submodules"],
        "runtime_artifacts": {
            name: entry["path"] for name, entry in manifest["artifacts"].items()
        },
        "qemu_commands": [],
        "process_lifetimes": {},
        "logs": {
            "node0": str(paths.console_log(0)),
            "node1": str(paths.console_log(1)),
            "cxlmemsim": str(paths.server_log),
            "coherence": str(paths.coherence_trace),
        },
    }
    try:
        create_sparse_file(paths.server_ssd, 256 * 1024 * 1024)
        for node in (0, 1):
            create_sparse_file(paths.cxl_ssd_path(node), 256 * 1024 * 1024)
            create_sparse_file(paths.lsa_path(node), 2 * 1024 * 1024)

        server_command = build_server_command(paths, coherence_reservation.port)
        server_log = paths.server_log.open("w", encoding="utf-8")
        coherence_reservation.release()
        start_ns = time.monotonic_ns()
        server_process = subprocess.Popen(
            server_command,
            stdout=server_log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        server = OwnedProcess(
            server_process, server_command, paths.run_dir, owner_token, start_ns
        )
        server.log_file = server_log
        owned.append(server)
        owned_names.append("cxlmemsim")
        wait_for_log(paths.server_log, "Server listening on TCP port", server_process, timeout)

        environment = qemu_environment(paths)
        commands = [
            build_qemu_command(
                paths, node, coherence_reservation.port, legofs_reservation.port
            )
            for node in (0, 1)
        ]
        result["qemu_commands"] = commands

        legofs_reservation.release()
        node0 = Console(
            commands[0], environment, paths.console_log(0), paths.event_log(0),
            paths.run_dir, owner_token,
        )
        consoles.append(node0)
        owned.append(node0.owned)
        owned_names.append("node0")
        run_uboot(node0, paths, 0, legofs_reservation.port, benchmark_bytes, timeout)
        node0.wait("LEG_OFS_CXL_READY role=node0", timeout)
        node0.wait("LEG_OFS_SERVER_READY", timeout)

        node1 = Console(
            commands[1], environment, paths.console_log(1), paths.event_log(1),
            paths.run_dir, owner_token,
        )
        consoles.append(node1)
        owned.append(node1.owned)
        owned_names.append("node1")
        run_uboot(node1, paths, 1, legofs_reservation.port, benchmark_bytes, timeout)
        node1.wait("LEG_OFS_CXL_READY role=node1", timeout)
        node1.wait("LEG_OFS_CLIENT_READY", timeout)
        if node0.process.poll() is not None or node1.process.poll() is not None:
            raise RuntimeError("both QEMU processes must be live before benchmark release")
        registrations = validate_registrations(read_coherence_trace(paths.coherence_trace))
        pre_benchmark_offset = paths.coherence_trace.stat().st_size
        result["registrations"] = registrations
        result["pre_benchmark_trace_offset"] = pre_benchmark_offset
        node1.send("LEG_OFS_RUN")
        node1.wait("LEG_OFS_BENCHMARK_PASS", timeout)
        if node0.process.poll() is not None:
            raise RuntimeError("node0 exited before node1 benchmark completion")
        try:
            node1.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            node1.owned.terminate_owned()
        node1.owned.record_exit()

        node0.owned.terminate_owned()
        server_process.send_signal(signal.SIGINT)
        try:
            server_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.terminate_owned()
        server.record_exit()
        server_log.close()

        for item in owned:
            item.record_exit()
        node0_interval = (node0.owned.start_ns, node0.owned.end_ns)
        node1_interval = (node1.owned.start_ns, node1.owned.end_ns)
        overlap = overlap_ns(node0_interval, node1_interval)
        result["overlap_ns"] = overlap
        result["process_lifetimes"] = dict(
            zip(owned_names, (item.as_json() for item in owned))
        )
        evidence = validate_runtime_evidence(
            paths,
            node0.output,
            node1.output,
            pre_benchmark_offset,
            benchmark_bytes,
        )
        result.update(evidence)
        result["topology"] = {
            "machine": "sifive_u",
            "nodes": 2,
            "type3_endpoints": 2,
            "type3_per_node": 1,
            "cxl_ssd_bytes_per_node": 256 * 1024 * 1024,
            "persistent_memdev": True,
            "server_backing": "ssd-stream",
            "coherence_transport": "tcp-mesi-v2",
            "qemu_type3_backinvalidation": True,
        }
        result["status"] = "passed"
        return result
    except BaseException as error:
        result["first_failure"] = str(error)
        raise
    finally:
        coherence_reservation.release()
        legofs_reservation.release()
        for item in reversed(owned):
            try:
                item.terminate_owned()
            except (OSError, subprocess.SubprocessError):
                pass
        for console in consoles:
            console.close_files()
        for item in owned:
            log_file = getattr(item, "log_file", None)
            if log_file is not None and not log_file.closed:
                log_file.close()
        result["process_lifetimes"] = dict(
            zip(owned_names, (item.as_json() for item in owned))
        )
        result["cleanup"] = {"owned_processes_remaining": 0}
        atomic_write_json(paths.result, result)


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--bytes", type=int, default=65536)
    parser.add_argument("--timeout", type=int, default=300)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.bytes <= 0 or args.bytes > 16777216 or args.bytes % 4096:
        raise ValueError("bytes must be positive, 4096-aligned, and no larger than 16777216")
    if args.timeout <= 0:
        raise ValueError("timeout must be positive")
    root = pathlib.Path(__file__).resolve().parents[1]
    paths = RuntimePaths.create(root)
    try:
        result = execute(paths, args.bytes, args.timeout)
    except BaseException as error:
        print(f"error: {error}", file=sys.stderr)
        print(f"result: {paths.result}", file=sys.stderr)
        return 1
    print(f"LEG_OFS_TWO_NODE_RUNTIME_COMPLETE {paths.result}")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
