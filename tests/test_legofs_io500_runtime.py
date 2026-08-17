import importlib.util
import json
import pathlib
import tempfile
import threading
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "legofs_io500.py"
BUILD_SCRIPT = ROOT / "scripts" / "build_legofs_io500.sh"
NOV_GCC = ROOT / "scripts" / "riscv64-nov-gcc"
RANK_SCRIPT = ROOT / "guest" / "legofs_io500_rank.sh"
INIT_SCRIPT = ROOT / "guest" / "legofs_io500_init.sh"


def load_runner():
    spec = importlib.util.spec_from_file_location("legofs_io500", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Io500RuntimeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runner = load_runner()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = self.runner.Paths(pathlib.Path(self.temporary.name), "tiny")

    def tearDown(self):
        self.temporary.cleanup()

    def test_variable_client_count_and_result_label_are_explicit(self):
        args = self.runner.parse_args(
            [
                "--stage",
                "easy-smoke",
                "--client-count",
                "2",
                "--server-count",
                "2",
                "--result-label",
                "easy-c2s2-r1",
            ]
        )
        self.assertEqual(args.client_count, 2)
        self.assertEqual(args.server_count, 2)
        self.assertFalse(args.server_read_exclusive)
        paths = self.runner.Paths(
            pathlib.Path(self.temporary.name), args.stage, args.result_label
        )
        self.assertEqual(paths.stage, "easy-smoke")
        self.assertEqual(paths.run.name, "easy-c2s2-r1")
        self.assertEqual(paths.bundle.name, "easy-c2s2-r1")

        negative_control = self.runner.parse_args(
            ["--stage", "easy-smoke", "--server-read-exclusive"]
        )
        self.assertTrue(negative_control.server_read_exclusive)

        init = INIT_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("io500.client_count=", RUNNER.read_text(encoding="utf-8"))
        self.assertIn('-n "$client_count"', init)

    def test_topology_has_unique_hosts_on_one_shared_device_dram(self):
        commands = [
            self.runner.qemu_command(
                self.paths,
                role="server" if host == 0 else "client",
                server_index=0 if host == 0 else None,
                client_index=None if host == 0 else host - 1,
                host_id=host,
                coherence_port=19000,
                multicast_port=19001,
            )
            for host in range(11)
        ]
        joined = [" ".join(command) for command in commands]
        self.assertEqual(len(commands), 11)
        for host, text in enumerate(joined):
            self.assertIn("-M sifive_u", text)
            self.assertIn(
                "-cpu rv64,h=false,sstc=false,svadu=false,zicboz=false,"
                "zicbom=true,cbom_blocksize=64",
                text,
            )
            self.assertIn("cxl-fmw.0.size=64G", text)
            self.assertIn("persistent-memdev=", text)
            self.assertIn("size=64G,share=on", text)
            self.assertIn(f"mem-path={self.paths.device_dram}", text)
            self.assertNotIn("pmem=on", text)
            self.assertIn(f"coherence-v2-host-id={host}", text)
            self.assertIn("mcast=230.77.0.1:19001", text)
            self.assertIn(f"mac=52:54:00:77:00:{host:02x}", text)
        self.assertTrue(
            all("coherence-v2-read-exclusive=off" in text for text in joined)
        )
        proof_server = " ".join(
            self.runner.qemu_command(
                self.paths,
                role="server",
                server_index=0,
                client_index=None,
                host_id=0,
                coherence_port=19000,
                multicast_port=19001,
                read_exclusive=True,
            )
        )
        self.assertIn("coherence-v2-read-exclusive=on", proof_server)
        self.assertEqual(len({self.paths.device_dram for _ in range(11)}), 1)

    def test_coherence_cache_capacity_is_an_explicit_experiment_variable(self):
        args = self.runner.parse_args(
            ["--stage", "easy-smoke", "--coherence-cache-mib", "2048"]
        )
        command = self.runner.qemu_command(
            self.paths,
            role="client",
            server_index=None,
            client_index=0,
            host_id=1,
            coherence_port=19000,
            multicast_port=19001,
            coherence_cache_bytes=args.coherence_cache_mib * 1024**2,
        )
        self.assertIn(
            "coherence-v2-cache-capacity=2147483648", " ".join(command)
        )

    def test_ssd_residency_capacity_is_an_explicit_experiment_variable(self):
        args = self.runner.parse_args(
            ["--stage", "easy-smoke", "--ssd-cache-mib", "16384"]
        )
        command = self.runner.server_command(
            self.paths, 19000, False, args.ssd_cache_mib
        )
        self.assertIn("--ssd-cache-mb=16384", command)

    def test_two_server_topology_has_twelve_unique_cxl_hosts(self):
        commands = []
        for server in range(2):
            commands.append(
                self.runner.qemu_command(
                    self.paths,
                    role="server",
                    server_index=server,
                    client_index=None,
                    host_id=server,
                    coherence_port=19000,
                    multicast_port=19001,
                )
            )
        for client in range(10):
            commands.append(
                self.runner.qemu_command(
                    self.paths,
                    role="client",
                    server_index=None,
                    client_index=client,
                    host_id=client + 2,
                    coherence_port=19000,
                    multicast_port=19001,
                )
            )
        text = [" ".join(command) for command in commands]
        self.assertEqual(len(commands), 12)
        self.assertTrue(all("coherence-v2-read-exclusive=off" in item for item in text))
        for host, item in enumerate(text):
            self.assertIn(f"coherence-v2-host-id={host}", item)
        self.assertIn(str(self.paths.server_state(0)), text[0])
        self.assertIn(str(self.paths.server_state(1)), text[1])

    def test_server_uses_one_64_gib_ssd_stream_region(self):
        text = " ".join(self.runner.server_command(self.paths, 19000, True))
        self.assertIn("--capacity=65536", text)
        self.assertIn("--backing-mode=ssd-stream", text)
        self.assertIn("--coherence-v2=true", text)
        self.assertIn("--coherence-v2-proof-trace=", text)

        counter_text = " ".join(self.runner.server_command(self.paths, 19000, False))
        self.assertIn("--coherence-v2-counters=true", counter_text)

        full_text = " ".join(
            self.runner.server_command(self.paths, 19000, False, full_trace=True)
        )
        self.assertIn("--coherence-v2-trace=", full_text)
        self.assertNotIn("--coherence-v2-counters=true", full_text)
        self.assertNotIn("/dev/null", counter_text)

    def test_guests_derive_the_shared_region_identity_from_dax(self):
        init = INIT_SCRIPT.read_text(encoding="utf-8")
        rank = RANK_SCRIPT.read_text(encoding="utf-8")
        self.assertIn('BADFS_LIFECYCLE_DEVICE="$dax_path"', init)
        self.assertIn('BADFS_LIFECYCLE_DEVICES="$lifecycle_devices"', init)
        self.assertIn('BADFS_LIFECYCLE_DEVICES="$(cat /run/lifecycle-devices)"', rank)
        self.assertIn("BADFS_LIFECYCLE_POOL_OFFSET", init)
        self.assertIn("BADFS_LIFECYCLE_REGION_SIZE=68719476736", init)
        self.assertNotIn('BADFS_FABRIC_REGION_ID=', init + rank)

    def test_sifive_u_payload_build_has_strict_isa_and_intercept_abi(self):
        wrapper = NOV_GCC.read_text(encoding="utf-8")
        build = BUILD_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("-march=rv64imafdc", wrapper)
        self.assertIn("-mabi=lp64d", wrapper)
        self.assertIn('"${CROSS_COMPILE}objdump" -d "$binary"', build)
        self.assertIn('require_sifive_u_isa "$MUSL_PREFIX/lib/libc.so"', build)
        self.assertIn('require_sifive_u_isa "$BUSYBOX_BUILD/busybox"', build)
        self.assertIn('require_sifive_u_isa "$LIBUNWIND_PREFIX/lib/libunwind.so.1"', build)
        self.assertIn("--mattr=+m,+a,+f,+d,+c,+zicsr,+zifencei", build)
        self.assertIn("invalid instruction encoding", build)
        self.assertIn("LIBCC=$BUILTINS_ARCHIVE", build)
        self.assertIn("clang_rt.builtins-riscv64", build)
        self.assertIn("MUSL_BUILTINS $MUSL_EMPTY_LIBGCC_EH", build)
        self.assertIn("musl GCC specs do not bind the pinned compiler runtime", build)
        self.assertIn('require_needed "$PAYLOAD_ROOT/lib/libbadfs_intercept.so"', build)
        self.assertIn("require_syscall_intercept_abi", build)
        self.assertIn(
            'require_syscall_intercept_abi "$PAYLOAD_ROOT/lib/libbadfs_intercept.so"',
            build,
        )
        self.assertIn('does not import the syscall-intercept hook point', build)
        self.assertIn('lacks the required POSIX interception entry', build)
        self.assertIn("require_unwind_provider", build)
        self.assertIn("reject_glibc_versions", build)
        self.assertIn("-C panic=abort", build)
        self.assertIn(
            'SYSINT_ROOT="$LEGOFS_TOOL_ROOT/syscall-intercept-riscv"',
            build,
        )
        self.assertIn(
            'cargo build --manifest-path "$LEGOFS_ROOT/Cargo.toml"',
            build,
        )
        self.assertIn("LLVM_COMMIT=87f0227cb60147a26a1eeb4fb06e3b505e9c7261", build)
        self.assertIn("/lib/ld-musl-riscv64.so.1", build)
        self.assertNotIn("/usr/riscv64-linux-gnu/lib/*.so", build)

    def test_manifest_checks_live_artifacts_without_hash_or_head_gate(self):
        artifacts = {
            "qemu": self.paths.qemu,
            "cxlmemsim_server": self.paths.cxlmemsim,
            "opensbi": self.paths.opensbi,
            "u_boot": self.paths.uboot,
            "linux": self.paths.linux,
            "payload": self.paths.payload,
            "dependency_versions": self.paths.dependency_versions,
            "isa_gate": self.paths.isa_gate,
        }
        for path in artifacts.values():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")
        self.paths.manifest.parent.mkdir(parents=True, exist_ok=True)
        self.paths.manifest.write_text(
            json.dumps({
                "schema_version": 2,
                "superproject_commit": "development-tree-may-be-dirty",
                "artifacts": {
                    name: {"path": str(path), "size": 1}
                    for name, path in artifacts.items()
                },
            }),
            encoding="utf-8",
        )
        build = self.runner.verify_manifest(self.paths)
        self.assertNotIn("sha256", json.dumps(build))

    def test_registration_gate_requires_exact_host_and_session_sets(self):
        records = [
            {
                "event": "registration",
                "status": "OK",
                "src_host": host,
                "session_id": host + 100,
            }
            for host in range(11)
        ]
        accepted = self.runner.registrations(records, 11)
        self.assertEqual([record["src_host"] for record in accepted], list(range(11)))
        with self.assertRaisesRegex(ValueError, "expected host IDs"):
            self.runner.registrations(records[:-1], 11)

    def test_hello_gate_requires_rank_host_mapping_and_overlap(self):
        class FakeConsole:
            def __init__(self, output):
                self.output = output

        output = "\n".join(
            f"LEGOFS_MPI_HELLO rank={rank} size=10 host=client{rank} "
            f"begin_ns={100 + rank} end_ns={1000 + rank}"
            for rank in range(10)
        )
        result = self.runner.parse_hello([FakeConsole(output)])
        self.assertGreater(result["overlap_ns"], 0)

    def test_hydra_proxy_commands_drop_console_carriage_returns(self):
        output = "".join(
            f"HYDRA_LAUNCH: /payload/bin/hydra_pmi_proxy --proxy-id {rank} \r\n"
            for rank in range(10)
        )
        commands = self.runner.parse_hydra_proxy_commands(output)
        self.assertEqual(sorted(commands), list(range(10)))
        self.assertTrue(all("\r" not in command for command in commands.values()))

    def test_clock_sync_requires_one_receipt_from_all_eleven_guests(self):
        class FakeProcess:
            @staticmethod
            def poll():
                return None

        class FakeConsole:
            def __init__(self, role, index, split=False):
                self.role = role
                self.index = index
                self.split = split
                self.output = ""
                self.sent = []
                self.condition = threading.Condition()
                self.process = FakeProcess()
                self.timer = None

            def send(self, command):
                self.sent.append(command)
                target = int(command.rsplit(" ", 1)[1])
                receipt = (
                    f"LEGOFS_IO500_TIME_SYNC role={self.role} index={self.index} "
                    f"requested={target} observed={target}\r\n"
                )
                with self.condition:
                    if not self.split:
                        self.output += receipt
                        self.condition.notify_all()
                        return
                    split_at = receipt.index(" observed=") + len(" observed=") + 1
                    self.output += receipt[:split_at]

                def complete_receipt():
                    with self.condition:
                        self.output += receipt[split_at:]
                        self.condition.notify_all()

                self.timer = threading.Timer(0.01, complete_receipt)
                self.timer.start()

        server = FakeConsole("server", 0, split=True)
        clients = [FakeConsole("client", index) for index in range(10)]
        result = self.runner.synchronize_guest_clocks(
            [server], clients, target_epoch=1_800_000_000
        )
        server.timer.join()
        self.assertEqual(result["target_epoch"], 1_800_000_000)
        self.assertEqual(len(result["records"]), 11)
        self.assertEqual(
            [record["index"] for record in result["records"]],
            [0] + list(range(10)),
        )
        self.assertTrue(all(
            console.sent == ["LEGOFS_SET_TIME 1800000000"]
            for console in [server] + clients
        ))

    def test_scc_and_standard_timestamp_metadata_share_legofs_clock(self):
        for stage in ("scc", "standard"):
            config = (ROOT / "configs" / f"io500-{stage}.ini").read_text()
            self.assertIn(f"datadir = /badfs/io500-{stage}\n", config)
            self.assertIn(
                f"resultdir = /badfs/io500-{stage}-results\n", config
            )
            self.assertNotIn(f"resultdir = /results/{stage}\n", config)

        rank_script = RANK_SCRIPT.read_text()
        self.assertIn('/payload/bin/export-io500-results "$stage"', rank_script)
        self.assertNotIn("/bin/busybox cp", rank_script)
        build = BUILD_SCRIPT.read_text()
        self.assertIn('"$MUSL_CC" -O2 -Wall -Wextra -Werror', build)
        self.assertIn('io500_result_export=$PAYLOAD_ROOT/bin/export-io500-results', build)
        exporter = (ROOT / "guest" / "export_io500_results.c").read_text()
        self.assertIn("opendir(source_directory)", exporter)
        self.assertIn("while ((entry = readdir(directory)) != NULL)", exporter)
        self.assertIn('strcmp(entry->d_name, "config.ini")', exporter)
        self.assertIn('strcmp(entry->d_name, "result.txt")', exporter)

    def test_io500_avoids_diagnostic_sync_io_in_the_timed_product_path(self):
        init = (ROOT / "guest" / "legofs_io500_init.sh").read_text()
        rank = RANK_SCRIPT.read_text()
        self.assertIn("mount -t ext2 -o rw /dev/vdb /state", init)
        self.assertNotIn("mount -t ext2 -o rw,sync /dev/vdb /state", init)
        self.assertIn("export BADFS_LIFECYCLE_TRACE=/tmp/lifecycle.jsonl", init)
        self.assertIn("export BADFS_FSYNC_ON_CLOSE=1", init)
        self.assertIn("export BADFS_FSYNC_ON_CLOSE=1", rank)
        self.assertIn("export BADFS_LIFECYCLE_BLOB=0", init)
        self.assertIn("export BADFS_LIFECYCLE_BLOB=0", rank)
        self.assertIn('[ "$dax_align" = 4096 ]', init)
        self.assertIn('export BADFS_CXL_MAP_ALIGNMENT="$dax_align"', init)
        self.assertIn('export BADFS_CXL_MAP_ALIGNMENT="$(cat /run/dax-align)"', rank)
        self.assertIn("export INTERCEPT_ALL_OBJS=1", rank)

    def test_guest_shutdown_grace_is_concurrent(self):
        barrier = threading.Barrier(3)

        class FakeProcess:
            def wait(self, timeout):
                self.timeout = timeout
                barrier.wait(timeout=1)
                return 0

        class FakeConsole:
            def __init__(self):
                self.process = FakeProcess()

        consoles = [FakeConsole() for _ in range(3)]
        self.runner.finish_guest_processes(consoles, grace_seconds=0.5)
        self.assertEqual([item.process.timeout for item in consoles], [0.5] * 3)

    def test_nonzero_mpi_exit_is_reported_immediately(self):
        class FakeProcess:
            returncode = None

            @staticmethod
            def poll():
                return None

        class FakeConsole:
            def __init__(self, output):
                self.output = output
                self.condition = threading.Condition()
                self.process = FakeProcess()

        console = FakeConsole(
            "LEGOFS_IO500_MPI_EXIT stage=tiny rc=11\r\n"
        )
        with self.assertRaisesRegex(
            self.runner.MpiStageError, "MPI stage tiny exited with rc=11"
        ):
            self.runner.wait_mpi_exit(console, "tiny", timeout=3600, start=0)

        console.output = "LEGOFS_IO500_MPI_EXIT stage=tiny rc=0\r\n"
        self.assertEqual(
            self.runner.wait_mpi_exit(console, "tiny", timeout=1, start=0), 0
        )

        console.output = (
            "BADFS_STRICT_LIFECYCLE_DIRECT_INIT_FAILED: refusing fallback\r\n"
        )
        with self.assertRaisesRegex(RuntimeError, "fatal marker"):
            self.runner.wait_mpi_exit(console, "tiny", timeout=3600, start=0)

    def test_verifier_exit_is_observed_immediately(self):
        class FakeProcess:
            returncode = None

            @staticmethod
            def poll():
                return None

        class FakeConsole:
            def __init__(self, output):
                self.output = output
                self.condition = threading.Condition()
                self.process = FakeProcess()

        console = FakeConsole("LEGOFS_IO500_VERIFY_EXIT stage=tiny rc=1\r\n")
        self.assertEqual(
            self.runner.wait_verify_exit(console, "tiny", timeout=600, start=0), 1
        )

        console.output = "LEGOFS_IO500_VERIFY_EXIT stage=tiny rc=0\r\n"
        self.assertEqual(
            self.runner.wait_verify_exit(console, "tiny", timeout=1, start=0), 0
        )

    def test_verifier_classification_separates_tiny_invalid_from_clean_runs(self):
        tiny = self.runner.classify_io500_verifier(
            "tiny", 1, "[OK] But this is an invalid run!\r\n"
        )
        self.assertIn("expected INVALID", tiny)
        with self.assertRaisesRegex(RuntimeError, "expected verified INVALID"):
            self.runner.classify_io500_verifier(
                "tiny",
                1,
                "ERROR: Score hash expected: A read: B\r\n",
            )

        hard = self.runner.classify_io500_verifier(
            "hard-smoke", 1, "[OK] But this is an invalid run!\r\n"
        )
        self.assertIn("expected INVALID", hard)
        rnd4k = self.runner.classify_io500_verifier(
            "rnd4k", 1, "[OK] But this is an invalid run!\r\n"
        )
        self.assertIn("expected INVALID", rnd4k)
        for stage in ("easy-smoke", "metadata-smoke"):
            verdict = self.runner.classify_io500_verifier(
                stage, 1, "[OK] But this is an invalid run!\r\n"
            )
            self.assertIn("expected INVALID", verdict)

        scc = self.runner.classify_io500_verifier("scc", 0, "[OK]\r\n")
        self.assertIn("rc=0", scc)
        with self.assertRaisesRegex(RuntimeError, "failed clean verification"):
            self.runner.classify_io500_verifier(
                "standard", 1, "[OK] But this is an invalid run!\r\n"
            )

        failed = self.runner.io500_verifier_record(
            "scc", 1, "[OK] But this is an invalid run!\r\n"
        )
        self.assertEqual(failed["rc"], 1)
        self.assertIn("failed clean verification", failed["failure"])
        self.assertTrue(failed["verdict"].startswith("FAIL:"))

        source = RUNNER.read_text()
        self.assertLess(
            source.index("io500 = extract_results(paths)"),
            source.index('if verifier["failure"] is not None'),
        )
        self.assertLess(
            source.index('result["coherence_final_stats"] = json.loads'),
            source.index('if verifier["failure"] is not None'),
        )

    def test_hard_smoke_is_measurement_only_and_runs_both_shared_file_phases(self):
        config = (ROOT / "configs" / "io500-hard-smoke.ini").read_text()
        self.assertIn("[ior-hard]\n", config)
        self.assertIn("segmentCount = 16\n", config)
        self.assertIn("[ior-hard-write]\nAPI = POSIX\nrun = TRUE\n", config)
        self.assertIn("[ior-hard-read]\nAPI = POSIX\nrun = TRUE\n", config)
        self.assertIn("[ior-easy]\nrun = FALSE\n", config)
        self.assertIn(
            "tiny|easy-smoke|hard-smoke|metadata-smoke|rnd4k|scc|standard",
            RANK_SCRIPT.read_text(),
        )
        self.assertIn(
            "tiny easy-smoke hard-smoke metadata-smoke rnd4k scc standard",
            BUILD_SCRIPT.read_text(),
        )

    def test_fixed_easy_and_metadata_smokes_isolate_official_phase_shapes(self):
        easy = (ROOT / "configs" / "io500-easy-smoke.ini").read_text()
        self.assertIn("transferSize = 1m\n", easy)
        self.assertIn("blockSize = 64m\n", easy)
        self.assertIn("[ior-easy-write]\nAPI = POSIX\nrun = TRUE\n", easy)
        self.assertIn("[ior-easy-read]\nAPI = POSIX\nrun = TRUE\n", easy)
        self.assertIn("[ior-hard]\nrun = FALSE\n", easy)

        metadata = (ROOT / "configs" / "io500-metadata-smoke.ini").read_text()
        self.assertIn("[mdtest-easy]\nAPI = POSIX\nn = 128\nrun = TRUE\n", metadata)
        self.assertIn("[mdtest-hard]\nAPI = POSIX\nn = 128\nrun = TRUE\n", metadata)
        self.assertIn("[find]\nrun = TRUE\n", metadata)
        self.assertIn("[ior-easy]\nrun = FALSE\n", metadata)

    def test_rnd4k_smoke_generates_official_easy_files_then_reads_them(self):
        config = (ROOT / "configs" / "io500-rnd4k.ini").read_text()
        self.assertIn("transferSize = 2m\n", config)
        self.assertIn("blockSize = 9920000m\n", config)
        self.assertIn("[ior-easy-write]\nAPI = POSIX\nrun = TRUE\n", config)
        self.assertIn("[ior-easy-read]\nrun = FALSE\n", config)
        self.assertIn("[ior-rnd4K-easy-read]\nrun = TRUE\n", config)

    def test_posix_summary_gate_allows_idle_exec_children_but_requires_active_ranks(self):
        records = []
        for rank in range(10):
            base = {
                "schema_version": "badfs.posix.path-summary.v2",
                "mpi_rank": rank,
                "endpoint": rank,
                "intercept_enabled": True,
                "syscall_classification": {
                    "totals": {
                        "handled": 1,
                        "rejected": 0,
                        "non_badfs_forward": 0,
                        "forbidden_badfs_forward": 0,
                    },
                    "syscalls": [],
                },
            }
            records.append(dict(base, stats={"open_ops": 0, "read_ops": 0, "write_ops": 0}))
            records.append(dict(base, stats={"open_ops": 1, "read_ops": 1, "write_ops": 1}))
        self.runner.validate_posix_summaries(records)

        records[-1]["stats"] = {"open_ops": 0, "read_ops": 0, "write_ops": 0}
        with self.assertRaisesRegex(ValueError, "active POSIX summaries"):
            self.runner.validate_posix_summaries(records)

    def test_posix_summary_gate_rejects_rank_endpoint_mismatch(self):
        records = [
            {
                "schema_version": "badfs.posix.path-summary.v2",
                "mpi_rank": rank,
                "endpoint": rank,
                "intercept_enabled": True,
                "stats": {"open_ops": 1},
                "syscall_classification": {
                    "totals": {
                        "handled": 1,
                        "rejected": 0,
                        "non_badfs_forward": 0,
                        "forbidden_badfs_forward": 0,
                    },
                    "syscalls": [],
                },
            }
            for rank in range(10)
        ]
        records[4]["endpoint"] = 5
        with self.assertRaisesRegex(ValueError, "rank/endpoint mismatch"):
            self.runner.validate_posix_summaries(records)

    def test_fixed_stage_roots_refuse_overwrite(self):
        self.paths.run.mkdir(parents=True)
        with self.assertRaisesRegex(FileExistsError, "already exists"):
            self.runner.prepare_paths(self.paths)

    def test_strict_bi_proof_correlates_owner_range_and_host_order(self):
        self.paths.bundle.mkdir(parents=True)
        direct = {
            "schema_version": "badfs.direct-map-trace.v1",
            "event": "unmap",
            "access": "write",
            "rc": 0,
            "owner": 77,
            "op_id": 9,
            "offset": 4096,
            "length": 4096,
        }
        lifecycle_base = {
            "schema_version": "badfs.lifecycle.v1",
            "owner": 77,
            "op_id": 9,
            "mapping_offset": 4096,
            "mapping_length": 4096,
            "fault_point": "dirty_range_ownership",
        }
        self.paths.event_log("client0").write_text(
            json.dumps({
                "host_capture_ns": 100,
                "line": "BADFS_DIRECT_MAP_TRACE_JSON "
                + json.dumps(direct, separators=(",", ":")),
            }) + "\n",
            encoding="utf-8",
        )
        server_lines = []
        for capture, event in ((90, "store_direct_begin"), (500, "store_direct_success")):
            record = dict(lifecycle_base, event=event)
            server_lines.append(
                json.dumps({
                    "host_capture_ns": capture,
                    "line": "BADFS_LIFECYCLE_TRACE_JSON "
                    + json.dumps(record, separators=(",", ":")),
                })
            )
        self.paths.event_log("server0").write_text("\n".join(server_lines) + "\n", encoding="utf-8")
        coherence = [
            {"schema_version": 1, "event": "snoop_send", "opcode": "SNP_DATA_INV",
             "dst_host": 4, "line_address": 4096, "snoop_id": 5, "session_id": 8,
             "epoch": 2, "monotonic_ns": 200},
            {"schema_version": 1, "event": "snoop_ack", "opcode": "SNOOP_ACK",
             "src_host": 4, "ack_strength": "MODEL", "dirty_data": True,
             "payload_len": 64, "status": "OK", "snoop_id": 5, "session_id": 8,
             "epoch": 2, "monotonic_ns": 300},
            {"schema_version": 1, "event": "dirty_completion", "opcode": "SNOOP_ACK",
             "src_host": 4, "ack_strength": "MODEL", "dirty_data": True,
             "payload_len": 64, "status": "OK", "snoop_id": 5, "session_id": 8,
             "epoch": 2, "monotonic_ns": 400},
        ]
        self.paths.coherence.write_text(
            "".join(json.dumps(record) + "\n" for record in coherence),
            encoding="utf-8",
        )
        proof = self.runner.strict_persistency_proof(
            self.paths, [{"owner": 77, "endpoint": 3}], 1
        )
        self.assertEqual(proof["count"], 1)
        self.assertEqual(proof["backinvalidation"]["first"]["cxl_host_id"], 4)

    def test_strict_persistency_proof_accepts_writer_persisted_fast_path(self):
        self.paths.bundle.mkdir(parents=True)
        direct = {
            "schema_version": "badfs.direct-map-trace.v1",
            "event": "persisted",
            "access": "write",
            "rc": 0,
            "owner": 77,
            "op_id": 9,
            "offset": 4096,
            "length": 4096,
        }
        self.paths.event_log("client0").write_text(
            json.dumps({
                "host_capture_ns": 100,
                "line": "BADFS_DIRECT_MAP_TRACE_JSON "
                + json.dumps(direct, separators=(",", ":")),
            }) + "\n",
            encoding="utf-8",
        )
        server_lines = []
        for capture, event in ((200, "store_direct_begin"), (300, "store_direct_success")):
            record = {
                "schema_version": "badfs.lifecycle.v1",
                "event": event,
                "owner": 77,
                "op_id": 9,
                "mapping_offset": 4096,
                "mapping_length": 4096,
                "fault_point": "writer_persisted",
            }
            server_lines.append(json.dumps({
                "host_capture_ns": capture,
                "line": "BADFS_LIFECYCLE_TRACE_JSON "
                + json.dumps(record, separators=(",", ":")),
            }))
        self.paths.event_log("server0").write_text(
            "\n".join(server_lines) + "\n", encoding="utf-8"
        )
        self.paths.coherence.write_text("", encoding="utf-8")
        proof = self.runner.strict_persistency_proof(
            self.paths, [{"owner": 77, "endpoint": 3}], 1
        )
        self.assertEqual(proof["count"], 1)
        self.assertEqual(proof["writer_persisted"]["count"], 1)
        self.assertEqual(proof["backinvalidation"]["count"], 0)

    def test_tiny_provider_counters_follow_the_selected_persistence_path(self):
        writer = {
            "writer_persisted": {"count": 1},
            "backinvalidation": {"count": 0},
        }
        self.runner.validate_tiny_provider_counters(
            {
                "putm": 1,
                "request_fence": 1,
                "persistence_fence_completions": 1,
            },
            writer,
        )
        self.runner.validate_tiny_provider_counters(
            {
                "putm": 0,
                "shared_write_grants": 1,
                "request_fence": 1,
                "persistence_fence_completions": 1,
            },
            writer,
        )
        with self.assertRaisesRegex(ValueError, "persistence_fence_completions"):
            self.runner.validate_tiny_provider_counters(
                {"putm": 1, "request_fence": 1}, writer
            )

        backinvalidation = {
            "writer_persisted": {"count": 0},
            "backinvalidation": {"count": 1},
        }
        self.runner.validate_tiny_provider_counters(
            {"snp_data_inv": 1, "model_acks": 1, "dirty_data_completions": 1},
            backinvalidation,
        )
        with self.assertRaisesRegex(ValueError, "dirty_data_completions"):
            self.runner.validate_tiny_provider_counters(
                {"snp_data_inv": 1, "model_acks": 1}, backinvalidation
            )


if __name__ == "__main__":
    unittest.main()
