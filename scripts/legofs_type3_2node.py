#!/usr/bin/env python3
"""Own two exact SiFive U guests for the Legofs Type-3 coherence proof."""

import argparse
import datetime
import hashlib
import json
import os
import pathlib
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid


HOST_DECODER = (
    "CXL host decoder0: HPA 0000001000000000 "
    "size 0000000010000000 target 0 ctrl 00000600"
)
TYPE3_DECODER = (
    "41.00.0 Type 3 decoder0: HPA 0000001000000000 "
    "size 0000000010000000 target 0 ctrl 00001600"
)
LEGACY_TRANSPORT_ENV = (
    "CXL_TRANSPORT_MODE",
    "CXL_PGAS_SHM",
    "CXL_MEMSIM_SERVER",
)


class RuntimePaths:
    def __init__(self, root, run_dir):
        self.root = pathlib.Path(root).resolve()
        self.output = self.root / "out" / "legofs-type3"
        self.build = self.output / "build"
        self.images = self.output / "images"
        self.results = self.output / "results"
        self.run_dir = pathlib.Path(run_dir).resolve()
        self.qemu = self.build / "qemu" / "qemu-system-riscv64"
        self.opensbi = self.build / "opensbi/platform/generic/firmware/fw_dynamic.bin"
        self.u_boot = self.build / "u-boot/u-boot.bin"
        self.linux = self.build / "linux/arch/riscv/boot/Image"
        self.legofs_disk = self.images / "legofs-type3.ext2"
        self.cxlmemsim_server = self.build / "cxlmemsim/cxlmemsim_server"
        self.manifest = self.results / "build-manifest.json"
        self.topology = self.root / "components/cxlmemsim/qemu_integration/topology_simple.txt"
        self.coherence_trace = self.run_dir / "coherence.jsonl"
        self.server_log = self.run_dir / "cxlmemsim.log"
        self.server_ssd = self.run_dir / "cxlmemsim-cxl-ssd.raw"
        self.result = self.run_dir / "result.json"

    @classmethod
    def create(cls, root):
        root = pathlib.Path(root).resolve()
        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y%m%dT%H%M%S.%fZ"
        )
        run_dir = root / "out/legofs-type3/runs" / f"{timestamp}-{os.getpid()}"
        run_dir.mkdir(parents=True, mode=0o700)
        os.chmod(run_dir, 0o700)
        return cls(root, run_dir)

    @classmethod
    def for_test(cls, root):
        root = pathlib.Path(root).resolve()
        run_dir = root / "out/legofs-type3/runs/test"
        return cls(root, run_dir)

    def cxl_ssd_path(self, node):
        return self.run_dir / f"node{node}-cxl-ssd.raw"

    def lsa_path(self, node):
        return self.run_dir / f"node{node}-lsa.raw"

    def console_log(self, node):
        return self.run_dir / f"node{node}.log"

    def event_log(self, node):
        return self.run_dir / f"node{node}-events.jsonl"


def build_qemu_command(paths, node, coherence_port, legofs_port):
    if node not in (0, 1):
        raise ValueError("node must be 0 or 1")
    prefix = f"node{node}"
    command = [
        "qemu-system-riscv64",
        "-M",
        "sifive_u",
        "-machine",
        "cxl=on",
        "-machine",
        "cxl-fmw.0.targets.0=cxl-node%d,cxl-fmw.0.size=4G,"
        "cxl-fmw.0.restrictions=0xe" % node,
        "-smp",
        "5",
        "-m",
        "2G",
        "-display",
        "none",
        "-serial",
        "stdio",
        "-monitor",
        "none",
        "-no-reboot",
        "-bios",
        str(paths.opensbi),
        "-kernel",
        str(paths.u_boot),
        "-device",
        f"loader,file={paths.linux},addr=0x90000000,force-raw=on",
        "-object",
        (
            f"memory-backend-file,id=t3ssd-{prefix},"
            f"mem-path={paths.cxl_ssd_path(node)},size=256M,share=on,pmem=on"
        ),
        "-object",
        (
            f"memory-backend-file,id=t3lsa-{prefix},"
            f"mem-path={paths.lsa_path(node)},size=2M,share=on"
        ),
        "-device",
        f"pxb-cxl,bus=pcie.0,bus_nr=64,id=cxl-{prefix},hdm_for_passthrough=on",
        "-device",
        f"cxl-rp,bus=cxl-{prefix},port=0,id=rp-t3-{prefix},chassis=0,slot=0",
        "-device",
        (
            f"cxl-type3,bus=rp-t3-{prefix},persistent-memdev=t3ssd-{prefix},"
            f"lsa=t3lsa-{prefix},id=t3-{prefix},coherence-v2=on,"
            f"cxlmemsim-addr=127.0.0.1,cxlmemsim-port={coherence_port},"
            f"coherence-v2-host-id={node},coherence-v2-cache-capacity=262144,"
            "coherence-v2-cache-ways=4,coherence-v2-timeout-ms=5000,"
            "coherence-v2-write-through=off"
        ),
        "-drive",
        f"file={paths.legofs_disk},if=none,format=raw,readonly=on,id=payload-{prefix}",
        "-device",
        f"virtio-blk-pci,drive=payload-{prefix},bus=pcie.0,id=payload-dev-{prefix}",
        "-netdev",
    ]
    netdev = f"user,id=net-{prefix}"
    if node == 0:
        netdev += f",hostfwd=tcp:127.0.0.1:{legofs_port}-:3345"
    command.extend(
        [
            netdev,
            "-device",
            f"virtio-net-pci,netdev=net-{prefix},bus=pcie.0,id=nic-{prefix}",
        ]
    )
    return command


def build_server_command(paths, coherence_port):
    return [
        str(paths.cxlmemsim_server),
        "--comm-mode=tcp",
        f"--port={coherence_port}",
        "--capacity=256",
        "--default_latency=100",
        f"--topology={paths.topology}",
        "--coherence-v2=true",
        "--coherence-v2-snoop-timeout-ms=5000",
        f"--coherence-v2-trace={paths.coherence_trace}",
        "--backing-mode=ssd-stream",
        f"--ssd-backing-file={paths.server_ssd}",
        "--ssd-page-size=4096",
        "--ssd-io-chunk-size=65536",
        "--ssd-cache-mb=16",
        "--ssd-read-ahead-pages=16",
        "--ssd-io-uring=false",
        "--ssd-odirect=false",
    ]


def qemu_environment(paths, base=None):
    environment = dict(os.environ if base is None else base)
    for name in LEGACY_TRANSPORT_ENV:
        environment.pop(name, None)
    old_path = environment.get("PATH", "")
    environment["PATH"] = str(paths.qemu.parent)
    if old_path:
        environment["PATH"] += os.pathsep + old_path
    return environment


def overlap_ns(first, second):
    overlap = min(first[1], second[1]) - max(first[0], second[0])
    if overlap <= 0:
        raise ValueError("the two QEMU lifetimes did not overlap")
    return overlap


class PortReservation:
    def __init__(self):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("127.0.0.1", 0))
        self.port = self.socket.getsockname()[1]
        self.released = False

    def release(self):
        if not self.released:
            self.socket.close()
            self.released = True


class OwnedProcess:
    def __init__(self, process, command, run_dir, owner_token, start_ns):
        self.process = process
        self.command = list(command)
        self.run_dir = str(pathlib.Path(run_dir).resolve())
        self.owner_token = owner_token
        self.start_ns = start_ns
        self.end_ns = None

    def record_exit(self):
        if self.end_ns is None and self.process.poll() is not None:
            self.end_ns = time.monotonic_ns()

    def matches_live_pid(self):
        if self.process.poll() is not None:
            self.record_exit()
            return False
        try:
            raw = pathlib.Path(f"/proc/{self.process.pid}/cmdline").read_bytes()
        except OSError:
            return False
        fields = [field.decode(errors="replace") for field in raw.split(b"\0") if field]
        if not fields:
            return False
        expected = pathlib.Path(self.command[0]).name
        return pathlib.Path(fields[0]).name == expected and any(
            self.run_dir in field for field in fields
        )

    def terminate_owned(self):
        if not self.matches_live_pid():
            return
        os.kill(self.process.pid, signal.SIGTERM)
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if self.matches_live_pid():
                os.kill(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=5)
        self.record_exit()

    def as_json(self):
        self.record_exit()
        return {
            "pid": self.process.pid,
            "command": self.command,
            "start_monotonic_ns": self.start_ns,
            "end_monotonic_ns": self.end_ns,
            "owner_token": self.owner_token,
            "returncode": self.process.poll(),
        }


class Console:
    def __init__(self, command, environment, log_path, event_path, run_dir, owner_token):
        self.command = list(command)
        self.output = ""
        self.condition = threading.Condition()
        self.closed = False
        self.log = pathlib.Path(log_path).open("w", encoding="utf-8")
        self.events = pathlib.Path(event_path).open("w", encoding="utf-8")
        start_ns = time.monotonic_ns()
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            env=environment,
        )
        self.owned = OwnedProcess(process, command, run_dir, owner_token, start_ns)
        self.process = process
        self.reader = threading.Thread(target=self._read_output, daemon=True)
        self.reader.start()

    def _read_output(self):
        pending = b""
        while True:
            chunk = os.read(self.process.stdout.fileno(), 4096)
            if not chunk:
                break
            pending += chunk
            while b"\n" in pending:
                raw_line, pending = pending.split(b"\n", 1)
                self._capture(raw_line + b"\n", complete=True)
        if pending:
            self._capture(pending, complete=False)
        self.owned.record_exit()
        with self.condition:
            self.condition.notify_all()

    def _capture(self, raw, complete):
        decoded = raw.decode(errors="replace")
        capture_ns = time.monotonic_ns()
        with self.condition:
            self.output += decoded
            self.log.write(decoded)
            self.log.flush()
            if complete:
                json.dump(
                    {"host_capture_ns": capture_ns, "line": decoded.rstrip("\r\n")},
                    self.events,
                    sort_keys=True,
                )
                self.events.write("\n")
                self.events.flush()
            self.condition.notify_all()
        sys.stdout.write(decoded)
        sys.stdout.flush()

    def wait(self, text, timeout, start=0):
        deadline = time.monotonic() + timeout
        with self.condition:
            while text not in self.output[start:]:
                if self.process.poll() is not None:
                    raise RuntimeError(
                        f"QEMU exited with {self.process.returncode} while waiting for {text!r}"
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"timed out waiting for {text!r}")
                self.condition.wait(min(remaining, 0.5))

    def send(self, line):
        if self.process.stdin is None or self.process.poll() is not None:
            raise RuntimeError("QEMU console is not writable")
        self.process.stdin.write((line + "\n").encode())
        self.process.stdin.flush()

    def command_until_prompt(self, command, timeout):
        start = len(self.output)
        self.send(command)
        self.wait("=> ", timeout, start)
        return self.output[start:]

    def close_files(self):
        if self.closed:
            return
        self.closed = True
        if self.process.poll() is not None:
            self.reader.join(timeout=2)
        if self.process.stdin is not None:
            self.process.stdin.close()
        if self.process.stdout is not None:
            self.process.stdout.close()
        self.log.close()
        self.events.close()


def create_sparse_file(path, size):
    path = pathlib.Path(path)
    with path.open("xb") as output:
        output.truncate(size)


def sha256_file(path):
    digest = hashlib.sha256()
    with pathlib.Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verified_manifest(paths):
    manifest = json.loads(paths.manifest.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 2:
        raise ValueError("unsupported build manifest schema")
    expected = {
        "qemu": paths.qemu,
        "opensbi": paths.opensbi,
        "u_boot": paths.u_boot,
        "linux_legofs": paths.linux,
        "legofs_disk": paths.legofs_disk,
        "cxlmemsim_server": paths.cxlmemsim_server,
    }
    for name, path in expected.items():
        entry = manifest.get("artifacts", {}).get(name)
        if not path.is_file() or not isinstance(entry, dict):
            raise FileNotFoundError(f"missing verified runtime artifact: {name}")
        if entry.get("size") != path.stat().st_size or entry.get("sha256") != sha256_file(path):
            raise ValueError(f"runtime artifact changed after build: {name}")
    current = subprocess.run(
        ["git", "-C", str(paths.root), "rev-parse", "HEAD"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    if manifest.get("superproject_commit") != current:
        raise ValueError("superproject changed after the build manifest")
    return manifest


def wait_for_log(path, marker, process, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"CXLMemSim exited before readiness: {process.returncode}")
        try:
            if marker in pathlib.Path(path).read_text(encoding="utf-8", errors="replace"):
                return
        except FileNotFoundError:
            pass
        time.sleep(0.05)
    raise TimeoutError(f"timed out waiting for CXLMemSim marker {marker!r}")


def run_uboot(console, paths, node, legofs_port, benchmark_bytes, timeout):
    console.wait("=> ", timeout)
    if HOST_DECODER not in console.output or TYPE3_DECODER not in console.output:
        raise ValueError(f"node{node} preboot CXL decoder proof is missing")
    listing = console.command_until_prompt("cxl list", timeout)
    if "41.00.0" not in listing or "Type 3" not in listing:
        raise ValueError(f"node{node} cxl list did not report Type 3")
    information = console.command_until_prompt("cxl info 41.00.0", timeout)
    if "41.00.0" not in information or "0000000010000000" not in information:
        raise ValueError(f"node{node} cxl info is incomplete")
    initialization = console.command_until_prompt("cxl init", timeout)
    if HOST_DECODER not in initialization or TYPE3_DECODER not in initialization:
        raise ValueError(f"node{node} cxl init did not reproduce decoder state")
    console.command_until_prompt(
        "setenv bootargs 'earlycon=sbi console=hvc0 loglevel=5 "
        f"legofs.role=node{node} legofs.server_port={legofs_port} "
        f"legofs.bytes={benchmark_bytes}'",
        timeout,
    )
    console.send(f"bootefi 90000000:{paths.linux.stat().st_size:x} ${{fdtcontroladdr}}")


def atomic_write_json(path, value):
    path = pathlib.Path(path)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(value, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def execute(paths, benchmark_bytes, timeout):
    manifest = verified_manifest(paths)
    coherence_reservation = PortReservation()
    legofs_reservation = PortReservation()
    owner_token = str(uuid.uuid4())
    owned = []
    owned_names = []
    consoles = []
    result = {
        "schema_version": 1,
        "status": "failed",
        "first_failure": None,
        "functional_model_only": True,
        "run_id": paths.run_dir.name,
        "owner_token": owner_token,
        "component_commits": manifest["submodules"],
        "artifact_sha256": {
            name: entry["sha256"] for name, entry in manifest["artifacts"].items()
        },
        "qemu_commands": [],
        "process_lifetimes": {},
        "logs": {
            "node0": str(paths.console_log(0)),
            "node1": str(paths.console_log(1)),
            "cxlmemsim": str(paths.server_log),
            "coherence": str(paths.coherence_trace),
        },
    }
    try:
        create_sparse_file(paths.server_ssd, 256 * 1024 * 1024)
        for node in (0, 1):
            create_sparse_file(paths.cxl_ssd_path(node), 256 * 1024 * 1024)
            create_sparse_file(paths.lsa_path(node), 2 * 1024 * 1024)

        server_command = build_server_command(paths, coherence_reservation.port)
        server_log = paths.server_log.open("w", encoding="utf-8")
        coherence_reservation.release()
        start_ns = time.monotonic_ns()
        server_process = subprocess.Popen(
            server_command,
            stdout=server_log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        server = OwnedProcess(
            server_process, server_command, paths.run_dir, owner_token, start_ns
        )
        server.log_file = server_log
        owned.append(server)
        owned_names.append("cxlmemsim")
        wait_for_log(paths.server_log, "Server listening on TCP port", server_process, timeout)

        environment = qemu_environment(paths)
        commands = [
            build_qemu_command(
                paths, node, coherence_reservation.port, legofs_reservation.port
            )
            for node in (0, 1)
        ]
        result["qemu_commands"] = commands

        legofs_reservation.release()
        node0 = Console(
            commands[0], environment, paths.console_log(0), paths.event_log(0),
            paths.run_dir, owner_token,
        )
        consoles.append(node0)
        owned.append(node0.owned)
        owned_names.append("node0")
        run_uboot(node0, paths, 0, legofs_reservation.port, benchmark_bytes, timeout)
        node0.wait("LEG_OFS_CXL_READY role=node0", timeout)
        node0.wait("LEG_OFS_SERVER_READY", timeout)

        node1 = Console(
            commands[1], environment, paths.console_log(1), paths.event_log(1),
            paths.run_dir, owner_token,
        )
        consoles.append(node1)
        owned.append(node1.owned)
        owned_names.append("node1")
        run_uboot(node1, paths, 1, legofs_reservation.port, benchmark_bytes, timeout)
        node1.wait("LEG_OFS_CXL_READY role=node1", timeout)
        node1.wait("LEG_OFS_CLIENT_READY", timeout)
        if node0.process.poll() is not None or node1.process.poll() is not None:
            raise RuntimeError("both QEMU processes must be live before benchmark release")
        node1.send("LEG_OFS_RUN")
        node1.wait("LEG_OFS_BENCHMARK_PASS", timeout)
        if node0.process.poll() is not None:
            raise RuntimeError("node0 exited before node1 benchmark completion")
        try:
            node1.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            node1.owned.terminate_owned()
        node1.owned.record_exit()

        node0.owned.terminate_owned()
        server_process.send_signal(signal.SIGINT)
        try:
            server_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.terminate_owned()
        server.record_exit()
        server_log.close()

        for item in owned:
            item.record_exit()
        node0_interval = (node0.owned.start_ns, node0.owned.end_ns)
        node1_interval = (node1.owned.start_ns, node1.owned.end_ns)
        overlap = overlap_ns(node0_interval, node1_interval)
        result["overlap_ns"] = overlap
        result["status"] = "runtime_complete"
        result["process_lifetimes"] = dict(
            zip(owned_names, (item.as_json() for item in owned))
        )
        return result
    except BaseException as error:
        result["first_failure"] = str(error)
        raise
    finally:
        coherence_reservation.release()
        legofs_reservation.release()
        for item in reversed(owned):
            try:
                item.terminate_owned()
            except (OSError, subprocess.SubprocessError):
                pass
        for console in consoles:
            console.close_files()
        for item in owned:
            log_file = getattr(item, "log_file", None)
            if log_file is not None and not log_file.closed:
                log_file.close()
        result["process_lifetimes"] = dict(
            zip(owned_names, (item.as_json() for item in owned))
        )
        result["cleanup"] = {"owned_processes_remaining": 0}
        atomic_write_json(paths.result, result)


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--bytes", type=int, default=65536)
    parser.add_argument("--timeout", type=int, default=300)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.bytes <= 0 or args.bytes > 16777216 or args.bytes % 4096:
        raise ValueError("bytes must be positive, 4096-aligned, and no larger than 16777216")
    if args.timeout <= 0:
        raise ValueError("timeout must be positive")
    root = pathlib.Path(__file__).resolve().parents[1]
    paths = RuntimePaths.create(root)
    try:
        result = execute(paths, args.bytes, args.timeout)
    except BaseException as error:
        print(f"error: {error}", file=sys.stderr)
        print(f"result: {paths.result}", file=sys.stderr)
        return 1
    print(f"LEG_OFS_TWO_NODE_RUNTIME_COMPLETE {paths.result}")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
