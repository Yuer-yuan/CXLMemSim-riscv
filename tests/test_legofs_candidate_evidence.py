import copy
import importlib.util
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "legofs_candidate_evidence.py"
RUNNER_PATH = ROOT / "scripts" / "legofs_io500.py"


def load_module():
    spec = importlib.util.spec_from_file_location(
        "legofs_candidate_evidence",
        MODULE_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CandidateEvidenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def build_manifest(self):
        return {
            "schema_version": 2,
            "sources": {"legofs": {"commit": "a" * 40}},
            "artifacts": {
                name: {
                    "path": f"payload/{name}",
                    "size": index + 1,
                    "sha256": f"{index + 1:064x}",
                }
                for index, name in enumerate(self.module.CANDIDATE_ARTIFACTS)
            },
        }

    def skeleton(self):
        return self.module.create_skeleton(
            run_id="candidate-v0-r1",
            stage="hello",
            owner_token="owner",
            build_manifest=self.build_manifest(),
            evaluation_manifest=None,
            server_count=1,
            client_count=2,
            qemu_machine="sifive_u",
            shared_region_bytes=64 * 1024**3,
        )

    def resign(self, document):
        document.pop("evidence_digest", None)
        document["evidence_digest"] = self.module.canonical_digest(document)

    def test_skeleton_is_explicitly_incomplete_and_predeclares_cold_observer(self):
        evidence = self.skeleton()
        self.assertEqual(evidence["status"], "not_yet_supported")
        self.assertTrue(evidence["functional_model_only"])
        self.assertFalse(evidence["physical_hardware_evidence"])
        self.assertFalse(evidence["official_candidate"])
        topology = evidence["sections"]["qemu_bi_topology"]
        guests = topology["guest_declarations"]
        self.assertEqual([item["endpoint_host_id"] for item in guests], [0, 1, 2, 3])
        self.assertEqual(len({item["guest_uuid"] for item in guests}), 4)
        self.assertEqual(len({item["independent_address_space"] for item in guests}), 4)
        self.assertEqual(guests[-1]["role"], "cold-observer")
        self.assertEqual(guests[-1]["launch_state"], "predeclared-not-launched")
        self.assertTrue(all(item["region_generation"] is None for item in guests))

    def test_default_transport_label_cannot_make_network_closure_complete(self):
        evidence = self.skeleton()
        evidence["sections"]["network_closure"] = {
            "status": "complete",
            "transport": "cxl-dax-ring",
        }
        evidence["status"] = "not_yet_supported"
        self.resign(evidence)
        with self.assertRaisesRegex(
            self.module.CandidateEvidenceError,
            "nonzero route",
        ):
            self.module.validate(evidence)

    def test_complete_process_status_without_terminal_records_fails_closed(self):
        evidence = self.skeleton()
        evidence["sections"]["process_terminals"] = {
            "status": "complete",
            "expected_processes": 0,
            "records": [],
            "root_operations_issued": 0,
            "root_operations_terminal": 0,
        }
        self.resign(evidence)
        with self.assertRaisesRegex(
            self.module.CandidateEvidenceError,
            "exact records",
        ):
            self.module.validate(evidence)

    def test_complete_response_status_without_seals_fails_closed(self):
        evidence = self.skeleton()
        evidence["sections"]["response_and_final_drain"] = {
            "status": "complete",
            "requests_admitted": 0,
            "responses_completed": 0,
            "final_drain_complete": True,
        }
        self.resign(evidence)
        with self.assertRaisesRegex(
            self.module.CandidateEvidenceError,
            "not digest closed",
        ):
            self.module.validate(evidence)

    def test_candidate_evidence_rejects_legacy_inspector_sources(self):
        evidence = self.skeleton()
        evidence["sections"]["artifact_completion"]["lifecycle_inspection"] = {}
        self.resign(evidence)
        with self.assertRaisesRegex(
            self.module.CandidateEvidenceError,
            "legacy source",
        ):
            self.module.validate(evidence)

    def test_complete_topology_requires_region_generation_and_observer_receipt(self):
        evidence = self.skeleton()
        topology = evidence["sections"]["qemu_bi_topology"]
        topology["status"] = "complete"
        topology.pop("reason")
        topology.pop("missing_evidence")
        topology["hdm_db_bi_observed"] = True
        self.resign(evidence)
        with self.assertRaisesRegex(
            self.module.CandidateEvidenceError,
            "region generation",
        ):
            self.module.validate(evidence)

    def test_digest_tamper_fails_closed(self):
        evidence = self.skeleton()
        tampered = copy.deepcopy(evidence)
        tampered["run_id"] = "different"
        with self.assertRaisesRegex(
            self.module.CandidateEvidenceError,
            "digest mismatch",
        ):
            self.module.validate(tampered)

    def test_failed_section_requires_failed_aggregate(self):
        evidence = self.skeleton()
        evidence["sections"]["artifact_completion"] = {
            "status": "failed",
            "reason": "terminal artifact write failed",
            "failure_class": "infrastructure",
        }
        self.resign(evidence)
        with self.assertRaisesRegex(
            self.module.CandidateEvidenceError,
            "aggregate status",
        ):
            self.module.validate(evidence)

    def test_runner_selects_candidate_collector_before_legacy_evidence(self):
        runner = RUNNER_PATH.read_text(encoding="utf-8")
        self.assertIn("from legofs_candidate_evidence import", runner)
        self.assertNotIn(
            "rdwo-candidate IO500 evidence collection is unavailable until V0.3",
            runner,
        )
        candidate_branch = runner.split(
            'if filesystem_mode == "rdwo-candidate":',
            1,
        )[1]
        self.assertIn("create_candidate_evidence", candidate_branch)


if __name__ == "__main__":
    unittest.main()
