import os
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "scripts" / "legofs_toolchain_path.sh"


class LegofsToolchainPathTest(unittest.TestCase):
    def run_bash(self, script, *, environment=None):
        return subprocess.run(
            ["/bin/bash", "-c", script],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def make_tool(self, directory, name):
        path = pathlib.Path(directory) / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_explicit_search_path_is_resolved_and_frozen(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = pathlib.Path(temporary) / "first"
            second = pathlib.Path(temporary) / "second"
            first.mkdir()
            second.mkdir()
            cargo = self.make_tool(first, "cargo")
            python = self.make_tool(second, "python3")
            environment = os.environ.copy()
            environment["LEGOFS_TOOLCHAIN_PATH"] = f"{first}:{second}"
            result = self.run_bash(
                f"source {BOOTSTRAP!s}; "
                "legofs_toolchain_activate test cargo python3; "
                "printf 'frozen=%s\\n' \"$PATH\"",
                environment=environment,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"legofs_tool[test].cargo={cargo}", result.stdout)
        self.assertIn(f"legofs_tool[test].python3={python}", result.stdout)
        self.assertIn(f"frozen={first}:{second}", result.stdout)

    def test_missing_tool_fails_before_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            environment = os.environ.copy()
            environment["LEGOFS_TOOLCHAIN_PATH"] = temporary
            result = self.run_bash(
                f"source {BOOTSTRAP!s}; "
                "legofs_toolchain_activate test definitely-not-a-tool",
                environment=environment,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("required tool is missing", result.stderr)
        self.assertIn("definitely-not-a-tool", result.stderr)

    def test_frozen_path_preserves_search_precedence(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = pathlib.Path(temporary) / "first"
            second = pathlib.Path(temporary) / "second"
            first.mkdir()
            second.mkdir()
            preferred = first / "cargo"
            preferred.write_text("#!/bin/sh\nprintf 'preferred\\n'\n", encoding="utf-8")
            preferred.chmod(0o755)
            shadow = second / "cargo"
            shadow.write_text("#!/bin/sh\nprintf 'shadow\\n'\n", encoding="utf-8")
            shadow.chmod(0o755)
            self.make_tool(second, "python3")
            environment = os.environ.copy()
            environment["LEGOFS_TOOLCHAIN_PATH"] = f"{first}:{second}"
            result = self.run_bash(
                f"source {BOOTSTRAP!s}; "
                "legofs_toolchain_activate test python3 cargo; cargo",
                environment=environment,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.endswith("preferred\n"), result.stdout)
        self.assertIn(f"legofs_toolchain[test].path={first}:{second}", result.stdout)

    def test_relative_search_path_is_rejected(self):
        environment = os.environ.copy()
        environment["LEGOFS_TOOLCHAIN_PATH"] = "relative:/usr/bin"
        result = self.run_bash(
            f"source {BOOTSTRAP!s}; legofs_toolchain_activate test sh",
            environment=environment,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PATH entry must be absolute", result.stderr)

    def test_type3_and_io500_entrypoints_use_bootstrap(self):
        entrypoints = (
            ROOT / "run-legofs-type3.sh",
            ROOT / "run-legofs-io500.sh",
            ROOT / "scripts" / "build_legofs_type3.sh",
            ROOT / "scripts" / "build_legofs_io500.sh",
            ROOT / "scripts" / "rebuild_legofs_io500_payload.sh",
            ROOT / "scripts" / "rebuild_legofs_io500_bootstrap.sh",
            ROOT / "scripts" / "rebuild_legofs_io500_cxlmemsim.sh",
        )
        for entrypoint in entrypoints:
            with self.subTest(entrypoint=entrypoint):
                source = entrypoint.read_text(encoding="utf-8")
                self.assertIn("legofs_toolchain_path.sh", source)
                self.assertIn("legofs_toolchain_activate", source)

        for runner in (ROOT / "run-legofs-type3.sh", ROOT / "run-legofs-io500.sh"):
            with self.subTest(runner=runner):
                self.assertIn(
                    "LEGOFS_TOOLCHAIN_PATH",
                    runner.read_text(encoding="utf-8"),
                )


if __name__ == "__main__":
    unittest.main()
