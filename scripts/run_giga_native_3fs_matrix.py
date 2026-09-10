#!/usr/bin/env python3
"""Run the giga-native 3FS IO500 1c1s, 2c1s, 3c1s matrix sequentially."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time


TOPOLOGIES = ("1c1s", "2c1s", "3c1s")


def digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def validate(path: Path) -> dict:
    value = json.loads(path.read_text())
    if value.get("schema") != "hf3fs.giga-native-matrix.v1":
        raise ValueError("matrix schema changed")
    if value.get("official") is not False or value.get("expected_invalid") is not True:
        raise ValueError("matrix result classification changed")
    cases = value.get("cases", [])
    if [case.get("topology") for case in cases] != list(TOPOLOGIES):
        raise ValueError("matrix topology order or coverage differs")
    if any(case.get("status") != "passed" or len(case.get("phases", [])) != 22 for case in cases):
        raise ValueError("matrix contains an incomplete case")
    if len({case.get("build_manifest_sha256") for case in cases}) != 1 or len({case.get("profile_sha256") for case in cases}) != 1:
        raise ValueError("matrix cases do not share one build/profile closure")
    return value


def execute(repo: Path, prefix: str) -> Path:
    repo = repo.resolve(strict=True)
    if not prefix or "/" in prefix or prefix in (".", ".."):
        raise ValueError("prefix must be a safe path component")
    result_root = repo / "target/results/giga-native-3fs"
    summary = result_root / f"{prefix}-summary.json"
    if summary.exists():
        raise FileExistsError(summary)
    started = time.monotonic_ns()
    cases = []
    runner = repo / "scripts/run_giga_native_3fs_io500.sh"
    for name in TOPOLOGIES:
        run_id = f"{prefix}-{name}"
        completed = subprocess.run([str(runner), "--topology", name, "--run-id", run_id], cwd=repo)
        result_path = result_root / run_id / "result.json"
        if not result_path.is_file():
            raise RuntimeError(f"case {name} produced no result receipt")
        result = json.loads(result_path.read_text())
        workload = result.get("workload", {})
        case = {
            "topology": name, "run_id": run_id, "status": result.get("status"),
            "ranks": result.get("ranks"), "official": False, "expected_invalid": True,
            "build_manifest_sha256": result.get("build_manifest_sha256"),
            "profile_sha256": result.get("profile_sha256"),
            "phases": workload.get("phases", []), "bundle_sha256": digest(result_path),
        }
        cases.append(case)
        if completed.returncode or result.get("status") != "passed":
            raise RuntimeError(f"matrix stopped at failed topology {name}: {result.get('first_failure')}")
    value = {"schema": "hf3fs.giga-native-matrix.v1", "status": "passed", "official": False,
             "expected_invalid": True, "prefix": prefix, "host_elapsed_ns": time.monotonic_ns() - started,
             "cases": cases}
    summary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    validate(summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--prefix")
    parser.add_argument("--validate-only", type=Path)
    args = parser.parse_args()
    try:
        if args.validate_only:
            value = validate(args.validate_only)
            print(f"hf3fs-giga-matrix=PASS cases={len(value['cases'])} summary={args.validate_only}")
        else:
            if args.prefix is None:
                parser.error("--prefix is required unless --validate-only is used")
            path = execute(args.repo, args.prefix)
            print(f"hf3fs-giga-matrix=PASS summary={path}")
        return 0
    except Exception as error:
        print(f"hf3fs-giga-matrix=FAIL reason={error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
