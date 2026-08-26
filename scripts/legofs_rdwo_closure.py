#!/usr/bin/env python3
"""Fail-closed V1.1 dependency and ELF closure attestation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


SCHEMA = "legofs.rdwo-build-closure.v1"
REQUIRED_ROLES = ("client", "server", "host_agent", "intercept")
BANNED_PACKAGES = frozenset(
    {
        "tarpc",
        "badfs-client",
        "badfs-server",
        "badfs-intercept",
        "badfs-host-agent",
        "badfs-authority",
    }
)
BANNED_SYMBOL_TEXT = (
    "badfsclient",
    "v2authorityengine",
    "inmemoryoracle",
    "badfsservice",
    "commit_staged_write",
    "reserve_staged_write",
    "badfs_posix_data_path",
    "badfs_control_transport",
    "hostfs",
    "tarpc",
)
PACKAGE_RE = re.compile(r"(?:^|[\s─])([A-Za-z0-9_-]+) v[0-9]")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def parse_artifacts(values: list[str]) -> dict[str, Path]:
    artifacts: dict[str, Path] = {}
    for value in values:
        role, separator, raw_path = value.partition("=")
        if not separator or role not in REQUIRED_ROLES or role in artifacts:
            raise ValueError(f"invalid or duplicate candidate artifact: {value}")
        path = Path(raw_path).resolve()
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"missing candidate artifact: {role}={path}")
        artifacts[role] = path
    if set(artifacts) != set(REQUIRED_ROLES):
        raise ValueError(
            f"candidate roles must be exactly {list(REQUIRED_ROLES)}, got {sorted(artifacts)}"
        )
    return artifacts


def cargo_packages(tree: str) -> set[str]:
    return {match.group(1) for match in PACKAGE_RE.finditer(tree)}


def reject_banned_packages(tree: str) -> list[str]:
    packages = cargo_packages(tree)
    banned = sorted(packages & BANNED_PACKAGES)
    if banned:
        raise ValueError(f"candidate Cargo closure contains legacy packages: {banned}")
    return sorted(packages)


def run_tool(command: list[str]) -> str:
    result = subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return result.stdout


def inspect_artifact(
    role: str,
    path: Path,
    readelf: str,
    nm: str,
    strings: str,
) -> dict:
    dynamic = run_tool([readelf, "-d", str(path)])
    symbols = run_tool([nm, "-D", "--defined-only", str(path)])
    printable = run_tool([strings, "-a", str(path)])
    searchable = f"{dynamic}\n{symbols}\n{printable}".lower()
    matches = [token for token in BANNED_SYMBOL_TEXT if token in searchable]
    if matches:
        raise ValueError(f"candidate {role} contains banned legacy text: {matches}")
    needed = sorted(
        set(re.findall(r"Shared library: \[([^]]+)\]", dynamic))
    )
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "needed": needed,
        "defined_dynamic_symbol_count": sum(
            1 for line in symbols.splitlines() if line.strip()
        ),
        "legacy_text_matches": [],
    }


def canonical_digest(record: dict) -> str:
    payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def write_atomic(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(record, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def create_attestation(arguments: argparse.Namespace) -> dict:
    cargo_tree_path = Path(arguments.cargo_tree).resolve()
    source_identity_path = Path(arguments.source_identity).resolve()
    if not cargo_tree_path.is_file() or not source_identity_path.is_file():
        raise ValueError("Cargo tree and source identity must exist")
    tree = cargo_tree_path.read_text(encoding="utf-8")
    packages = reject_banned_packages(tree)
    artifacts = parse_artifacts(arguments.artifact)
    record = {
        "schema": SCHEMA,
        "status": "passed",
        "build_flavor": "rdwo-product",
        "entry_eligible": False,
        "entry_eligibility_reason": (
            "V1.1 proves manifest and link closure only; V1.2 syscall admission is absent"
        ),
        "legacy_runtime_linked": False,
        "cargo_closure": {
            "path": str(cargo_tree_path),
            "sha256": sha256_file(cargo_tree_path),
            "packages": packages,
            "banned_packages": [],
        },
        "source_identity": {
            "path": str(source_identity_path),
            "sha256": sha256_file(source_identity_path),
        },
        "artifacts": {
            role: inspect_artifact(
                role, path, arguments.readelf, arguments.nm, arguments.strings
            )
            for role, path in sorted(artifacts.items())
        },
        "forbidden_runtime_selectors": [
            "phase",
            "workload",
            "filename",
            "transfer_size",
            "legacy_fallback",
        ],
        "physical_hardware_evidence": False,
        "official_candidate": False,
    }
    record["record_digest"] = canonical_digest(record)
    return record


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cargo-tree", required=True)
    parser.add_argument("--source-identity", required=True)
    parser.add_argument("--artifact", action="append", default=[], required=True)
    parser.add_argument("--readelf", default="readelf")
    parser.add_argument("--nm", default="nm")
    parser.add_argument("--strings", default="strings")
    parser.add_argument("--output", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv)
    try:
        record = create_attestation(arguments)
        write_atomic(Path(arguments.output).resolve(), record)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"rdwo closure rejected: {error}") from error
    print(Path(arguments.output).resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
