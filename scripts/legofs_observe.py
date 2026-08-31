#!/usr/bin/env python3
"""Decode and compare bounded LegoFS process-local observation arenas."""

import argparse
import csv
import hashlib
import json
import math
import os
import pathlib
import statistics
import struct
import sys


MAGIC = b"LEGOFSO1"
SCHEMA_MAJOR = 1
SCHEMA_MINOR = 0
SCHEMA_DIGEST = "b5640c61c8e38775"
HEADER_BYTES = 4096
EVENT_RECORD_BYTES = 64
IDENTITY_BASE = 4096
PRODUCER_CONTROL_BYTES = 64
METRIC_SLOTS = 384
HISTOGRAM_BINS = 576
HISTOGRAM_MIN_NS = 100
HISTOGRAM_GAMMA = 1.02 / 0.98
MIN_PERCENTILE_SAMPLES = 100
METRIC_HEADER_BYTES = 104
METRIC_RECORD_BYTES = METRIC_HEADER_BYTES + HISTOGRAM_BINS * 2
COMPACT_METRIC_BYTES = 64
WORKLOAD_METRIC_BYTES = METRIC_SLOTS * METRIC_RECORD_BYTES
DIAGNOSTIC_METRIC_BYTES = METRIC_SLOTS * COMPACT_METRIC_BYTES
STAGE_COUNT = 58

COMPONENTS = (
    "syscall_intercept",
    "client_semantics",
    "direct_metadata_read",
    "cxl_client_transport",
    "authority_progress",
    "dispatcher_codec",
    "namespace_metadata",
    "lifecycle_transaction",
    "data_arena_extent",
    "persistence",
    "startup_recovery",
)
ROOTS = (
    "create", "mkdir", "stat", "unlink", "rmdir", "readdir", "open",
    "close", "read", "write", "fsync", "sync", "rename", "other",
)
SEMANTICS = ROOTS
STAGES = (
    "hook_to_client", "result_translate",
    "local_validation", "namespace_resolution", "fd_ofd", "local_finalization",
    "read_admission", "root_load", "marker_load", "payload_read", "revalidate",
    "encode", "call_gate_wait", "sq_credit_observe", "sq_publish",
    "cq_active_poll", "cq_sleep", "cq_detection_bound", "decode",
    "lane_scan", "sq_detection_bound", "admission", "scheduler_wait",
    "active_dispatch", "cqe_publish",
    "dispatcher_decode", "dispatcher_route", "dispatcher_encode",
    "metadata_lookup", "metadata_mutation", "metadata_lock_wait",
    "metadata_lock_hold", "metadata_record_copy",
    "lifecycle_reserve", "lifecycle_stage", "lifecycle_commit",
    "lifecycle_publish", "dependency_wait",
    "arena_lock_wait", "arena_state_clone", "arena_plan", "arena_backend_reserve",
    "arena_state_build", "arena_validate", "arena_install", "arena_state_persist",
    "persistence_enqueue", "persistence_queue_wait", "persistence_batch",
    "persistence_provider", "persistence_barrier",
    "startup_bootstrap", "startup_mapping", "startup_authority_open",
    "startup_namespace_recovery", "startup_control_open", "startup_listener_ready",
    "startup_lane_provision",
)
STAGE_COMPONENTS = (
    ("syscall_intercept",) * 2
    + ("client_semantics",) * 4
    + ("direct_metadata_read",) * 5
    + ("cxl_client_transport",) * 8
    + ("authority_progress",) * 6
    + ("dispatcher_codec",) * 3
    + ("namespace_metadata",) * 5
    + ("lifecycle_transaction",) * 5
    + ("data_arena_extent",) * 8
    + ("persistence",) * 5
    + ("startup_recovery",) * 7
)
ROLES = {1: "client-rank", 2: "server", 3: "diagnostic-client"}
MODES = {0: "off", 1: "aggregate", 2: "sampled"}

assert len(STAGES) == STAGE_COUNT
assert len(STAGE_COMPONENTS) == STAGE_COUNT


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _u64(data: bytes, offset: int) -> int:
    return struct.unpack_from("<Q", data, offset)[0]


def _canonical_json_bytes(value: dict) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def profile_digest(profile: dict) -> str:
    unsigned = dict(profile)
    unsigned.pop("profile_sha256", None)
    return hashlib.sha256(_canonical_json_bytes(unsigned)).hexdigest()


def atomic_json(path: pathlib.Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(value, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def decode_header(data: bytes) -> dict:
    if len(data) < HEADER_BYTES:
        raise ValueError("arena is shorter than the fixed observation header")
    if data[:8] != MAGIC:
        raise ValueError("arena magic mismatch")
    major = _u16(data, 8)
    minor = _u16(data, 10)
    header_bytes = _u16(data, 12)
    event_bytes = _u16(data, 14)
    if major != SCHEMA_MAJOR:
        raise ValueError("unsupported observation schema major")
    if header_bytes != HEADER_BYTES or event_bytes < EVENT_RECORD_BYTES:
        raise ValueError("unsupported observation record layout")
    if minor > SCHEMA_MINOR and event_bytes < EVENT_RECORD_BYTES:
        raise ValueError("new observation minor cannot be skipped")
    arena_bytes = _u64(data, 16)
    if arena_bytes != len(data):
        raise ValueError("arena header/file length mismatch")
    role_id = data[26]
    requested_mode = data[24]
    effective_mode = data[25]
    if role_id not in ROLES or requested_mode not in MODES or effective_mode not in MODES:
        raise ValueError("arena contains an unknown role or mode")
    schema_digest = f"{_u64(data, 416):016x}"
    if schema_digest != SCHEMA_DIGEST:
        raise ValueError("arena schema digest mismatch")
    identity = {
        "role": ROLES.get(data[IDENTITY_BASE], "unknown"),
        "endpoint_id": _u32(data, IDENTITY_BASE + 4),
        "pid": _u32(data, IDENTITY_BASE + 8),
        "mpi_rank": _u32(data, IDENTITY_BASE + 12),
        "exec_incarnation": _u64(data, IDENTITY_BASE + 16),
        "serving_generation": _u64(data, IDENTITY_BASE + 24),
    }
    header = {
        "schema_major": major,
        "schema_minor": minor,
        "schema_digest": schema_digest,
        "arena_bytes": arena_bytes,
        "requested_mode": MODES[requested_mode],
        "effective_mode": MODES[effective_mode],
        "role": ROLES[role_id],
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
        "identity": identity,
    }
    if (
        identity["role"] != header["role"]
        or identity["endpoint_id"] != header["endpoint_id"]
        or identity["pid"] != header["pid"]
        or identity["exec_incarnation"] != header["exec_incarnation"]
        or identity["serving_generation"] != header["serving_generation"]
    ):
        raise ValueError("arena header/identity table mismatch")
    return header


def decode_metric(data: bytes, base: int) -> dict:
    if base < 0 or base + METRIC_RECORD_BYTES > len(data):
        raise ValueError("metric record lies outside arena")
    bins = [
        _u16(data, base + METRIC_HEADER_BYTES + index * 2)
        for index in range(HISTOGRAM_BINS)
    ]
    return {
        "calls": _u64(data, base),
        "duration_samples": _u64(data, base + 8),
        "sampled_duration_sum_ns": _u64(data, base + 16),
        "sampled_min_ns": _u64(data, base + 24),
        "sampled_max_ns": _u64(data, base + 32),
        "objects": _u64(data, base + 40),
        "bytes": _u64(data, base + 48),
        "successes": _u64(data, base + 56),
        "errors": _u64(data, base + 64),
        "retries": _u64(data, base + 72),
        "histogram_underflow": _u64(data, base + 80),
        "histogram_overflow": _u64(data, base + 88),
        "histogram_saturation": _u64(data, base + 96),
        "histogram_bins": bins,
    }


def decode_compact_metric(data: bytes, base: int) -> dict:
    if base < 0 or base + COMPACT_METRIC_BYTES > len(data):
        raise ValueError("compact metric record lies outside arena")
    calls = _u64(data, base)
    errors = _u64(data, base + 48)
    retries = _u64(data, base + 56)
    return {
        "calls": calls,
        "duration_samples": calls,
        "sampled_duration_sum_ns": _u64(data, base + 8),
        "sampled_min_ns": _u64(data, base + 16),
        "sampled_max_ns": _u64(data, base + 24),
        "objects": _u64(data, base + 32),
        "bytes": _u64(data, base + 40),
        "successes": max(0, calls - errors - retries),
        "errors": errors,
        "retries": retries,
        "histogram_underflow": 0,
        "histogram_overflow": 0,
        "histogram_saturation": 0,
        "histogram_bins": None,
    }


def add_metric(target: dict, source: dict) -> None:
    target_had_metric = bool(target.get("calls", 0))
    for name, value in source.items():
        if name in ("sampled_min_ns", "sampled_max_ns", "histogram_bins"):
            continue
        target[name] = target.get(name, 0) + value
    source_bins = source.get("histogram_bins")
    if not target_had_metric:
        target["histogram_bins"] = (
            list(source_bins) if source_bins is not None else None
        )
    elif target.get("histogram_bins") is None or source_bins is None:
        # Compact metrics carry no histogram. Once mixed with a full metric,
        # percentiles for the combined population cannot be reconstructed.
        target["histogram_bins"] = None
    else:
        target_bins = target["histogram_bins"]
        for index, count in enumerate(source_bins):
            target_bins[index] += count
    if source["duration_samples"]:
        prior_samples = target.get("duration_samples", 0) - source["duration_samples"]
        target["sampled_min_ns"] = (
            source["sampled_min_ns"]
            if prior_samples == 0
            else min(target["sampled_min_ns"], source["sampled_min_ns"])
        )
        target["sampled_max_ns"] = max(
            target.get("sampled_max_ns", 0), source["sampled_max_ns"]
        )


def histogram_representative_ns(index: int) -> int:
    lower = HISTOGRAM_MIN_NS * HISTOGRAM_GAMMA**index
    # Rust's positive f64::round() rounds half away from zero; Python's round
    # uses ties-to-even. Keep the offline decoder compatible with the writer.
    return math.floor(lower * 1.02 + 0.5)


def metric_percentiles(metric: dict) -> dict:
    sample_count = int(metric.get("duration_samples", 0))
    result = {
        "sample_count": sample_count,
        "minimum_required": MIN_PERCENTILE_SAMPLES,
        "status": "insufficient_samples",
        "p50_ns": None,
        "p95_ns": None,
        "p99_ns": None,
    }
    if "histogram_bins" not in metric:
        result["status"] = "no_samples" if sample_count == 0 else "histogram_unavailable"
        return result
    bins = metric["histogram_bins"]
    if bins is None:
        result["status"] = "not_available_compact_metric"
        return result
    saturation = int(metric.get("histogram_saturation", 0))
    represented = (
        int(metric.get("histogram_underflow", 0))
        + int(metric.get("histogram_overflow", 0))
        + saturation
        + sum(int(value) for value in bins)
    )
    if represented != sample_count:
        result["status"] = "histogram_count_mismatch"
        return result
    if saturation:
        result["status"] = "histogram_saturated"
        return result
    if sample_count < MIN_PERCENTILE_SAMPLES:
        return result

    underflow = int(metric.get("histogram_underflow", 0))
    overflow = int(metric.get("histogram_overflow", 0))
    quantiles = (("p50_ns", 0.50), ("p95_ns", 0.95), ("p99_ns", 0.99))
    overflow_quantile = False
    for name, quantile in quantiles:
        rank = max(1, math.ceil(sample_count * quantile))
        if rank <= underflow:
            result[name] = HISTOGRAM_MIN_NS
            continue
        cursor = underflow
        for index, count in enumerate(bins):
            cursor += int(count)
            if rank <= cursor:
                result[name] = histogram_representative_ns(index)
                break
        else:
            if overflow and rank <= cursor + overflow:
                overflow_quantile = True
    result["status"] = "overflow_quantile" if overflow_quantile else "ok"
    return result


def public_metric(metric: dict) -> dict:
    value = {name: field for name, field in metric.items() if name != "histogram_bins"}
    value["percentiles"] = metric_percentiles(metric)
    return value


def decode_arena(path: pathlib.Path) -> dict:
    data = path.read_bytes()
    header = decode_header(data)
    producer_base = header["producer_base"]
    producer_stride = header["producer_stride"]
    if producer_stride < PRODUCER_CONTROL_BYTES + WORKLOAD_METRIC_BYTES:
        raise ValueError("producer stride is too small for observation-v1 metrics")
    producers = []
    matrix = {}
    semantic_matrix = {}
    stage_matrix = {}
    startup_metric = {}
    startup_stages = {}
    for slot in range(header["producer_slots"]):
        control = producer_base + slot * producer_stride
        if control + producer_stride > header["event_base"]:
            raise ValueError("producer shard overlaps the sampled event region")
        if _u64(data, control) == 0:
            continue
        producer = {
            "slot": slot,
            "thread_identity": _u64(data, control + 8),
            "trace_sequence": _u64(data, control + 16),
            "event_head": _u64(data, control + 24),
            "event_drops": _u64(data, control + 32),
        }
        producers.append(producer)
        workload = control + PRODUCER_CONTROL_BYTES
        for component_index, component in enumerate(COMPONENTS):
            for root_index, root in enumerate(ROOTS):
                metric_index = component_index * len(ROOTS) + root_index
                metric = decode_metric(
                    data, workload + metric_index * METRIC_RECORD_BYTES
                )
                if not metric["calls"]:
                    continue
                add_metric(matrix.setdefault((component, root), {}), metric)
            for semantic_index, semantic in enumerate(SEMANTICS):
                metric_index = (
                    len(COMPONENTS) * len(ROOTS)
                    + component_index * len(SEMANTICS)
                    + semantic_index
                )
                metric = decode_metric(
                    data, workload + metric_index * METRIC_RECORD_BYTES
                )
                if not metric["calls"]:
                    continue
                add_metric(
                    semantic_matrix.setdefault((component, semantic), {}), metric
                )
        stage_base = len(COMPONENTS) * (len(ROOTS) + len(SEMANTICS))
        for stage_index, stage in enumerate(STAGES):
            metric = decode_metric(
                data, workload + (stage_base + stage_index) * METRIC_RECORD_BYTES
            )
            if metric["calls"]:
                add_metric(
                    stage_matrix.setdefault((STAGE_COMPONENTS[stage_index], stage), {}),
                    metric,
                )
        startup = workload + WORKLOAD_METRIC_BYTES + DIAGNOSTIC_METRIC_BYTES
        for stage_index, stage in enumerate(STAGES):
            metric = decode_compact_metric(
                data, startup + stage_index * COMPACT_METRIC_BYTES
            )
            if metric["calls"]:
                add_metric(startup_metric, metric)
                add_metric(
                    startup_stages.setdefault(
                        (STAGE_COMPONENTS[stage_index], stage), {}
                    ),
                    metric,
                )
    histogram_mismatches = sum(
        1
        for metric in (
            list(matrix.values())
            + list(semantic_matrix.values())
            + list(stage_matrix.values())
        )
        if metric_percentiles(metric)["status"] == "histogram_count_mismatch"
    )
    invalid_fields = {
        name: header[name]
        for name in (
            "flags", "producer_overflow", "identity_overflow", "event_overflow",
            "histogram_saturation", "active_scopes", "active_requests",
            "pending_persistence", "abandoned_scopes", "initialization_error",
            "export_error",
        )
        if header[name]
    }
    if histogram_mismatches:
        invalid_fields["histogram_count_mismatch"] = histogram_mismatches
    return {
        "path": str(path),
        "sha256": hashlib.sha256(data).hexdigest(),
        "header": header,
        "producers": producers,
        "matrix": [
            {"component": component, "root": root, **metric}
            for (component, root), metric in sorted(matrix.items())
        ],
        "semantic_matrix": [
            {"component": component, "semantic": semantic, **metric}
            for (component, semantic), metric in sorted(semantic_matrix.items())
        ],
        "stage_matrix": [
            {"component": component, "stage": stage, **metric}
            for (component, stage), metric in sorted(stage_matrix.items())
        ],
        "startup_metric": startup_metric,
        "startup_stages": [
            {"component": component, "stage": stage, **metric}
            for (component, stage), metric in sorted(startup_stages.items())
        ],
        "valid": not invalid_fields,
        "invalid_fields": invalid_fields,
    }


def component_summary(decoded: list[dict], config: dict | None = None) -> dict:
    combined = {component: {} for component in COMPONENTS}
    for arena in decoded:
        for row in arena["matrix"]:
            add_metric(combined[row["component"]], {
                name: value
                for name, value in row.items()
                if name not in ("component", "root")
            })
        if arena["startup_metric"]:
            add_metric(combined["startup_recovery"], arena["startup_metric"])
    runtime_enabled = bool(decoded)
    components = []
    for component in COMPONENTS:
        instrumented = component in (
            "syscall_intercept", "client_semantics", "startup_recovery"
        )
        metric = combined[component]
        if not runtime_enabled:
            coverage = "observation_off"
        elif instrumented:
            coverage = "instrumented_at_o1"
        else:
            coverage = "not_instrumented_at_o1"
        components.append({
            "component": component,
            "coverage": coverage,
            "call_semantics": "completed_observation_scopes_not_unique_syscalls",
            "time_semantics": (
                "sampled_non_additive_parent_or_explicit_stage"
                if instrumented else "not_available"
            ),
            "metrics": public_metric(metric) if runtime_enabled and instrumented else None,
        })
    valid = all(arena["valid"] for arena in decoded)
    expected_enabled = config and config.get("mode") in ("aggregate", "sampled")
    if expected_enabled and not decoded:
        valid = False
    if config and config.get("mode") in ("default", "off") and decoded:
        valid = False
    return {
        "schema_version": "legofs.observation.component-summary.v1",
        "observation_valid": valid,
        "config": config,
        "arena_count": len(decoded),
        "components": components,
        "limitations": [
            "O1 instruments syscall roots and server startup only",
            "component calls count completed scopes; use stage metrics for stage counts",
            "sampled duration sums are not scaled into unsampled wall time",
            "parent durations are non-additive",
            "unreached components are not reported as zero cost",
        ],
    }


def analyze_observation_files(
    files: list[pathlib.Path], output_dir: pathlib.Path, *, config: dict | None = None
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    decoded = [decode_arena(pathlib.Path(path)) for path in files]
    summary = component_summary(decoded, config)
    atomic_json(output_dir / "decoded.json", {
        "schema_version": "legofs.observation.decoded.v1",
        "arenas": decoded,
    })
    atomic_json(output_dir / "component-summary.json", summary)
    with (output_dir / "operation-component-matrix.csv").open(
        "w", encoding="utf-8", newline=""
    ) as output:
        writer = csv.DictWriter(output, fieldnames=(
            "role", "endpoint", "component", "root", "calls", "duration_samples",
            "sampled_duration_sum_ns", "p50_ns", "p95_ns", "p99_ns",
            "percentile_status", "objects", "bytes", "errors", "retries",
        ))
        writer.writeheader()
        for arena in decoded:
            header = arena["header"]
            for row in arena["matrix"]:
                percentiles = metric_percentiles(row)
                writer.writerow({
                    "role": header["role"],
                    "endpoint": header["endpoint_id"],
                    "component": row["component"],
                    "root": row["root"],
                    "calls": row.get("calls", 0),
                    "duration_samples": row.get("duration_samples", 0),
                    "sampled_duration_sum_ns": row.get("sampled_duration_sum_ns", 0),
                    "p50_ns": percentiles["p50_ns"],
                    "p95_ns": percentiles["p95_ns"],
                    "p99_ns": percentiles["p99_ns"],
                    "percentile_status": percentiles["status"],
                    "objects": row.get("objects", 0),
                    "bytes": row.get("bytes", 0),
                    "errors": row.get("errors", 0),
                    "retries": row.get("retries", 0),
                })
    with (output_dir / "semantic-component-matrix.csv").open(
        "w", encoding="utf-8", newline=""
    ) as output:
        writer = csv.DictWriter(output, fieldnames=(
            "role", "endpoint", "component", "semantic", "calls",
            "duration_samples", "sampled_duration_sum_ns", "p50_ns",
            "p95_ns", "p99_ns", "percentile_status", "objects", "bytes",
            "errors", "retries",
        ))
        writer.writeheader()
        for arena in decoded:
            header = arena["header"]
            for row in arena["semantic_matrix"]:
                percentiles = metric_percentiles(row)
                writer.writerow({
                    "role": header["role"],
                    "endpoint": header["endpoint_id"],
                    "component": row["component"],
                    "semantic": row["semantic"],
                    "calls": row.get("calls", 0),
                    "duration_samples": row.get("duration_samples", 0),
                    "sampled_duration_sum_ns": row.get("sampled_duration_sum_ns", 0),
                    "p50_ns": percentiles["p50_ns"],
                    "p95_ns": percentiles["p95_ns"],
                    "p99_ns": percentiles["p99_ns"],
                    "percentile_status": percentiles["status"],
                    "objects": row.get("objects", 0),
                    "bytes": row.get("bytes", 0),
                    "errors": row.get("errors", 0),
                    "retries": row.get("retries", 0),
                })
    with (output_dir / "stage-metrics.csv").open(
        "w", encoding="utf-8", newline=""
    ) as output:
        writer = csv.DictWriter(output, fieldnames=(
            "role", "endpoint", "bank", "component", "stage", "calls",
            "duration_samples", "sampled_duration_sum_ns", "p50_ns",
            "p95_ns", "p99_ns", "percentile_status", "objects", "bytes",
            "errors", "retries",
        ))
        writer.writeheader()
        for arena in decoded:
            header = arena["header"]
            for bank, rows in (
                ("workload", arena["stage_matrix"]),
                ("startup", arena["startup_stages"]),
            ):
                for row in rows:
                    percentiles = metric_percentiles(row)
                    writer.writerow({
                        "role": header["role"],
                        "endpoint": header["endpoint_id"],
                        "bank": bank,
                        "component": row["component"],
                        "stage": row["stage"],
                        "calls": row.get("calls", 0),
                        "duration_samples": row.get("duration_samples", 0),
                        "sampled_duration_sum_ns": row.get("sampled_duration_sum_ns", 0),
                        "p50_ns": percentiles["p50_ns"],
                        "p95_ns": percentiles["p95_ns"],
                        "p99_ns": percentiles["p99_ns"],
                        "percentile_status": percentiles["status"],
                        "objects": row.get("objects", 0),
                        "bytes": row.get("bytes", 0),
                        "errors": row.get("errors", 0),
                        "retries": row.get("retries", 0),
                    })
    atomic_json(output_dir / "critical-path.json", {
        "schema_version": "legofs.observation.critical-path.v1",
        "coverage": "not_instrumented_at_o1",
        "reason": "request linkage and exclusive ledgers enter at O2/O4",
    })
    atomic_json(output_dir / "state-transitions.json", {
        "schema_version": "legofs.observation.state-transitions.v1",
        "coverage": "not_instrumented_at_o1",
        "reason": "bounded transition probes enter at O4",
    })
    report = [
        "# LegoFS observation report",
        "",
        f"Observation valid: `{str(summary['observation_valid']).lower()}`",
        f"Decoded arenas: `{len(decoded)}`",
        "",
        "O1 covers syscall roots and server startup. Transport, metadata, lifecycle, arena,",
        "persistence, causal closure and state transitions remain explicitly uninstrumented.",
        "",
    ]
    (output_dir / "report.md").write_text("\n".join(report), encoding="utf-8")
    return {
        "observation_valid": summary["observation_valid"],
        "component_summary": "observation/component-summary.json",
        "operation_component_matrix": "observation/operation-component-matrix.csv",
        "semantic_component_matrix": "observation/semantic-component-matrix.csv",
        "stage_metrics": "observation/stage-metrics.csv",
        "critical_path": "observation/critical-path.json",
        "state_transitions": "observation/state-transitions.json",
        "report": "observation/report.md",
    }


def resolve_summary(path: pathlib.Path) -> dict:
    candidates = (
        path,
        path / "component-summary.json",
        path / "observation/component-summary.json",
    )
    for candidate in candidates:
        if candidate.is_file():
            value = json.loads(candidate.read_text(encoding="utf-8"))
            if value.get("schema_version") == "legofs.observation.component-summary.v1":
                return value
    raise ValueError(f"cannot locate component-summary.json under {path}")


def compare_summaries(baseline: dict, candidate: dict) -> dict:
    def keyed(summary):
        return {item["component"]: item for item in summary["components"]}
    left = keyed(baseline)
    right = keyed(candidate)
    rows = []
    for component in COMPONENTS:
        before = (left.get(component, {}).get("metrics") or {})
        after = (right.get(component, {}).get("metrics") or {})
        rows.append({
            "component": component,
            "baseline_calls": before.get("calls"),
            "candidate_calls": after.get("calls"),
            "baseline_sampled_duration_ns": before.get("sampled_duration_sum_ns"),
            "candidate_sampled_duration_ns": after.get("sampled_duration_sum_ns"),
            "comparable": (
                left.get(component, {}).get("coverage")
                == right.get(component, {}).get("coverage")
            ),
        })
    return {
        "schema_version": "legofs.observation.compare.v1",
        "baseline_valid": baseline.get("observation_valid"),
        "candidate_valid": candidate.get("observation_valid"),
        "components": rows,
        "performance_claim": "not_evaluated_without_paired_io500_runs",
    }


def paired_metric_stats(ratios: list[float]) -> dict:
    if not ratios or any(value <= 0 for value in ratios):
        raise ValueError("paired throughput ratios must all be positive")
    median = statistics.median(ratios)
    mad = statistics.median(abs(value - median) for value in ratios)
    rmad = 1.4826 * mad / median
    return {
        "paired_ratios": ratios,
        "median_ratio": median,
        "median_regression": max(0.0, 1.0 - median),
        "mad": mad,
        "rMAD": rmad,
        "primary_improvement_threshold": max(0.05, 2.0 * rmad),
        "high_noise": rmad > 0.10,
    }


def arm_metric_stats(values: list[float]) -> dict:
    if not values or any(value <= 0 for value in values):
        raise ValueError("arm throughput values must all be positive")
    median = statistics.median(values)
    mad = statistics.median(abs(value - median) for value in values)
    return {
        "values": values,
        "median": median,
        "mad": mad,
        "rMAD": 1.4826 * mad / median,
    }


def _phase(result: dict, name: str) -> dict:
    phase = next(
        (
            item
            for item in result.get("io500", {}).get("metrics", {}).get("phases", [])
            if item.get("name") == name
        ),
        None,
    )
    if phase is None or phase.get("score", 0) <= 0 or phase.get("seconds", 0) <= 0:
        raise ValueError(f"paired phase {name!r} is missing or nonpositive")
    return phase


def _nested_number(value: dict, path: str) -> float:
    current = value
    for component in path.split("."):
        if not isinstance(current, dict) or component not in current:
            raise ValueError(f"paired input lacks numeric counter {path!r}")
        current = current[component]
    if not isinstance(current, (int, float)):
        raise ValueError(f"paired counter {path!r} is not numeric")
    return float(current)


def _delta_correlation(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < 3:
        return None
    left_mean = statistics.mean(left)
    right_mean = statistics.mean(right)
    numerator = sum(
        (left_value - left_mean) * (right_value - right_mean)
        for left_value, right_value in zip(left, right)
    )
    left_energy = sum((value - left_mean) ** 2 for value in left)
    right_energy = sum((value - right_mean) ** 2 for value in right)
    denominator = math.sqrt(left_energy * right_energy)
    return numerator / denominator if denominator else None


def _candidate_work_signature(result: dict) -> list[dict]:
    keys = (
        "open_ops", "close_ops", "read_ops", "write_ops",
        "read_bytes", "write_bytes", "unlink_ops", "rename_ops",
    )
    return sorted(
        (
            {
                "rank": summary.get("mpi_rank"),
                **{
                    name: summary.get("stats", {}).get(name, 0)
                    for name in keys
                },
                "syscalls": sorted(
                    (
                        {
                            "number": syscall.get("number"),
                            "name": syscall.get("name"),
                            "handled": syscall.get("handled", 0),
                            "rejected": syscall.get("rejected", 0),
                            "forbidden_badfs_forward": syscall.get(
                                "forbidden_badfs_forward", 0
                            ),
                        }
                        for syscall in summary.get("syscall_classification", {}).get(
                            "syscalls", []
                        )
                    ),
                    key=lambda item: (item["number"], item["name"]),
                ),
            }
            for summary in result.get("posix_path_summaries", [])
        ),
        key=lambda item: item["rank"],
    )


def _candidate_path_counters(result: dict) -> dict:
    totals = {
        "fused_open_attempts": 0,
        "fused_open_pins": 0,
        "fused_open_fallbacks": 0,
        "fused_close_attempts": 0,
        "fused_close_snapshot_releases": 0,
    }
    opcodes = {}
    for summary in result.get("posix_path_summaries", []):
        for evidence in summary.get("cxl_serving_evidence", []):
            for name in totals:
                totals[name] += evidence.get(name, 0)
            for opcode, count in evidence.get("dispatches_by_opcode", {}).items():
                opcodes[opcode] = opcodes.get(opcode, 0) + count
    totals["dispatches_by_opcode"] = dict(sorted(opcodes.items()))
    return totals


def _candidate_platform_fingerprint(result: dict) -> dict:
    artifacts = result.get("build", {}).get("manifest", {}).get("artifacts", {})
    names = (
        "cxlmemsim_server", "io500", "linux", "opensbi", "qemu", "u_boot",
    )
    fingerprint = {}
    for name in names:
        digest = artifacts.get(name, {}).get("sha256")
        if not digest:
            raise ValueError(f"paired input lacks {name} digest")
        fingerprint[name] = digest
    return fingerprint


def _candidate_payload_digest(result: dict) -> str:
    try:
        digest = result["build"]["manifest"]["artifacts"]["payload"]["sha256"]
    except (KeyError, TypeError) as error:
        raise ValueError("paired input lacks a payload digest") from error
    if not digest:
        raise ValueError("paired input has an empty payload digest")
    return digest


def _audit_candidate_result(result: dict) -> dict:
    if (
        result.get("status") != "passed"
        or result.get("filesystem_valid") is not True
        or result.get("observation_valid") is not True
        or result.get("guest_visible_cxl_evidence") is not True
    ):
        raise ValueError("paired input is not a passed CXL filesystem result")
    if not _post_ready_paths_are_zero(result):
        raise ValueError("paired input contains post-ready fallback traffic")

    coherence = result.get("coherence_final_stats", {})
    for name in (
        "timeouts", "protocol_errors", "delivery_failures", "active_bindings",
    ):
        if coherence.get(name) != 0:
            raise ValueError(f"paired input has nonzero coherence {name}")
    if coherence.get("request_fence", 0) <= 0 or (
        coherence.get("request_fence")
        != coherence.get("persistence_fence_completions")
    ):
        raise ValueError("paired input lacks matched persistence fences")

    lifecycle = result.get("lifecycle_inspection", {})
    audit = lifecycle.get("audit", {})
    for name in ("pending_operations", "pending_arena_slots", "active_read_leases"):
        if audit.get(name) != 0:
            raise ValueError(f"paired input has nonzero lifecycle {name}")
    visibility = audit.get("visibility_durability", {})
    if visibility.get("published_v") != visibility.get("durable_d"):
        raise ValueError("paired input does not close V=D")
    direct_items = audit.get("direct_write_commit_items", 0)
    if direct_items and audit.get("writer_persisted_direct_items", 0) < direct_items:
        raise ValueError("paired input lacks direct-write persistence evidence")
    if result.get("cleanup", {}).get("owned_processes_remaining"):
        raise ValueError("paired input leaves owned processes running")

    normalized_topology = dict(result.get("topology", {}))
    normalized_topology.pop("legofs_open_mode", None)
    normalized_topology.pop("legofs_direct_metadata_mode", None)
    return {
        "stage": result.get("stage"),
        "topology": normalized_topology,
        "platform": _candidate_platform_fingerprint(result),
        "payload_sha256": _candidate_payload_digest(result),
        "work": _candidate_work_signature(result),
        "commands": result.get("legofs_timing", {}).get(
            "transport_authority", {}
        ).get("sqe_consumed"),
        "path_counters": _candidate_path_counters(result),
        "functional_model_only": result.get("functional_model_only"),
        "physical_hardware_evidence": result.get("physical_hardware_evidence"),
    }


def _paired_phase(
    pairs: list[dict],
    phase_name: str,
    *,
    explanatory_counter: str | None = None,
    protected_regression_limit: float = 0.03,
    race_rmad_limit: float = 0.10,
    sign_flip_is_race: bool = True,
) -> dict:
    ratios = []
    baseline_scores = []
    candidate_scores = []
    values = []
    units = set()
    elapsed_deltas = []
    counter_deltas = []
    residual_ratios = []
    for pair in pairs:
        baseline = _phase(pair["baseline"], phase_name)
        candidate = _phase(pair["candidate"], phase_name)
        units.update((baseline["unit"], candidate["unit"]))
        ratio = candidate["score"] / baseline["score"]
        ratios.append(ratio)
        baseline_scores.append(baseline["score"])
        candidate_scores.append(candidate["score"])
        row = {
            "pair": pair["pair"],
            "baseline_score": baseline["score"],
            "candidate_score": candidate["score"],
            "baseline_elapsed_s": baseline["seconds"],
            "candidate_elapsed_s": candidate["seconds"],
            "ratio": ratio,
        }
        if explanatory_counter is not None:
            baseline_counter_s = (
                _nested_number(pair["baseline"], explanatory_counter) / 1e9
            )
            candidate_counter_s = (
                _nested_number(pair["candidate"], explanatory_counter) / 1e9
            )
            baseline_residual_s = baseline["seconds"] - baseline_counter_s
            candidate_residual_s = candidate["seconds"] - candidate_counter_s
            if baseline_residual_s <= 0 or candidate_residual_s <= 0:
                raise ValueError(
                    f"counter {explanatory_counter!r} consumes all of {phase_name}"
                )
            elapsed_deltas.append(candidate["seconds"] - baseline["seconds"])
            counter_deltas.append(candidate_counter_s - baseline_counter_s)
            residual_ratio = baseline_residual_s / candidate_residual_s
            residual_ratios.append(residual_ratio)
            row.update({
                "baseline_explanatory_counter_s": baseline_counter_s,
                "candidate_explanatory_counter_s": candidate_counter_s,
                "baseline_residual_elapsed_s": baseline_residual_s,
                "candidate_residual_elapsed_s": candidate_residual_s,
                "residual_throughput_ratio": residual_ratio,
            })
        values.append(row)
    if len(units) != 1:
        raise ValueError(f"paired phase {phase_name!r} unit mismatch")

    stats = paired_metric_stats(ratios)
    baseline_arm = arm_metric_stats(baseline_scores)
    candidate_arm = arm_metric_stats(candidate_scores)
    paired_log_median_improvement = math.exp(
        statistics.median(math.log(value) for value in ratios)
    ) - 1.0
    sign_flip = any(value > 1.0 for value in ratios) and any(
        value < 1.0 for value in ratios
    )
    raw_race_signal = stats["rMAD"] > race_rmad_limit or (
        sign_flip_is_race and sign_flip
    )
    explanation = None
    explanation_valid = False
    if explanatory_counter is not None:
        residual_stats = paired_metric_stats(residual_ratios)
        correlation = _delta_correlation(elapsed_deltas, counter_deltas)
        explanation_valid = (
            correlation is not None
            and correlation >= 0.90
            and residual_stats["rMAD"] <= race_rmad_limit
            and residual_stats["median_regression"] <= protected_regression_limit
        )
        explanation = {
            "counter": explanatory_counter,
            "elapsed_delta_correlation": correlation,
            "residual": residual_stats,
            "valid": explanation_valid,
        }
    return {
        "phase": phase_name,
        "unit": units.pop(),
        **stats,
        "median_improvement": paired_log_median_improvement,
        "positive_pair_count": sum(value > 1.0 for value in ratios),
        "baseline_arm": baseline_arm,
        "candidate_arm": candidate_arm,
        "sign_flip": sign_flip,
        "sign_flip_is_race": sign_flip_is_race,
        "raw_race_signal": raw_race_signal,
        "high_noise": stats["rMAD"] > race_rmad_limit,
        "race_explained": raw_race_signal and explanation_valid,
        "unexplained_race": raw_race_signal and not explanation_valid,
        "protected_median_pass": (
            stats["median_regression"] <= protected_regression_limit
        ),
        "baseline_median_score": statistics.median(
            value["baseline_score"] for value in values
        ),
        "candidate_median_score": statistics.median(
            value["candidate_score"] for value in values
        ),
        "baseline_median_elapsed_s": statistics.median(
            value["baseline_elapsed_s"] for value in values
        ),
        "candidate_median_elapsed_s": statistics.median(
            value["candidate_elapsed_s"] for value in values
        ),
        "explanation": explanation,
        "pairs": values,
    }


def paired_candidate_gate(
    baseline_paths: list[pathlib.Path],
    candidate_paths: list[pathlib.Path],
    *,
    primary_phase: str,
    protected_phases: list[str],
    explanatory_counters: dict[str, str] | None = None,
    minimum_pairs: int = 5,
    confirmation_pairs: int = 3,
    minimum_primary_improvement: float = 0.05,
    protected_regression_limit: float = 0.03,
    race_rmad_limit: float = 0.10,
    minimum_command_reduction: int = 0,
) -> dict:
    if len(baseline_paths) != len(candidate_paths):
        raise ValueError("baseline/candidate paired input counts differ")
    if not baseline_paths:
        raise ValueError("candidate gate requires at least one pair")
    explanatory_counters = explanatory_counters or {}
    pairs = []
    baseline_payloads = set()
    candidate_payloads = set()
    platform_fingerprints = set()
    normalized_topologies = set()
    stages = set()
    command_reductions = []
    for index, (baseline_path, candidate_path) in enumerate(
        zip(baseline_paths, candidate_paths), 1
    ):
        baseline = resolve_io500_result(baseline_path)
        candidate = resolve_io500_result(candidate_path)
        baseline_audit = _audit_candidate_result(baseline)
        candidate_audit = _audit_candidate_result(candidate)
        if baseline_audit["work"] != candidate_audit["work"]:
            raise ValueError(f"pair {index} does not execute identical work")
        if not baseline_audit["work"]:
            raise ValueError(f"pair {index} lacks per-rank work evidence")
        if not isinstance(baseline_audit["commands"], int) or not isinstance(
            candidate_audit["commands"], int
        ):
            raise ValueError(f"pair {index} lacks command counts")
        reduction = baseline_audit["commands"] - candidate_audit["commands"]
        command_reductions.append(reduction)
        baseline_payloads.add(baseline_audit["payload_sha256"])
        candidate_payloads.add(candidate_audit["payload_sha256"])
        for audit in (baseline_audit, candidate_audit):
            platform_fingerprints.add(json.dumps(audit["platform"], sort_keys=True))
            normalized_topologies.add(json.dumps(audit["topology"], sort_keys=True))
            stages.add(audit["stage"])
        pairs.append({
            "pair": index,
            "baseline": baseline,
            "candidate": candidate,
            "baseline_audit": baseline_audit,
            "candidate_audit": candidate_audit,
            "command_reduction": reduction,
        })
    if len(baseline_payloads) != 1 or len(candidate_payloads) != 1:
        raise ValueError("each side of paired inputs must use one frozen payload")
    if len(platform_fingerprints) != 1:
        raise ValueError("paired inputs do not use one frozen platform")
    if len(normalized_topologies) != 1 or len(stages) != 1:
        raise ValueError("paired inputs do not use one normalized topology and stage")

    primary = _paired_phase(
        pairs,
        primary_phase,
        race_rmad_limit=race_rmad_limit,
    )
    primary_threshold = max(
        minimum_primary_improvement,
        2.0 * max(
            primary["baseline_arm"]["rMAD"],
            primary["candidate_arm"]["rMAD"],
        ),
    )
    primary["acceptance_threshold"] = primary_threshold
    primary["acceptance_pass"] = (
        primary["median_improvement"] > primary_threshold
    )
    protected = [
        _paired_phase(
            pairs,
            phase,
            explanatory_counter=explanatory_counters.get(phase),
            protected_regression_limit=protected_regression_limit,
            race_rmad_limit=race_rmad_limit,
            sign_flip_is_race=False,
        )
        for phase in protected_phases
    ]

    mechanism_pass = all(
        value >= minimum_command_reduction for value in command_reductions
    )
    protected_pass = all(item["protected_median_pass"] for item in protected)
    unexplained_race = primary["unexplained_race"] or any(
        item["unexplained_race"] for item in protected
    )
    if len(pairs) >= confirmation_pairs and unexplained_race:
        classification = "INCONCLUSIVE_RACE"
    elif not primary["acceptance_pass"]:
        classification = "REJECTED_NO_SIGNAL"
    elif not mechanism_pass:
        classification = "REJECTED_MECHANISM_NOT_REACHED"
    elif len(pairs) < confirmation_pairs:
        classification = (
            "PROMISING" if protected_pass else "PROMISING_PROTECTED_RISK"
        )
    elif not protected_pass:
        classification = "REJECTED_PROTECTED_REGRESSION"
    elif len(pairs) >= minimum_pairs and primary["positive_pair_count"] < 4:
        classification = "REJECTED_NO_SIGNAL"
    elif len(pairs) >= minimum_pairs:
        classification = "ACCEPTED"
    else:
        classification = "READY_FOR_CONFIRMATION"

    first = pairs[0]
    return {
        "schema_version": "legofs.performance.paired-candidate-gate.v1",
        "classification": classification,
        "pair_count": len(pairs),
        "minimum_pairs": minimum_pairs,
        "confirmation_pairs": confirmation_pairs,
        "minimum_primary_improvement": minimum_primary_improvement,
        "protected_regression_limit": protected_regression_limit,
        "race_rMAD_limit": race_rmad_limit,
        "stage": next(iter(stages)),
        "baseline_payload_sha256": next(iter(baseline_payloads)),
        "candidate_payload_sha256": next(iter(candidate_payloads)),
        "platform": json.loads(next(iter(platform_fingerprints))),
        "primary": primary,
        "protected": protected,
        "mechanism": {
            "minimum_command_reduction": minimum_command_reduction,
            "paired_command_reductions": command_reductions,
            "pass": mechanism_pass,
            "baseline_path_counters": first["baseline_audit"]["path_counters"],
            "candidate_path_counters": first["candidate_audit"]["path_counters"],
        },
        "evidence_scope": {
            "functional_model_only": all(
                pair["baseline_audit"]["functional_model_only"] is True
                and pair["candidate_audit"]["functional_model_only"] is True
                for pair in pairs
            ),
            "physical_hardware_evidence": all(
                pair["baseline_audit"]["physical_hardware_evidence"] is True
                and pair["candidate_audit"]["physical_hardware_evidence"] is True
                for pair in pairs
            ),
        },
        "performance_claim": (
            "accepted" if classification == "ACCEPTED" else "not_accepted"
        ),
    }


def resolve_io500_result(path: pathlib.Path) -> dict:
    candidate = path / "result.json" if path.is_dir() else path
    value = json.loads(candidate.read_text(encoding="utf-8"))
    if value.get("schema_version") != "legofs.riscv.io500.v2":
        raise ValueError(f"unsupported IO500 result schema: {candidate}")
    return value


def _post_ready_paths_are_zero(result: dict) -> bool:
    fields = (
        "filesystem_tcp_requests_after_cxl_ready",
        "legacy_tarpc_calls_after_cxl_ready",
        "blob_tcp_bytes_after_cxl_ready",
        "transport_fallbacks_after_cxl_ready",
        "unsupported_serving_calls_after_cxl_ready",
    )
    evidence = [
        item
        for summary in result.get("posix_path_summaries", [])
        for item in summary.get("cxl_serving_evidence", [])
    ]
    return bool(evidence) and all(item.get(name) == 0 for item in evidence for name in fields)


def _audit_paired_result(result: dict, expected_mode: str) -> str:
    if (
        result.get("status") != "passed"
        or result.get("filesystem_valid") is not True
        or result.get("observation_valid") is not True
    ):
        raise ValueError("paired input is not a passed filesystem/observation result")
    config = result.get("observation_config", {})
    if config.get("mode") != expected_mode:
        raise ValueError("paired input observation mode mismatch")
    files = result.get("observation", {}).get("files", [])
    expected_files = 3 if expected_mode == "aggregate" else 0
    if len(files) != expected_files:
        raise ValueError("paired input observation arena count mismatch")
    for record in files:
        header = record.get("header", {})
        if not (
            header.get("runtime_valid") is True
            and header.get("snapshot_complete") is True
            and header.get("flags") == 0
        ):
            raise ValueError("paired input contains an invalid observation arena")
    if not _post_ready_paths_are_zero(result):
        raise ValueError("paired input contains post-ready fallback traffic")
    try:
        return result["build"]["manifest"]["artifacts"]["payload"]["sha256"]
    except (KeyError, TypeError) as error:
        raise ValueError("paired input lacks a payload digest") from error


def paired_io500_gate(
    off_paths: list[pathlib.Path],
    aggregate_paths: list[pathlib.Path],
    default_paths: list[pathlib.Path],
    *,
    minimum_pairs: int = 5,
    protected_regression_limit: float = 0.03,
) -> dict:
    if not (len(off_paths) == len(aggregate_paths) == len(default_paths)):
        raise ValueError("off/aggregate/default paired input counts differ")
    triples = []
    payload_digests = set()
    for index, paths in enumerate(zip(off_paths, aggregate_paths, default_paths), 1):
        modes = {}
        for mode, path in zip(("off", "aggregate", "default"), paths):
            result = resolve_io500_result(path)
            payload_digests.add(_audit_paired_result(result, mode))
            modes[mode] = result
        triples.append({"pair": index, "modes": modes})
    if len(payload_digests) != 1:
        raise ValueError("paired inputs do not use one frozen payload")
    if not triples:
        raise ValueError("paired gate requires at least one triple")

    phase_order = [
        phase["name"]
        for phase in triples[0]["modes"]["off"]["io500"]["metrics"]["phases"]
    ]
    comparisons = {}
    for candidate_mode in ("aggregate", "default"):
        phases = []
        for phase_name in phase_order:
            ratios = []
            pair_values = []
            units = set()
            for triple in triples:
                selected = {}
                for mode in ("off", candidate_mode):
                    phase = next(
                        (
                            item
                            for item in triple["modes"][mode]["io500"]["metrics"]["phases"]
                            if item["name"] == phase_name
                        ),
                        None,
                    )
                    if phase is None or phase.get("score", 0) <= 0:
                        raise ValueError("paired phase is missing or has a nonpositive score")
                    units.add(phase["unit"])
                    selected[mode] = phase
                ratio = selected[candidate_mode]["score"] / selected["off"]["score"]
                ratios.append(ratio)
                pair_values.append({
                    "pair": triple["pair"],
                    "off_score": selected["off"]["score"],
                    "candidate_score": selected[candidate_mode]["score"],
                    "off_elapsed_s": selected["off"]["seconds"],
                    "candidate_elapsed_s": selected[candidate_mode]["seconds"],
                    "ratio": ratio,
                })
            if len(units) != 1:
                raise ValueError("paired phase unit mismatch")
            stats = paired_metric_stats(ratios)
            phases.append({
                "phase": phase_name,
                "unit": units.pop(),
                **stats,
                "protected_median_pass": (
                    stats["median_regression"] <= protected_regression_limit
                ),
                "off_median_score": statistics.median(
                    value["off_score"] for value in pair_values
                ),
                "candidate_median_score": statistics.median(
                    value["candidate_score"] for value in pair_values
                ),
                "off_median_elapsed_s": statistics.median(
                    value["off_elapsed_s"] for value in pair_values
                ),
                "candidate_median_elapsed_s": statistics.median(
                    value["candidate_elapsed_s"] for value in pair_values
                ),
                "pairs": pair_values,
            })
        comparisons[f"{candidate_mode}_vs_off"] = {
            "candidate_mode": candidate_mode,
            "phases": phases,
            "protected_failures": [
                item["phase"] for item in phases if not item["protected_median_pass"]
            ],
            "high_noise_phases": [item["phase"] for item in phases if item["high_noise"]],
        }

    control = comparisons["default_vs_off"]
    candidate = comparisons["aggregate_vs_off"]
    if len(triples) < minimum_pairs:
        classification = "insufficient_pairs"
    elif control["protected_failures"] or control["high_noise_phases"]:
        classification = "inconclusive_control_or_noise"
    elif candidate["high_noise_phases"]:
        classification = "inconclusive_high_noise"
    elif candidate["protected_failures"]:
        classification = "failed_protected_regression"
    else:
        classification = "passed"
    return {
        "schema_version": "legofs.observation.paired-io500-gate.v1",
        "classification": classification,
        "pair_count": len(triples),
        "minimum_pairs": minimum_pairs,
        "protected_regression_limit": protected_regression_limit,
        "payload_sha256": next(iter(payload_digests)),
        "comparisons": comparisons,
        "performance_claim": (
            "accepted" if classification == "passed" else "not_accepted"
        ),
    }


def freeze_profile(args) -> dict:
    profile = {
        "schema_version": "legofs.observation.profile.v1",
        "name": args.name,
        "mode": args.mode,
        "sample_shift": args.sample_shift,
        "arena_mib": args.arena_mib,
        "producer_slots": args.producer_slots,
        "histogram": {
            "bins": HISTOGRAM_BINS,
            "minimum_ns": 100,
            "gamma": "1.02/0.98",
            "relative_error_bound": 0.02,
        },
        "idle_sample_stride": args.idle_sample_stride,
        "observation_schema": {
            "major": SCHEMA_MAJOR,
            "minor": SCHEMA_MINOR,
            "event_record_bytes": EVENT_RECORD_BYTES,
            "digest": SCHEMA_DIGEST,
        },
    }
    profile["profile_sha256"] = profile_digest(profile)
    atomic_json(pathlib.Path(args.output), profile)
    return profile


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    decode = subparsers.add_parser("decode")
    decode.add_argument("arenas", nargs="+", type=pathlib.Path)
    decode.add_argument("--output-dir", type=pathlib.Path, required=True)

    validate = subparsers.add_parser("validate")
    validate.add_argument("arenas", nargs="+", type=pathlib.Path)

    compare = subparsers.add_parser("compare")
    compare.add_argument("baseline", type=pathlib.Path)
    compare.add_argument("candidate", type=pathlib.Path)
    compare.add_argument("--output", type=pathlib.Path)

    paired = subparsers.add_parser("paired-gate")
    paired.add_argument("--off", nargs="+", required=True, type=pathlib.Path)
    paired.add_argument("--aggregate", nargs="+", required=True, type=pathlib.Path)
    paired.add_argument("--default", nargs="+", required=True, type=pathlib.Path)
    paired.add_argument("--output", required=True, type=pathlib.Path)
    paired.add_argument("--minimum-pairs", type=int, default=5)
    paired.add_argument("--protected-regression-limit", type=float, default=0.03)

    candidate = subparsers.add_parser("candidate-gate")
    candidate.add_argument(
        "--baseline", nargs="+", required=True, type=pathlib.Path
    )
    candidate.add_argument(
        "--candidate", nargs="+", required=True, type=pathlib.Path
    )
    candidate.add_argument("--primary-phase", required=True)
    candidate.add_argument(
        "--protected-phase", action="append", default=[]
    )
    candidate.add_argument(
        "--protected-elapsed-counter", action="append", default=[]
    )
    candidate.add_argument("--output", required=True, type=pathlib.Path)
    candidate.add_argument("--minimum-pairs", type=int, default=5)
    candidate.add_argument("--confirmation-pairs", type=int, default=3)
    candidate.add_argument(
        "--minimum-primary-improvement", type=float, default=0.05
    )
    candidate.add_argument(
        "--protected-regression-limit", type=float, default=0.03
    )
    candidate.add_argument("--race-rmad-limit", type=float, default=0.10)
    candidate.add_argument("--minimum-command-reduction", type=int, default=0)

    freeze = subparsers.add_parser("freeze-profile")
    freeze.add_argument("--output", required=True)
    freeze.add_argument("--name", default="o1-aggregate-v1")
    freeze.add_argument("--mode", choices=("off", "aggregate", "sampled"), default="aggregate")
    freeze.add_argument("--sample-shift", type=int, required=True)
    freeze.add_argument("--arena-mib", type=int, default=12)
    freeze.add_argument("--producer-slots", type=int, default=8)
    freeze.add_argument("--idle-sample-stride", type=int, default=1024)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "decode":
            result = analyze_observation_files(args.arenas, args.output_dir)
        elif args.command == "validate":
            decoded = [decode_arena(path) for path in args.arenas]
            result = {
                "observation_valid": all(item["valid"] for item in decoded),
                "arenas": decoded,
            }
        elif args.command == "compare":
            result = compare_summaries(
                resolve_summary(args.baseline), resolve_summary(args.candidate)
            )
            if args.output is not None:
                atomic_json(args.output, result)
        elif args.command == "paired-gate":
            if args.minimum_pairs <= 0:
                raise ValueError("minimum-pairs must be positive")
            if not 0 <= args.protected_regression_limit < 1:
                raise ValueError("protected-regression-limit must be in [0,1)")
            result = paired_io500_gate(
                args.off,
                args.aggregate,
                args.default,
                minimum_pairs=args.minimum_pairs,
                protected_regression_limit=args.protected_regression_limit,
            )
            atomic_json(args.output, result)
        elif args.command == "candidate-gate":
            if args.minimum_pairs <= 0 or args.confirmation_pairs <= 0:
                raise ValueError("pair thresholds must be positive")
            if args.confirmation_pairs > args.minimum_pairs:
                raise ValueError("confirmation-pairs cannot exceed minimum-pairs")
            for name, value in (
                ("minimum-primary-improvement", args.minimum_primary_improvement),
                ("protected-regression-limit", args.protected_regression_limit),
                ("race-rmad-limit", args.race_rmad_limit),
            ):
                if not 0 <= value < 1:
                    raise ValueError(f"{name} must be in [0,1)")
            explanations = {}
            for item in args.protected_elapsed_counter:
                if "=" not in item:
                    raise ValueError(
                        "protected-elapsed-counter must be PHASE=COUNTER.PATH"
                    )
                phase, counter = item.split("=", 1)
                if not phase or not counter or phase in explanations:
                    raise ValueError("invalid or duplicate protected counter")
                explanations[phase] = counter
            unknown = set(explanations) - set(args.protected_phase)
            if unknown:
                raise ValueError(
                    "protected counter names a non-protected phase: "
                    + ", ".join(sorted(unknown))
                )
            result = paired_candidate_gate(
                args.baseline,
                args.candidate,
                primary_phase=args.primary_phase,
                protected_phases=args.protected_phase,
                explanatory_counters=explanations,
                minimum_pairs=args.minimum_pairs,
                confirmation_pairs=args.confirmation_pairs,
                minimum_primary_improvement=args.minimum_primary_improvement,
                protected_regression_limit=args.protected_regression_limit,
                race_rmad_limit=args.race_rmad_limit,
                minimum_command_reduction=args.minimum_command_reduction,
            )
            atomic_json(args.output, result)
        else:
            if not 0 <= args.sample_shift <= 20:
                raise ValueError("sample-shift must be in 0..20")
            if not 8 <= args.arena_mib <= 64:
                raise ValueError("arena-mib must be in 8..64")
            if not 1 <= args.producer_slots <= 8:
                raise ValueError("producer-slots must be in 1..8")
            if args.idle_sample_stride <= 0:
                raise ValueError("idle-sample-stride must be positive")
            result = freeze_profile(args)
        print(json.dumps(result, sort_keys=True))
        if args.command == "validate" and not result["observation_valid"]:
            return 1
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
