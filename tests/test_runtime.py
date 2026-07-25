import dataclasses
import importlib.util
import json
import pathlib
import struct
import subprocess
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


if __name__ == "__main__":
    unittest.main()
