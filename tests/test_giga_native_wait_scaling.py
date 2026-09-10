import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("wait_scaling", Path(__file__).resolve().parents[1] / "scripts/run_giga_native_wait_scaling.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class MeasuredCommandVerdictTest(unittest.TestCase):
    def test_perf_zero_cannot_hide_missing_failed_or_unclean_workload(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            self.assertFalse(MODULE.accepted_case(0, bundle)["accepted"])
            for verdict, absent, expected in [("failed", True, False), ("accepted", False, False), ("accepted", True, True)]:
                (bundle / "result.json").write_text(json.dumps({"verdict": verdict, "cleanup": {"absent": absent}}))
                self.assertEqual(MODULE.accepted_case(0, bundle)["accepted"], expected)
            self.assertFalse(MODULE.accepted_case(1, bundle)["accepted"])


if __name__ == "__main__":
    unittest.main()
