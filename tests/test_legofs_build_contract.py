import pathlib
import os
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUN = ROOT / "run-legofs-type3.sh"
BUILD = ROOT / "scripts" / "build_legofs_type3.sh"


class LegofsBuildContractTest(unittest.TestCase):
    def test_kernel_fragment_has_built_in_devdax_and_network(self):
        config = (ROOT / "configs/linux-cxl.config").read_text().splitlines()
        required = {
            "CONFIG_DAX=y",
            "CONFIG_DEV_DAX=y",
            "CONFIG_DEV_DAX_CXL=y",
            "CONFIG_NET=y",
            "CONFIG_INET=y",
            "CONFIG_UNIX=y",
            "CONFIG_PACKET=y",
            "CONFIG_VIRTIO_NET=y",
        }
        self.assertTrue(required.issubset(set(config)))

    def test_init_has_role_dax_and_strict_markers(self):
        source = (ROOT / "guest/legofs_node_init.c").read_text()
        for marker in (
            "legofs.role=",
            "LEG_OFS_CXL_READY",
            "LEG_OFS_SERVER_READY",
            "LEG_OFS_BENCHMARK_BEGIN",
            "LEG_OFS_BENCHMARK_PASS",
            "BADFS_LIFECYCLE_DIRECT_REQUIRED=1",
            "BADFS_LIFECYCLE_DIRECT_READ_REQUIRED=1",
            "BADFS_CXL_MAP_ALIGNMENT=2097152",
        ):
            self.assertIn(marker, source)
        self.assertNotIn("BADFS_CXL_MAP_ALIGNMENT=4096", source)

    def run_cli(self, *arguments, environment=None):
        return subprocess.run(
            [str(RUN), *arguments],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_run_cli_validation_and_accepted_size(self):
        self.assertTrue(RUN.is_file(), "run-legofs-type3.sh is missing")
        help_run = self.run_cli("--help")
        self.assertEqual(help_run.returncode, 0, help_run.stderr)
        conflict = self.run_cli("--build-only", "--run-only")
        self.assertNotEqual(conflict.returncode, 0)
        invalid = self.run_cli("--bytes", "0")
        self.assertNotEqual(invalid.returncode, 0)

        with tempfile.TemporaryDirectory() as temporary:
            fake = pathlib.Path(temporary) / "build"
            fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake.chmod(0o755)
            environment = os.environ.copy()
            environment["LEGOFS_BUILD_SCRIPT"] = str(fake)
            accepted = self.run_cli(
                "--build-only", "--bytes", "65536", environment=environment
            )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)

    def test_build_script_names_complete_manifest(self):
        self.assertTrue(BUILD.is_file(), "scripts/build_legofs_type3.sh is missing")
        source = BUILD.read_text(encoding="utf-8")
        self.assertIn("riscv64gc-unknown-linux-musl", source)
        self.assertIn(
            "a9a118bbe84d8764da0ea0d28b3ab3fae8477fc7e4085d90102b8596fc7c75e4",
            source,
        )
        self.assertIn('"${CROSS_COMPILE}strip" --strip-debug', source)
        self.assertIn('cmp "${badfs_server}" "${verify_server}"', source)
        self.assertNotIn("./config.status", source)
        self.assertNotIn("qemu_configure=", source)
        self.assertIn('--compiler "qemu=${qemu} --version"', source)
        for artifact in (
            "qemu",
            "opensbi",
            "u_boot",
            "linux_legofs",
            "legofs_disk",
            "badfs_server",
            "badfs_bench",
            "cxlmemsim_server",
        ):
            with self.subTest(artifact=artifact):
                self.assertIn(f'--artifact "{artifact}=', source)


if __name__ == "__main__":
    unittest.main()
