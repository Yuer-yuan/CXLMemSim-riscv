#!/usr/bin/env python3
"""Two-guest application-visible CXL BI coherence experiment."""

from __future__ import annotations

import argparse
import datetime
import json
import math
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
from collections import Counter
from dataclasses import dataclass
from typing import Iterable


RECORD_PREFIX = "CXLBI_JSON "
TRACE_SCHEMA_VERSION = 1
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


@dataclass(frozen=True)
class RunConfig:
    iterations: int = 256
    stream_bytes: int = 1024 * 1024
    timeout_seconds: int = 300
    link_gbps: float = 32.0
    media_ns: float = 100.0
    request_ns: float = 150.0
    bi_ns: float = 200.0
    guest_memory: str = "1G"


class RuntimePaths:
    """All generated state is scoped to one unique experiment directory."""

    def __init__(self, root: pathlib.Path, run_dir: pathlib.Path):
        self.root = pathlib.Path(root).resolve()
        configured_output = os.environ.get("CXL_BI_APP_OUT")
        if configured_output and not pathlib.Path(configured_output).is_absolute():
            raise ValueError("CXL_BI_APP_OUT must be an absolute path")
        self.output = pathlib.Path(
            configured_output or self.root / "out" / "cxl-bi-app"
        ).resolve()
        configured_legacy = os.environ.get("LEGOFS_TYPE3_OUT")
        if configured_legacy and not pathlib.Path(configured_legacy).is_absolute():
            raise ValueError("LEGOFS_TYPE3_OUT must be an absolute path")
        self.legacy_output = pathlib.Path(
            configured_legacy or self.root / "out" / "legofs-type3"
        ).resolve()
        self.legacy_build = self.legacy_output / "build"
        self.images = self.output / "images"
        self.results = self.output / "results"
        self.run_dir = pathlib.Path(run_dir).resolve()
        self.qemu = self.legacy_build / "qemu" / "qemu-system-riscv64"
        self.cxlmemsim_server = (
            self.legacy_build / "cxlmemsim" / "cxlmemsim_server"
        )
        self.opensbi = (
            self.legacy_build
            / "opensbi"
            / "platform"
            / "generic"
            / "firmware"
            / "fw_dynamic.bin"
        )
        self.u_boot = self.legacy_build / "u-boot" / "u-boot.bin"
        self.linux = self.images / "Image"
        self.manifest = self.results / "build-manifest.json"
        self.topology = (
            self.root
            / "components"
            / "cxlmemsim"
            / "qemu_integration"
            / "topology_simple.txt"
        )
        self.coherence_trace = self.run_dir / "coherence.jsonl"
        self.server_log = self.run_dir / "cxlmemsim.log"
        self.server_ssd = self.run_dir / "cxlmemsim-authority.raw"
        self.result = self.run_dir / "result.json"

    @classmethod
    def create(cls, root: pathlib.Path) -> "RuntimePaths":
        root = pathlib.Path(root).resolve()
        output = pathlib.Path(
            os.environ.get("CXL_BI_APP_OUT") or root / "out" / "cxl-bi-app"
        )
        if not output.is_absolute():
            raise ValueError("CXL_BI_APP_OUT must be an absolute path")
        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y%m%dT%H%M%S.%fZ"
        )
        run_dir = output / "runs" / f"{timestamp}-{os.getpid()}"
        run_dir.mkdir(parents=True, mode=0o700)
        os.chmod(run_dir, 0o700)
        return cls(root, run_dir)

    @classmethod
    def for_test(cls, root: pathlib.Path) -> "RuntimePaths":
        root = pathlib.Path(root).resolve()
        return cls(root, root / "out" / "cxl-bi-app" / "runs" / "test")

    def endpoint_memory(self, node: int) -> pathlib.Path:
        return self.run_dir / f"node{node}-cxl-pmem.raw"

    def lsa(self, node: int) -> pathlib.Path:
        return self.run_dir / f"node{node}-lsa.raw"

    def console_log(self, node: int) -> pathlib.Path:
        return self.run_dir / f"node{node}-serial.log"

    def event_log(self, node: int) -> pathlib.Path:
        return self.run_dir / f"node{node}-host-events.jsonl"


def build_qemu_command(
    paths: RuntimePaths,
    config: RunConfig,
    node: int,
    coherence_port: int,
    *,
    bi_enabled: bool = True,
) -> list[str]:
    if node not in (0, 1):
        raise ValueError("node must be 0 or 1")
    if not 0 < coherence_port <= 65535:
        raise ValueError("coherence port must be in 1..65535")
    prefix = f"node{node}"
    restrictions = "0x29" if bi_enabled else "0x09"
    return [
        str(paths.qemu),
        "-name",
        f"cxl-bi-{prefix}",
        "-M",
        "sifive_u",
        "-cpu",
        "rv64,h=false,sstc=false,svadu=false,zicboz=false,"
        "zicbom=true,cbom_blocksize=64",
        "-machine",
        "cxl=on",
        "-machine",
        f"cxl-fmw.0.targets.0=cxl-{prefix},cxl-fmw.0.size=4G,"
        f"cxl-fmw.0.restrictions={restrictions}",
        "-smp",
        "5",
        "-m",
        config.guest_memory,
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
            f"memory-backend-file,id=t3pmem-{prefix},"
            f"mem-path={paths.endpoint_memory(node)},size=256M,share=on,pmem=on"
        ),
        "-object",
        (
            f"memory-backend-file,id=t3lsa-{prefix},"
            f"mem-path={paths.lsa(node)},size=2M,share=on"
        ),
        "-device",
        f"pxb-cxl,bus=pcie.0,bus_nr=64,id=cxl-{prefix},hdm_for_passthrough=on",
        "-device",
        f"cxl-rp,bus=cxl-{prefix},port=0,id=rp-t3-{prefix},chassis=0,slot=0,"
        "x-256b-flit=on",
        "-device",
        (
            f"cxl-type3,bus=rp-t3-{prefix},persistent-memdev=t3pmem-{prefix},"
            f"lsa=t3lsa-{prefix},id=t3-{prefix},coherence-v2=on,"
            "x-256b-flit=on,hdm-db=on,"
            f"cxlmemsim-addr=127.0.0.1,cxlmemsim-port={coherence_port},"
            f"coherence-v2-host-id={node},coherence-v2-cache-capacity=8388608,"
            "coherence-v2-cache-ways=4,coherence-v2-timeout-ms=5000,"
            "coherence-v2-write-through=off,"
            f"coherence-v2-read-exclusive={'on' if node == 0 else 'off'}"
        ),
    ]


def build_bootargs(role: str, config: RunConfig) -> str:
    if role not in {"reader", "writer"}:
        raise ValueError("role must be reader or writer")
    return (
        "earlycon=sbi console=hvc0 loglevel=6 panic=-1 "
        "cxl_core.pmem_as_dax=1 "
        f"cxlbi.role={role} cxlbi.iterations={config.iterations} "
        f"cxlbi.stream_bytes={config.stream_bytes} "
        f"cxlbi.timeout_ms={config.timeout_seconds * 1000}"
    )


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _strict_json_object(text: str, description: str) -> dict[str, object]:
    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicate_pairs)
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid {description}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{description} must be a JSON object")
    return value


def parse_guest_records(text: str) -> list[dict[str, object]]:
    """Extract strict schema-v1 records from a noisy serial console."""

    records: list[dict[str, object]] = []
    for line in text.splitlines():
        marker = line.find(RECORD_PREFIX)
        if marker < 0:
            continue
        record = _strict_json_object(
            line[marker + len(RECORD_PREFIX) :].strip(), "guest JSON record"
        )
        if record.get("schema_version") != TRACE_SCHEMA_VERSION:
            raise ValueError("unsupported guest record schema")
        event = record.get("event")
        if not isinstance(event, str) or not event:
            raise ValueError("guest record event must be a nonempty string")
        records.append(record)
    return records


def percentiles_ns(samples: Iterable[int]) -> dict[str, int | float]:
    """Return deterministic nearest-rank timing statistics."""

    ordered = sorted(samples)
    if not ordered:
        raise ValueError("cannot calculate percentiles for an empty sample set")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in ordered):
        raise ValueError("timing samples must be nonnegative integers")

    def nearest_rank(percent: float) -> int:
        index = max(0, math.ceil(percent * len(ordered)) - 1)
        return ordered[index]

    return {
        "count": len(ordered),
        "min_ns": ordered[0],
        "mean_ns": sum(ordered) / len(ordered),
        "p50_ns": nearest_rank(0.50),
        "p95_ns": nearest_rank(0.95),
        "p99_ns": nearest_rank(0.99),
        "max_ns": ordered[-1],
    }


def analytical_envelope(
    link_gbps: float,
    media_ns: float,
    request_ns: float,
    bi_ns: float,
) -> dict[str, object]:
    """Calculate a transparent model; these values are not measurements."""

    assumptions = {
        "link_payload_Gbps": float(link_gbps),
        "media_latency_ns": float(media_ns),
        "request_response_ns": float(request_ns),
        "bi_snoop_ack_ns": float(bi_ns),
    }
    if any(not math.isfinite(value) or value <= 0 for value in assumptions.values()):
        raise ValueError("analytical assumptions must be finite and positive")
    dirty_ns = assumptions["request_response_ns"] + assumptions["bi_snoop_ack_ns"] + assumptions["media_latency_ns"]
    clean_ns = assumptions["request_response_ns"] + assumptions["bi_snoop_ack_ns"]
    return {
        "classification": "analytical_not_measured",
        "assumptions": assumptions,
        "dirty_read_latency_ns": dirty_ns,
        "clean_transfer_latency_ns": clean_ns,
        "streaming_ceiling_GBps": assumptions["link_payload_Gbps"] / 8.0,
        # One byte per nanosecond is one decimal GB/s.
        "serialized_dirty_64B_GBps": 64.0 / dirty_ns,
    }


def _required_int(record: dict[str, object], name: str) -> int:
    value = record.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"trace field {name} must be an integer")
    return value


def _read_trace(path: pathlib.Path) -> list[dict[str, object]]:
    raw = pathlib.Path(path).read_bytes()
    if raw and not raw.endswith(b"\n"):
        raise ValueError("truncated final coherence trace record")
    records: list[dict[str, object]] = []
    previous_ns: int | None = None
    for number, raw_line in enumerate(raw.splitlines(), 1):
        try:
            line = raw_line.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError(f"invalid UTF-8 in trace record {number}") from error
        record = _strict_json_object(line, f"coherence trace record {number}")
        if record.get("schema_version") != TRACE_SCHEMA_VERSION:
            raise ValueError(f"unsupported trace schema in record {number}")
        monotonic_ns = _required_int(record, "monotonic_ns")
        if previous_ns is not None and monotonic_ns < previous_ns:
            raise ValueError("coherence trace timestamps went backwards")
        previous_ns = monotonic_ns
        records.append(record)
    return records


def _valid_dirty_path(
    request: dict[str, object],
    snoop: dict[str, object],
    ack: dict[str, object],
    completion: dict[str, object],
) -> bool:
    try:
        line = _required_int(snoop, "line_address")
        snoop_id = _required_int(snoop, "snoop_id")
        session = _required_int(snoop, "session_id")
        epoch = _required_int(snoop, "epoch")
        return (
            request.get("event") == "request"
            and request.get("opcode") in {"GETS", "GETM", "UPGRADE"}
            and _required_int(request, "line_address") == line
            and _required_int(request, "monotonic_ns") <= _required_int(snoop, "monotonic_ns")
            and snoop.get("event") == "snoop_send"
            and snoop.get("opcode") == "SNP_DATA_INV"
            and snoop.get("status") == "OK"
            and ack.get("event") == "snoop_ack"
            and ack.get("opcode") == "SNOOP_ACK"
            and _required_int(ack, "snoop_id") == snoop_id
            and _required_int(ack, "line_address") == line
            and _required_int(ack, "session_id") == session
            and _required_int(ack, "epoch") == epoch
            and ack.get("status") == "OK"
            and ack.get("ack_strength") == "MODEL"
            and ack.get("dirty_data") is True
            and _required_int(ack, "payload_len") == 64
            and completion.get("event") == "dirty_completion"
            and completion.get("opcode") == "SNOOP_ACK"
            and _required_int(completion, "snoop_id") == snoop_id
            and _required_int(completion, "line_address") == line
            and _required_int(completion, "session_id") == session
            and _required_int(completion, "epoch") == epoch
            and completion.get("status") == "OK"
            and completion.get("ack_strength") == "MODEL"
            and completion.get("dirty_data") is True
            and _required_int(completion, "payload_len") == 64
            and _required_int(snoop, "monotonic_ns") <= _required_int(ack, "monotonic_ns")
            and _required_int(ack, "monotonic_ns") <= _required_int(completion, "monotonic_ns")
        )
    except ValueError:
        return False


def analyze_trace(path: pathlib.Path, target_dpas: set[int]) -> dict[str, object]:
    """Find complete dirty BI paths for cache lines containing target DPAs."""

    if not target_dpas:
        raise ValueError("at least one target DPA is required")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in target_dpas):
        raise ValueError("target DPAs must be nonnegative integers")
    target_lines = sorted({value & ~63 for value in target_dpas})
    records = _read_trace(path)
    event_counts = Counter(
        str(record.get("event")) for record in records if isinstance(record.get("event"), str)
    )
    dirty_paths: list[dict[str, object]] = []
    completed_lines: set[int] = set()

    for snoop_index, snoop in enumerate(records):
        if (
            snoop.get("event") != "snoop_send"
            or snoop.get("opcode") != "SNP_DATA_INV"
        ):
            continue
        try:
            line = _required_int(snoop, "line_address")
            snoop_id = _required_int(snoop, "snoop_id")
        except ValueError:
            continue
        if line not in target_lines:
            continue
        requests = [
            record
            for record in records[: snoop_index + 1]
            if record.get("event") == "request"
            and record.get("opcode") in {"GETS", "GETM", "UPGRADE"}
            and record.get("line_address") == line
        ]
        acks = [
            record
            for record in records[snoop_index + 1 :]
            if record.get("event") == "snoop_ack"
            and record.get("snoop_id") == snoop_id
        ]
        completions = [
            record
            for record in records[snoop_index + 1 :]
            if record.get("event") == "dirty_completion"
            and record.get("snoop_id") == snoop_id
        ]
        if not requests or not acks or not completions:
            continue
        request = requests[-1]
        ack = acks[0]
        completion = completions[0]
        if not _valid_dirty_path(request, snoop, ack, completion):
            continue
        dirty_paths.append(
            {
                "line_address": line,
                "request_id": _required_int(request, "request_id"),
                "request_opcode": request["opcode"],
                "requester_host": _required_int(request, "src_host"),
                "owner_host": _required_int(snoop, "dst_host"),
                "snoop_id": snoop_id,
                "session_id": _required_int(snoop, "session_id"),
                "epoch": _required_int(snoop, "epoch"),
                "payload_len": _required_int(ack, "payload_len"),
                "request_ns": _required_int(request, "monotonic_ns"),
                "snoop_ns": _required_int(snoop, "monotonic_ns"),
                "ack_ns": _required_int(ack, "monotonic_ns"),
                "completion_ns": _required_int(completion, "monotonic_ns"),
            }
        )
        completed_lines.add(line)

    return {
        "complete": bool(dirty_paths),
        "target_lines": [hex(value) for value in target_lines],
        "missing_target_lines": [hex(value) for value in target_lines if value not in completed_lines],
        "event_counts": dict(sorted(event_counts.items())),
        "dirty_paths": dirty_paths,
    }


def _one_event(
    records: list[dict[str, object]], event: str, role: str
) -> dict[str, object]:
    selected = [record for record in records if record.get("event") == event]
    if len(selected) != 1:
        raise ValueError(
            f"expected exactly one {event} record for {role}, got {len(selected)}"
        )
    record = selected[0]
    if record.get("role") != role:
        raise ValueError(f"{event} record has the wrong role for {role}")
    return record


def _integer(record: dict[str, object], name: str, context: str) -> int:
    value = record.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{context} field {name} must be an integer")
    return value


def _elapsed_mibps(record: dict[str, object], context: str) -> float:
    byte_count = _integer(record, "bytes", context)
    elapsed_ns = _integer(record, "elapsed_ns", context)
    if byte_count <= 0 or elapsed_ns <= 0:
        raise ValueError(f"{context} bytes and elapsed_ns must be positive")
    return byte_count * 1_000_000_000.0 / elapsed_ns / (1024.0 * 1024.0)


def validate_guest_evidence(
    reader_records: list[dict[str, object]],
    writer_records: list[dict[str, object]],
    iterations: int,
    stream_bytes: int,
) -> dict[str, object]:
    """Validate independent application gates before considering model traces."""

    by_role = {"reader": reader_records, "writer": writer_records}
    for role, records in by_role.items():
        if any(record.get("event") == "fatal" for record in records):
            raise ValueError(f"{role} emitted a fatal record")
        if not records:
            raise ValueError(f"{role} emitted no guest records")
        for record in records:
            if record.get("schema_version") != TRACE_SCHEMA_VERSION:
                raise ValueError(f"{role} emitted an unsupported record schema")
            if record.get("role") != role:
                raise ValueError(f"{role} stream contains a foreign-role record")

    mapped = {
        role: _one_event(records, "mapped", role)
        for role, records in by_role.items()
    }
    if _integer(mapped["reader"], "endpoint_id", "reader mapped") != 0:
        raise ValueError("reader endpoint ID must be 0")
    if _integer(mapped["writer"], "endpoint_id", "writer mapped") != 1:
        raise ValueError("writer endpoint ID must be 1")
    geometry_fields = (
        "dax_size",
        "dax_align",
        "mapping_offset",
        "mapping_bytes",
        "litmus_generation_offset",
        "payload_offset",
        "ping_request_offset",
        "ping_ack_offset",
        "stream_offset",
    )
    for name in geometry_fields:
        reader_value = _integer(mapped["reader"], name, "reader mapped")
        writer_value = _integer(mapped["writer"], name, "writer mapped")
        if reader_value != writer_value:
            raise ValueError(f"guest DAX geometry differs for {name}")
    payload_offset = _integer(mapped["reader"], "payload_offset", "reader mapped")
    if payload_offset % 64:
        raise ValueError("litmus payload offset is not cache-line aligned")

    litmus = {
        role: _one_event(records, "litmus", role)
        for role, records in by_role.items()
    }
    if litmus["reader"].get("operation") != "observe":
        raise ValueError("reader litmus operation is not observe")
    if litmus["writer"].get("operation") != "publish":
        raise ValueError("writer litmus operation is not publish")
    for role, record in litmus.items():
        if _integer(record, "generation", f"{role} litmus") != 1:
            raise ValueError(f"{role} litmus has the wrong generation")
        if _integer(record, "errors", f"{role} litmus") != 0:
            raise ValueError(f"{role} litmus reported an application error")
        if _integer(record, "elapsed_ns", f"{role} litmus") <= 0:
            raise ValueError(f"{role} litmus elapsed time must be positive")
    if _integer(litmus["reader"], "payload_checksum", "reader litmus") != _integer(
        litmus["writer"], "payload_checksum", "writer litmus"
    ):
        raise ValueError("reader and writer payload checksum disagree")

    ping = {
        role: _one_event(records, "pingpong", role)
        for role, records in by_role.items()
    }
    for role, record in ping.items():
        if _integer(record, "iterations", f"{role} pingpong") != iterations:
            raise ValueError(f"{role} pingpong iteration count differs")
        if _integer(record, "errors", f"{role} pingpong") != 0:
            raise ValueError(f"{role} pingpong reported an application error")
        if _integer(record, "elapsed_ns", f"{role} pingpong") <= 0:
            raise ValueError(f"{role} pingpong elapsed time must be positive")
    reader_ping_fields = (
        "min_ns",
        "mean_ns",
        "p50_ns",
        "p95_ns",
        "p99_ns",
        "max_ns",
    )
    for name in reader_ping_fields:
        if _integer(ping["reader"], name, "reader pingpong") < 0:
            raise ValueError(f"reader pingpong {name} must be nonnegative")
    if not (
        ping["reader"]["min_ns"]
        <= ping["reader"]["p50_ns"]
        <= ping["reader"]["p95_ns"]
        <= ping["reader"]["p99_ns"]
        <= ping["reader"]["max_ns"]
    ):
        raise ValueError("reader pingpong percentiles are not monotonic")

    streams: dict[tuple[str, int, str], dict[str, object]] = {}
    for role, records in by_role.items():
        selected = [record for record in records if record.get("event") == "stream"]
        if len(selected) != 2:
            raise ValueError(f"expected two stream records for {role}")
        for record in selected:
            direction = _integer(record, "direction", f"{role} stream")
            operation = record.get("operation")
            if direction not in (0, 1) or operation not in {"write", "read_verify"}:
                raise ValueError(f"{role} emitted an invalid stream record")
            key = (role, direction, str(operation))
            if key in streams:
                raise ValueError(f"duplicate stream record: {key}")
            if _integer(record, "bytes", f"{role} stream") != stream_bytes:
                raise ValueError(f"{role} stream byte count differs")
            if _integer(record, "errors", f"{role} stream") != 0:
                raise ValueError(f"{role} stream verification failed")
            streams[key] = record
    expected_streams = {
        ("writer", 0, "write"),
        ("reader", 0, "read_verify"),
        ("reader", 1, "write"),
        ("writer", 1, "read_verify"),
    }
    if set(streams) != expected_streams:
        raise ValueError("stream direction/role operations are incomplete")

    summaries = {
        role: _one_event(records, "summary", role)
        for role, records in by_role.items()
    }
    for role, summary in summaries.items():
        if summary.get("status") != "pass" or _integer(
            summary, "errors", f"{role} summary"
        ) != 0:
            raise ValueError(f"{role} summary did not pass")
        if _integer(summary, "iterations", f"{role} summary") != iterations:
            raise ValueError(f"{role} summary iteration count differs")
        if _integer(summary, "stream_bytes", f"{role} summary") != stream_bytes:
            raise ValueError(f"{role} summary stream byte count differs")
        if _integer(
            summary, "explicit_flush_calls", f"{role} summary"
        ) != 0:
            raise ValueError(f"{role} reported an explicit flush")

    ping_elapsed = _integer(ping["reader"], "elapsed_ns", "reader pingpong")
    measurements = {
        "classification": "qemu_tcg_tcp_wallclock",
        "warning": (
            "Functional-model timing includes QEMU TCG, host scheduling, "
            "TCP, logging, and guest polling; it is not hardware CXL timing."
        ),
        "litmus": {
            "reader_end_to_end_ns": _integer(
                litmus["reader"], "elapsed_ns", "reader litmus"
            ),
            "writer_publish_ns": _integer(
                litmus["writer"], "elapsed_ns", "writer litmus"
            ),
        },
        "pingpong": {
            name: ping["reader"][name] for name in reader_ping_fields
        }
        | {
            "iterations": iterations,
            "elapsed_ns": ping_elapsed,
            "round_trips_per_second": iterations * 1_000_000_000.0 / ping_elapsed,
            "coherence_transfers_per_second": iterations
            * 2.0
            * 1_000_000_000.0
            / ping_elapsed,
        },
        "stream": {
            "direction_0_write_MiBps": _elapsed_mibps(
                streams[("writer", 0, "write")], "direction 0 write"
            ),
            "direction_0_read_verify_MiBps": _elapsed_mibps(
                streams[("reader", 0, "read_verify")],
                "direction 0 read verify",
            ),
            "direction_1_write_MiBps": _elapsed_mibps(
                streams[("reader", 1, "write")], "direction 1 write"
            ),
            "direction_1_read_verify_MiBps": _elapsed_mibps(
                streams[("writer", 1, "read_verify")],
                "direction 1 read verify",
            ),
            "bytes_per_direction": stream_bytes,
        },
    }
    return {
        "application_pass": True,
        "application_errors": 0,
        "payload_offset": payload_offset,
        "ping_ack_offset": _integer(
            mapped["reader"], "ping_ack_offset", "reader mapped"
        ),
        "stream_offset": _integer(
            mapped["reader"], "stream_offset", "reader mapped"
        ),
        "mapped": mapped,
        "litmus": litmus,
        "summaries": summaries,
        "measurements": measurements,
    }


def parse_server_stats(output: str) -> dict[str, object]:
    marker = "COHERENCE_V2_STATS_JSON "
    records = []
    for line in output.splitlines():
        position = line.find(marker)
        if position >= 0:
            records.append(
                _strict_json_object(
                    line[position + len(marker) :].strip(),
                    "coherence server stats",
                )
            )
    if len(records) != 1:
        raise ValueError(
            f"expected exactly one final coherence stats object, got {len(records)}"
        )
    stats = records[0]
    for name in (
        "timeouts",
        "protocol_errors",
        "delivery_failures",
        "server_copy_failures",
        "active_bindings",
    ):
        if _integer(stats, name, "server stats") != 0:
            raise ValueError(f"coherence server counter is nonzero: {name}")
    return stats


def build_server_command(paths: RuntimePaths, coherence_port: int) -> list[str]:
    if not 0 < coherence_port <= 65535:
        raise ValueError("coherence port must be in 1..65535")
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


def qemu_environment(paths: RuntimePaths) -> dict[str, str]:
    environment = dict(os.environ)
    for name in LEGACY_TRANSPORT_ENV:
        environment.pop(name, None)
    old_path = environment.get("PATH", "")
    environment["PATH"] = str(paths.qemu.parent)
    if old_path:
        environment["PATH"] += os.pathsep + old_path
    return environment


class PortReservation:
    def __init__(self):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("127.0.0.1", 0))
        self.port = int(self.socket.getsockname()[1])
        self.released = False

    def release(self) -> None:
        if not self.released:
            self.socket.close()
            self.released = True


class OwnedProcess:
    """A child whose identity and process group are checked before signaling."""

    def __init__(
        self,
        process: subprocess.Popen,
        command: list[str],
        run_dir: pathlib.Path,
        owner_token: str,
        start_ns: int,
    ):
        self.process = process
        self.command = list(command)
        self.run_dir = str(pathlib.Path(run_dir).resolve())
        self.owner_token = owner_token
        self.start_ns = start_ns
        self.end_ns: int | None = None
        self.stop_reason: str | None = None

    def record_exit(self) -> None:
        if self.end_ns is None and self.process.poll() is not None:
            self.end_ns = time.monotonic_ns()

    def matches_live_pid(self) -> bool:
        if self.process.poll() is not None:
            self.record_exit()
            return False
        try:
            raw = pathlib.Path(f"/proc/{self.process.pid}/cmdline").read_bytes()
            process_group = os.getpgid(self.process.pid)
        except OSError:
            return False
        fields = [field.decode(errors="replace") for field in raw.split(b"\0") if field]
        if not fields or process_group != self.process.pid:
            return False
        expected = pathlib.Path(self.command[0]).name
        return pathlib.Path(fields[0]).name == expected and any(
            self.run_dir in field for field in fields
        )

    def send_signal(self, requested_signal: int, reason: str) -> bool:
        if not self.matches_live_pid():
            return False
        os.killpg(self.process.pid, requested_signal)
        self.stop_reason = reason
        return True

    def terminate_owned(self, reason: str = "cleanup") -> None:
        if not self.send_signal(signal.SIGTERM, reason):
            return
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if self.matches_live_pid():
                os.killpg(self.process.pid, signal.SIGKILL)
                self.stop_reason = reason + ":sigkill"
            self.process.wait(timeout=5)
        self.record_exit()

    def as_json(self) -> dict[str, object]:
        self.record_exit()
        return {
            "pid": self.process.pid,
            "command": self.command,
            "start_monotonic_ns": self.start_ns,
            "end_monotonic_ns": self.end_ns,
            "owner_token": self.owner_token,
            "returncode": self.process.poll(),
            "stop_reason": self.stop_reason,
        }


class Console:
    def __init__(
        self,
        name: str,
        command: list[str],
        environment: dict[str, str],
        log_path: pathlib.Path,
        event_path: pathlib.Path,
        run_dir: pathlib.Path,
        owner_token: str,
    ):
        self.name = name
        self.command = list(command)
        self.output = ""
        self.completed_lines: list[str] = []
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
            start_new_session=True,
        )
        self.owned = OwnedProcess(process, command, run_dir, owner_token, start_ns)
        self.process = process
        self.reader = threading.Thread(target=self._read_output, daemon=True)
        self.reader.start()

    def _capture_event(self, raw_line: bytes) -> None:
        decoded = raw_line.decode(errors="replace").rstrip("\r")
        capture_ns = time.monotonic_ns()
        self.completed_lines.append(decoded)
        json.dump(
            {"host_capture_ns": capture_ns, "line": decoded},
            self.events,
            sort_keys=True,
        )
        self.events.write("\n")
        self.events.flush()
        if RECORD_PREFIX in decoded:
            print(f"[{self.name}] {decoded}", flush=True)

    def _read_output(self) -> None:
        pending = b""
        assert self.process.stdout is not None
        try:
            while True:
                chunk = os.read(self.process.stdout.fileno(), 4096)
                if not chunk:
                    break
                decoded = chunk.decode(errors="replace")
                with self.condition:
                    self.output += decoded
                    self.log.write(decoded)
                    self.log.flush()
                    pending += chunk
                    while b"\n" in pending:
                        raw_line, pending = pending.split(b"\n", 1)
                        self._capture_event(raw_line)
                    self.condition.notify_all()
            if pending:
                with self.condition:
                    self._capture_event(pending)
        finally:
            self.owned.record_exit()
            with self.condition:
                self.condition.notify_all()

    def wait(self, text: str, timeout: float, start: int = 0) -> int:
        deadline = time.monotonic() + timeout
        with self.condition:
            while text not in self.output[start:]:
                fatal_position = self.output.find(
                    f'{RECORD_PREFIX}{{"schema_version":1,"event":"fatal"', start
                )
                if fatal_position >= 0:
                    line = self.output[fatal_position:].splitlines()[0]
                    raise RuntimeError(f"{self.name} reported {line}")
                if self.process.poll() is not None:
                    raise RuntimeError(
                        f"{self.name} QEMU exited with {self.process.returncode} "
                        f"while waiting for {text!r}"
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"timed out waiting for {text!r} on {self.name}")
                self.condition.wait(min(remaining, 0.5))
            return self.output.find(text, start)

    def send(self, line: str) -> None:
        if self.process.stdin is None or self.process.poll() is not None:
            raise RuntimeError(f"{self.name} QEMU console is not writable")
        self.process.stdin.write((line + "\n").encode())
        self.process.stdin.flush()

    def wait_guest_event(self, event: str, role: str, timeout: float) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                for line in self.completed_lines:
                    if RECORD_PREFIX not in line:
                        continue
                    records = parse_guest_records(line)
                    if len(records) != 1:
                        raise ValueError(f"{self.name} guest line has multiple records")
                    record = records[0]
                    if record.get("event") == "fatal":
                        raise RuntimeError(f"{self.name} reported {line}")
                    if record.get("event") == event and record.get("role") == role:
                        return record
                if self.process.poll() is not None:
                    raise RuntimeError(
                        f"{self.name} QEMU exited with {self.process.returncode} "
                        f"while waiting for complete {event}/{role} record"
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"timed out waiting for complete {event}/{role} record "
                        f"on {self.name}"
                    )
                self.condition.wait(min(remaining, 0.5))

    def command_until_prompt(self, command: str, timeout: float) -> str:
        start = len(self.output)
        self.send(command)
        self.wait("=> ", timeout, start)
        return self.output[start:]

    def close_files(self) -> None:
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


def create_sparse_file(path: pathlib.Path, size: int) -> None:
    with pathlib.Path(path).open("xb") as output:
        output.truncate(size)


def _sha256(path: pathlib.Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with pathlib.Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_path(root: pathlib.Path, value: object) -> pathlib.Path:
    if not isinstance(value, str) or not value:
        raise ValueError("manifest artifact path must be a nonempty string")
    path = pathlib.Path(value)
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def load_runtime_manifest(paths: RuntimePaths) -> dict[str, object]:
    manifest = _strict_json_object(
        paths.manifest.read_text(encoding="utf-8"), "build manifest"
    )
    if manifest.get("schema_version") != 2:
        raise ValueError("unsupported build manifest schema")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ValueError("build manifest lacks artifacts")
    expected = {
        "qemu": (paths.qemu, True),
        "cxlmemsim_server": (paths.cxlmemsim_server, True),
        "opensbi": (paths.opensbi, False),
        "u_boot": (paths.u_boot, False),
        "linux_cxl_bi": (paths.linux, False),
    }
    for name, (expected_path, executable) in expected.items():
        entry = artifacts.get(name)
        if not isinstance(entry, dict):
            raise ValueError(f"build manifest lacks artifact {name}")
        actual_path = _manifest_path(paths.root, entry.get("path"))
        if actual_path != expected_path.resolve():
            raise ValueError(f"manifest path differs for artifact {name}")
        if not actual_path.is_file() or actual_path.stat().st_size == 0:
            raise FileNotFoundError(f"runtime artifact is missing: {name}")
        if executable and not os.access(actual_path, os.X_OK):
            raise PermissionError(f"runtime artifact is not executable: {name}")
        expected_hash = entry.get("sha256")
        if not isinstance(expected_hash, str) or _sha256(actual_path) != expected_hash:
            raise ValueError(f"runtime artifact hash differs for {name}")
    if not paths.topology.is_file():
        raise FileNotFoundError(f"CXLMemSim topology is missing: {paths.topology}")
    return manifest


def wait_for_log(
    path: pathlib.Path,
    marker: str,
    process: subprocess.Popen,
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"CXLMemSim exited before readiness: {process.returncode}"
            )
        try:
            if marker in pathlib.Path(path).read_text(
                encoding="utf-8", errors="replace"
            ):
                return
        except FileNotFoundError:
            pass
        time.sleep(0.05)
    raise TimeoutError(f"timed out waiting for CXLMemSim marker {marker!r}")


def run_uboot(
    console: Console,
    paths: RuntimePaths,
    role: str,
    config: RunConfig,
    *,
    commit_decoder: bool = True,
) -> dict[str, object]:
    timeout = config.timeout_seconds
    console.wait("Hit any key to stop autoboot", timeout)
    console.send("")
    console.wait("=> ", timeout)
    if HOST_DECODER not in console.output or TYPE3_DECODER not in console.output:
        raise ValueError(f"{console.name} preboot CXL decoder proof is missing")
    listing = console.command_until_prompt("cxl list", timeout)
    if "41.00.0" not in listing or "Type 3" not in listing:
        raise ValueError(f"{console.name} cxl list did not report Type 3")
    information = console.command_until_prompt("cxl info 41.00.0", timeout)
    if "41.00.0" not in information or "0000000010000000" not in information:
        raise ValueError(f"{console.name} cxl info is incomplete")
    initialization = "not requested"
    if commit_decoder:
        initialization = console.command_until_prompt("cxl init", timeout)
        if HOST_DECODER not in initialization or TYPE3_DECODER not in initialization:
            raise ValueError(f"{console.name} cxl init did not commit decoder state")
    bootargs = build_bootargs(role, config)
    console.command_until_prompt(f"setenv bootargs '{bootargs}'", timeout)
    console.send(f"bootefi 90000000:{paths.linux.stat().st_size:x} ${{fdtcontroladdr}}")
    return {
        "role": role,
        "cxl_list_type3": True,
        "cxl_info_capacity": True,
        "decoder_committed": commit_decoder,
        "bootargs": bootargs,
    }


def _atomic_write_json(path: pathlib.Path, value: object) -> None:
    path = pathlib.Path(path)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as output:
            json.dump(value, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _backing_metadata(paths: RuntimePaths) -> dict[str, object]:
    entries = []
    identities = set()
    for node in (0, 1):
        path = paths.endpoint_memory(node)
        status = path.stat()
        identity = (status.st_dev, status.st_ino)
        if identity in identities:
            raise ValueError("endpoint backing files share an inode")
        identities.add(identity)
        entries.append(
            {
                "node": node,
                "path": str(path),
                "st_dev": status.st_dev,
                "st_ino": status.st_ino,
                "size": status.st_size,
            }
        )
    if entries[0]["path"] == entries[1]["path"]:
        raise ValueError("endpoint backing paths are not distinct")
    return {"distinct_paths_and_inodes": True, "endpoints": entries}


def _validate_registrations(records: list[dict[str, object]]) -> list[dict[str, object]]:
    registrations = [
        record
        for record in records
        if record.get("event") == "registration" and record.get("status") == "OK"
    ]
    if len(registrations) != 2:
        raise ValueError(
            f"expected two successful coherence registrations, got {len(registrations)}"
        )
    by_host: dict[int, dict[str, object]] = {}
    sessions = set()
    for record in registrations:
        host = _required_int(record, "src_host")
        session = _required_int(record, "session_id")
        if host not in (0, 1) or host in by_host or session <= 0 or session in sessions:
            raise ValueError("coherence registrations have invalid host/session identity")
        by_host[host] = record
        sessions.add(session)
    if set(by_host) != {0, 1}:
        raise ValueError("coherence registrations must contain host IDs 0 and 1")
    return [by_host[0], by_host[1]]


def _host_capture_summary(path: pathlib.Path, role: str) -> dict[str, int]:
    captures = []
    for raw_line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
        wrapper = _strict_json_object(raw_line, "host event sidecar record")
        line = wrapper.get("line")
        capture_ns = wrapper.get("host_capture_ns")
        if not isinstance(line, str) or isinstance(capture_ns, bool) or not isinstance(capture_ns, int):
            raise ValueError("host event sidecar record has invalid fields")
        if RECORD_PREFIX not in line:
            continue
        parsed = parse_guest_records(line)
        if len(parsed) == 1 and parsed[0].get("role") == role:
            captures.append((str(parsed[0].get("event")), capture_ns))
    first_by_event: dict[str, int] = {}
    for event, capture_ns in captures:
        first_by_event.setdefault(event, capture_ns)
    if "mapped" not in first_by_event or "summary" not in first_by_event:
        raise ValueError(f"host capture is missing mapped/summary for {role}")
    return {
        "mapped_host_ns": first_by_event["mapped"],
        "summary_host_ns": first_by_event["summary"],
        "mapped_to_summary_ns": first_by_event["summary"] - first_by_event["mapped"],
    }


def _stop_server(server: OwnedProcess) -> None:
    if server.process.poll() is not None:
        server.record_exit()
        return
    if server.send_signal(signal.SIGINT, "runner_after_guest_summaries"):
        try:
            server.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.terminate_owned("server_sigint_timeout")
    server.record_exit()


def _last_lines(text: str, count: int = 80) -> list[str]:
    return text.splitlines()[-count:]


def run_experiment(paths: RuntimePaths, config: RunConfig) -> dict[str, object]:
    owner_token = str(uuid.uuid4())
    port = PortReservation()
    owned: dict[str, OwnedProcess] = {}
    consoles: dict[str, Console] = {}
    server_log_handle = None
    result: dict[str, object] = {
        "schema_version": 1,
        "verdict": "FAIL",
        "run_id": paths.run_dir.name,
        "run_directory": str(paths.run_dir),
        "first_failure": None,
        "functional_model_only": True,
        "claim_boundary": {
            "demonstrates": (
                "application-visible load/store coherence between two independent "
                "QEMU Type-3 endpoint caches through CXLMemSim MESI-v2 BI"
            ),
            "does_not_demonstrate": [
                "native physical CPU-cache invalidation",
                "physical CXL link compliance or performance",
                "GPF or power-failure persistence",
            ],
        },
        "configuration": {
            "iterations": config.iterations,
            "stream_bytes": config.stream_bytes,
            "timeout_seconds": config.timeout_seconds,
            "guest_memory": config.guest_memory,
        },
        "logs": {
            "reader_serial": str(paths.console_log(0)),
            "writer_serial": str(paths.console_log(1)),
            "reader_host_events": str(paths.event_log(0)),
            "writer_host_events": str(paths.event_log(1)),
            "cxlmemsim": str(paths.server_log),
            "coherence_trace": str(paths.coherence_trace),
        },
        "processes": {},
    }
    try:
        manifest = load_runtime_manifest(paths)
        result["build_manifest"] = str(paths.manifest)
        result["component_commits"] = manifest.get("submodules", {})

        create_sparse_file(paths.server_ssd, 256 * 1024 * 1024)
        for node in (0, 1):
            create_sparse_file(paths.endpoint_memory(node), 256 * 1024 * 1024)
            create_sparse_file(paths.lsa(node), 2 * 1024 * 1024)
        result["backing_files"] = _backing_metadata(paths)

        server_command = build_server_command(paths, port.port)
        result["cxlmemsim_command"] = server_command
        server_log_handle = paths.server_log.open("w", encoding="utf-8")
        port.release()
        server_process = subprocess.Popen(
            server_command,
            stdout=server_log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        server = OwnedProcess(
            server_process,
            server_command,
            paths.run_dir,
            owner_token,
            time.monotonic_ns(),
        )
        owned["cxlmemsim"] = server
        wait_for_log(
            paths.server_log,
            "Server listening on TCP port",
            server_process,
            config.timeout_seconds,
        )

        qemu_commands = {
            "reader": build_qemu_command(paths, config, 0, port.port),
            "writer": build_qemu_command(paths, config, 1, port.port),
        }
        result["qemu_commands"] = qemu_commands
        environment = qemu_environment(paths)

        reader = Console(
            "reader",
            qemu_commands["reader"],
            environment,
            paths.console_log(0),
            paths.event_log(0),
            paths.run_dir,
            owner_token,
        )
        consoles["reader"] = reader
        owned["reader_qemu"] = reader.owned
        boot_evidence = {
            "reader": run_uboot(reader, paths, "reader", config)
        }
        reader.wait_guest_event("ready", "reader", config.timeout_seconds)

        writer = Console(
            "writer",
            qemu_commands["writer"],
            environment,
            paths.console_log(1),
            paths.event_log(1),
            paths.run_dir,
            owner_token,
        )
        consoles["writer"] = writer
        owned["writer_qemu"] = writer.owned
        boot_evidence["writer"] = run_uboot(writer, paths, "writer", config)
        result["uboot_evidence"] = boot_evidence

        writer.wait_guest_event("summary", "writer", config.timeout_seconds)
        reader.wait_guest_event("summary", "reader", config.timeout_seconds)

        for console in consoles.values():
            console.owned.terminate_owned("runner_after_guest_summary")
        for console in consoles.values():
            console.reader.join(timeout=3)
        time.sleep(0.25)
        _stop_server(server)
        if server_log_handle is not None and not server_log_handle.closed:
            server_log_handle.close()

        reader_records = parse_guest_records(reader.output)
        writer_records = parse_guest_records(writer.output)
        application = validate_guest_evidence(
            reader_records,
            writer_records,
            config.iterations,
            config.stream_bytes,
        )
        result["application_evidence"] = application
        result["guest_records"] = {
            "reader": reader_records,
            "writer": writer_records,
        }
        result["host_capture"] = {
            "reader": _host_capture_summary(paths.event_log(0), "reader"),
            "writer": _host_capture_summary(paths.event_log(1), "writer"),
        }

        trace_records = _read_trace(paths.coherence_trace)
        registrations = _validate_registrations(trace_records)
        trace_errors = [
            record
            for record in trace_records
            if record.get("event")
            in {"timeout", "protocol_error", "delivery_failure", "server_copy_failure"}
        ]
        if trace_errors:
            raise ValueError(
                f"coherence trace contains {len(trace_errors)} protocol/error events"
            )
        payload_dpa = application["mapped"]["reader"]["mapping_offset"] + application[
            "payload_offset"
        ]
        trace_evidence = analyze_trace(paths.coherence_trace, {int(payload_dpa)})
        if not trace_evidence["complete"]:
            raise ValueError(
                "application passed but no address-correlated dirty BI path covers "
                f"payload DPA {hex(int(payload_dpa))}"
            )
        trace_evidence["registrations"] = registrations
        trace_evidence["error_events"] = 0
        result["bi_trace_evidence"] = trace_evidence

        server_output = paths.server_log.read_text(encoding="utf-8", errors="replace")
        stats = parse_server_stats(server_output)
        if _integer(stats, "registrations", "server stats") != 2:
            raise ValueError("coherence server did not register exactly two endpoints")
        if _integer(stats, "snp_data_inv", "server stats") <= 0:
            raise ValueError("coherence server recorded no SNP_DATA_INV")
        if _integer(stats, "dirty_data_completions", "server stats") <= 0:
            raise ValueError("coherence server recorded no dirty data completion")
        result["coherence_server_stats"] = stats
        result["performance"] = {
            "observed": application["measurements"],
            "analytical": analytical_envelope(
                config.link_gbps,
                config.media_ns,
                config.request_ns,
                config.bi_ns,
            ),
        }
        result["topology"] = {
            "machine": "sifive_u",
            "guests": 2,
            "type3_endpoints": 2,
            "endpoint_backing": "two distinct persistent-memdev files",
            "logical_memory_authority": "one CXLMemSim MESI-v2 server",
            "server_backing": "ssd-stream NAND-like media model",
            "fmw_restrictions": "DEVMEM|PMEM|BI (0x29)",
            "hdm_db": True,
            "flit_mode_bytes": 256,
            "reader_read_exclusive": True,
        }
        result["verdict"] = "PASS"
        return result
    except BaseException as error:
        result["first_failure"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        port.release()
        for console in reversed(list(consoles.values())):
            try:
                console.owned.terminate_owned("finally_cleanup")
            except (OSError, subprocess.SubprocessError):
                pass
        server = owned.get("cxlmemsim")
        if server is not None and server.process.poll() is None:
            try:
                _stop_server(server)
            except (OSError, subprocess.SubprocessError):
                try:
                    server.terminate_owned("finally_cleanup")
                except (OSError, subprocess.SubprocessError):
                    pass
        for console in consoles.values():
            try:
                console.close_files()
            except OSError:
                pass
        if server_log_handle is not None and not server_log_handle.closed:
            server_log_handle.close()
        result["processes"] = {
            name: process.as_json() for name, process in owned.items()
        }
        result["cleanup"] = {
            "owned_processes_remaining": sum(
                process.matches_live_pid() for process in owned.values()
            )
        }
        result["serial_tail"] = {
            role: _last_lines(console.output) for role, console in consoles.items()
        }
        _atomic_write_json(paths.result, result)


def parse_args(argv: list[str] | None = None) -> RunConfig:
    parser = argparse.ArgumentParser(
        description=(
            "Boot two RISC-V QEMU guests and validate application-visible "
            "CXL Type-3 BI coherence through CXLMemSim."
        )
    )
    parser.add_argument("--iterations", type=int, default=256)
    parser.add_argument("--stream-bytes", type=int, default=1024 * 1024)
    parser.add_argument("--timeout", type=int, default=300, dest="timeout_seconds")
    parser.add_argument("--link-gbps", type=float, default=32.0)
    parser.add_argument("--media-ns", type=float, default=100.0)
    parser.add_argument("--request-ns", type=float, default=150.0)
    parser.add_argument("--bi-ns", type=float, default=200.0)
    parser.add_argument("--guest-memory", default="1G")
    args = parser.parse_args(argv)
    if not 1 <= args.iterations <= 16384:
        parser.error("iterations must be in 1..16384")
    if not 64 <= args.stream_bytes <= 4 * 1024 * 1024 or args.stream_bytes % 64:
        parser.error("stream-bytes must be 64-aligned and in 64..4194304")
    if not 1 <= args.timeout_seconds <= 600:
        parser.error("timeout must be in 1..600 seconds")
    if not re.fullmatch(r"[1-9][0-9]*[MG]", args.guest_memory):
        parser.error("guest-memory must use a positive QEMU M or G suffix")
    try:
        analytical_envelope(
            args.link_gbps, args.media_ns, args.request_ns, args.bi_ns
        )
    except ValueError as error:
        parser.error(str(error))
    return RunConfig(
        iterations=args.iterations,
        stream_bytes=args.stream_bytes,
        timeout_seconds=args.timeout_seconds,
        link_gbps=args.link_gbps,
        media_ns=args.media_ns,
        request_ns=args.request_ns,
        bi_ns=args.bi_ns,
        guest_memory=args.guest_memory,
    )


def main(argv: list[str] | None = None) -> int:
    config = parse_args(argv)
    root = pathlib.Path(__file__).resolve().parents[1]
    paths = RuntimePaths.create(root)
    try:
        result = run_experiment(paths, config)
    except BaseException as error:
        print(f"error: {error}", file=sys.stderr)
        print(f"result: {paths.result}", file=sys.stderr)
        return 1
    print(f"CXL_BI_APP_COMPLETE {paths.result}")
    print(json.dumps({"verdict": result["verdict"], "result": str(paths.result)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
