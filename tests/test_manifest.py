import hashlib
import json
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "write_manifest.py"


class ManifestTest(unittest.TestCase):
    def invoke(self, output, *artifacts, compilers=()):
        self.assertTrue(SCRIPT.is_file(), "scripts/write_manifest.py is missing")
        command = [
            "python3",
            str(SCRIPT),
            "--root",
            str(ROOT),
            "--output",
            str(output),
        ]
        for name, path in artifacts:
            command.extend(["--artifact", f"{name}={path}"])
        for name, compiler in compilers:
            command.extend(["--compiler", f"{name}={compiler}"])
        return subprocess.run(command, text=True, capture_output=True)

    def test_manifest_hashes_artifacts_and_records_gitlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = pathlib.Path(temporary)
            first = temporary_path / "first.bin"
            second = temporary_path / "second.bin"
            output = temporary_path / "build-manifest.json"
            first.write_bytes(b"abc")
            second.write_bytes(b"xyz")
            run = self.invoke(
                output,
                ("first", first),
                ("second", second),
                compilers=(("python", "python3 --version"),),
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            manifest = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual(
            manifest["superproject_commit"],
            subprocess.run(
                ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip(),
        )
        expected_qemu = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD:components/qemu"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        self.assertEqual(manifest["submodules"]["components/qemu"], expected_qemu)
        self.assertIn("Python", manifest["compilers"]["python"]["version"])
        self.assertEqual(manifest["artifacts"]["first"]["size"], 3)
        self.assertEqual(
            manifest["artifacts"]["first"]["sha256"],
            hashlib.sha256(b"abc").hexdigest(),
        )

    def test_missing_artifact_does_not_replace_existing_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = pathlib.Path(temporary)
            output = temporary_path / "build-manifest.json"
            output.write_text('{"sentinel": true}\n', encoding="utf-8")
            run = self.invoke(
                output,
                ("missing", temporary_path / "missing.bin"),
            )
            self.assertNotEqual(run.returncode, 0)
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8")),
                {"sentinel": True},
            )
            self.assertFalse(
                pathlib.Path(str(output) + ".tmp").exists(),
            )

    def test_duplicate_artifact_name_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = pathlib.Path(temporary)
            artifact = temporary_path / "file.bin"
            artifact.write_bytes(b"x")
            run = self.invoke(
                temporary_path / "manifest.json",
                ("same", artifact),
                ("same", artifact),
            )
            self.assertNotEqual(run.returncode, 0)
            self.assertIn("duplicate artifact name", run.stderr)


if __name__ == "__main__":
    unittest.main()
