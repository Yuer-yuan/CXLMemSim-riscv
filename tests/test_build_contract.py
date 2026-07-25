import pathlib
import re
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "linux-cxl.config"
BUILD_SCRIPT = ROOT / "scripts" / "build.sh"
PYLIBFDT_PREP = ROOT / "scripts" / "prepare_uboot_pylibfdt.py"
REQUIRED_BUILTINS = (
    "CONFIG_PCI",
    "CONFIG_PCIEPORTBUS",
    "CONFIG_EFI",
    "CONFIG_EFI_STUB",
    "CONFIG_NONPORTABLE",
    "CONFIG_HVC_RISCV_SBI",
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

    def test_build_script_names_every_required_build_contract(self):
        self.assertTrue(BUILD_SCRIPT.is_file(), "scripts/build.sh is missing")
        source = BUILD_SCRIPT.read_text(encoding="utf-8")
        for contract in (
            "--target-list=riscv64-softmmu",
            "--disable-werror",
            "sifive_unleashed_qemu_cxl_defconfig",
            "prepare_uboot_pylibfdt.py",
            'OPENSBI="${BUILD}/opensbi/platform/generic/firmware/fw_dynamic.bin"',
            "platform-cflags-y=-std=gnu11",
            "PLATFORM=generic",
            "-march=rv64imafdc",
            "-mabi=lp64d",
            "-nostdlib",
            "CONFIG_INITRAMFS_SOURCE",
            "olddefconfig",
            "mke2fs",
            "debugfs",
            "cxlmemsim_server",
            "write_manifest.py",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, source)
        self.assertNotIn(
            "NO_PYTHON=1",
            source,
            "binman needs the in-tree pylibfdt built for the selected Python",
        )
        self.assertLess(
            source.index("[build] OpenSBI"),
            source.index("[build] U-Boot"),
            "OpenSBI must exist before U-Boot binman assembles its ITB",
        )
        self.assertRegex(
            source,
            re.compile(r'merge_config\.sh" \\\n\s+-m '),
            "merge_config must not run make from the superproject root",
        )
        subprocess.run(["bash", "-n", str(BUILD_SCRIPT)], check=True)

    def test_pylibfdt_adapter_rewrites_all_legacy_swig_calls(self):
        legacy = (
            "SWIG_Python_AppendOutput(resultobj, buff);\n"
            "SWIG_Python_AppendOutput(resultobj, val);\n"
            "SWIG_Python_AppendOutput(resultobj, val);\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            source = pathlib.Path(temporary) / "libfdt.i_shipped"
            output = pathlib.Path(temporary) / "libfdt.i"
            source.write_text(legacy, encoding="utf-8")
            run = subprocess.run(
                [
                    "python3",
                    str(PYLIBFDT_PREP),
                    "--source",
                    str(source),
                    "--output",
                    str(output),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            adapted = output.read_text(encoding="utf-8")
        self.assertNotIn("SWIG_Python_AppendOutput", adapted)
        self.assertEqual(adapted.count("SWIG_AppendOutput"), 3)


if __name__ == "__main__":
    unittest.main()
