import pathlib
import os
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUN = ROOT / "run-legofs-type3.sh"
BUILD = ROOT / "scripts" / "build_legofs_type3.sh"


class LegofsBuildContractTest(unittest.TestCase):
    def test_kernel_fragment_has_built_in_devdax_and_network(self):
        config = (ROOT / "configs/linux-cxl.config").read_text().splitlines()
        required = {
            "CONFIG_DAX=y",
            "CONFIG_FS_DAX=y",
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
            'char alignment_entry[64] = "BADFS_CXL_MAP_ALIGNMENT="',
            'append_text(sysfs, sizeof(sysfs), "/align")',
            'char block_entry[64] = "BADFS_BENCH_BLOCK_SIZE="',
        ):
            self.assertIn(marker, source)
        self.assertNotIn("BADFS_CXL_MAP_ALIGNMENT=4096", source)
        self.assertNotIn("BADFS_BENCH_BLOCK_SIZE=4096", source)
        self.assertIn('set_ifreq_name(&request, "lo")', source)
        self.assertIn("set_sockaddr(&request.value.address, ipv4(127, 0, 0, 1))", source)
        self.assertIn("BADFS_CONTROL_TRANSPORT=cxl", source)
        self.assertIn("BADFS_CXL_CLIENT_SLOT=0", source)
        self.assertIn("control_transport=cxl-dax-ring", source)
        self.assertNotIn("connect_tcp(", source)
        self.assertNotIn("BADFS_SERVERS=", source)

    def test_cxl_devdax_exposes_real_persistence_flush(self):
        device = (ROOT / "components/linux/drivers/dax/device.c").read_text()
        cxl = (ROOT / "components/linux/drivers/dax/cxl.c").read_text()
        self.assertIn("static int dax_fsync", device)
        self.assertIn("dax_flush(dev_dax->dax_dev", device)
        self.assertIn(".fsync = dax_fsync", device)
        self.assertIn(".persistent = true", cxl)

    def run_cli(self, *arguments, environment=None):
        return subprocess.run(
            [str(RUN), *arguments],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_run_cli_validation_and_accepted_size(self):
        self.assertTrue(RUN.is_file(), "run-legofs-type3.sh is missing")
        help_run = self.run_cli("--help")
        self.assertEqual(help_run.returncode, 0, help_run.stderr)
        conflict = self.run_cli("--build-only", "--run-only")
        self.assertNotEqual(conflict.returncode, 0)
        invalid = self.run_cli("--bytes", "0")
        self.assertNotEqual(invalid.returncode, 0)

        with tempfile.TemporaryDirectory() as temporary:
            fake = pathlib.Path(temporary) / "build"
            fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake.chmod(0o755)
            environment = os.environ.copy()
            environment["LEGOFS_BUILD_SCRIPT"] = str(fake)
            accepted = self.run_cli(
                "--build-only", "--bytes", "65536", environment=environment
            )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)

    def test_build_script_names_complete_manifest(self):
        self.assertTrue(BUILD.is_file(), "scripts/build_legofs_type3.sh is missing")
        source = BUILD.read_text(encoding="utf-8")
        self.assertIn("riscv64gc-unknown-linux-musl", source)
        self.assertIn(
            "a9a118bbe84d8764da0ea0d28b3ab3fae8477fc7e4085d90102b8596fc7c75e4",
            source,
        )
        self.assertIn('"${CROSS_COMPILE}strip" --strip-debug', source)
        self.assertIn('cmp "${badfs_server}" "${verify_server}"', source)
        self.assertNotIn("./config.status", source)
        self.assertNotIn("qemu_configure=", source)
        self.assertIn('LEGOFS_SOURCE_ROOT="${ROOT}/components/legofs"', source)
        self.assertIn('OUT="${LEGOFS_TYPE3_OUT:-', source)
        self.assertIn('--source "legofs=${LEGOFS_SOURCE_ROOT}"', source)
        self.assertIn("--enable-libpmem", source)
        self.assertIn("--enable-slirp", source)
        self.assertIn(
            "SLIRP_COMMIT=26be815b86e8d49add8c9a8b320239b9594ff03d",
            source,
        )
        self.assertIn('fetch --depth=1 origin "${SLIRP_COMMIT}"', source)
        self.assertIn("PMEM_DEB_VERSION=1.13.1-1.1ubuntu2", source)
        self.assertIn("PMEM_DEV_SHA256=f710b78c", source)
        self.assertIn("PMEM_RUNTIME_SHA256=8f1be1cc", source)
        self.assertIn("pkg-config --modversion libpmem", source)
        self.assertIn("pkg-config --variable=libdir libpmem", source)
        self.assertIn(
            's|^libdir=/usr/lib/x86_64-linux-gnu$|libdir=${pmem_libdir}|',
            source,
        )
        self.assertIn('libpmem_build=${PMEM_MANIFEST}', source)
        self.assertIn("SPDLOG_DEB_VERSION=1:1.12.0+ds-2build1", source)
        self.assertIn("SPDLOG_DEV_SHA256=850b97a9", source)
        self.assertIn("FMT_DEB_VERSION=9.1.0+ds1-2", source)
        self.assertIn("FMT_DEV_SHA256=cc05cae4", source)
        self.assertIn("-DCXLMEMSIM_ENABLE_RDMA=OFF", source)
        self.assertIn("-DCXLMEMSIM_ENABLE_SLUGALLOCATOR=OFF", source)
        self.assertIn('cxlmemsim_build_deps=${CXL_DEPS_MANIFEST}', source)
        self.assertIn("--no-artifact-hashes", source)
        self.assertNotIn('${ROOT}/../..', source)
        self.assertIn('--compiler "qemu=${qemu} --version"', source)
        for artifact in (
            "qemu",
            "opensbi",
            "u_boot",
            "linux_legofs",
            "legofs_disk",
            "badfs_server",
            "badfs_bench",
            "cxlmemsim_server",
        ):
            with self.subTest(artifact=artifact):
                self.assertIn(f'--artifact "{artifact}=', source)

    def test_io500_build_hashes_artifacts_and_records_live_legofs_source(self):
        source = (ROOT / "scripts/build_legofs_io500.sh").read_text(encoding="utf-8")
        self.assertIn('--source "legofs=$LEGOFS_ROOT"', source)
        self.assertIn(
            'cxlmemsim_build_deps=$ROOT/out/legofs-type3/results/cxlmemsim-build-debs.txt',
            source,
        )
        self.assertNotIn("--no-artifact-hashes", source)
        self.assertIn('fetch --depth=1 origin "$commit"', source)
        self.assertNotIn('git clone "$repository" "$directory"', source)
        self.assertIn(
            'fetch --depth=1 --filter=blob:none origin "$LLVM_COMMIT"',
            source,
        )
        self.assertNotIn("git clone --filter=blob:none --no-checkout", source)

    def test_linked_rust_toolchain_uses_its_actual_sysroot_target(self):
        for path in (BUILD, ROOT / "scripts/build_legofs_io500.sh"):
            source = path.read_text(encoding="utf-8")
            self.assertIn("rustc --print sysroot", source)
            self.assertIn("libstd-*.rlib", source)
            self.assertNotIn("rustup target list", source)


if __name__ == "__main__":
    unittest.main()
