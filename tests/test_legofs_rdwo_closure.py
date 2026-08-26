import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "legofs_rdwo_closure.py"
SPEC = importlib.util.spec_from_file_location("legofs_rdwo_closure", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class RdwoClosureTest(unittest.TestCase):
    def test_candidate_package_parser_rejects_exact_legacy_dependencies(self):
        clean = "badfs-rdwo-client v0.1.0\n└── badfs-common v0.1.0\n└── sha2 v0.10.9\n"
        self.assertEqual(
            MODULE.reject_banned_packages(clean),
            ["badfs-common", "badfs-rdwo-client", "sha2"],
        )
        with self.assertRaisesRegex(ValueError, "tarpc"):
            MODULE.reject_banned_packages(clean + "└── tarpc v0.26.2\n")
        with self.assertRaisesRegex(ValueError, "badfs-server"):
            MODULE.reject_banned_packages(clean + "└── badfs-server v0.1.0\n")

    def test_artifact_set_is_closed_and_nonempty(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {}
            for role in MODULE.REQUIRED_ROLES:
                path = root / role
                path.write_bytes(role.encode())
                paths[role] = path
            values = [f"{role}={path}" for role, path in paths.items()]
            parsed = MODULE.parse_artifacts(values)
            self.assertEqual(set(parsed), set(MODULE.REQUIRED_ROLES))
            with self.assertRaisesRegex(ValueError, "exactly"):
                MODULE.parse_artifacts(values[:-1])
            paths["client"].write_bytes(b"")
            with self.assertRaisesRegex(ValueError, "missing"):
                MODULE.parse_artifacts(values)

    def test_atomic_record_digest_is_reproducible(self):
        record = {"schema": MODULE.SCHEMA, "entry_eligible": False}
        digest = MODULE.canonical_digest(record)
        self.assertEqual(digest, MODULE.canonical_digest(record))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "closure.json"
            record["record_digest"] = digest
            MODULE.write_atomic(path, record)
            self.assertEqual(json.loads(path.read_text()), record)

    def test_v11_script_hard_codes_non_entry_eligibility(self):
        source = MODULE_PATH.read_text()
        self.assertIn('"entry_eligible": False', source)
        self.assertIn("V1.2 syscall admission is absent", source)
        for token in ("tarpc", "v2authorityengine", "inmemoryoracle", "hostfs"):
            self.assertIn(token, source.lower())

    def test_io500_builder_cross_builds_and_attests_all_candidate_artifacts(self):
        source = (ROOT / "scripts" / "build_legofs_io500.sh").read_text()
        self.assertIn("[io500-build] link-time independent RDWO V1.1 artifacts", source)
        for package in (
            "badfs-rdwo-client",
            "badfs-rdwo-server",
            "badfs-rdwo-host-agent",
            "badfs-rdwo-intercept",
        ):
            self.assertIn(f"-p {package}", source)
        self.assertIn('python3 "$ROOT/scripts/legofs_rdwo_closure.py"', source)
        self.assertIn("d-before-v", source)
        self.assertIn("bootstrap-template-not-serving", source)
        self.assertIn('rdwo_closure=$RDWO_CLOSURE', source)
        self.assertIn('rdwo_engine_manifest=$PAYLOAD_ROOT/etc/legofs-rdwo-engine.manifest', source)


if __name__ == "__main__":
    unittest.main()
