import configparser
import hashlib
import importlib.util
import json
import pathlib
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "legofs_evaluation_manifest.py"


def load_module():
    spec = importlib.util.spec_from_file_location(
        "legofs_evaluation_manifest",
        MODULE_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EvaluationManifestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.source = self.root / "io500-standard.ini"
        self.source.write_text(
            "[global]\n"
            "scc = FALSE\n"
            "dataPacketType = timestamp\n"
            "\n"
            "[debug]\n"
            "stonewall-time = 300\n",
            encoding="utf-8",
        )
        artifacts = {}
        required = (
            self.module.REQUIRED_BUILD_ARTIFACTS
            + self.module.MODE_BUILD_ARTIFACTS["legacy-cxl-reference"]
        )
        for name in required:
            path = self.root / name
            path.write_bytes(name.encode("ascii"))
            artifacts[name] = {
                "path": str(path),
                "size": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        self.build_manifest = self.root / "build-manifest.json"
        self.build_manifest.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "superproject_commit": "test",
                    "sources": {"legofs": {"commit": "a" * 40}},
                    "compilers": {"rustc": "test"},
                    "artifacts": artifacts,
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def build(self, **overrides):
        arguments = {
            "source_config": self.source,
            "output_config": self.root / "effective.ini",
            "build_manifest": self.build_manifest,
            "filesystem_mode": "legacy-cxl-reference",
            "io500_mode": "standard",
            "profile_name": "mdtest-easy-write-v0",
            "enabled_phases": ["mdtest-easy-write"],
            "stonewall_seconds": 5,
            "mdtest_items": 32,
            "server_count": 1,
            "client_count": 2,
        }
        arguments.update(overrides)
        return self.module.build_evaluation_manifest(**arguments)

    def test_mdtest_canary_changes_only_run_stonewall_and_item_count(self):
        manifest = self.build()
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        parser.read(self.root / "effective.ini")

        enabled = {
            section
            for section in self.module.RUN_SECTIONS
            if parser.get(section, "run") == "TRUE"
        }
        self.assertEqual(enabled, {"mdtest-easy", "mdtest-easy-write"})
        self.assertEqual(parser.get("debug", "stonewall-time"), "5")
        self.assertEqual(parser.get("mdtest-easy", "n"), "32")
        self.assertEqual(parser.get("global", "dataPacketType"), "timestamp")
        self.assertFalse(
            any(
                key.lower() in {"transfersize", "fileperproc", "uniquedir"}
                for section in parser.sections()
                for key, _ in parser.items(section)
            )
        )

        self.assertEqual(manifest["classification"], "diagnostic-upstream-shape")
        self.assertFalse(manifest["official_parameter_shape"])
        self.assertFalse(manifest["official_candidate"])
        self.assertFalse(manifest["product_consumes_this_manifest"])
        self.assertEqual(manifest["topology"]["client_guests"], 2)
        self.assertEqual(
            [
                item["phase"]
                for item in manifest["phase_evidence"]["records"]
            ],
            ["mdtest-easy-write"],
        )
        self.assertEqual(
            manifest["phase_evidence"]["schema_version"],
            "legofs.phase-evidence-manifest.v1",
        )
        self.assertEqual(
            manifest["phase_evidence"]["records"][0]["required_metric_units"],
            ["kIOPS"],
        )
        self.assertEqual(
            manifest["phase_evidence"]["records"][0]["config_section_closure"],
            ["mdtest-easy", "mdtest-easy-write"],
        )

    def test_ior_requires_bandwidth_and_data_iops(self):
        manifest = self.build(
            enabled_phases=["ior-easy-write"],
            mdtest_items=None,
        )
        evidence = manifest["phase_evidence"]["records"][0]
        self.assertEqual(evidence["primary_metric_unit"], "GiB/s")
        self.assertEqual(evidence["required_metric_units"], ["GiB/s", "kIOPS"])

    def test_read_profile_includes_write_setup_dependency(self):
        manifest = self.build(
            enabled_phases=["ior-easy-read"],
            mdtest_items=None,
        )
        self.assertEqual(
            manifest["phase_evidence"]["execution_phases"],
            ["ior-easy-write", "ior-easy-read"],
        )
        records = manifest["phase_evidence"]["records"]
        self.assertEqual([record["role"] for record in records], ["setup", "measurement"])

    def test_optional_phase_requires_extended_mode(self):
        with self.assertRaisesRegex(ValueError, "extended mode"):
            self.build(
                enabled_phases=["find-easy"],
                mdtest_items=None,
            )
        manifest = self.build(
            io500_mode="extended",
            enabled_phases=["find-easy"],
            mdtest_items=None,
        )
        self.assertEqual(
            manifest["phase_evidence"]["execution_phases"],
            ["mdtest-easy-write", "find-easy"],
        )

    def test_full_shape_cannot_change_stonewall_or_object_count(self):
        with self.assertRaisesRegex(ValueError, "cannot change"):
            self.build(enabled_phases=[], stonewall_seconds=5, mdtest_items=None)
        with self.assertRaisesRegex(ValueError, "cannot change"):
            self.build(enabled_phases=[], stonewall_seconds=None, mdtest_items=4)

    def test_smoke_config_cannot_authorize_a_stage(self):
        smoke = self.root / "io500-tiny.ini"
        smoke.write_text(self.source.read_text(encoding="utf-8"), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "io500-standard.ini"):
            self.build(source_config=smoke)

    def test_build_artifact_hash_mismatch_fails_closed(self):
        manifest = json.loads(self.build_manifest.read_text(encoding="utf-8"))
        artifact = pathlib.Path(manifest["artifacts"]["qemu"]["path"])
        artifact.write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "hash mismatch: qemu"):
            self.build()

    def test_relative_build_artifacts_are_resolved_from_workspace_root(self):
        manifest = json.loads(self.build_manifest.read_text(encoding="utf-8"))
        for record in manifest["artifacts"].values():
            record["path"] = pathlib.Path(record["path"]).name
        self.build_manifest.write_text(json.dumps(manifest), encoding="utf-8")
        original_root = self.module.WORKSPACE_ROOT
        self.module.WORKSPACE_ROOT = self.root
        try:
            result = self.build()
        finally:
            self.module.WORKSPACE_ROOT = original_root
        self.assertEqual(result["artifacts"]["qemu"]["path"], "qemu")

    def test_candidate_cannot_reuse_legacy_artifacts(self):
        with self.assertRaisesRegex(
            ValueError,
            "lacks required artifact: rdwo_client",
        ):
            self.build(filesystem_mode="rdwo-candidate")

    def test_unknown_phase_and_invalid_topology_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "unknown IO500 phase"):
            self.build(enabled_phases=["made-up-phase"])
        with self.assertRaisesRegex(ValueError, "client count"):
            self.build(client_count=11)

    def test_effective_config_cannot_overwrite_source(self):
        with self.assertRaisesRegex(ValueError, "must not overwrite"):
            self.build(output_config=self.source)

    def test_digest_closes_manifest_without_becoming_product_input(self):
        manifest = self.build()
        digest = manifest.pop("manifest_digest")
        self.assertEqual(digest, self.module.canonical_digest(manifest))
        self.assertIsNone(manifest["product_engine_manifest_digest"])
        self.assertFalse(manifest["product_consumes_this_manifest"])


if __name__ == "__main__":
    unittest.main()
