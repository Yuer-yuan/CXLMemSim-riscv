import pathlib
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
CXL_BASE = "716c16c9efc7a733006d0772f8c6c4bb055f7b15"
LEGOFS_BASE = "96f733940251d6484dad0ba2cfbe99dcf5259776"


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

    def test_legofs_descends_from_approved_commit(self):
        component = ROOT / "components" / "legofs"
        self.assertTrue(component.is_dir(), "components/legofs is missing")
        self.assertEqual(git("merge-base", "HEAD", LEGOFS_BASE, cwd=component), LEGOFS_BASE)

    def test_gitmodules_uses_approved_legofs_remote(self):
        modules = (ROOT / ".gitmodules").read_text(encoding="utf-8")
        self.assertIn("path = components/legofs", modules)
        self.assertIn("url = https://github.com/Zettai-US/legofs.git", modules)


if __name__ == "__main__":
    unittest.main()
