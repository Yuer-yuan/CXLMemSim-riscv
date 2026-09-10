import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("pressure_patch", ROOT / "scripts/patch_io500_pressure.py")
PATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PATCH)


class PressurePatchTests(unittest.TestCase):
    def test_random_segment_bound_preserves_default_and_rejects_source_drift(self):
        for name in ("phase_ior_rnd1MB.c", "phase_ior_rnd4K.c"):
            with self.subTest(name=name):
                source = (ROOT / "tests/fixtures/io500" / name).read_text()
                result = PATCH.patch_ior_random_source(source)
                self.assertIn('INI_INT, "10000000", & legofs_segment_count', result)
                self.assertIn('"-s=%d", legofs_segment_count', result)
                self.assertIn('if(legofs_segment_count <= 0)', result)
                self.assertIn('if(legofs_segment_count != 10000000)', result)
                self.assertIn('not submission eligible', result)
                self.assertEqual(PATCH.patch_ior_random_source(result), result)
                for invalid in (source.replace('"-s=%d", 10000000', '"-s=%d", 999'),
                                source + '\nint legofs_segment_count;\n'):
                    with self.assertRaises(ValueError):
                        PATCH.patch_ior_random_source(invalid)

    def test_easy_fixed_block_preserves_default_and_wearout(self):
        source = (ROOT / "tests/fixtures/io500/phase_ior_easy_write.c").read_text()
        result = PATCH.patch_ior_easy_source(source)
        self.assertIn('INI_BOOL, "FALSE", & o.legofs_fixed_work', result)
        self.assertIn('o.legofs_fixed_work ? 601 : opt.stonewall', result)
        self.assertIn('stoneWallingWearOut=1', result)
        self.assertEqual(PATCH.patch_ior_easy_source(result), result)
        with self.assertRaises(ValueError):
            PATCH.patch_ior_easy_source(source.replace('opt.stonewall', '999'))
        with self.assertRaises(ValueError):
            PATCH.patch_ior_easy_source(source + '\nint legofs_fixed_work;\n')

    def source(self):
        # Unmodified source from pinned IO500 a69cf60cf765.
        return (ROOT / "tests/fixtures/io500/phase_mdtest_easy_write.c").read_text()

    def test_fixed_work_is_opt_in_and_hash_visible(self):
        result = PATCH.patch_source(self.source())
        self.assertIn('INI_BOOL, "FALSE", & o.legofs_fixed_work', result)
        self.assertIn('if(!o.legofs_fixed_work){\n    u_argv_push(argv, "-W")', result)
        self.assertIn('mdtest_easy_add_params(argv);', result)
        self.assertIn('not submission eligible', result)
        self.assertEqual(PATCH.patch_source(result), result)

    def test_source_drift_and_partial_patch_fail_closed(self):
        for text in (
            self.source().replace('"-W"', '"-X"'),
            self.source() + '\nint legofs_fixed_work;\n',
            self.source().replace('mdtest_easy_add_params(argv);', ''),
        ):
            with self.assertRaises(ValueError):
                PATCH.patch_source(text)


if __name__ == '__main__':
    unittest.main()
