import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "linux-cxl.config"
REQUIRED_BUILTINS = (
    "CONFIG_PCI",
    "CONFIG_PCIEPORTBUS",
    "CONFIG_EFI",
    "CONFIG_EFI_STUB",
    "CONFIG_CXL_BUS",
    "CONFIG_CXL_PCI",
    "CONFIG_CXL_ACPI",
    "CONFIG_CXL_PORT",
    "CONFIG_CXL_REGION",
    "CONFIG_CXL_TYPE2_ACCEL",
    "CONFIG_DEVTMPFS",
    "CONFIG_DEVTMPFS_MOUNT",
    "CONFIG_DEVMEM",
    "CONFIG_VIRTIO",
    "CONFIG_VIRTIO_PCI",
    "CONFIG_VIRTIO_BLK",
    "CONFIG_EXT4_FS",
    "CONFIG_EXT4_USE_FOR_EXT2",
    "CONFIG_BLK_DEV_INITRD",
    "CONFIG_PROC_FS",
    "CONFIG_SYSFS",
    "CONFIG_TMPFS",
    "CONFIG_BINFMT_ELF",
)


class BuildContractTest(unittest.TestCase):
    def test_linux_fragment_requires_all_runtime_features_builtin(self):
        self.assertTrue(CONFIG.is_file(), "configs/linux-cxl.config is missing")
        values = {}
        for line in CONFIG.read_text(encoding="utf-8").splitlines():
            if line.startswith("CONFIG_") and "=" in line:
                name, value = line.split("=", 1)
                values[name] = value
        for name in REQUIRED_BUILTINS:
            with self.subTest(name=name):
                self.assertEqual(values.get(name), "y")
        self.assertNotIn("CONFIG_INITRAMFS_SOURCE", values)

if __name__ == "__main__":
    unittest.main()
