#!/usr/bin/env python3
"""Repeat native LLC topologies with explicit, independent SQ/CQ wait policies.

Run on the selected native host after building. Results retain PMU definitions,
background load, exact commands and actual component-runner verdicts. perf's exit
code alone is insufficient: some perf versions return zero for a failed child.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import time

POLICIES = {
    "timer": ("timer_sleep", "timer_sleep"),
    "client-yield": ("cooperative_yield", "timer_sleep"),
    "authority-yield": ("timer_sleep", "cooperative_yield"),
    "both-yield": ("cooperative_yield", "cooperative_yield"),
}


def accepted_case(returncode, bundle):
    """Require actual workload acceptance and cleanup even if perf succeeded."""
    path = bundle / "result.json"
    failure = bundle / "failure.json"
    result = json.loads(path.read_text()) if path.is_file() else {}
    return {
        "accepted": returncode == 0 and result.get("verdict") == "accepted"
        and result.get("cleanup", {}).get("absent") is True,
        "verdict": result.get("verdict"), "cleanup": result.get("cleanup"),
        "failure": json.loads(failure.read_text()) if failure.is_file() else None,
    }


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument("--label", required=True)
    p.add_argument("--policies", nargs="+", choices=POLICIES, default=["timer", "both-yield"])
    p.add_argument("--repetitions", type=int, default=2)
    p.add_argument("--order", default="1,3,2")
    p.add_argument("--profile", choices=["bounded", "capacity-5s", "capacity-20s"], default="capacity-5s")
    p.add_argument("--pmu", help="optional sysfs PMU device, e.g. cxl_pmu_mem0.0")
    p.add_argument("--events", default="m2s_req_memrd,m2s_rwd_memwr,ddr_casrd,ddr_caswr")
    a = p.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,70}", a.label):
        p.error("invalid label")
    order = [int(x) for x in a.order.split(",")]
    if sorted(order) != [1, 2, 3] or a.repetitions < 1:
        p.error("order must contain 1, 2, 3 exactly once; repetitions must be positive")
    root = a.root.resolve()
    out = root / "target/results/legofs-dual-host/native" / a.label
    out.mkdir(parents=True, exist_ok=False)
    manifest = root / "target/results/giga-native/build-manifest.json"
    source = manifest.read_bytes()
    (out / "build-manifest.json").write_bytes(source)
    events = a.events.split(",")
    if a.pmu:
        if not all(re.fullmatch(r"[A-Za-z0-9_.-]+", x) for x in [a.pmu, *events]):
            p.error("invalid PMU/event name")
        pmu = Path("/sys/bus/event_source/devices") / a.pmu
        definitions = {x: (pmu / "events" / x).read_text().strip() for x in events}
        (out / "pmu-definitions.json").write_text(json.dumps({
            "device": a.pmu, "events": definitions,
            "scope": "system-wide events; counts are not link bytes without a verified event scale",
        }, indent=2) + "\n")
    results = []
    for repeat in range(1, a.repetitions + 1):
        policies = a.policies if repeat % 2 else list(reversed(a.policies))
        topologies = order if repeat % 2 else list(reversed(order))
        for policy in policies:
            client_mode, server_mode = POLICIES[policy]
            for n in topologies:
                label = f"dual-{a.label}-r{repeat}-{policy}-{n}c1s"
                sample = out / f"r{repeat}-{policy}-{n}c1s"
                sample.mkdir()
                wrapper = [str(root / "scripts/run_giga_native_legofs_io500.sh"),
                           "--topology", f"{n}c1s", "--profile", a.profile, "--run-id", label,
                           "--cq-wait-mode", client_mode, "--authority-wait-mode", server_mode]
                command = shlex.split(subprocess.check_output([*wrapper, "--print-command"], text=True))
                bundle = Path(command[command.index("--bundle-dir") + 1])
                run_root = Path(command[command.index("--run-root") + 1])
                if bundle.exists() or run_root.exists() or manifest.read_bytes() != source:
                    raise RuntimeError("existing run or build changed during matrix")
                measured = command
                if a.pmu:
                    measured = ["perf", "stat", "-a", "-I", "1000", "-x", ",", "-o",
                                str(sample / "perf.csv"), "-e",
                                ",".join(f"{a.pmu}/{e}/" for e in events), "--", *command]
                entry = dict(label=label, repetition=repeat, policy=policy, clients=n,
                             command=command, measured_command=measured, bundle=str(bundle),
                             started_unix=time.time(), started_monotonic=time.monotonic(),
                             build_manifest_sha256=hashlib.sha256(source).hexdigest())
                with (sample / "output.log").open("w") as log, (sample / "host-samples.jsonl").open("w") as observations:
                    process = subprocess.Popen(measured, cwd=root, stdout=log,
                                               stderr=subprocess.STDOUT, start_new_session=True)
                    entry["pid"] = process.pid
                    entry["proc_stat"] = Path(f"/proc/{process.pid}/stat").read_text()
                    (sample / "identity.json").write_text(json.dumps(entry, indent=2) + "\n")
                    try:
                        while process.poll() is None:
                            system = dict(unix=time.time(), monotonic=time.monotonic(),
                                          load=Path("/proc/loadavg").read_text().strip(),
                                          stat=Path("/proc/stat").read_text(),
                                          memory=Path("/proc/meminfo").read_text())
                            system["top_cpu"] = subprocess.check_output(
                                ["ps", "-eo", "pid,ppid,comm,pcpu,pmem", "--sort=-pcpu"], text=True
                            ).splitlines()[:26]
                            system["clock_ticks_per_second"] = os.sysconf("SC_CLK_TCK")
                            system["process_stats"] = {}
                            for row in system["top_cpu"][1:]:
                                pid = row.split()[0]
                                try:
                                    system["process_stats"][pid] = Path(f"/proc/{pid}/stat").read_text()
                                except FileNotFoundError:
                                    pass  # A short-lived observed process already exited.
                            observations.write(json.dumps(system) + "\n")
                            observations.flush()
                            time.sleep(5)
                    finally:
                        if process.poll() is None:
                            os.killpg(process.pid, signal.SIGINT)
                            process.wait(timeout=60)
                entry.update(rc=process.returncode, finished_unix=time.time(),
                             finished_monotonic=time.monotonic())
                entry.update(accepted_case(process.returncode, bundle))
                results.append(entry)
                (sample / "result.json").write_text(json.dumps(entry, indent=2) + "\n")
                (out / "matrix.json").write_text(json.dumps(results, indent=2) + "\n")
                print(json.dumps(entry), flush=True)
                if not entry["accepted"]:
                    return process.returncode or 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
