#!/usr/bin/env python3
import argparse
import dataclasses
import datetime
import hashlib
import json
import os
import pathlib
import re
import selectors
import signal
import struct
import subprocess
import sys
import time


HOST_DECODER = (
    "CXL host decoder0: HPA 0000001000000000 "
    "size 0000000010000000 target 0 ctrl 00000600"
)
TYPE3_DECODER = (
    "41.00.0 Type 3 decoder0: HPA 0000001000000000 "
    "size 0000000010000000 target 0 ctrl 00001600"
)
SHM_CONNECTED = "CXL Type3: SHM connected to /cxlmemsim_pgas"
GUEST_PASS = "CXL_QEMU_UBOOT_LINUX_BENCH_PASS"
SHM_ERRORS = (
    "Failed to open shared memory",
    "Failed to open SHM",
    "SHM invalid magic",
    "SHM server not ready",
    "SHM slot busy timeout",
    "SHM request timeout",
)
REQUIRED_GUEST_MARKERS = (
    "CXL_GUEST_INIT_START",
    "CXL_DISK_PASS",
    "CXL_TOPOLOGY_PASS",
    GUEST_PASS,
)
SHM_MAGIC = 0x43584C53484D454D
SHM_VERSION = 1
SHM_CAPACITY = 268435456
SHM_PATH = pathlib.Path("/dev/shm/cxlmemsim_pgas")
SHM_FORMAT = "<QIIIIQQQII8x"
SHM_HEADER_SIZE = struct.calcsize(SHM_FORMAT)


@dataclasses.dataclass(frozen=True)
class RuntimePaths:
    qemu_dir: pathlib.Path
    bios: pathlib.Path
    uboot: pathlib.Path
    linux: pathlib.Path
    disk: pathlib.Path
    server: pathlib.Path
    topology: pathlib.Path
    manifest: pathlib.Path
    logs: pathlib.Path
    results: pathlib.Path


class Console:
    def __init__(
        self,
        command,
        timeout,
        environment,
        log_path,
    ):
        self.timeout = timeout
        self.output = ""
        self.log_path = pathlib.Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log = self.log_path.open("w", encoding="utf-8")
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            env=environment,
        )
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)

    def _read_once(self, timeout):
        read_data = False
        for key, _events in self.selector.select(timeout):
            data = os.read(key.fileobj.fileno(), 4096)
            if not data:
                continue
            decoded = data.decode(errors="replace")
            self.output += decoded
            self.log.write(decoded)
            self.log.flush()
            sys.stdout.write(decoded)
            sys.stdout.flush()
            read_data = True
        return read_data

    def wait(self, text, start=0):
        deadline = time.monotonic() + self.timeout
        while text not in self.output[start:]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"timed out waiting for {text!r}")
            self._read_once(min(remaining, 0.5))
            if self.process.poll() is not None:
                self._read_once(0)
                if text not in self.output[start:]:
                    raise RuntimeError(
                        f"QEMU exited with {self.process.returncode} "
                        f"while waiting for {text!r}"
                    )

    def send(self, command):
        if self.process.stdin is None:
            raise RuntimeError("QEMU console stdin is unavailable")
        self.process.stdin.write((command + "\n").encode())
        self.process.stdin.flush()

    def wait_for_exit(self, timeout):
        deadline = time.monotonic() + timeout
        while self.process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for QEMU to exit")
            self._read_once(min(remaining, 0.5))
        while self._read_once(0):
            pass
        return self.process.returncode

    def terminate(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)

    def close(self):
        try:
            self.terminate()
            while self._read_once(0):
                pass
        finally:
            self.selector.close()
            if self.process.stdin is not None:
                self.process.stdin.close()
            if self.process.stdout is not None:
                self.process.stdout.close()
            self.log.close()


def build_qemu_command(paths):
    return [
        "qemu-system-riscv64",
        "-M",
        "sifive_u",
        "-machine",
        "cxl=on",
        "-machine",
        (
            "cxl-fmw.0.targets.0=cxl.1,cxl-fmw.0.size=4G,"
            "cxl-fmw.0.restrictions=0xe"
        ),
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
        str(paths.bios),
        "-kernel",
        str(paths.uboot),
        "-device",
        f"loader,file={paths.linux},addr=0x90000000,force-raw=on",
        "-object",
        "memory-backend-ram,id=t3mem,size=256M,share=on",
        "-object",
        "memory-backend-ram,id=t3lsa,size=2M,share=on",
        "-device",
        "pxb-cxl,bus=pcie.0,bus_nr=64,id=cxl.1,hdm_for_passthrough=on",
        "-device",
        "cxl-rp,bus=cxl.1,port=0,id=rp-t3,chassis=0,slot=0",
        "-device",
        "cxl-type3,bus=rp-t3,volatile-memdev=t3mem,lsa=t3lsa,id=t3",
        "-drive",
        f"file={paths.disk},if=none,format=raw,readonly=on,id=bench",
        "-device",
        "virtio-blk-pci,drive=bench,bus=pcie.0",
    ]


def build_qemu_environment(paths, base_environment=None):
    environment = dict(os.environ if base_environment is None else base_environment)
    old_path = environment.get("PATH", "")
    environment["PATH"] = str(paths.qemu_dir)
    if old_path:
        environment["PATH"] += os.pathsep + old_path
    environment["CXL_TRANSPORT_MODE"] = "shm"
    environment["CXL_PGAS_SHM"] = "/cxlmemsim_pgas"
    environment["CXL_LATENCY_INJECT"] = "0"
    return environment


def parse_shm_header(data):
    if len(data) < SHM_HEADER_SIZE:
        raise ValueError(
            f"CXLMemSim SHM header is short: {len(data)} bytes"
        )
    (
        magic,
        version,
        num_slots,
        server_ready,
        flags,
        memory_base,
        memory_size,
        num_cachelines,
        metadata_enabled,
        entry_size,
    ) = struct.unpack(SHM_FORMAT, data[:SHM_HEADER_SIZE])
    if magic != SHM_MAGIC:
        raise ValueError(f"CXLMemSim SHM magic is invalid: 0x{magic:x}")
    if version != SHM_VERSION:
        raise ValueError(f"CXLMemSim SHM version is invalid: {version}")
    if not num_slots or num_slots > 64:
        raise ValueError(f"CXLMemSim SHM slot count is invalid: {num_slots}")
    if server_ready != 1:
        raise ValueError("CXLMemSim SHM server is not ready")
    if memory_size != SHM_CAPACITY:
        raise ValueError(
            f"CXLMemSim SHM capacity is invalid: {memory_size}"
        )
    return {
        "magic": magic,
        "version": version,
        "num_slots": num_slots,
        "server_ready": server_ready,
        "flags": flags,
        "memory_base": memory_base,
        "memory_size": memory_size,
        "num_cachelines": num_cachelines,
        "metadata_enabled": metadata_enabled,
        "entry_size": entry_size,
    }


def server_command(paths):
    return [
        str(paths.server),
        "--comm-mode=pgas-shm",
        "--pgas-shm-name=/cxlmemsim_pgas",
        "--capacity=256",
        "--default_latency=100",
        f"--topology={paths.topology.resolve()}",
    ]


def wait_for_shm(
    process,
    shm_path=SHM_PATH,
    timeout=30,
    monotonic=time.monotonic,
    sleeper=time.sleep,
):
    deadline = monotonic() + timeout
    last_error = None
    while monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            raise RuntimeError(
                f"CXLMemSim exited before SHM became ready: {return_code}"
            )
        try:
            with pathlib.Path(shm_path).open("rb") as source:
                return parse_shm_header(source.read(SHM_HEADER_SIZE))
        except (FileNotFoundError, ValueError) as error:
            last_error = error
        sleeper(0.05)
    detail = f": {last_error}" if last_error else ""
    raise TimeoutError(f"timed out waiting for CXLMemSim SHM{detail}")


def start_server(
    paths,
    shm_path=SHM_PATH,
    popen_factory=subprocess.Popen,
):
    shm_path = pathlib.Path(shm_path)
    if shm_path.exists():
        raise FileExistsError(
            f"refusing to reuse existing SHM object: {shm_path}"
        )
    paths.logs.mkdir(parents=True, exist_ok=True)
    log_file = (paths.logs / "cxlmemsim-server.log").open(
        "w", encoding="utf-8"
    )
    process = None
    try:
        process = popen_factory(
            server_command(paths),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )
        process._cxlmemsim_log = log_file
        header = wait_for_shm(process, shm_path=shm_path)
        return process, header
    except BaseException:
        if process is not None:
            stop_owned_process(process)
        if not log_file.closed:
            log_file.close()
        raise


def stop_owned_process(process):
    if process is None:
        return
    try:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    finally:
        log_file = getattr(process, "__dict__", {}).get("_cxlmemsim_log")
        if log_file is not None:
            log_file.close()


def run_console_command(console, command):
    start = len(console.output)
    console.send(command)
    console.wait("=> ", start=start)
    return console.output[start:]


def run_uboot_guest(console, paths, benchmark_bytes):
    console.wait("=> ")
    if HOST_DECODER not in console.output or TYPE3_DECODER not in console.output:
        raise ValueError("preboot U-Boot decoder proof is missing")

    listing = run_console_command(console, "cxl list")
    if "41.00.0" not in listing or "Type 3" not in listing:
        raise ValueError("cxl list did not report the Type 3 endpoint")

    information = run_console_command(console, "cxl info 41.00.0")
    if (
        "41.00.0" not in information
        or "0000000010000000" not in information
    ):
        raise ValueError("cxl info did not report the expected endpoint")

    initialization = run_console_command(console, "cxl init")
    if HOST_DECODER not in initialization or TYPE3_DECODER not in initialization:
        raise ValueError("second cxl init did not reproduce decoder state")

    bootargs = (
        "setenv bootargs 'earlycon=sbi console=hvc0 loglevel=4 "
        f"cxl_bench_bytes={benchmark_bytes}'"
    )
    run_console_command(console, bootargs)

    image_size = paths.linux.stat().st_size
    start = len(console.output)
    console.send(
        f"bootefi 90000000:{image_size:x} ${{fdtcontroladdr}}"
    )
    console.wait(GUEST_PASS, start=start)
    return validate_console(console.output)


def atomic_write_result(output, value):
    output = pathlib.Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = pathlib.Path(str(output) + ".tmp")
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


def build_result(
    command,
    environment,
    manifest,
    header,
    guest,
    read_count,
    write_count,
):
    shm = dict(header)
    shm["name"] = "/cxlmemsim_pgas"
    shm["magic_hex"] = f"0x{header['magic']:016x}"
    return {
        "schema_version": 1,
        "timestamp_utc": datetime.datetime.now(
            datetime.timezone.utc
        ).isoformat(),
        "command": list(command),
        "environment": dict(environment),
        "superproject_commit": manifest["superproject_commit"],
        "submodules": manifest["submodules"],
        "artifacts": manifest["artifacts"],
        "shm": shm,
        "topology": {
            "machine": "sifive_u",
            "endpoint": "0000:41:00.0",
            "endpoint_type": "CXL Type 3",
            "hpa_base": "0x1000000000",
            "window_bytes": 268435456,
            "host_decoder_control": "0x600",
            "endpoint_decoder_control": "0x1600",
            "hdm_for_passthrough": True,
        },
        "guest": guest,
        "server": {
            "total_reads": read_count,
            "total_writes": write_count,
        },
        "proofs": {
            "shm_ready": True,
            "qemu_shm_connected": True,
            "uboot_decoder_reinitialized": True,
            "linux_cxl_topology": True,
            "guest_bit_exact": True,
            "server_reads_nonzero": True,
            "server_writes_nonzero": True,
            "shm_cleaned_up": True,
        },
        "latency_injection": False,
        "interpretation": (
            "QEMU/TCG plus synchronous CXLMemSim software transport; "
            "not real CXL hardware performance."
        ),
    }


def sha256_file(path):
    digest = hashlib.sha256()
    with pathlib.Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_paths(paths):
    return {
        "qemu": (paths.qemu_dir / "qemu-system-riscv64").resolve(),
        "opensbi": paths.bios.resolve(),
        "u_boot": paths.uboot.resolve(),
        "linux": paths.linux.resolve(),
        "benchmark_disk": paths.disk.resolve(),
        "guest_benchmark": (paths.disk.parent / "cxl_mmap_bench").resolve(),
        "cxlmemsim_server": paths.server.resolve(),
    }


def load_verified_manifest(paths):
    if not paths.manifest.is_file():
        raise FileNotFoundError(f"missing build manifest: {paths.manifest}")
    manifest = json.loads(paths.manifest.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise ValueError("unsupported build manifest schema")
    recorded = manifest.get("artifacts")
    if not isinstance(recorded, dict):
        raise ValueError("build manifest artifacts are missing")
    for name, path in artifact_paths(paths).items():
        if not path.is_file():
            raise FileNotFoundError(f"missing runtime artifact: {path}")
        entry = recorded.get(name)
        if not isinstance(entry, dict):
            raise ValueError(f"build manifest is missing artifact: {name}")
        if entry.get("size") != path.stat().st_size:
            raise ValueError(f"artifact size changed after build: {name}")
        if entry.get("sha256") != sha256_file(path):
            raise ValueError(f"artifact hash changed after build: {name}")
    root = paths.manifest.parents[2]
    current_commit = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    if manifest.get("superproject_commit") != current_commit:
        raise ValueError("superproject changed after the build manifest")
    return manifest


def wait_for_shm_removed(
    shm_path=SHM_PATH,
    timeout=5,
    monotonic=time.monotonic,
    sleeper=time.sleep,
):
    deadline = monotonic() + timeout
    shm_path = pathlib.Path(shm_path)
    while shm_path.exists() and monotonic() < deadline:
        sleeper(0.05)
    if shm_path.exists():
        raise RuntimeError(
            f"harness-owned SHM object remains after cleanup: {shm_path}"
        )


def finish_qemu(console):
    console.wait("reboot: Power down")
    try:
        return_code = console.wait_for_exit(timeout=5)
    except TimeoutError:
        console.terminate()
        return "terminated-by-harness"
    if return_code != 0:
        raise RuntimeError(f"QEMU exited with status {return_code}")
    return "guest-poweroff"


def execute_workflow(paths, benchmark_bytes, timeout, shm_path=SHM_PATH):
    manifest = load_verified_manifest(paths)
    command = build_qemu_command(paths)
    environment = build_qemu_environment(paths)
    server = None
    console = None
    guest = None
    header = None
    try:
        server, header = start_server(paths, shm_path=shm_path)
        console = Console(
            command,
            timeout=timeout,
            environment=environment,
            log_path=paths.logs / "qemu-console.log",
        )
        guest = run_uboot_guest(
            console,
            paths,
            benchmark_bytes=benchmark_bytes,
        )
        finish_qemu(console)
    finally:
        if console is not None:
            console.close()
        if server is not None:
            stop_owned_process(server)

    wait_for_shm_removed(shm_path=shm_path)
    server_log = (paths.logs / "cxlmemsim-server.log").read_text(
        encoding="utf-8",
        errors="replace",
    )
    read_count, write_count = parse_server_counts(server_log)
    selected_environment = {
        name: environment[name]
        for name in (
            "CXL_TRANSPORT_MODE",
            "CXL_PGAS_SHM",
            "CXL_LATENCY_INJECT",
        )
    }
    result = build_result(
        command=command,
        environment=selected_environment,
        manifest=manifest,
        header=header,
        guest=guest,
        read_count=read_count,
        write_count=write_count,
    )
    atomic_write_result(
        paths.results / "type3-shm-result.json",
        result,
    )
    return result


def default_paths(root):
    root = pathlib.Path(root).resolve()
    return RuntimePaths(
        qemu_dir=root / "out/runtime-bin",
        bios=(
            root
            / "out/build/opensbi/platform/generic/firmware/fw_dynamic.bin"
        ),
        uboot=root / "out/build/u-boot/u-boot.bin",
        linux=root / "out/build/linux/arch/riscv/boot/Image",
        disk=root / "out/images/cxl-type3-benchmark.ext2",
        server=root / "out/build/cxlmemsim/cxlmemsim_server",
        topology=(
            root
            / "components/cxlmemsim/qemu_integration/topology_simple.txt"
        ),
        manifest=root / "out/results/build-manifest.json",
        logs=root / "out/logs",
        results=root / "out/results",
    )


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--benchmark-bytes",
        type=int,
        default=1048576,
    )
    parser.add_argument("--timeout", type=int, default=600)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if (
        args.benchmark_bytes <= 0
        or args.benchmark_bytes % 8
        or args.benchmark_bytes > SHM_CAPACITY
    ):
        raise ValueError(
            "benchmark bytes must be positive, 8-byte aligned, "
            "and no larger than 268435456"
        )
    if args.timeout <= 0:
        raise ValueError("timeout must be positive")
    root = pathlib.Path(__file__).resolve().parents[1]
    result = execute_workflow(
        default_paths(root),
        benchmark_bytes=args.benchmark_bytes,
        timeout=args.timeout,
    )
    print(GUEST_PASS)
    print(
        "CXLMemSim requests: "
        f"reads={result['server']['total_reads']} "
        f"writes={result['server']['total_writes']}"
    )
    return 0


def extract_guest_json(output):
    prefix = "CXL_BENCH_JSON "
    matches = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped.startswith(prefix):
            continue
        try:
            candidate = json.loads(stripped[len(prefix) :])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            matches.append(candidate)
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one guest benchmark JSON object, got {len(matches)}"
        )
    return matches[0]


def validate_benchmark(result):
    if result.get("status") != "pass" or result.get("verified") is not True:
        raise ValueError("guest benchmark did not report verified pass")
    for name in ("write_seconds", "read_seconds"):
        samples = result.get(name)
        if (
            not isinstance(samples, list)
            or len(samples) != 3
            or any(
                not isinstance(value, (int, float)) or value <= 0
                for value in samples
            )
        ):
            raise ValueError(f"invalid guest benchmark field: {name}")
    for name in (
        "write_mib_s_median",
        "read_mib_s_median",
        "random_ns_per_load",
    ):
        value = result.get(name)
        if not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(f"invalid guest benchmark field: {name}")


def parse_server_counts(output):
    reads = re.findall(r"Total Reads:\s*([0-9]+)", output)
    writes = re.findall(r"Total Writes:\s*([0-9]+)", output)
    if not reads or not writes:
        raise ValueError("CXLMemSim final read/write counters are missing")
    read_count = int(reads[-1])
    write_count = int(writes[-1])
    if read_count <= 0 or write_count <= 0:
        raise ValueError(
            "CXLMemSim final read/write counters must both be positive"
        )
    return read_count, write_count


def validate_console(output):
    for error in SHM_ERRORS:
        if error in output:
            raise ValueError(f"QEMU SHM transport error: {error}")
    for marker in (HOST_DECODER, TYPE3_DECODER, SHM_CONNECTED):
        if marker not in output:
            raise ValueError(f"missing firmware or QEMU proof: {marker}")
    for marker in REQUIRED_GUEST_MARKERS:
        if marker not in output:
            raise ValueError(f"missing guest proof: {marker}")
    result = extract_guest_json(output)
    validate_benchmark(result)
    return result


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        FileNotFoundError,
        OSError,
        RuntimeError,
        TimeoutError,
        ValueError,
        subprocess.CalledProcessError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
