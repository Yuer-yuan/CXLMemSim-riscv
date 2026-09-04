#!/usr/bin/env python3
"""Atomically refresh one artifact identity in an existing IO500 manifest."""

import argparse
import hashlib
import json
import os
import pathlib


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--artifact", required=True, metavar="NAME=PATH")
    args = parser.parse_args()

    root = pathlib.Path(args.root).expanduser().resolve()
    manifest_path = pathlib.Path(args.manifest).expanduser().resolve()
    name, separator, raw_path = args.artifact.partition("=")
    if not separator or not name or not raw_path:
        raise ValueError("artifact must use non-empty NAME=PATH")
    artifact_path = pathlib.Path(raw_path).expanduser().resolve()
    if not artifact_path.is_file():
        raise FileNotFoundError(f"artifact is not a file: {artifact_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 2:
        raise ValueError("unsupported build manifest schema")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or name not in artifacts:
        raise ValueError(f"manifest does not already declare artifact: {name}")
    try:
        recorded_path = str(artifact_path.relative_to(root))
    except ValueError:
        recorded_path = str(artifact_path)
    artifacts[name] = {
        "path": recorded_path,
        "sha256": sha256(artifact_path),
        "size": artifact_path.stat().st_size,
    }

    temporary = manifest_path.with_name(manifest_path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as destination:
            json.dump(manifest, destination, indent=2, sort_keys=True)
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, manifest_path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    main()
