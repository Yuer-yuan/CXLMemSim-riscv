import importlib.util
import pathlib
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "legofs_type3_2node.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("legofs_type3_2node", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LegofsRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.runner = load_runner()
        self.temporary = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.temporary.name)
        self.paths = self.runner.RuntimePaths.for_test(root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_commands_are_exact_sifive_u_persistent_type3_nodes(self):
        commands = [
            self.runner.build_qemu_command(self.paths, node, 19300, 23345)
            for node in (0, 1)
        ]
        for node, command in enumerate(commands):
            joined = " ".join(command)
            self.assertEqual(command[:3], ["qemu-system-riscv64", "-M", "sifive_u"])
            self.assertEqual(sum("cxl-type3" in argument for argument in command), 1)
            self.assertIn("coherence-v2=on", joined)
            self.assertIn(f"coherence-v2-host-id={node}", joined)
            self.assertIn("coherence-v2-cache-capacity=262144", joined)
            self.assertIn("coherence-v2-cache-ways=4", joined)
            self.assertIn("coherence-v2-write-through=off", joined)
            self.assertNotIn("-M virt", joined)
            self.assertNotIn("volatile-memdev", joined)
            self.assertIn("memory-backend-file", joined)
            self.assertIn("pmem=on", joined)
            self.assertIn(f"persistent-memdev=t3ssd-node{node}", joined)
            self.assertIn("cxlmemsim-port=19300", joined)
            self.assertEqual(joined.count("persistent-memdev="), 1)
            for identifier in ("t3ssd", "t3lsa", "cxl", "rp-t3", "t3", "net"):
                self.assertIn(f"{identifier}-node{node}", joined)

        self.assertIn("hostfwd=tcp:127.0.0.1:23345-:3345", " ".join(commands[0]))
        self.assertNotIn("hostfwd=", " ".join(commands[1]))
        self.assertNotEqual(
            self.paths.cxl_ssd_path(0),
            self.paths.cxl_ssd_path(1),
        )

    def test_server_uses_authoritative_ssd_and_tcp_v2_trace(self):
        command = self.runner.build_server_command(self.paths, 19300)
        joined = " ".join(command)
        self.assertIn("--comm-mode=tcp", joined)
        self.assertIn("--port=19300", joined)
        self.assertIn("--coherence-v2=true", joined)
        self.assertIn("--coherence-v2-snoop-timeout-ms=5000", joined)
        self.assertIn("--coherence-v2-trace=", joined)
        self.assertIn("--backing-mode=ssd-stream", joined)
        self.assertIn("--ssd-backing-file=", joined)

    def test_environment_removes_legacy_transports(self):
        environment = self.runner.qemu_environment(
            self.paths,
            {
                "PATH": "/bin",
                "CXL_TRANSPORT_MODE": "shm",
                "CXL_PGAS_SHM": "/wrong",
                "CXL_MEMSIM_SERVER": "wrong",
            },
        )
        for name in ("CXL_TRANSPORT_MODE", "CXL_PGAS_SHM", "CXL_MEMSIM_SERVER"):
            self.assertNotIn(name, environment)
        self.assertEqual(environment["PATH"].split(":", 1)[0], str(self.paths.qemu.parent))

    def test_overlap_requires_two_live_intervals(self):
        self.assertEqual(self.runner.overlap_ns((10, 50), (20, 60)), 30)
        with self.assertRaisesRegex(ValueError, "did not overlap"):
            self.runner.overlap_ns((10, 20), (20, 30))

    def test_uboot_sequence_interrupts_autoboot_before_cxl_commands(self):
        source = RUNNER.read_text(encoding="utf-8")
        interrupt = source.index('console.wait("Hit any key to stop autoboot"')
        prompt = source.index('console.wait("=> ", timeout)', interrupt)
        listing = source.index('console.command_until_prompt("cxl list"', prompt)
        self.assertLess(interrupt, prompt)
        self.assertLess(prompt, listing)

    def test_linux_explicitly_routes_persistent_cxl_region_to_devdax(self):
        source = RUNNER.read_text(encoding="utf-8")
        self.assertIn("cxl_core.pmem_as_dax=1", source)

        config = (ROOT / "configs" / "linux-cxl.config").read_text(encoding="utf-8")
        for option in (
            "CONFIG_MEMORY_HOTPLUG=y",
            "CONFIG_MEMORY_HOTREMOVE=y",
            "CONFIG_SPARSEMEM_VMEMMAP=y",
            "CONFIG_ZONE_DEVICE=y",
            "# CONFIG_DEV_DAX_KMEM is not set",
        ):
            self.assertIn(option, config)

        region_source = (
            ROOT / "components" / "linux" / "drivers" / "cxl" / "core" / "region.c"
        ).read_text(encoding="utf-8")
        self.assertIn("module_param_named(pmem_as_dax", region_source)
        self.assertIn("if (cxl_pmem_as_dax)", region_source)
        self.assertIn("return devm_cxl_add_dax_region(cxlr);", region_source)

        cxl_dax_source = (
            ROOT / "components" / "linux" / "drivers" / "dax" / "cxl.c"
        ).read_text(encoding="utf-8")
        self.assertIn("failed to create device-dax for CXL region", cxl_dax_source)
        dax_device_source = (
            ROOT / "components" / "linux" / "drivers" / "dax" / "device.c"
        ).read_text(encoding="utf-8")
        self.assertIn("failed to map device-dax pages", dax_device_source)

    def test_guest_waits_for_asynchronous_cxl_region_and_dax_probe(self):
        source = (ROOT / "guest" / "legofs_node_init.c").read_text(encoding="utf-8")
        self.assertIn('wait_for_prefix("/sys/bus/cxl/devices", "region", 1)', source)
        self.assertIn('wait_for_prefix("/sys/bus/cxl/devices", "decoder", 1)', source)
        self.assertIn('wait_for_prefix("/sys/bus/dax/devices", "dax", 1)', source)
        self.assertNotIn("/sys/class/dax", source)
        self.assertIn("__builtin_offsetof(struct linux_dirent64, d_name) + 1", source)
        self.assertNotIn("sizeof(*entry) + 1", source)

    def test_console_waiting_uses_chunks_but_sidecars_use_complete_lines(self):
        source = RUNNER.read_text(encoding="utf-8")
        append = source.index("self.output += decoded")
        split = source.index('while b"\\n" in pending:', append)
        event = source.index("self._capture_event(raw_line)", split)
        self.assertLess(append, split)
        self.assertLess(split, event)


if __name__ == "__main__":
    unittest.main()
