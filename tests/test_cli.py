import os
import pathlib
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUN = ROOT / "run.sh"
DEPS = ROOT / "scripts" / "check-deps.sh"


class CliTest(unittest.TestCase):
    def run_cli(self, *args):
        if not RUN.exists():
            self.fail("run.sh is missing")
        return subprocess.run(
            [str(RUN), *args],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_help_does_not_build(self):
        run = self.run_cli("--help")
        self.assertEqual(run.returncode, 0)
        self.assertIn("--build-only", run.stdout)
        self.assertIn("--benchmark-bytes", run.stdout)

    def test_conflicting_modes_fail(self):
        run = self.run_cli("--build-only", "--run-only")
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("mutually exclusive", run.stderr)

    def test_invalid_bytes_fail_before_build(self):
        for value in ("0", "7", "268435464", "text"):
            with self.subTest(value=value):
                run = self.run_cli("--benchmark-bytes", value)
                self.assertNotEqual(run.returncode, 0)
                self.assertIn("benchmark bytes", run.stderr.lower())

    def test_dependency_checker_lists_every_missing_command(self):
        if not DEPS.exists():
            self.fail("scripts/check-deps.sh is missing")
        env = os.environ.copy()
        env["CXL_REQUIRED_COMMANDS"] = (
            "definitely_missing_one definitely_missing_two"
        )
        run = subprocess.run(
            [str(DEPS)],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("definitely_missing_one", run.stderr)
        self.assertIn("definitely_missing_two", run.stderr)

    def test_submodule_initialization_is_limited_to_top_level_components(self):
        self.assertTrue(RUN.is_file(), "run.sh is missing")
        source = RUN.read_text(encoding="utf-8")
        self.assertNotIn("submodule status --recursive", source)
        self.assertNotIn("submodule update --init --recursive", source)


if __name__ == "__main__":
    unittest.main()
