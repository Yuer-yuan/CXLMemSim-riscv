#!/usr/bin/env python3
"""Build and validate fail-closed evidence for the RDWO product path."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import pathlib
import uuid


SCHEMA_VERSION = "legofs.rdwo-candidate-evidence.v1"
SECTION_NAMES = (
    "engine_build_attestation",
    "process_terminals",
    "network_closure",
    "response_and_final_drain",
    "qemu_bi_topology",
    "artifact_completion",
)
CANDIDATE_ARTIFACTS = (
    "rdwo_client",
    "rdwo_server",
    "rdwo_host_agent",
    "rdwo_intercept",
    "product_engine_manifest",
    "product_capability_manifest",
)
BANNED_LEGACY_KEYS = {
    "posix_path_summaries",
    "lifecycle_inspection",
    "legofs_timing",
    "legacy_lifecycle_inspector",
    "destructor_summary",
    "badfs_bench_timing",
}
BANNED_LEGACY_VALUE_FRAGMENTS = (
    "badfs-bench",
    "legacy lifecycle inspector",
    "destructor summary",
)
HEX_DIGEST_LENGTH = 64


class CandidateEvidenceError(ValueError):
    """The candidate evidence document is incomplete or contradictory."""


def canonical_digest(value: dict) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def unsupported(reason: str, missing_evidence: list[str]) -> dict:
    if not reason or not missing_evidence:
        raise CandidateEvidenceError(
            "not_yet_supported sections require a reason and missing evidence"
        )
    return {
        "status": "not_yet_supported",
        "reason": reason,
        "missing_evidence": list(missing_evidence),
    }


def _artifact_record(manifest: dict, name: str) -> dict:
    try:
        record = manifest["artifacts"][name]
        path = record["path"]
        size = record["size"]
        digest = record["sha256"]
    except (KeyError, TypeError) as error:
        raise CandidateEvidenceError(
            f"candidate build manifest lacks artifact: {name}"
        ) from error
    if not isinstance(path, str) or not path:
        raise CandidateEvidenceError(f"candidate artifact has invalid path: {name}")
    if not isinstance(size, int) or size <= 0:
        raise CandidateEvidenceError(f"candidate artifact has invalid size: {name}")
    if (
        not isinstance(digest, str)
        or len(digest) != HEX_DIGEST_LENGTH
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise CandidateEvidenceError(f"candidate artifact has invalid digest: {name}")
    return {"path": path, "size": size, "sha256": digest}


def _guest_declarations(
    run_uuid: str,
    server_count: int,
    client_count: int,
) -> list[dict]:
    declarations = []
    endpoint = 0
    for role, count in (("server", server_count), ("client", client_count)):
        for index in range(count):
            guest_uuid = str(uuid.uuid5(uuid.UUID(run_uuid), f"{role}:{index}"))
            declarations.append(
                {
                    "role": role,
                    "index": index,
                    "guest_uuid": guest_uuid,
                    "independent_address_space": f"qemu-process:{guest_uuid}",
                    "endpoint_host_id": endpoint,
                    "region_generation": None,
                    "launch_state": "required",
                }
            )
            endpoint += 1
    observer_uuid = str(uuid.uuid5(uuid.UUID(run_uuid), "cold-observer:0"))
    declarations.append(
        {
            "role": "cold-observer",
            "index": 0,
            "guest_uuid": observer_uuid,
            "independent_address_space": f"qemu-process:{observer_uuid}",
            "endpoint_host_id": endpoint,
            "region_generation": None,
            "launch_state": "predeclared-not-launched",
        }
    )
    return declarations


def create_skeleton(
    *,
    run_id: str,
    stage: str,
    owner_token: str,
    build_manifest: dict,
    evaluation_manifest: dict | None,
    server_count: int,
    client_count: int,
    qemu_machine: str,
    shared_region_bytes: int,
) -> dict:
    """Create V0 evidence without inferring product-path success from topology."""
    run_uuid = str(uuid.uuid4())
    artifacts = {
        name: _artifact_record(build_manifest, name)
        for name in CANDIDATE_ARTIFACTS
    }
    build_identity = copy.deepcopy(build_manifest)
    build_identity.pop("artifacts", None)
    sections = {
        "engine_build_attestation": {
            "status": "complete",
            "build_identity_digest": canonical_digest(build_identity),
            "artifacts": artifacts,
            "product_engine_manifest_digest": artifacts[
                "product_engine_manifest"
            ]["sha256"],
            "product_capability_manifest_digest": artifacts[
                "product_capability_manifest"
            ]["sha256"],
        },
        "process_terminals": unsupported(
            "candidate process-terminal ABI is introduced in V1",
            ["root_operation_conservation", "per_process_terminal_records"],
        ),
        "network_closure": unsupported(
            "candidate socket and route accounting is introduced in V1",
            ["filesystem_socket_ledger", "route_allowlist_digest"],
        ),
        "response_and_final_drain": unsupported(
            "candidate response and durability seals are introduced in V1",
            ["response_seal", "fully_drained_seal", "phase_terminal"],
        ),
        "qemu_bi_topology": {
            "status": "not_yet_supported",
            "reason": (
                "guest identities are predeclared, but region generations and the "
                "cold-observer launch receipt are not yet emitted"
            ),
            "missing_evidence": [
                "per_guest_region_generation",
                "cold_observer_launch_receipt",
            ],
            "qemu_machine": qemu_machine,
            "hdm_db_bi_required": True,
            "shared_region_bytes": shared_region_bytes,
            "guest_declarations": _guest_declarations(
                run_uuid,
                server_count,
                client_count,
            ),
        },
        "artifact_completion": unsupported(
            "candidate phase artifacts cannot close before V1 terminal records exist",
            ["phase_artifact_digest", "terminal_evidence_digest"],
        ),
    }
    document = {
        "schema_version": SCHEMA_VERSION,
        "filesystem_mode": "rdwo-candidate",
        "status": "not_yet_supported",
        "run_id": run_id,
        "run_uuid": run_uuid,
        "owner_token": owner_token,
        "stage": stage,
        "functional_model_only": True,
        "physical_hardware_evidence": False,
        "official_candidate": False,
        "evaluation_manifest_digest": (
            evaluation_manifest.get("manifest_digest")
            if evaluation_manifest is not None
            else None
        ),
        "sections": sections,
    }
    document["evidence_digest"] = canonical_digest(document)
    validate(document)
    return document


def _walk(value, path: tuple[str, ...] = ()):
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = path + (str(key),)
            yield child_path, child
            yield from _walk(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_path = path + (str(index),)
            yield child_path, child
            yield from _walk(child, child_path)


def _validate_unsupported(name: str, section: dict) -> None:
    reason = section.get("reason")
    missing = section.get("missing_evidence")
    if not isinstance(reason, str) or not reason:
        raise CandidateEvidenceError(f"{name} lacks not_yet_supported reason")
    if not isinstance(missing, list) or not missing or not all(
        isinstance(item, str) and item for item in missing
    ):
        raise CandidateEvidenceError(f"{name} lacks explicit missing evidence")


def _is_digest(value) -> bool:
    return (
        isinstance(value, str)
        and len(value) == HEX_DIGEST_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_process_terminals(section: dict) -> None:
    if section.get("status") != "complete":
        return
    records = section.get("records")
    expected = section.get("expected_processes")
    if not isinstance(records, list) or not records or expected != len(records):
        raise CandidateEvidenceError("complete process terminals lack exact records")
    identities = []
    for record in records:
        if not isinstance(record, dict):
            raise CandidateEvidenceError("candidate process terminal record is invalid")
        identity = (record.get("process_uuid"), record.get("exec_incarnation"))
        if not all(isinstance(item, str) and item for item in identity):
            raise CandidateEvidenceError("candidate process terminal lacks identity")
        if record.get("terminal") not in {"completed", "failed", "cancelled"}:
            raise CandidateEvidenceError("candidate process terminal is unclosed")
        if not _is_digest(record.get("terminal_digest")):
            raise CandidateEvidenceError("candidate process terminal lacks a digest")
        identities.append(identity)
    if len(set(identities)) != len(identities):
        raise CandidateEvidenceError("candidate process terminal identity is reused")
    issued = section.get("root_operations_issued")
    terminal = section.get("root_operations_terminal")
    if not isinstance(issued, int) or issued < 0 or terminal != issued:
        raise CandidateEvidenceError("candidate RootOperation conservation is open")


def _validate_network_closure(section: dict) -> None:
    if section.get("status") != "complete":
        return
    for field in (
        "filesystem_socket_calls",
        "filesystem_tcp_routes",
        "unknown_semantic_routes",
    ):
        if section.get(field) != 0:
            raise CandidateEvidenceError("candidate network closure has a nonzero route")
    if section.get("filesystem_network_namespace_enforced") is not True:
        raise CandidateEvidenceError("candidate filesystem network namespace is not closed")
    if section.get("functional_model_transport_classified") is not True:
        raise CandidateEvidenceError("candidate functional-model transport is unclassified")
    if not _is_digest(section.get("route_allowlist_digest")):
        raise CandidateEvidenceError("candidate route allowlist is not digest closed")


def _validate_response_and_drain(section: dict) -> None:
    if section.get("status") != "complete":
        return
    admitted = section.get("requests_admitted")
    completed = section.get("responses_completed")
    if not isinstance(admitted, int) or admitted < 0 or completed != admitted:
        raise CandidateEvidenceError("candidate response conservation is open")
    if section.get("final_drain_complete") is not True:
        raise CandidateEvidenceError("candidate final drain is incomplete")
    for field in (
        "response_seal_digest",
        "fully_drained_seal_digest",
        "phase_terminal_digest",
    ):
        if not _is_digest(section.get(field)):
            raise CandidateEvidenceError(f"candidate {field} is not digest closed")


def _validate_topology(section: dict) -> None:
    declarations = section.get("guest_declarations")
    if not isinstance(declarations, list) or not declarations:
        raise CandidateEvidenceError("candidate topology lacks guest declarations")
    uuids = [record.get("guest_uuid") for record in declarations]
    spaces = [record.get("independent_address_space") for record in declarations]
    endpoints = [record.get("endpoint_host_id") for record in declarations]
    if len(set(uuids)) != len(uuids) or any(not item for item in uuids):
        raise CandidateEvidenceError("candidate guest UUIDs are absent or reused")
    if len(set(spaces)) != len(spaces) or any(not item for item in spaces):
        raise CandidateEvidenceError("candidate address spaces are absent or reused")
    if len(set(endpoints)) != len(endpoints) or any(
        not isinstance(item, int) or item < 0 for item in endpoints
    ):
        raise CandidateEvidenceError("candidate endpoint identities are invalid")
    observers = [record for record in declarations if record.get("role") == "cold-observer"]
    if len(observers) != 1:
        raise CandidateEvidenceError("candidate topology must predeclare one cold observer")
    if section.get("status") == "complete":
        if section.get("hdm_db_bi_observed") is not True:
            raise CandidateEvidenceError("complete candidate topology lacks BI evidence")
        if any(record.get("region_generation") is None for record in declarations):
            raise CandidateEvidenceError(
                "complete candidate topology lacks a region generation"
            )
        if observers[0].get("launch_state") != "launched-and-observed":
            raise CandidateEvidenceError(
                "complete candidate topology lacks a cold-observer receipt"
            )


def _validate_artifact_completion(section: dict) -> None:
    if section.get("status") != "complete":
        return
    if not isinstance(section.get("phase_artifact_path"), str) or not section.get(
        "phase_artifact_path"
    ):
        raise CandidateEvidenceError("candidate phase artifact path is missing")
    for field in ("phase_artifact_digest", "terminal_evidence_digest"):
        if not _is_digest(section.get(field)):
            raise CandidateEvidenceError(f"candidate {field} is not digest closed")
    units = section.get("metric_units")
    if not isinstance(units, list) or any(
        unit not in {"GiB/s", "kIOPS"} for unit in units
    ):
        raise CandidateEvidenceError("candidate metric units are invalid")


def validate(document: dict) -> dict:
    if not isinstance(document, dict):
        raise CandidateEvidenceError("candidate evidence must be a JSON object")
    if document.get("schema_version") != SCHEMA_VERSION:
        raise CandidateEvidenceError("candidate evidence schema mismatch")
    if document.get("filesystem_mode") != "rdwo-candidate":
        raise CandidateEvidenceError("candidate evidence has the wrong filesystem mode")
    if document.get("functional_model_only") is not True:
        raise CandidateEvidenceError("V0 candidate evidence must remain functional-model-only")
    if document.get("physical_hardware_evidence") is not False:
        raise CandidateEvidenceError("V0 candidate evidence cannot claim physical hardware")
    if document.get("official_candidate") is not False:
        raise CandidateEvidenceError("V0 candidate evidence cannot claim official eligibility")

    for path, value in _walk(document):
        if path and path[-1] in BANNED_LEGACY_KEYS:
            raise CandidateEvidenceError(
                f"candidate evidence contains legacy source: {'.'.join(path)}"
            )
        if isinstance(value, str) and any(
            fragment in value.lower() for fragment in BANNED_LEGACY_VALUE_FRAGMENTS
        ):
            raise CandidateEvidenceError(
                f"candidate evidence cites a forbidden legacy source: {'.'.join(path)}"
            )

    sections = document.get("sections")
    if not isinstance(sections, dict) or set(sections) != set(SECTION_NAMES):
        raise CandidateEvidenceError("candidate evidence section set is not closed")
    for name in SECTION_NAMES:
        section = sections[name]
        if not isinstance(section, dict):
            raise CandidateEvidenceError(f"candidate evidence section is invalid: {name}")
        status = section.get("status")
        if status not in {"complete", "not_yet_supported", "failed"}:
            raise CandidateEvidenceError(f"candidate evidence status is invalid: {name}")
        if status == "not_yet_supported":
            _validate_unsupported(name, section)
        if status == "failed":
            if not isinstance(section.get("reason"), str) or not section.get("reason"):
                raise CandidateEvidenceError(f"{name} lacks an explicit failure reason")
            if not isinstance(section.get("failure_class"), str) or not section.get(
                "failure_class"
            ):
                raise CandidateEvidenceError(f"{name} lacks an explicit failure class")

    attestation = sections["engine_build_attestation"]
    if attestation.get("status") != "complete":
        raise CandidateEvidenceError("candidate engine/build attestation must be complete")
    if set(attestation.get("artifacts", {})) != set(CANDIDATE_ARTIFACTS):
        raise CandidateEvidenceError("candidate build attestation artifact set is not closed")
    for field in (
        "build_identity_digest",
        "product_engine_manifest_digest",
        "product_capability_manifest_digest",
    ):
        if not _is_digest(attestation.get(field)):
            raise CandidateEvidenceError(f"candidate {field} is not digest closed")
    for name in CANDIDATE_ARTIFACTS:
        _artifact_record({"artifacts": attestation["artifacts"]}, name)

    _validate_process_terminals(sections["process_terminals"])
    _validate_network_closure(sections["network_closure"])
    _validate_response_and_drain(sections["response_and_final_drain"])
    _validate_topology(sections["qemu_bi_topology"])
    _validate_artifact_completion(sections["artifact_completion"])

    statuses = [sections[name].get("status") for name in SECTION_NAMES]
    if "failed" in statuses:
        expected_status = "failed"
    elif all(status == "complete" for status in statuses):
        expected_status = "complete"
    else:
        expected_status = "not_yet_supported"
    if document.get("status") != expected_status:
        raise CandidateEvidenceError("candidate aggregate status disagrees with sections")
    expected_digest = document.get("evidence_digest")
    unsigned = copy.deepcopy(document)
    unsigned.pop("evidence_digest", None)
    if expected_digest != canonical_digest(unsigned):
        raise CandidateEvidenceError("candidate evidence digest mismatch")
    return document


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence", type=pathlib.Path)
    args = parser.parse_args(argv)
    with args.evidence.open("r", encoding="utf-8") as source:
        document = json.load(source)
    validate(document)
    print(f"candidate_evidence=PASS path={args.evidence}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
