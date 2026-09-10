import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ior_seed", ROOT / "scripts/patch_io500_ior_random_seed.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class IorSeedPatchTest(unittest.TestCase):
    SOURCE = "static int init_random_seed(void) { srandom(seed); }\nIOR_offset_t *GetOffsetArrayRandom(void) { srandom(seed); }"

    def test_both_passes_seed_rand_and_patch_is_idempotent(self):
        fixed = MODULE.patch_source(self.SOURCE)
        self.assertEqual(fixed.count("srand(seed);"), 2)
        self.assertNotIn("srandom", fixed)
        self.assertEqual(MODULE.patch_source(fixed), fixed)

    def test_changed_or_partially_patched_source_fails_closed(self):
        for source in (self.SOURCE.replace("srandom(seed);", "srand(seed);", 1),
                       self.SOURCE.replace("GetOffsetArrayRandom", "DifferentAlgorithm")):
            with self.assertRaises(ValueError):
                MODULE.patch_source(source)

    def test_both_payload_build_paths_apply_dependency_fix(self):
        for name in ("build_legofs_io500.sh", "rebuild_legofs_io500_payload.sh"):
            self.assertIn("patch_io500_ior_random_seed.py", (ROOT / "scripts" / name).read_text())


if __name__ == "__main__":
    unittest.main()
