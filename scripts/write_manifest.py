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
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        metavar="NAME=PATH",
    )
    parser.add_argument(
        "--no-artifact-hashes",
        action="store_true",
        help="record artifact paths and sizes without content hashes",
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
        if state in ("-", "U"):
            raise ValueError(
                f"submodule is unavailable or conflicted: {line[1:]}"
            )
        if state not in (" ", "+"):
            raise ValueError(f"unknown submodule state: {line}")
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


def parse_artifacts(root, specifications, include_hashes=True):
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
        entry = {
            "path": display_path(root, path),
            "size": path.stat().st_size,
        }
        if include_hashes:
            entry["sha256"] = sha256(path)
        artifacts[name] = entry
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


def parse_sources(root, specifications):
    sources = {}
    for specification in specifications:
        name, separator, raw_path = specification.partition("=")
        if not separator or not name or not raw_path:
            raise ValueError(
                f"source must use non-empty NAME=PATH: {specification}"
            )
        if name in sources:
            raise ValueError(f"duplicate source name: {name}")
        path = pathlib.Path(raw_path).expanduser().resolve()
        if not path.is_dir():
            raise FileNotFoundError(f"source is not a directory: {path}")
        worktree_root = pathlib.Path(
            git_output(path, "rev-parse", "--show-toplevel").strip()
        ).resolve()
        if worktree_root != path:
            raise ValueError(f"source is not a Git worktree root: {path}")
        status = git_output(
            path, "status", "--porcelain=v1", "--untracked-files=all"
        )
        origin = subprocess.run(
            ["git", "-C", str(path), "config", "--get", "remote.origin.url"],
            check=False,
            text=True,
            capture_output=True,
        ).stdout.strip()
        sources[name] = {
            "path": os.path.relpath(path, root),
            "commit": git_output(path, "rev-parse", "HEAD").strip(),
            "tree": git_output(path, "rev-parse", "HEAD^{tree}").strip(),
            "clean": not bool(status),
            "origin": origin or None,
        }
    return dict(sorted(sources.items()))


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
        "sources": parse_sources(root, args.source),
        "compilers": parse_compilers(args.compiler),
        "artifacts": parse_artifacts(
            root,
            args.artifact,
            include_hashes=not args.no_artifact_hashes,
        ),
    }
    atomic_write_json(output, manifest)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
