import importlib.util
import json
import tempfile
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("scaling_analysis", ROOT / "scripts/analyze_legofs_scaling.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ScalingAnalysisTest(unittest.TestCase):
    def sample(self, bundle, **changes):
        sample = dict(bundle=bundle, environment="giga-native", ranks=10, source="a",
                      profile="full22-5s", resources={"server_cpus": [18, 19, 20, 21]},
                      phases={p:dict(score=2.0, seconds=5.0, unit="kIOPS") for p in MODULE.FULL_IO500_PHASES})
        sample.update(changes)
        return sample

    def test_only_comparable_samples_are_aggregated(self):
        samples = [self.sample("a"), self.sample("b"),
                   self.sample("c", resources={"server_cpus": [19]}),
                   self.sample("d", environment="vlm-qemu-bi"),
                   self.sample("e", source="new"), self.sample("f", profile="bounded")]
        groups = MODULE.summarize(samples)
        self.assertEqual(sorted(g["repetitions"] for g in groups), [1, 1, 1, 1, 2])
        self.assertEqual(len(groups[0]["phases"]), 22)

    def test_opcode_diagnostics_separate_open_from_create_and_reject_duplicate_rank(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary)
            directory = bundle / "posix-path-summaries"
            directory.mkdir()
            record = {"mpi_rank": 0, "cxl_serving_evidence": [{
                "dispatches_by_opcode": {"59": 2, "61": 5},
                "cq_wait_ns_by_opcode": {"59": 2000000, "61": 500000},
                "authority_timing": {"dispatcher_backend_ns": 2400000},
                "client_timing": {"cq_wait_ns": 2500000}}]}
            (directory / "rank0.json").write_text(json.dumps(record))
            diagnostic = MODULE.operation_diagnostics(bundle)
            operations = diagnostic["per_rank"][0]["operations"]
            self.assertEqual([(o["name"], o["mean_cq_wait_us"]) for o in operations],
                             [("OPEN_LIFECYCLE_PATH", 1000), ("OPEN_OR_CREATE_LIFECYCLE_PATH", 100)])
            (directory / "rank0-copy.json").write_text(json.dumps(record))
            with self.assertRaisesRegex(ValueError, "duplicate final rank"):
                MODULE.operation_diagnostics(bundle)

    def test_duplicate_bundle_is_not_an_independent_repetition(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            MODULE.summarize([self.sample("a"), self.sample("a")])

    def test_runtime_worker_counts_are_not_merged_on_identical_cpu_sets(self):
        samples = [self.sample(str(workers), resources={"server_cpus": [19],
                    "server_runtime_workers": workers}) for workers in (1, 4, "unrecorded")]
        self.assertEqual(len(MODULE.summarize(samples)), 3)


if __name__ == "__main__":
    unittest.main()
