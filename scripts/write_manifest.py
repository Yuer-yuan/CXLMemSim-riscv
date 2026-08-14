#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import pathlib
import shlex
import subprocess
import sys


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--artifact",
        action="append",
        default=[],
        metavar="NAME=PATH",
    )
    parser.add_argument(
        "--compiler",
        action="append",
        default=[],
        metavar="NAME=COMMAND",
    )
    return parser.parse_args(argv)


def git_output(root, *arguments):
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        text=True,
        capture_output=True,
    ).stdout


def read_submodules(root):
    submodules = {}
    output = git_output(root, "submodule", "status")
    for line in output.splitlines():
        if not line:
            continue
        state = line[0]
        if state != " ":
            raise ValueError(
                f"submodule is not at its recorded gitlink: {line[1:]}"
            )
        fields = line[1:].split()
        if len(fields) < 2:
            raise ValueError(f"malformed submodule status: {line}")
        commit, path = fields[:2]
        submodules[path] = commit
    return dict(sorted(submodules.items()))


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def display_path(root, path):
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def parse_artifacts(root, specifications):
    artifacts = {}
    for specification in specifications:
        name, separator, raw_path = specification.partition("=")
        if not separator or not name or not raw_path:
            raise ValueError(
                f"artifact must use non-empty NAME=PATH: {specification}"
            )
        if name in artifacts:
            raise ValueError(f"duplicate artifact name: {name}")
        path = pathlib.Path(raw_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"artifact is not a file: {path}")
        artifacts[name] = {
            "path": display_path(root, path),
            "size": path.stat().st_size,
            "sha256": sha256(path),
        }
    return dict(sorted(artifacts.items()))


def parse_compilers(specifications):
    compilers = {}
    for specification in specifications:
        name, separator, raw_command = specification.partition("=")
        if not separator or not name or not raw_command:
            raise ValueError(
                f"compiler must use non-empty NAME=COMMAND: {specification}"
            )
        if name in compilers:
            raise ValueError(f"duplicate compiler name: {name}")
        command = shlex.split(raw_command)
        if not command:
            raise ValueError(f"compiler command is empty: {specification}")
        run = subprocess.run(
            command,
            check=True,
            text=True,
            capture_output=True,
        )
        version = (run.stdout or run.stderr).strip()
        if not version:
            raise ValueError(f"compiler produced no version output: {name}")
        compilers[name] = {"command": command, "version": version}
    return dict(sorted(compilers.items()))


def atomic_write_json(output, value):
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as destination:
            json.dump(value, destination, indent=2, sort_keys=True)
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, output)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def main(argv=None):
    args = parse_args(argv)
    root = pathlib.Path(args.root).expanduser().resolve()
    output = pathlib.Path(args.output).expanduser().resolve()
    if not (root / ".git").exists():
        raise ValueError(f"root is not a Git checkout: {root}")
    manifest = {
        "schema_version": 2,
        "superproject_commit": git_output(root, "rev-parse", "HEAD").strip(),
        "submodules": read_submodules(root),
        "compilers": parse_compilers(args.compiler),
        "artifacts": parse_artifacts(root, args.artifact),
    }
    atomic_write_json(output, manifest)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
