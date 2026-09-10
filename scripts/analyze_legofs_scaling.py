#!/usr/bin/env python3
"""Summarize full-phase IO500 bundles without mixing environments or resources."""
import argparse
import configparser
from collections import Counter
import hashlib
import json
import math
import re
from pathlib import Path
import statistics

from legofs_io500 import FULL_IO500_PHASES, parse_io500_metrics


def normalized_profile(path):
    config = configparser.ConfigParser(interpolation=None, strict=True)
    if not config.read(path):
        raise ValueError(f"missing effective config: {path}")
    normalized = {section: dict(config[section]) for section in config.sections()}
    for key in ("datadir", "resultdir"):
        normalized.get("global", {}).pop(key, None)
    return hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()


def operation_diagnostics(bundle):
    """Final per-rank waits, not per-phase CPU costs or mutually exclusive time."""
    source = Path(__file__).resolve().parents[1] / "components/legofs/badfs-common/src/fs_ops.rs"
    names = {int(value): name for name, value in re.findall(
        r"pub const (\w+): u16 = (\d+);", source.read_text())}
    ranks, observed = [], set()
    for path in sorted((bundle / "posix-path-summaries").glob("*.json")):
        record = json.loads(path.read_text())
        rank = record["mpi_rank"]
        if rank in observed:
            raise ValueError(f"duplicate final rank summary: {rank}")
        observed.add(rank)
        calls, waits = Counter(), Counter()
        authority, client = Counter(), Counter()
        for evidence in record["cxl_serving_evidence"]:
            calls.update({int(k): v for k, v in evidence["dispatches_by_opcode"].items()})
            waits.update({int(k): v for k, v in evidence["cq_wait_ns_by_opcode"].items()})
            authority.update(evidence["authority_timing"])
            client.update(evidence["client_timing"])
        if any(value < 0 or calls[op] <= 0 for op, value in waits.items()):
            raise ValueError("opcode wait has no matching dispatch count")
        ranks.append(dict(rank=rank, authority_timing=authority, client_timing=client,
            operations=[dict(opcode=op, name=names.get(op, str(op)), calls=calls[op],
                cq_wait_ns=waits[op], mean_cq_wait_us=waits[op] / calls[op] / 1000)
                for op in sorted(calls) if calls[op] > 0]))
    return dict(per_rank=ranks, scope="Whole-run elapsed awaits, including teardown. Not per-phase or CPU time. Authority/client waits overlap; never sum them as exclusive costs.")


def read_sample(bundle):
    result = json.loads((bundle / "result.json").read_text())
    if result.get("claim") == "cxl-numa-samehost":
        if result.get("verdict") != "accepted" or not result.get("cleanup", {}).get("absent"):
            raise ValueError("native bundle failed acceptance or cleanup")
        placement = result["cpu_llc_placement"]
        server = placement["server"]
        cpus = server.get("requested_cpus", [server["requested_cpu"]])
        metrics = parse_io500_metrics((bundle / "io500-results/result.txt").read_text())
        preflight = json.loads((bundle / "preflight.json").read_text())
        source = preflight["build_manifest"]["source_manifest_sha256"]
        environment = "giga-native"
        resources = {"client_cpus": [c["requested_cpu"] for c in placement["clients"]],
                     "server_runtime_workers": result.get("server_runtime_workers", "unrecorded"),
                     "server_cpus": cpus,
                     "trace_mode": result["trace"].get("mode", "full"),
                     "cq_wait_mode": result["cxl_serving"]["cq_wait_mode"],
                     "authority_wait_mode": result["cxl_serving"]["authority_idle"][0]["mode"]}
        ranks = len(placement["clients"])
        profile = normalized_profile(bundle / "io500-results/config.ini")
    else:
        if result.get("status") != "passed" or result.get("cleanup", {}).get("owned_processes_remaining", ["unknown"]):
            raise ValueError("QEMU bundle failed acceptance or cleanup")
        if result.get("stage") != "full22":
            raise ValueError("reduced QEMU pressure suite is not full22 performance coverage")
        environment = "vlm-qemu-bi"
        metrics = result["io500"]["metrics"]
        resources = dict(result["topology"])
        resources["observation"] = {k: v for k, v in result["observation_config"].items() if k != "run_id"}
        ranks = resources["mpi_ranks"]
        source = hashlib.sha256(json.dumps({k: v["sha256"] for k, v in result["build"]["manifest"]["artifacts"].items()}, sort_keys=True).encode()).hexdigest()
        profile = normalized_profile(bundle / "config.ini")
    phases = metrics["phases"]
    if len(phases) != 22 or {p["name"] for p in phases} != FULL_IO500_PHASES:
        raise ValueError("bundle does not contain exactly all22 phases")
    for phase in phases:
        if not math.isfinite(phase["score"]) or phase["score"] < 0 or not math.isfinite(phase["seconds"]) or phase["seconds"] <= 0:
            raise ValueError(f"invalid phase metrics: {phase}")
    return dict(bundle=str(bundle.resolve()), environment=environment, ranks=ranks,
                source=source, profile=profile, resources=resources,
                operations=operation_diagnostics(bundle),
                phases={p["name"]: {k:p[k] for k in ("score", "unit", "seconds")} for p in phases})


def summarize(samples):
    groups = {}
    seen = set()
    for sample in samples:
        if sample["bundle"] in seen:
            raise ValueError("duplicate bundle cannot count as a repetition")
        seen.add(sample["bundle"])
        identity = {key:sample[key] for key in ("environment", "ranks", "source", "profile", "resources")}
        key = json.dumps(identity, sort_keys=True)
        group = groups.setdefault(key, {**identity, "samples": []})
        group["samples"].append(sample)
    output = []
    for group in groups.values():
        records = group.pop("samples")
        group["bundles"] = [r["bundle"] for r in records]
        group["repetitions"] = len(records)
        group["operation_diagnostics"] = [{"bundle": r["bundle"], **r["operations"]} for r in records if "operations" in r]
        group["phases"] = {}
        for name in sorted(FULL_IO500_PHASES):
            values = [r["phases"][name]["score"] for r in records]
            seconds = [r["phases"][name]["seconds"] for r in records]
            units = {r["phases"][name]["unit"] for r in records}
            if len(units) != 1:
                raise ValueError("inconsistent units")
            group["phases"][name] = dict(unit=units.pop(), mean=statistics.mean(values),
                median=statistics.median(values), minimum=min(values), maximum=max(values),
                stdev=statistics.stdev(values) if len(values)>1 else None,
                mean_seconds=statistics.mean(seconds))
        output.append(group)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundles", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    samples, rejected = [], []
    for bundle in args.bundles:
        try:
            samples.append(read_sample(bundle))
        except (KeyError, ValueError, OSError) as error:
            rejected.append({"bundle": str(bundle), "reason": str(error)})
    result = dict(schema_version="legofs.scaling-analysis.v1", groups=summarize(samples),
                  rejected=rejected, scope="Diagnostic IO500, not an official submission. Separate source, geometry, environment and CPU budgets; do not sum overlapping timers. Single samples do not establish gains.")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2)+"\n")
    print(json.dumps({"accepted_samples":len(samples), "groups":len(result["groups"]), "rejected":rejected}))
    return bool(rejected)


if __name__ == "__main__":
    raise SystemExit(main())
