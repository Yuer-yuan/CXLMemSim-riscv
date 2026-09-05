import pathlib
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
BUILD = ROOT / "scripts" / "build_giga_native_io500.sh"
RUN = ROOT / "scripts" / "run_giga_native_legofs_io500.sh"


class GigaNativeIo500ScriptsTest(unittest.TestCase):
    def test_native_build_script_keeps_pinned_sources(self):
        self.assertTrue(BUILD.is_file(), "native giga build script is missing")
        text = BUILD.read_text(encoding="utf-8")
        for commit in (
            "a69cf60cf76538a34c1332bc448838cf9a560a9b",
            "5fcf0ba995fd92164d50e344597e2d8203298c08",
            "d08501f9976caf1adabdebfb883d4701dd98fe35",
        ):
            with self.subTest(commit=commit):
                self.assertIn(commit, text)
        self.assertIn("/usr/bin/mpicc.openmpi", text)
        self.assertIn("/usr/bin/mpirun.openmpi", text)
        self.assertNotIn("qemu-system", text)
        self.assertNotIn("cxlmemsim_server", text)

    def test_native_build_is_fail_closed_and_emits_a_manifest(self):
        self.assertTrue(BUILD.is_file(), "native giga build script is missing")
        text = BUILD.read_text(encoding="utf-8")
        for contract in (
            "uname -m",
            "ensure_checkout",
            "git -C \"$directory\" remote get-url origin",
            "git -C \"$directory\" rev-parse HEAD",
            "cargo build",
            "--locked",
            "syscall-intercept-backend",
            "ldd",
            "sha256sum",
            "build-manifest.json",
            "x86-64",
            'if [ -f "$CAPSTONE_INCLUDE_ROOT/capstone.h" ]',
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, text)

    def render(self, topology, profile="bounded"):
        self.assertTrue(RUN.is_file(), "native giga run script is missing")
        completed = subprocess.run(
            [
                str(RUN),
                "--print-command",
                "--topology",
                topology,
                "--profile",
                profile,
                "--run-id",
                f"render-{topology}-{profile}",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout

    def test_run_command_maps_each_topology_to_distinct_llcs(self):
        one = self.render("1c1s")
        self.assertIn("--mpi-ranks 1", one)
        self.assertIn("--server-cpu 7", one)
        self.assertIn("--client-cpus 1", one)

        two = self.render("2c1s")
        self.assertIn("--mpi-ranks 2", two)
        self.assertIn("--server-cpu 13", two)
        self.assertIn("--client-cpus 1,7", two)

        three = self.render("3c1s")
        self.assertIn("--mpi-ranks 3", three)
        self.assertIn("--server-cpu 19", three)
        self.assertIn("--client-cpus 1,7,13", three)

    def test_capacity_five_second_profile_preserves_official_geometry(self):
        rendered = self.render("2c1s", "capacity-5s")
        self.assertIn("lifecycle-io500-capacity-safe-5s.ini", rendered)
        config = (
            ROOT
            / "components"
            / "legofs"
            / "scripts"
            / "lifecycle-io500-capacity-safe-5s.ini"
        ).read_text(encoding="utf-8")
        self.assertIn("stonewall-time = 5", config)
        self.assertIn("blockSize = 9920000m", config)

    def test_run_command_forces_cxl_numa_contract_and_fixed_profiles(self):
        for profile, config in (
            ("bounded", "lifecycle-io500-all-bounded.ini"),
            ("find-valid", "lifecycle-io500-find-valid-bounded.ini"),
            ("capacity-20s", "lifecycle-io500-capacity-safe-20s.ini"),
            ("official", "lifecycle-io500-official-300s.ini"),
        ):
            command = self.render("2c1s", profile)
            with self.subTest(profile=profile):
                for field in (
                    "--claim cxl-numa-samehost",
                    "--serving-transport cxl",
                    "--cpu-node 0",
                    "--memory-node 1",
                    "--wrong-node 0",
                    "--region-size-gib 64",
                    "--packed-small-segments on",
                    "--small-segment-count 2048",
                    "--durability-profile coherent-seal-no-writeback",
                    "--payload-persistence-owner writer-receipt",
                    "--close-batch-mode batched",
                    "--server-count 1",
                    config,
                ):
                    self.assertIn(field, command)
        self.assertIn("--lifecycle-trace off", self.render("1c1s", "official"))
        self.assertIn(
            "--lifecycle-trace off", self.render("1c1s", "capacity-20s")
        )

    def test_run_script_exposes_build_and_preflight_without_qemu(self):
        self.assertTrue(RUN.is_file(), "native giga run script is missing")
        text = RUN.read_text(encoding="utf-8")
        self.assertIn("--build", text)
        self.assertIn("--preflight-only", text)
        self.assertIn("build_giga_native_io500.sh", text)
        self.assertNotIn("qemu-system", text)
        self.assertNotIn("cxlmemsim_server", text)


if __name__ == "__main__":
    unittest.main()
