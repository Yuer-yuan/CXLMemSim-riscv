import hashlib
import json
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "write_manifest.py"


class ManifestTest(unittest.TestCase):
    def invoke(
        self,
        output,
        *artifacts,
        compilers=(),
        sources=(),
        no_artifact_hashes=False,
    ):
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
        for name, source in sources:
            command.extend(["--source", f"{name}={source}"])
        if no_artifact_hashes:
            command.append("--no-artifact-hashes")
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

    def test_manifest_records_external_git_source_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = pathlib.Path(temporary)
            source = temporary_path / "source"
            source.mkdir()
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            (source / "input.txt").write_text("source\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(source), "add", "input.txt"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(source),
                    "-c",
                    "user.name=Manifest Test",
                    "-c",
                    "user.email=manifest@example.invalid",
                    "commit",
                    "-qm",
                    "source",
                ],
                check=True,
            )
            output = temporary_path / "manifest.json"
            run = self.invoke(output, sources=(("legofs", source),))
            self.assertEqual(run.returncode, 0, run.stderr)
            entry = json.loads(output.read_text(encoding="utf-8"))["sources"][
                "legofs"
            ]
        self.assertTrue(entry["clean"])
        self.assertEqual(len(entry["commit"]), 40)
        self.assertEqual(len(entry["tree"]), 40)
        self.assertIsNone(entry["origin"])

    def test_manifest_can_skip_artifact_hashes_for_local_iteration(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = pathlib.Path(temporary)
            artifact = temporary_path / "artifact.bin"
            artifact.write_bytes(b"abc")
            output = temporary_path / "manifest.json"
            run = self.invoke(
                output,
                ("artifact", artifact),
                no_artifact_hashes=True,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            entry = json.loads(output.read_text(encoding="utf-8"))["artifacts"][
                "artifact"
            ]
        self.assertEqual(entry["size"], 3)
        self.assertNotIn("sha256", entry)

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
