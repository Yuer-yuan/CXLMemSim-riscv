#!/usr/bin/env python3
import dataclasses
import json
import os
import pathlib
import re
import signal
import struct
import subprocess
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
