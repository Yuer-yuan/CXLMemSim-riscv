#!/usr/bin/env python3
"""Own one server and two concurrent clients for the Legofs Type-3 BI loop."""

import argparse
import datetime
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

import legofs_type3_2node as base


CASES = (
    "disjoint-write",
    "same-range",
    "writer-reader-handoff",
    "shared-read",
    "client-crash",
)

BARRIERS = (
    "disjoint-created",
    "disjoint-written",
    "same-created",
    "same-written",
    "handoff-committed",
    "handoff-observed",
    "shared-read-ready",
    "shared-read-done",
    "crash-armed",
)


class BarrierServer:
    """A host-side two-party barrier that never touches the BadFS data path."""

    def __init__(self, reservation, expected=BARRIERS):
        self.expected = tuple(expected)
        if not self.expected or len(set(self.expected)) != len(self.expected):
            raise ValueError("barrier names must be nonempty and unique")
        self.expected_set = set(self.expected)
        self.port = reservation.port
        self.listener = reservation.socket
        self.listener.listen(8)
        self.listener.settimeout(0.2)
        reservation.released = True
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.arrivals = {}
        self.completed = []
        self.errors = []
        self.handlers = []
        self.active_connections = set()
        self.thread = threading.Thread(target=self._accept, daemon=True)

    def start(self):
        self.thread.start()

    def _record_error(self, error):
        with self.lock:
            self.errors.append(str(error))

    def _accept(self):
        while not self.stop_event.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError as error:
                if not self.stop_event.is_set():
                    self._record_error(f"barrier accept failed: {error}")
                return
            with self.lock:
                self.active_connections.add(connection)
            handler = threading.Thread(
                target=self._handle, args=(connection,), daemon=True
            )
            self.handlers.append(handler)
            handler.start()

    def _handle(self, connection):
        retained = False
        try:
            connection.settimeout(10)
            payload = bytearray()
            while len(payload) <= 256:
                chunk = connection.recv(256)
                if not chunk:
                    break
                payload.extend(chunk)
            if len(payload) > 256:
                raise ValueError("barrier request is too large")
            fields = payload.decode("ascii").strip().split()
            if len(fields) != 2 or fields[1] not in ("0", "1"):
                raise ValueError(f"invalid barrier request: {bytes(payload)!r}")
            name, client_text = fields
            client = int(client_text)
            peers = None
            with self.lock:
                if name not in self.expected_set:
                    raise ValueError(f"unknown barrier: {name}")
                if name in self.completed:
                    raise ValueError(f"barrier already completed: {name}")
                arrivals = self.arrivals.setdefault(name, {})
                if client in arrivals:
                    raise ValueError(
                        f"duplicate barrier arrival: {name} client={client}"
                    )
                arrivals[client] = connection
                retained = True
                if set(arrivals) == {0, 1}:
                    peers = [arrivals[0], arrivals[1]]
                    del self.arrivals[name]
                    self.completed.append(name)
            if peers is not None:
                send_errors = []
                for peer in peers:
                    try:
                        peer.sendall(b"GO\n")
                    except OSError as error:
                        send_errors.append(str(error))
                    finally:
                        peer.close()
                if send_errors:
                    raise OSError(f"barrier release failed: {send_errors}")
        except (OSError, UnicodeError, ValueError) as error:
            self._record_error(error)
        finally:
            with self.lock:
                self.active_connections.discard(connection)
            if not retained:
                try:
                    connection.close()
                except OSError:
                    pass

    def snapshot(self):
        with self.lock:
            return {
                "port": self.port,
                "completed": list(self.completed),
                "pending": {
                    name: sorted(arrivals) for name, arrivals in self.arrivals.items()
                },
                "errors": list(self.errors),
            }

    def assert_complete(self):
        state = self.snapshot()
        if state["errors"]:
            raise ValueError(f"barrier server errors: {state['errors']}")
        if set(state["completed"]) != self.expected_set or state["pending"]:
            raise ValueError(f"incomplete barrier coverage: {state}")
        return state

    def close(self):
        self.stop_event.set()
        try:
            self.listener.close()
        except OSError:
            pass
        with self.lock:
            connections = set(self.active_connections)
            for arrivals in self.arrivals.values():
                connections.update(arrivals.values())
        for connection in connections:
            try:
                connection.close()
            except OSError:
                pass
        if self.thread.is_alive():
            self.thread.join(timeout=2)
        for handler in self.handlers:
            if handler.is_alive():
                handler.join(timeout=2)


def create_paths(root):
    root = pathlib.Path(root).resolve()
    configured = os.environ.get("LEGOFS_TYPE3_OUT")
    output = pathlib.Path(configured or root / "out/legofs-type3")
    if not output.is_absolute():
        raise ValueError("LEGOFS_TYPE3_OUT must be an absolute path")
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y%m%dT%H%M%S.%fZ"
    )
    run_dir = output / "runs-2c1s" / f"{timestamp}-{os.getpid()}"
    run_dir.mkdir(parents=True, mode=0o700)
    os.chmod(run_dir, 0o700)
    return base.RuntimePaths(root, run_dir)


def validate_three_registrations(records):
    registrations = [
        record
        for record in records
        if record.get("event") == "registration" and record.get("status") == "OK"
    ]
    if len(registrations) != 3:
        raise ValueError(
            f"expected exactly three successful host registrations, got {len(registrations)}"
        )
    by_host = {}
    sessions = set()
    for record in registrations:
        host = base._required_integer(record, "src_host")
        session = base._required_integer(record, "session_id")
        if host in by_host:
            raise ValueError(f"duplicate coherence host ID: {host}")
        if host not in (0, 1, 2) or session <= 0 or session in sessions:
            raise ValueError("coherence registrations have invalid host/session identity")
        by_host[host] = record
        sessions.add(session)
    if set(by_host) != {0, 1, 2}:
        raise ValueError("coherence registrations must contain host IDs 0, 1, and 2")
    return [by_host[index] for index in range(3)]


def validate_case_outputs(outputs, benchmark_bytes):
    expected_results = {
        (case, client)
        for case in CASES
        for client in (0, 1)
        if not (case == "client-crash" and client == 0)
    }
    observed_results = {}
    for client, output in enumerate(outputs):
        records = base.parse_prefixed_records(
            output, "badfs_2c1s_result", "badfs.2c1s.case.v1"
        )
        for record in records:
            case = record.get("case")
            identity = record.get("client")
            key = (case, identity)
            if key in observed_results:
                raise ValueError(f"duplicate 2C1S result: {key}")
            if identity != client or key not in expected_results:
                raise ValueError(f"unexpected 2C1S result identity: {key}")
            if (
                record.get("status") != "passed"
                or record.get("validation") != "exact-byte-comparison"
                or record.get("bytes") != benchmark_bytes
            ):
                raise ValueError(f"invalid 2C1S result: {key}")
            observed_results[key] = record
        for case in CASES:
            marker = f"LEG_OFS_CASE_PASS case={case} client={client}"
            if output.count(marker) != 1:
                raise ValueError(f"missing or duplicate guest case marker: {marker}")
    if set(observed_results) != expected_results:
        missing = sorted(expected_results - set(observed_results))
        extra = sorted(set(observed_results) - expected_results)
        raise ValueError(f"2C1S result coverage mismatch: missing={missing}, extra={extra}")
    if outputs[0].count("BADFS_2C1S_CRASH_ARMED client=0") != 1:
        raise ValueError("client-crash did not prove an armed ungraceful client exit")
    return [observed_results[key] for key in sorted(observed_results)]


def validate_inspections(outputs):
    inspections = []
    for client, output in enumerate(outputs):
        records = base.parse_prefixed_records(
            output, "badfs_lifecycle_inspection", "badfs.lifecycle.inspection.v1"
        )
        if len(records) != 1:
            raise ValueError(f"client {client} must emit exactly one final inspection")
        inspections.extend(records)
    zero_fabric = (
        "staged_read_ops",
        "staged_read_bytes",
        "staged_write_ops",
        "staged_write_bytes",
        "blob_read_ops",
        "blob_read_bytes",
        "blob_write_ops",
        "blob_write_bytes",
        "legacy_read_file_block_ops",
        "legacy_write_file_block_ops",
        "legacy_read_fabric_block_ops",
        "legacy_write_fabric_block_ops",
        "stale_ref_rejections",
        "epoch_rejections",
        "checksum_failures",
        "quarantine_events",
        "active_leases",
        "quarantined_slots",
    )
    zero_audit = ("pending_operations", "quarantined_extents", "active_read_leases")
    owner_teardowns = 0
    direct_reads = 0
    direct_writes = 0
    for record in inspections:
        fabric = record.get("fabric")
        audit = record.get("audit")
        if not isinstance(fabric, dict) or not isinstance(audit, dict):
            raise ValueError("2C1S inspection lacks fabric/audit objects")
        for name in zero_fabric:
            if base._required_integer(fabric, name) != 0:
                raise ValueError(f"2C1S fallback/error counter is nonzero: {name}")
        for name in zero_audit:
            if base._required_integer(audit, name) != 0:
                raise ValueError(f"2C1S lifecycle audit is not quiescent: {name}")
        owner_teardowns = max(
            owner_teardowns, base._required_integer(fabric, "owner_teardowns")
        )
        direct_reads = max(
            direct_reads, base._required_integer(fabric, "trusted_direct_read_ops")
        )
        direct_writes = max(
            direct_writes, base._required_integer(fabric, "trusted_direct_write_ops")
        )
    if owner_teardowns <= 0:
        raise ValueError("client-crash did not produce server owner teardown evidence")
    if direct_reads <= 0 or direct_writes <= 0:
        raise ValueError("2C1S did not exercise strict direct read and write paths")
    return inspections


def validate_coherence(records):
    error_events = {
        "timeout",
        "protocol_error",
        "delivery_failure",
        "server_copy_failure",
    }
    for record in records:
        if record.get("event") in error_events:
            raise ValueError(f"2C1S coherence error event: {record.get('event')}")
    snoops = [record for record in records if record.get("event") == "snoop_send"]
    model_acks = [
        record
        for record in records
        if record.get("event") == "snoop_ack"
        and record.get("ack_strength") == "MODEL"
        and record.get("status") == "OK"
    ]
    dirty = [record for record in records if record.get("event") == "dirty_completion"]
    if not snoops or not model_acks or not dirty:
        raise ValueError("2C1S did not produce snoop/MODEL-ACK/dirty-completion evidence")
    return {
        "snoop_send": len(snoops),
        "model_ack": len(model_acks),
        "dirty_completion": len(dirty),
    }


def run_negative_controls():
    duplicate = [
        {"event": "registration", "status": "OK", "src_host": host, "session_id": index + 1}
        for index, host in enumerate((0, 1, 1))
    ]
    try:
        validate_three_registrations(duplicate)
    except ValueError as error:
        duplicate_host = "duplicate coherence host ID" in str(error)
    else:
        duplicate_host = False
    try:
        validate_case_outputs(("", ""), 4096)
    except ValueError:
        missing_case = True
    else:
        missing_case = False
    if not duplicate_host or not missing_case:
        raise RuntimeError("2C1S validator negative control failed")
    return {
        "duplicate_host_id_rejected": duplicate_host,
        "missing_case_evidence_rejected": missing_case,
    }


def triple_overlap_ns(processes):
    start = max(process.start_ns for process in processes)
    end = min(process.end_ns for process in processes)
    if end <= start:
        raise ValueError("the three QEMU lifetimes did not overlap")
    return end - start


def execute(paths, benchmark_bytes, timeout):
    manifest = base.load_runtime_manifest(paths)
    coherence_reservation = base.PortReservation()
    legofs_reservation = base.PortReservation()
    barrier_reservation = base.PortReservation()
    barrier = None
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
        "runtime_artifacts": {
            name: entry["path"] for name, entry in manifest["artifacts"].items()
        },
        "negative_controls": run_negative_controls(),
        "qemu_commands": [],
        "process_lifetimes": {},
        "case_durations_ns": {},
        "barrier": {"port": barrier_reservation.port},
        "logs": {
            **{f"node{node}": str(paths.console_log(node)) for node in range(3)},
            "cxlmemsim": str(paths.server_log),
            "coherence": str(paths.coherence_trace),
        },
    }
    try:
        base.create_sparse_file(paths.server_ssd, 256 * 1024 * 1024)
        for node in range(3):
            base.create_sparse_file(paths.cxl_ssd_path(node), 256 * 1024 * 1024)
            base.create_sparse_file(paths.lsa_path(node), 2 * 1024 * 1024)

        server_command = base.build_server_command(paths, coherence_reservation.port)
        server_log = paths.server_log.open("w", encoding="utf-8")
        coherence_reservation.release()
        server_process = subprocess.Popen(
            server_command,
            stdout=server_log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        server = base.OwnedProcess(
            server_process,
            server_command,
            paths.run_dir,
            owner_token,
            time.monotonic_ns(),
        )
        server.log_file = server_log
        owned.append(server)
        owned_names.append("cxlmemsim")
        base.wait_for_log(
            paths.server_log, "Server listening on TCP port", server_process, timeout
        )

        barrier = BarrierServer(barrier_reservation)
        barrier.start()

        environment = base.qemu_environment(paths)
        commands = [
            base.build_qemu_command(
                paths,
                node,
                coherence_reservation.port,
                legofs_reservation.port,
                guest_memory="1G",
            )
            for node in range(3)
        ]
        result["qemu_commands"] = commands
        legofs_reservation.release()

        for node, command in enumerate(commands):
            console = base.Console(
                command,
                environment,
                paths.console_log(node),
                paths.event_log(node),
                paths.run_dir,
                owner_token,
            )
            consoles.append(console)
            owned.append(console.owned)
            owned_names.append(f"node{node}")
            base.run_uboot(
                console,
                paths,
                node,
                legofs_reservation.port,
                benchmark_bytes,
                timeout,
                client_count=2,
                barrier_port=barrier.port,
            )
            console.wait(f"LEG_OFS_CXL_READY role=node{node}", timeout)
            if node == 0:
                console.wait("LEG_OFS_SERVER_READY", timeout)
            else:
                console.wait(
                    f"LEG_OFS_CASE_READY case={CASES[0]} client={node - 1}", timeout
                )

        if any(console.process.poll() is not None for console in consoles):
            raise RuntimeError("all three QEMU processes must be live before release")
        registrations = validate_three_registrations(
            base.read_coherence_trace(paths.coherence_trace)
        )
        result["registrations"] = registrations
        benchmark_offset = paths.coherence_trace.stat().st_size
        result["pre_benchmark_trace_offset"] = benchmark_offset

        for case_index, case in enumerate(CASES):
            if case_index:
                for client, console in enumerate(consoles[1:]):
                    console.wait(
                        f"LEG_OFS_CASE_READY case={case} client={client}", timeout
                    )
            start = time.monotonic_ns()
            consoles[1].send("LEG_OFS_RUN")
            consoles[2].send("LEG_OFS_RUN")
            for client, console in enumerate(consoles[1:]):
                console.wait(f"LEG_OFS_CASE_PASS case={case} client={client}", timeout)
            result["case_durations_ns"][case] = time.monotonic_ns() - start
            if consoles[0].process.poll() is not None:
                raise RuntimeError(f"server QEMU exited during {case}")

        for client, console in enumerate(consoles[1:]):
            console.wait(f"LEG_OFS_2C1S_COMPLETE client={client}", timeout)
            try:
                console.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                console.owned.terminate_owned()
            console.owned.record_exit()

        result["barrier"] = barrier.assert_complete()

        consoles[0].owned.terminate_owned()
        server_process.send_signal(signal.SIGINT)
        try:
            server_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.terminate_owned()
        server.record_exit()
        server_log.close()
        for item in owned:
            item.record_exit()

        result["triple_qemu_overlap_ns"] = triple_overlap_ns(
            [console.owned for console in consoles]
        )
        client_outputs = (consoles[1].output, consoles[2].output)
        result["case_results"] = validate_case_outputs(
            client_outputs, benchmark_bytes
        )
        result["inspections"] = validate_inspections(client_outputs)
        coherence = base.read_coherence_trace(
            paths.coherence_trace, benchmark_offset
        )
        result["coherence_delta"] = validate_coherence(coherence)
        result["coherence_final_stats"] = base.parse_server_stats(
            paths.server_log.read_text(encoding="utf-8", errors="replace")
        )
        result["topology"] = {
            "machine": "sifive_u",
            "nodes": 3,
            "servers": 1,
            "clients": 2,
            "type3_endpoints": 3,
            "type3_per_node": 1,
            "cxl_ssd_bytes_per_node": 256 * 1024 * 1024,
            "persistent_memdev": True,
            "server_backing": "ssd-stream",
            "coherence_transport": "tcp-mesi-v2",
            "qemu_type3_backinvalidation": True,
        }
        result["status"] = "passed"
        return result
    except BaseException as error:
        result["first_failure"] = str(error)
        raise
    finally:
        coherence_reservation.release()
        legofs_reservation.release()
        barrier_reservation.release()
        for item in reversed(owned):
            try:
                item.terminate_owned()
            except (OSError, subprocess.SubprocessError):
                pass
        for console in consoles:
            console.close_files()
        if barrier is not None:
            result["barrier"] = barrier.snapshot()
            barrier.close()
        for item in owned:
            log_file = getattr(item, "log_file", None)
            if log_file is not None and not log_file.closed:
                log_file.close()
        result["process_lifetimes"] = dict(
            zip(owned_names, (item.as_json() for item in owned))
        )
        remaining = sum(item.matches_live_pid() for item in owned)
        result["cleanup"] = {"owned_processes_remaining": remaining}
        base.atomic_write_json(paths.result, result)


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--bytes", type=int, default=65536)
    parser.add_argument("--timeout", type=int, default=300)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.bytes < 4096 or args.bytes > 16777216 or args.bytes % 4096:
        raise ValueError(
            "bytes must be 4096-aligned, at least 4096, and no larger than 16777216"
        )
    if args.timeout <= 0:
        raise ValueError("timeout must be positive")
    root = pathlib.Path(__file__).resolve().parents[1]
    paths = create_paths(root)
    try:
        result = execute(paths, args.bytes, args.timeout)
    except BaseException as error:
        print(f"error: {error}", file=sys.stderr)
        print(f"result: {paths.result}", file=sys.stderr)
        return 1
    print(f"LEG_OFS_THREE_NODE_RUNTIME_COMPLETE {paths.result}")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
