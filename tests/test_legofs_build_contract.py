import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class LegofsBuildContractTest(unittest.TestCase):
    def test_kernel_fragment_has_built_in_devdax_and_network(self):
        config = (ROOT / "configs/linux-cxl.config").read_text().splitlines()
        required = {
            "CONFIG_DAX=y",
            "CONFIG_DEV_DAX=y",
            "CONFIG_DEV_DAX_CXL=y",
            "CONFIG_NET=y",
            "CONFIG_INET=y",
            "CONFIG_UNIX=y",
            "CONFIG_PACKET=y",
            "CONFIG_VIRTIO_NET=y",
        }
        self.assertTrue(required.issubset(set(config)))

    def test_init_has_role_dax_and_strict_markers(self):
        source = (ROOT / "guest/legofs_node_init.c").read_text()
        for marker in (
            "legofs.role=",
            "LEG_OFS_CXL_READY",
            "LEG_OFS_SERVER_READY",
            "LEG_OFS_BENCHMARK_BEGIN",
            "LEG_OFS_BENCHMARK_PASS",
            "BADFS_LIFECYCLE_DIRECT_REQUIRED=1",
            "BADFS_LIFECYCLE_DIRECT_READ_REQUIRED=1",
        ):
            self.assertIn(marker, source)


if __name__ == "__main__":
    unittest.main()
