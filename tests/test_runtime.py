import dataclasses
import importlib.util
import json
import pathlib
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run.py"


def load_runtime():
    if not SCRIPT.is_file():
        return None
    specification = importlib.util.spec_from_file_location("runtime", SCRIPT)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


RUNTIME = load_runtime()


class RuntimeContractTest(unittest.TestCase):
    def runtime(self):
        self.assertIsNotNone(RUNTIME, "scripts/run.py is missing")
        return RUNTIME

    def require_api(self, name):
        runtime = self.runtime()
        self.assertTrue(
            hasattr(runtime, name),
            f"scripts/run.py is missing {name}()",
        )
        return getattr(runtime, name)

    def paths(self):
        runtime = self.runtime()
        return runtime.RuntimePaths(
            qemu_dir=pathlib.Path("/x/runtime-bin"),
            bios=pathlib.Path("/x/fw_dynamic.bin"),
            uboot=pathlib.Path("/x/u-boot.bin"),
            linux=pathlib.Path("/x/Image"),
            disk=pathlib.Path("/x/benchmark.ext2"),
            server=pathlib.Path("/x/cxlmemsim_server"),
            topology=pathlib.Path("/x/topology_simple.txt"),
            manifest=pathlib.Path("/x/build-manifest.json"),
            logs=pathlib.Path("/x/logs"),
            results=pathlib.Path("/x/results"),
        )

    def test_command_preserves_sifive_u_type3_topology(self):
        runtime = self.runtime()
        command = runtime.build_qemu_command(self.paths())
        self.assertEqual(
            command[:3],
            ["qemu-system-riscv64", "-M", "sifive_u"],
        )
        joined = " ".join(command)
        self.assertIn("hdm_for_passthrough=on", joined)
        self.assertIn("cxl-type3", joined)
        self.assertNotIn("cxl-type2", joined)
        self.assertIn("virtio-blk-pci,drive=bench,bus=pcie.0", joined)

    def test_extract_guest_json_requires_exactly_one_object(self):
        runtime = self.runtime()
        expected = {"status": "pass", "verified": True}
        output = "noise\nCXL_BENCH_JSON " + json.dumps(expected) + "\n"
        self.assertEqual(runtime.extract_guest_json(output), expected)
        with self.assertRaises(ValueError):
            runtime.extract_guest_json("noise only")
        with self.assertRaises(ValueError):
            runtime.extract_guest_json(output + output)

    def test_server_counts_must_both_be_positive(self):
        runtime = self.runtime()
        log = (
            "[info] Server Statistics:\n"
            "[info]   Total Reads: 123\n"
            "[info]   Total Writes: 45\n"
        )
        self.assertEqual(runtime.parse_server_counts(log), (123, 45))
        for invalid in (
            log.replace("Total Reads: 123", "Total Reads: 0"),
            log.replace("Total Writes: 45", "Total Writes: 0"),
            "no statistics",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    runtime.parse_server_counts(invalid)

    def test_console_requires_every_proof_marker_and_no_transport_error(self):
        runtime = self.runtime()
        guest = {
            "status": "pass",
            "bytes": 1048576,
            "write_seconds": [1, 1, 1],
            "read_seconds": [1, 1, 1],
            "write_mib_s_median": 1,
            "read_mib_s_median": 1,
            "random_ns_per_load": 1,
            "verified": True,
        }
        markers = (
            runtime.HOST_DECODER,
            runtime.TYPE3_DECODER,
            runtime.SHM_CONNECTED,
            "CXL_GUEST_INIT_START",
            "CXL_DISK_PASS",
            "CXL_TOPOLOGY_PASS",
            "CXL_BENCH_JSON " + json.dumps(guest),
            runtime.GUEST_PASS,
        )
        console = "\n".join(markers)
        self.assertEqual(runtime.validate_console(console), guest)
        for marker in markers:
            with self.subTest(marker=marker):
                with self.assertRaises(ValueError):
                    runtime.validate_console(console.replace(marker, "", 1))
        for transport_error in runtime.SHM_ERRORS:
            with self.subTest(transport_error=transport_error):
                with self.assertRaises(ValueError):
                    runtime.validate_console(console + "\n" + transport_error)

    def test_shm_header_requires_protocol_one_ready_and_256_mib(self):
        runtime = self.runtime()
        values = (
            0x43584C53484D454D,
            1,
            64,
            1,
            1,
            0,
            268435456,
            4194304,
            1,
            128,
        )
        header = struct.pack("<QIIIIQQQII8x", *values)
        parse_shm_header = self.require_api("parse_shm_header")
        parsed = parse_shm_header(header)
        self.assertEqual(parsed["magic"], values[0])
        self.assertEqual(parsed["version"], 1)
        self.assertEqual(parsed["server_ready"], 1)
        self.assertEqual(parsed["memory_size"], 268435456)
        self.assertEqual(parsed["num_slots"], 64)
        for index, replacement in (
            (0, 0),
            (1, 2),
            (3, 0),
            (6, 1048576),
        ):
            invalid = list(values)
            invalid[index] = replacement
            with self.subTest(index=index):
                with self.assertRaises(ValueError):
                    parse_shm_header(
                        struct.pack("<QIIIIQQQII8x", *invalid)
                    )
        with self.assertRaises(ValueError):
            parse_shm_header(b"short")

    def test_preexisting_shm_fails_before_server_launch(self):
        runtime = self.runtime()
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = pathlib.Path(temporary)
            shm_path = temporary_path / "cxlmemsim_pgas"
            shm_path.write_bytes(b"owned")
            paths = dataclasses.replace(
                self.paths(),
                logs=temporary_path / "logs",
            )
            factory = mock.Mock()
            start_server = self.require_api("start_server")
            with self.assertRaises(FileExistsError):
                start_server(
                    paths,
                    shm_path=shm_path,
                    popen_factory=factory,
                )
            factory.assert_not_called()

    def test_cleanup_escalates_only_the_owned_child(self):
        runtime = self.runtime()
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.side_effect = (
            subprocess.TimeoutExpired(["server"], 10),
            subprocess.TimeoutExpired(["server"], 5),
            0,
        )
        stop_owned_process = self.require_api("stop_owned_process")
        stop_owned_process(process)
        process.send_signal.assert_called_once()
        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual(process.wait.call_count, 3)

    def test_server_start_failure_cleans_up_the_created_child(self):
        runtime = self.runtime()
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = pathlib.Path(temporary)
            paths = dataclasses.replace(
                self.paths(),
                logs=temporary_path / "logs",
            )
            process = mock.Mock()
            with mock.patch.object(
                runtime,
                "wait_for_shm",
                side_effect=TimeoutError("not ready"),
            ), mock.patch.object(
                runtime,
                "stop_owned_process",
            ) as stop:
                with self.assertRaises(TimeoutError):
                    runtime.start_server(
                        paths,
                        shm_path=temporary_path / "cxlmemsim_pgas",
                        popen_factory=mock.Mock(return_value=process),
                    )
            stop.assert_called_once_with(process)

    def test_qemu_environment_explicitly_disables_latency_injection(self):
        runtime = self.runtime()
        build_environment = self.require_api("build_qemu_environment")
        environment = build_environment(
            self.paths(),
            {"PATH": "/usr/bin", "KEEP": "yes"},
        )
        self.assertEqual(
            environment["PATH"],
            "/x/runtime-bin:/usr/bin",
        )
        self.assertEqual(environment["CXL_TRANSPORT_MODE"], "shm")
        self.assertEqual(environment["CXL_PGAS_SHM"], "/cxlmemsim_pgas")
        self.assertEqual(environment["CXL_LATENCY_INJECT"], "0")
        self.assertEqual(environment["KEEP"], "yes")

    def test_uboot_state_machine_sends_exact_commands(self):
        runtime = self.runtime()
        run_uboot_guest = self.require_api("run_uboot_guest")
        guest = {
            "status": "pass",
            "bytes": 1048576,
            "write_seconds": [1, 1, 1],
            "read_seconds": [1, 1, 1],
            "write_mib_s_median": 1,
            "read_mib_s_median": 1,
            "random_ns_per_load": 1,
            "verified": True,
        }

        class FakeConsole:
            def __init__(self):
                self.output = "\n".join(
                    (
                        runtime.SHM_CONNECTED,
                        runtime.HOST_DECODER,
                        runtime.TYPE3_DECODER,
                        "=> ",
                    )
                )
                self.commands = []
                self.responses = [
                    "41.00.0 Type 3\n=> ",
                    "41.00.0 0000000010000000\n=> ",
                    runtime.HOST_DECODER
                    + "\n"
                    + runtime.TYPE3_DECODER
                    + "\n=> ",
                    "=> ",
                    "\n".join(
                        (
                            "CXL_GUEST_INIT_START",
                            "CXL_DISK_PASS",
                            "CXL_TOPOLOGY_PASS",
                            "CXL_BENCH_JSON " + json.dumps(guest),
                            runtime.GUEST_PASS,
                        )
                    ),
                ]

            def send(self, command):
                self.commands.append(command)
                self.output += "\n" + self.responses.pop(0)

            def wait(self, text, start=0):
                if text not in self.output[start:]:
                    raise AssertionError(f"missing wait marker: {text}")

        with tempfile.TemporaryDirectory() as temporary:
            image = pathlib.Path(temporary) / "Image"
            image.write_bytes(b"x" * 4660)
            paths = dataclasses.replace(self.paths(), linux=image)
            console = FakeConsole()
            result = run_uboot_guest(console, paths, 1048576)
        self.assertEqual(result, guest)
        self.assertEqual(console.commands[0], "cxl list")
        self.assertEqual(console.commands[1], "cxl info 41.00.0")
        self.assertEqual(console.commands[2], "cxl init")
        self.assertEqual(
            console.commands[3],
            "setenv bootargs 'earlycon=sbi console=hvc0 loglevel=4 "
            "cxl_bench_bytes=1048576'",
        )
        self.assertEqual(
            console.commands[4],
            "bootefi 90000000:1234 ${fdtcontroladdr}",
        )

    def test_atomic_result_preserves_old_success_on_write_failure(self):
        runtime = self.runtime()
        atomic_write_result = self.require_api("atomic_write_result")
        with tempfile.TemporaryDirectory() as temporary:
            output = pathlib.Path(temporary) / "type3-shm-result.json"
            output.write_text('{"old": true}\n', encoding="utf-8")
            with mock.patch.object(
                runtime.json,
                "dump",
                side_effect=RuntimeError("encode failed"),
            ):
                with self.assertRaises(RuntimeError):
                    atomic_write_result(output, {"new": True})
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8")),
                {"old": True},
            )
            self.assertFalse(
                pathlib.Path(str(output) + ".tmp").exists(),
            )
            atomic_write_result(output, {"new": True})
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8")),
                {"new": True},
            )

    def test_console_streams_commands_and_persists_output(self):
        runtime = self.runtime()
        console_class = self.require_api("Console")
        program = (
            "import sys\n"
            "print('READY', flush=True)\n"
            "line = sys.stdin.readline().strip()\n"
            "print('DONE ' + line, flush=True)\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            log = pathlib.Path(temporary) / "console.log"
            console = console_class(
                [sys.executable, "-u", "-c", program],
                timeout=2,
                environment=None,
                log_path=log,
            )
            try:
                console.wait("READY")
                start = len(console.output)
                console.send("hello")
                console.wait("DONE hello", start=start)
                self.assertEqual(console.wait_for_exit(timeout=2), 0)
            finally:
                console.close()
            self.assertTrue(console.process.stdin.closed)
            self.assertTrue(console.process.stdout.closed)
            persisted = log.read_text(encoding="utf-8")
        self.assertIn("READY", persisted)
        self.assertIn("DONE hello", persisted)

    def test_finish_qemu_terminates_owned_process_after_powerdown(self):
        finish_qemu = self.require_api("finish_qemu")
        console = mock.Mock()
        console.wait_for_exit.side_effect = TimeoutError(
            "timed out waiting for QEMU to exit"
        )
        self.assertEqual(finish_qemu(console), "terminated-by-harness")
        console.wait.assert_called_once_with("reboot: Power down")
        console.terminate.assert_called_once_with()

    def test_finish_qemu_accepts_clean_exit(self):
        finish_qemu = self.require_api("finish_qemu")
        console = mock.Mock()
        console.wait_for_exit.return_value = 0
        self.assertEqual(finish_qemu(console), "guest-poweroff")
        console.terminate.assert_not_called()

    def test_result_schema_records_all_proof_boundaries(self):
        runtime = self.runtime()
        build_result = self.require_api("build_result")
        manifest = {
            "superproject_commit": "a" * 40,
            "submodules": {"components/qemu": "b" * 40},
            "artifacts": {"qemu": {"sha256": "c" * 64, "size": 1}},
        }
        header = {
            "magic": 0x43584C53484D454D,
            "version": 1,
            "num_slots": 64,
            "server_ready": 1,
            "memory_size": 268435456,
        }
        guest = {
            "status": "pass",
            "verified": True,
            "bytes": 1048576,
        }
        result = build_result(
            command=["qemu-system-riscv64", "-M", "sifive_u"],
            environment={
                "CXL_TRANSPORT_MODE": "shm",
                "CXL_PGAS_SHM": "/cxlmemsim_pgas",
                "CXL_LATENCY_INJECT": "0",
            },
            manifest=manifest,
            header=header,
            guest=guest,
            read_count=123,
            write_count=45,
        )
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["topology"]["machine"], "sifive_u")
        self.assertEqual(result["server"]["total_reads"], 123)
        self.assertEqual(result["server"]["total_writes"], 45)
        self.assertTrue(result["guest"]["verified"])
        self.assertFalse(result["latency_injection"])
        self.assertIn("not real CXL hardware", result["interpretation"])

    def test_workflow_cleans_children_and_publishes_only_after_counts(self):
        runtime = self.runtime()
        execute_workflow = self.require_api("execute_workflow")
        guest = {
            "status": "pass",
            "bytes": 1048576,
            "write_seconds": [1, 1, 1],
            "read_seconds": [1, 1, 1],
            "write_mib_s_median": 1,
            "read_mib_s_median": 1,
            "random_ns_per_load": 1,
            "verified": True,
        }
        manifest = {
            "superproject_commit": "a" * 40,
            "submodules": {},
            "artifacts": {},
        }
        header = {
            "magic": 0x43584C53484D454D,
            "version": 1,
            "num_slots": 64,
            "server_ready": 1,
            "memory_size": 268435456,
        }
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = pathlib.Path(temporary)
            paths = dataclasses.replace(
                self.paths(),
                logs=temporary_path / "logs",
                results=temporary_path / "results",
            )
            paths.logs.mkdir()
            (paths.logs / "cxlmemsim-server.log").write_text(
                "Server Statistics:\n"
                "Total Reads: 123\n"
                "Total Writes: 45\n",
                encoding="utf-8",
            )
            process = mock.Mock()
            console = mock.Mock()
            console.wait_for_exit.return_value = 0
            with mock.patch.object(
                runtime,
                "load_verified_manifest",
                return_value=manifest,
            ), mock.patch.object(
                runtime,
                "start_server",
                return_value=(process, header),
            ), mock.patch.object(
                runtime,
                "Console",
                return_value=console,
            ), mock.patch.object(
                runtime,
                "run_uboot_guest",
                return_value=guest,
            ), mock.patch.object(
                runtime,
                "stop_owned_process",
            ) as stop, mock.patch.object(
                runtime,
                "wait_for_shm_removed",
            ):
                result = execute_workflow(
                    paths,
                    benchmark_bytes=1048576,
                    timeout=2,
                    shm_path=temporary_path / "cxlmemsim_pgas",
                )
            published = json.loads(
                (paths.results / "type3-shm-result.json").read_text(
                    encoding="utf-8"
                )
            )
        self.assertEqual(result["server"]["total_reads"], 123)
        self.assertEqual(published["server"]["total_writes"], 45)
        console.close.assert_called_once_with()
        stop.assert_called_once_with(process)


if __name__ == "__main__":
    unittest.main()
