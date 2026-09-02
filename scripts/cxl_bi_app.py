#!/usr/bin/env python3
"""Two-guest application-visible CXL BI coherence experiment."""

from __future__ import annotations

import json
import math
import pathlib
from collections import Counter
from typing import Iterable


RECORD_PREFIX = "CXLBI_JSON "
TRACE_SCHEMA_VERSION = 1


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


if __name__ == "__main__":
    raise SystemExit("runner CLI is added by the orchestration task")
