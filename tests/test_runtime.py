import importlib.util
import json
import pathlib
import unittest


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


if __name__ == "__main__":
    unittest.main()
