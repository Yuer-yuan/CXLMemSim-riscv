import json
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / "guest" / "cxl_mmap_bench.c"
INIT = ROOT / "guest" / "init.c"
CROSS_FLAGS = [
    "-O2",
    "-std=c11",
    "-Wall",
    "-Wextra",
    "-Werror",
    "-static",
    "-nostdlib",
    "-fno-builtin",
    "-fno-stack-protector",
    "-fno-pie",
    "-no-pie",
    "-march=rv64imafdc",
    "-mabi=lp64d",
]


class GuestBuildTest(unittest.TestCase):
    def require_source(self, path):
        self.assertTrue(path.is_file(), f"{path.relative_to(ROOT)} is missing")

    def assert_static_riscv_without_vector(self, binary):
        header = subprocess.run(
            ["riscv64-linux-gnu-readelf", "-h", str(binary)],
            check=True,
            text=True,
            capture_output=True,
        ).stdout
        program_headers = subprocess.run(
            ["riscv64-linux-gnu-readelf", "-l", str(binary)],
            check=True,
            text=True,
            capture_output=True,
        ).stdout
        attributes = subprocess.run(
            ["riscv64-linux-gnu-readelf", "-A", str(binary)],
            check=True,
            text=True,
            capture_output=True,
        ).stdout
        self.assertIn("RISC-V", header)
        self.assertNotIn("INTERP", program_headers)
        self.assertNotIn("_v", attributes)

    def test_benchmark_native_self_test_is_bit_exact(self):
        self.require_source(BENCHMARK)
        with tempfile.TemporaryDirectory() as temporary:
            binary = pathlib.Path(temporary) / "cxl_mmap_bench"
            subprocess.run(
                [
                    "gcc",
                    "-O2",
                    "-std=c11",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    str(BENCHMARK),
                    "-o",
                    str(binary),
                ],
                check=True,
            )
            run = subprocess.run(
                [str(binary), "--self-test"],
                check=True,
                text=True,
                capture_output=True,
            )
        result = json.loads(run.stdout)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(len(result["write_seconds"]), 3)
        self.assertEqual(len(result["read_seconds"]), 3)
        self.assertTrue(all(value > 0 for value in result["write_seconds"]))
        self.assertTrue(all(value > 0 for value in result["read_seconds"]))
        self.assertGreater(result["write_mib_s_median"], 0)
        self.assertGreater(result["read_mib_s_median"], 0)
        self.assertGreater(result["random_ns_per_load"], 0)
        self.assertTrue(result["verified"])

    def test_benchmark_freestanding_elf_matches_sifive_u_isa(self):
        self.require_source(BENCHMARK)
        with tempfile.TemporaryDirectory() as temporary:
            binary = pathlib.Path(temporary) / "cxl_mmap_bench.riscv64"
            subprocess.run(
                [
                    "riscv64-linux-gnu-gcc",
                    *CROSS_FLAGS,
                    "-DCXL_BENCH_FREESTANDING",
                    str(BENCHMARK),
                    "-o",
                    str(binary),
                ],
                check=True,
            )
            self.assert_static_riscv_without_vector(binary)
            strings = subprocess.run(
                ["strings", str(binary)],
                check=True,
                text=True,
                capture_output=True,
            ).stdout
            self.assertIn("CXL_BENCH_JSON ", strings)

    def test_init_freestanding_contract(self):
        self.require_source(INIT)
        source = INIT.read_text(encoding="utf-8")
        self.assertIn("#define SYS_DUP3 24", source)
        self.assertLess(
            source.index("\treopen_console();"),
            source.index('\twrite_text("CXL_GUEST_INIT_START'),
        )
        for marker in (
            "CXL_GUEST_INIT_START",
            "CXL_DISK_PASS",
            "CXL_TOPOLOGY_PASS",
            "CXL_QEMU_UBOOT_LINUX_BENCH_PASS",
            "CXL_GUEST_INIT_FAIL",
        ):
            self.assertIn(marker, source)
        self.assertNotIn("system(", source)
        self.assertNotIn("popen(", source)
        with tempfile.TemporaryDirectory() as temporary:
            binary = pathlib.Path(temporary) / "init.riscv64"
            subprocess.run(
                [
                    "riscv64-linux-gnu-gcc",
                    *CROSS_FLAGS,
                    str(INIT),
                    "-o",
                    str(binary),
                ],
                check=True,
            )
            self.assert_static_riscv_without_vector(binary)


if __name__ == "__main__":
    unittest.main()
