import pathlib
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
CXL_BASE = "716c16c9efc7a733006d0772f8c6c4bb055f7b15"


def git(*args, cwd=ROOT):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


class LegofsSourceTest(unittest.TestCase):
    def test_cxlmemsim_descends_from_approved_mesi_v2_commit(self):
        component = ROOT / "components" / "cxlmemsim"
        self.assertEqual(git("merge-base", "HEAD", CXL_BASE, cwd=component), CXL_BASE)

    def test_legofs_is_a_pinned_component(self):
        modules = (ROOT / ".gitmodules").read_text(encoding="utf-8")
        self.assertIn("path = components/legofs", modules)
        self.assertIn("url = https://github.com/Zettai-US/legofs.git", modules)
        self.assertTrue((ROOT / "components" / "legofs" / "Cargo.toml").is_file())

    def test_build_uses_only_the_legofs_component(self):
        build = (ROOT / "scripts" / "build_legofs_type3.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('LEGOFS_SOURCE_ROOT="${ROOT}/components/legofs"', build)
        self.assertIn('${LEGOFS_SOURCE_ROOT}/Cargo.toml', build)
        self.assertIn('--source "legofs=${LEGOFS_SOURCE_ROOT}"', build)
        self.assertNotIn('${ROOT}/../..', build)


if __name__ == "__main__":
    unittest.main()
