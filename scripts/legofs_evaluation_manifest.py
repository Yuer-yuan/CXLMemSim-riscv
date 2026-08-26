#!/usr/bin/env python3
"""Build a digest-closed, evaluator-only LegoFS IO500 run manifest."""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
import pathlib
import tempfile
from typing import Iterable


SCHEMA_VERSION = "legofs.evaluation-run-manifest.v1"
PHASE_EVIDENCE_SCHEMA_VERSION = "legofs.phase-evidence-manifest.v1"
FILESYSTEM_MODES = ("legacy-cxl-reference", "rdwo-candidate")
WORKSPACE_ROOT = pathlib.Path(__file__).resolve().parents[1]
REQUIRED_BUILD_ARTIFACTS = (
    "qemu",
    "cxlmemsim_server",
    "opensbi",
    "u_boot",
    "linux",
    "payload",
    "io500",
    "io500_verify",
    "mpiexec",
    "hydra_proxy",
    "io500_result_export",
    "dependency_versions",
    "libpmem_build",
    "isa_gate",
)
MODE_BUILD_ARTIFACTS = {
    "legacy-cxl-reference": (
        "badfs_server",
        "badfs_bench",
        "badfs_intercept",
        "syscall_intercept",
    ),
    "rdwo-candidate": (
        "rdwo_client",
        "rdwo_server",
        "rdwo_host_agent",
        "rdwo_intercept",
        "product_engine_manifest",
        "product_capability_manifest",
    ),
}
PHASE_GROUP = {
    "ior-easy-write": "ior-easy",
    "ior-easy-read": "ior-easy",
    "mdtest-easy-write": "mdtest-easy",
    "mdtest-easy-stat": "mdtest-easy",
    "mdtest-easy-delete": "mdtest-easy",
    "ior-hard-write": "ior-hard",
    "ior-hard-read": "ior-hard",
    "mdtest-hard-write": "mdtest-hard",
    "mdtest-hard-stat": "mdtest-hard",
    "mdtest-hard-read": "mdtest-hard",
    "mdtest-hard-delete": "mdtest-hard",
    "ior-rnd4K-write": "ior-rnd4K",
    "ior-rnd4K-read": "ior-rnd4K",
    "ior-rnd4K-easy-read": "ior-rnd4K",
    "ior-rnd1MB-write": "ior-rnd1MB",
    "ior-rnd1MB-read": "ior-rnd1MB",
    "mdworkbench-create": "mdworkbench",
    "mdworkbench-bench": "mdworkbench",
    "mdworkbench-delete": "mdworkbench",
}
EXTENDED_PHASE_ORDER = (
    "ior-easy-write",
    "ior-rnd4K-write",
    "mdtest-easy-write",
    "ior-rnd1MB-write",
    "mdworkbench-create",
    "find-easy",
    "ior-hard-write",
    "mdtest-hard-write",
    "find",
    "ior-rnd4K-read",
    "ior-rnd1MB-read",
    "find-hard",
    "mdworkbench-bench",
    "ior-easy-read",
    "mdtest-easy-stat",
    "ior-hard-read",
    "mdtest-hard-stat",
    "mdworkbench-delete",
    "mdtest-easy-delete",
    "mdtest-hard-read",
    "mdtest-hard-delete",
    "ior-rnd4K-easy-read",
)
STANDARD_PHASE_ORDER = tuple(
    phase
    for phase in EXTENDED_PHASE_ORDER
    if phase
    not in {
        "ior-rnd4K-write",
        "ior-rnd4K-read",
        "ior-rnd1MB-write",
        "ior-rnd1MB-read",
        "mdworkbench-create",
        "mdworkbench-bench",
        "mdworkbench-delete",
        "find-easy",
        "find-hard",
    }
)
PHASE_ORDER = EXTENDED_PHASE_ORDER
IO500_MODES = {
    "standard": STANDARD_PHASE_ORDER,
    "extended": EXTENDED_PHASE_ORDER,
}
PHASE_PREREQUISITES = {
    "ior-easy-read": ("ior-easy-write",),
    "ior-hard-read": ("ior-hard-write",),
    "ior-rnd4K-read": ("ior-rnd4K-write",),
    "ior-rnd1MB-read": ("ior-rnd1MB-write",),
    "ior-rnd4K-easy-read": ("ior-easy-write",),
    "mdtest-easy-stat": ("mdtest-easy-write",),
    "mdtest-easy-delete": ("mdtest-easy-write",),
    "mdtest-hard-stat": ("mdtest-hard-write",),
    "mdtest-hard-read": ("mdtest-hard-write",),
    "mdtest-hard-delete": ("mdtest-hard-write",),
    "find-easy": ("mdtest-easy-write",),
    "find": ("mdtest-easy-write", "mdtest-hard-write"),
    "find-hard": ("mdtest-hard-write",),
    "mdworkbench-bench": ("mdworkbench-create",),
    "mdworkbench-delete": ("mdworkbench-create",),
}
RUN_SECTIONS = tuple(
    dict.fromkeys(
        list(PHASE_GROUP.values())
        + list(PHASE_ORDER)
    )
)


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


def read_json(path: pathlib.Path) -> dict:
    with path.open("r", encoding="utf-8") as source:
        value = json.load(source)
    if not isinstance(value, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return value


def read_config(path: pathlib.Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    with path.open("r", encoding="utf-8") as source:
        parser.read_file(source)
    return parser


def option(
    parser: configparser.ConfigParser,
    section: str,
    name: str,
) -> str | None:
    if not parser.has_section(section):
        return None
    for candidate, value in parser.items(section):
        if candidate.lower() == name.lower():
            return value.strip()
    return None


def set_option(
    parser: configparser.ConfigParser,
    section: str,
    name: str,
    value: str,
) -> None:
    if not parser.has_section(section):
        parser.add_section(section)
    for candidate, _ in parser.items(section):
        if candidate.lower() == name.lower():
            parser.set(section, candidate, value)
            return
    parser.set(section, name, value)


def validate_standard_source(
    source: pathlib.Path,
    parser: configparser.ConfigParser,
) -> None:
    if source.name != "io500-standard.ini":
        raise ValueError(
            "stage-authorizing profiles must derive from io500-standard.ini"
        )
    if option(parser, "global", "scc") != "FALSE":
        raise ValueError("pinned standard config must keep scc=FALSE")
    if option(parser, "debug", "stonewall-time") != "300":
        raise ValueError("pinned standard config must keep 300-second stonewall")


def parse_config_bool(value: str | None, *, section: str) -> bool:
    if value is None:
        raise ValueError(f"diagnostic config must explicitly set {section}.run")
    normalized = value.upper()
    if normalized == "TRUE":
        return True
    if normalized == "FALSE":
        return False
    raise ValueError(f"diagnostic config has invalid {section}.run={value}")


def configured_phases(
    parser: configparser.ConfigParser,
    mode_order: Iterable[str],
) -> tuple[str, ...]:
    enabled = []
    for phase in mode_order:
        phase_enabled = parse_config_bool(option(parser, phase, "run"), section=phase)
        group = PHASE_GROUP.get(phase)
        group_enabled = (
            parse_config_bool(option(parser, group, "run"), section=group)
            if group is not None
            else True
        )
        if phase_enabled and group_enabled:
            enabled.append(phase)
    return tuple(enabled)


def derive_profile(
    source: pathlib.Path,
    enabled_phases: Iterable[str],
    *,
    io500_mode: str,
    stonewall_seconds: int | None,
    mdtest_items: int | None,
) -> tuple[
    configparser.ConfigParser,
    tuple[str, ...],
    tuple[str, ...],
    str,
]:
    parser = read_config(source)
    if io500_mode not in IO500_MODES:
        raise ValueError(f"unsupported IO500 mode: {io500_mode}")
    mode_order = IO500_MODES[io500_mode]
    requested = tuple(dict.fromkeys(enabled_phases))
    unknown = sorted(set(requested) - set(PHASE_ORDER))
    if unknown:
        raise ValueError(f"unknown IO500 phase(s): {', '.join(unknown)}")
    unavailable = sorted(set(requested) - set(mode_order))
    if unavailable:
        raise ValueError(
            f"phase(s) require IO500 extended mode: {', '.join(unavailable)}"
        )
    if source.name != "io500-standard.ini":
        if requested or stonewall_seconds is not None or mdtest_items is not None:
            raise ValueError(
                "custom diagnostic configs cannot be modified by the manifest tool"
            )
        phases = configured_phases(parser, mode_order)
        if not phases:
            raise ValueError("custom diagnostic config enables no IO500 phases")
        phase_set = set(phases)
        for phase in phases:
            missing = set(PHASE_PREREQUISITES.get(phase, ())) - phase_set
            if missing:
                raise ValueError(
                    f"diagnostic config phase {phase} lacks prerequisite(s): "
                    + ", ".join(sorted(missing))
                )
        return parser, phases, phases, "diagnostic-custom-shape"

    validate_standard_source(source, parser)
    if not requested:
        if stonewall_seconds is not None or mdtest_items is not None:
            raise ValueError(
                "full official-shape profile cannot change stonewall or object count"
            )
        return parser, mode_order, mode_order, "official-shape"

    closure = set(requested)
    pending = list(requested)
    while pending:
        phase = pending.pop()
        for prerequisite in PHASE_PREREQUISITES.get(phase, ()):
            if prerequisite not in closure:
                closure.add(prerequisite)
                pending.append(prerequisite)
    ordered = tuple(phase for phase in mode_order if phase in closure)
    requested_ordered = tuple(phase for phase in mode_order if phase in requested)

    enabled_sections = set(ordered)
    enabled_sections.update(
        PHASE_GROUP[phase] for phase in ordered if phase in PHASE_GROUP
    )
    for section in RUN_SECTIONS:
        set_option(
            parser,
            section,
            "run",
            "TRUE" if section in enabled_sections else "FALSE",
        )
    if stonewall_seconds is not None:
        if stonewall_seconds <= 0:
            raise ValueError("stonewall must be positive")
        set_option(parser, "debug", "stonewall-time", str(stonewall_seconds))
    if mdtest_items is not None:
        if mdtest_items <= 0:
            raise ValueError("mdtest item count must be positive")
        enabled_mdtest_groups = {
            PHASE_GROUP[phase]
            for phase in ordered
            if PHASE_GROUP.get(phase, "").startswith("mdtest-")
        }
        if not enabled_mdtest_groups:
            raise ValueError("mdtest item count requires an enabled MDTest phase")
        for section in enabled_mdtest_groups:
            set_option(parser, section, "n", str(mdtest_items))
    return parser, ordered, requested_ordered, "diagnostic-upstream-shape"


def config_bytes(parser: configparser.ConfigParser) -> bytes:
    from io import StringIO

    output = StringIO()
    parser.write(output, space_around_delimiters=True)
    return output.getvalue().encode("utf-8")


def write_atomic(path: pathlib.Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = pathlib.Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def resolve_artifacts(
    build_manifest_path: pathlib.Path,
    filesystem_mode: str,
) -> tuple[dict, dict]:
    build = read_json(build_manifest_path)
    if build.get("schema_version") != 2:
        raise ValueError("unsupported build manifest schema")
    artifacts = build.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ValueError("build manifest lacks artifact table")
    selected = {}
    required = REQUIRED_BUILD_ARTIFACTS + MODE_BUILD_ARTIFACTS[filesystem_mode]
    for name in required:
        record = artifacts.get(name)
        if not isinstance(record, dict):
            raise ValueError(f"build manifest lacks required artifact: {name}")
        path_value = record.get("path")
        expected = record.get("sha256")
        expected_size = record.get("size")
        if (
            not isinstance(path_value, str)
            or not isinstance(expected, str)
            or not isinstance(expected_size, int)
        ):
            raise ValueError(f"build artifact {name} lacks path, size, or sha256")
        path = pathlib.Path(path_value)
        if not path.is_absolute():
            path = WORKSPACE_ROOT / path
        if not path.is_file():
            raise FileNotFoundError(f"build artifact does not exist: {path}")
        if len(expected) != 64 or any(
            character not in "0123456789abcdef" for character in expected
        ):
            raise ValueError(f"build artifact {name} has invalid sha256")
        if expected_size <= 0:
            raise ValueError(f"build artifact {name} has invalid size")
        actual = sha256_file(path)
        if actual != expected:
            raise ValueError(f"build artifact hash mismatch: {name}")
        if path.stat().st_size != expected_size:
            raise ValueError(f"build artifact size mismatch: {name}")
        selected[name] = {
            "path": path_value,
            "size": path.stat().st_size,
            "sha256": actual,
        }
    identity = {
        "schema_version": build.get("schema_version"),
        "superproject_commit": build.get("superproject_commit"),
        "sources": build.get("sources"),
        "compilers": build.get("compilers"),
        "build_manifest_sha256": sha256_file(build_manifest_path),
    }
    identity["digest"] = canonical_digest(identity)
    return identity, selected


def phase_evidence(
    phases: Iterable[str],
    requested_phases: Iterable[str],
) -> list[dict]:
    requested = set(requested_phases)
    result = []
    for phase in phases:
        result.append(
            {
                "phase": phase,
                "config_section_closure": [
                    *([PHASE_GROUP[phase]] if phase in PHASE_GROUP else []),
                    phase,
                ],
                "workload_prerequisites": list(
                    PHASE_PREREQUISITES.get(phase, ())
                ),
                "role": "measurement" if phase in requested else "setup",
                "primary_metric_unit": (
                    "GiB/s" if phase.startswith("ior-") else "kIOPS"
                ),
                "required_metric_units": (
                    ["GiB/s", "kIOPS"]
                    if phase.startswith("ior-")
                    else ["kIOPS"]
                ),
                "product_visible_phase_identity": False,
            }
        )
    return result


def build_evaluation_manifest(
    *,
    source_config: pathlib.Path,
    output_config: pathlib.Path,
    build_manifest: pathlib.Path,
    filesystem_mode: str,
    io500_mode: str,
    profile_name: str,
    enabled_phases: Iterable[str],
    stonewall_seconds: int | None,
    mdtest_items: int | None,
    server_count: int,
    client_count: int,
) -> dict:
    if filesystem_mode not in FILESYSTEM_MODES:
        raise ValueError(f"unsupported filesystem mode: {filesystem_mode}")
    if not 1 <= server_count <= 2:
        raise ValueError("server count must be 1 or 2")
    if not 1 <= client_count <= 10:
        raise ValueError("client count must be between 1 and 10")
    if not profile_name or any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
        for character in profile_name
    ):
        raise ValueError("profile name contains unsupported characters")
    if source_config.resolve() == output_config.resolve():
        raise ValueError("effective config must not overwrite its source config")

    parser, phases, requested_phases, classification = derive_profile(
        source_config,
        enabled_phases,
        io500_mode=io500_mode,
        stonewall_seconds=stonewall_seconds,
        mdtest_items=mdtest_items,
    )
    rendered = (
        config_bytes(parser)
        if classification == "diagnostic-upstream-shape"
        else source_config.read_bytes()
    )
    build_identity, artifacts = resolve_artifacts(
        build_manifest,
        filesystem_mode,
    )
    write_atomic(output_config, rendered)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "profile_name": profile_name,
        "classification": classification,
        "official_parameter_shape": classification == "official-shape",
        "official_candidate": False,
        "filesystem_mode": filesystem_mode,
        "io500_mode": io500_mode,
        "product_consumes_this_manifest": False,
        "product_engine_manifest_digest": None,
        "source_config": {
            "path": str(source_config),
            "sha256": sha256_file(source_config),
        },
        "effective_config": {
            "path": str(output_config),
            "sha256": hashlib.sha256(rendered).hexdigest(),
        },
        "allowed_derivation": {
            "source_bytes_preserved": (
                classification != "diagnostic-upstream-shape"
            ),
            "run_flags_rewritten": (
                classification == "diagnostic-upstream-shape"
            ),
            "stonewall_seconds": stonewall_seconds,
            "mdtest_items": mdtest_items,
            "transfer_size_changed": False,
            "access_pattern_changed": False,
            "sharing_shape_changed": False,
        },
        "phase_evidence": {
            "schema_version": PHASE_EVIDENCE_SCHEMA_VERSION,
            "external_only": True,
            "product_visible_phase_identity": False,
            "requested_phases": list(requested_phases),
            "execution_phases": list(phases),
            "implicit_runtime_phases": ["timestamp"],
            "records": phase_evidence(phases, requested_phases),
        },
        "topology": {
            "server_guests": server_count,
            "client_guests": client_count,
            "mpi_ranks": client_count,
            "shared_cxl_type3_region_bytes": 64 * 1024**3,
            "hdm_db_bi_required": True,
            "functional_model_only": True,
            "physical_hardware_evidence": False,
        },
        "build_identity": build_identity,
        "artifacts": artifacts,
    }
    manifest["manifest_digest"] = canonical_digest(manifest)
    return manifest


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-config", type=pathlib.Path, required=True)
    parser.add_argument("--output-config", type=pathlib.Path, required=True)
    parser.add_argument("--build-manifest", type=pathlib.Path, required=True)
    parser.add_argument("--output-manifest", type=pathlib.Path, required=True)
    parser.add_argument("--filesystem-mode", choices=FILESYSTEM_MODES, required=True)
    parser.add_argument("--io500-mode", choices=tuple(IO500_MODES), default="standard")
    parser.add_argument("--profile-name", required=True)
    parser.add_argument("--enable-phase", action="append", default=[])
    parser.add_argument("--stonewall-seconds", type=int)
    parser.add_argument("--mdtest-items", type=int)
    parser.add_argument("--server-count", type=int, choices=(1, 2), default=1)
    parser.add_argument("--client-count", type=int, default=10)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output_manifest = args.output_manifest.resolve()
    if output_manifest in {
        args.source_config.resolve(),
        args.output_config.resolve(),
        args.build_manifest.resolve(),
    }:
        raise ValueError("output manifest must use a distinct path")
    manifest = build_evaluation_manifest(
        source_config=args.source_config,
        output_config=args.output_config,
        build_manifest=args.build_manifest,
        filesystem_mode=args.filesystem_mode,
        io500_mode=args.io500_mode,
        profile_name=args.profile_name,
        enabled_phases=args.enable_phase,
        stonewall_seconds=args.stonewall_seconds,
        mdtest_items=args.mdtest_items,
        server_count=args.server_count,
        client_count=args.client_count,
    )
    encoded = (
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    write_atomic(args.output_manifest, encoded)
    print(args.output_manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
