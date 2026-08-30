import importlib.util
import json
import pathlib
import tempfile
import threading
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
TOP_LEVEL_RUNNER = ROOT / "run-legofs-io500.sh"
RUNNER = ROOT / "scripts" / "legofs_io500.py"
BUILD_SCRIPT = ROOT / "scripts" / "build_legofs_io500.sh"
PAYLOAD_REBUILD_SCRIPT = ROOT / "scripts" / "rebuild_legofs_io500_payload.sh"
BOOTSTRAP_REBUILD_SCRIPT = ROOT / "scripts" / "rebuild_legofs_io500_bootstrap.sh"
NOV_GCC = ROOT / "scripts" / "riscv64-nov-gcc"
RANK_SCRIPT = ROOT / "guest" / "legofs_io500_rank.sh"
BENCH_WRAPPER = ROOT / "guest" / "legofs_badfs_bench.sh"
INIT_SCRIPT = ROOT / "guest" / "legofs_io500_init.sh"
BOOTSTRAP_INIT_SCRIPT = ROOT / "guest" / "legofs_io500_bootstrap_init.sh"


def load_runner():
    spec = importlib.util.spec_from_file_location("legofs_io500", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cxl_serving_evidence(endpoint, submitted=1):
    return [{
        "authority_id": 0,
        "lane_id": endpoint,
        "lane_role": "client_fs",
        "format_generation": 1,
        "session_generation": 2,
        "lane_generation": 3,
        "bootstrap_tcp_connections": 1,
        "bootstrap_tcp_exchanges": 1,
        "bootstrap_tcp_bytes": 128,
        "sqe_submitted": submitted,
        "cqe_consumed": submitted,
        "shared_sequences": {
            "sq_produced": submitted,
            "sq_consumed": submitted,
            "cq_produced": submitted,
            "cq_consumed": submitted,
        },
        "authority_timing": {
            "sqe_consumed": submitted,
            "cqe_published": submitted,
            "authority_queue_wait_ns": submitted,
            "dispatcher_backend_ns": submitted,
            "cqe_publish_ns": submitted,
        },
        "client_timing": {
            "calls": submitted,
            "call_gate_wait_ns": submitted,
            "syscall_prepare_ns": submitted,
            "sq_credit_wait_ns": 0,
            "sq_publish_ns": submitted,
            "cq_wait_ns": submitted,
        },
        "dispatches_by_opcode": {"35": 1} if submitted == 1 else {
            "1": submitted - 1,
            "35": 1,
        },
        "unsupported_serving_calls_after_cxl_ready": 0,
        "filesystem_tcp_requests_after_cxl_ready": 0,
        "legacy_tarpc_calls_after_cxl_ready": 0,
        "blob_tcp_bytes_after_cxl_ready": 0,
        "transport_fallbacks_after_cxl_ready": 0,
    }]


def recovery_checkpoint(server=0):
    return {
        "schema_version": "badfs.recovery-control.inspection.v1",
        "server": server,
        "retirement": {
            "identity": {
                "authority": server,
                "serving_incarnation": (1 << 32) | (server + 1),
            },
            "retired_lanes": 4,
            "active_client_lanes": 0,
        },
        "checkpoint": {
            "identity": {
                "authority": server,
                "serving_incarnation": (1 << 32) | server + 1,
            },
            "data_domain": "lifecycle",
            "metadata_lsn": 7,
            "data_lsn": 0,
            "lifecycle_lsn": 7,
        },
        "transport": {
            "authority_id": server,
            "lane_id": 60 + server,
            "lane_role": "recovery_control",
            "format_generation": 1,
            "session_generation": 2,
            "lane_generation": 3,
            "bootstrap_tcp_connections": 1,
            "bootstrap_tcp_exchanges": 1,
            "bootstrap_tcp_bytes": 128,
            "sqe_submitted": 2,
            "cqe_consumed": 2,
            "shared_sequences": {
                "sq_produced": 2,
                "sq_consumed": 2,
                "cq_produced": 2,
                "cq_consumed": 2,
            },
            "authority_timing": {
                "sqe_consumed": 2,
                "cqe_published": 2,
                "authority_queue_wait_ns": 1,
                "dispatcher_backend_ns": 1,
                "cqe_publish_ns": 0,
            },
            "client_timing": {
                "calls": 2,
                "call_gate_wait_ns": 1,
                "syscall_prepare_ns": 1,
                "sq_credit_wait_ns": 0,
                "sq_publish_ns": 1,
                "cq_wait_ns": 1,
            },
            "dispatches_by_opcode": {"1": 1, "2": 1},
            "unsupported_serving_calls_after_cxl_ready": 0,
            "filesystem_tcp_requests_after_cxl_ready": 0,
            "legacy_tarpc_calls_after_cxl_ready": 0,
            "blob_tcp_bytes_after_cxl_ready": 0,
            "transport_fallbacks_after_cxl_ready": 0,
        },
    }


class Io500RuntimeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runner = load_runner()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = self.runner.Paths(pathlib.Path(self.temporary.name), "tiny")

    def tearDown(self):
        self.temporary.cleanup()

    def test_functional_model_never_claims_physical_hardware_evidence(self):
        evidence = self.runner.functional_model_evidence()
        self.assertTrue(evidence["functional_model_only"])
        self.assertFalse(evidence["guest_visible_cxl_evidence"])
        self.assertFalse(evidence["physical_hardware_evidence"])
        self.assertFalse(evidence["physical_cxl_evidence"])

    def test_multi_authority_fabric_requires_completed_durable_transaction(self):
        fabric = {
            "authority_internal_submitted": 8,
            "authority_internal_completed": 8,
            "authority_prepare_receipts": 4,
            "authority_committed_transactions": 4,
            "authority_marker_publications": 8,
            "authority_durable_prefix": 8,
            "authority_journal_record_persists": 8,
            "authority_journal_anchor_persists": 10,
        }
        self.runner.validate_authority_internal_fabric(fabric, 2)
        self.runner.validate_authority_internal_fabric({}, 1)

        for key, value in (
            ("authority_internal_completed", 7),
            ("authority_prepare_receipts", 0),
            ("authority_committed_transactions", 0),
            ("authority_marker_publications", 7),
            ("authority_durable_prefix", 7),
            ("authority_journal_record_persists", 7),
            ("authority_journal_anchor_persists", 8),
        ):
            broken = dict(fabric)
            broken[key] = value
            with self.assertRaises(ValueError, msg=key):
                self.runner.validate_authority_internal_fabric(broken, 2)

    def test_recovery_control_checkpoint_requires_exact_cxl_lane_evidence(self):
        records = [recovery_checkpoint(0), recovery_checkpoint(1)]
        self.runner.validate_recovery_control_checkpoints(records, 2)

        broken = json.loads(json.dumps(records))
        broken[1]["transport"]["filesystem_tcp_requests_after_cxl_ready"] = 1
        with self.assertRaisesRegex(ValueError, "filesystem_tcp_requests"):
            self.runner.validate_recovery_control_checkpoints(broken, 2)

        broken = json.loads(json.dumps(records))
        broken[0]["checkpoint"]["lifecycle_lsn"] = 6
        with self.assertRaisesRegex(ValueError, "durable cursor"):
            self.runner.validate_recovery_control_checkpoints(broken, 2)

        broken = json.loads(json.dumps(records))
        broken[0]["transport"]["lane_id"] = 59
        with self.assertRaisesRegex(ValueError, "lane identity"):
            self.runner.validate_recovery_control_checkpoints(broken, 2)

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
        self.assertEqual(args.serving_transport, "legacy")
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

        cxl = self.runner.parse_args(
            ["--stage", "tiny", "--serving-transport", "cxl"]
        )
        self.assertEqual(cxl.serving_transport, "cxl")

        init = INIT_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("io500.client_count=", RUNNER.read_text(encoding="utf-8"))
        self.assertIn('-n "$client_count"', init)

    def test_clean_restart_fault_is_cxl_only_and_guest_commands_are_explicit(self):
        args = self.runner.parse_args([
            "--stage", "tiny",
            "--server-count", "1",
            "--client-count", "2",
            "--serving-transport", "cxl",
            "--fault-profile", "clean-server-restart",
        ])
        self.assertEqual(args.fault_profile, "clean-server-restart")
        init = INIT_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("LEGOFS_SERVER_CLEAN_RESTART", init)
        self.assertIn("BADFS_START_MODE=clean_restart", init)
        self.assertIn("LEGOFS_CLEAN_RESTART_CONTROL", init)
        self.assertIn("LEGOFS_CLIENT_GENERATION", init)
        self.assertIn("LEGOFS_CLEAN_RESTART_COHORT", init)
        self.assertNotIn("server-ready-timeout", init)
        self.assertIn("host runner owns the bounded readiness deadline", init)

    def test_unauthorized_clean_successor_probe_is_an_explicit_tiny_cxl_gate(self):
        args = self.runner.parse_args([
            "--stage", "tiny",
            "--server-count", "1",
            "--client-count", "2",
            "--serving-transport", "cxl",
            "--fault-profile", "reject-unauthorized-clean-restart",
        ])
        self.assertEqual(args.fault_profile, "reject-unauthorized-clean-restart")
        init = INIT_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("LEGOFS_SERVER_UNAUTHORIZED_RESTART_PROBE", init)
        self.assertIn("LEGOFS_IO500_UNAUTHORIZED_RESTART_REJECTED", init)
        self.assertIn("listener_open=0", init)

    def test_active_lane_retirement_probe_is_an_explicit_tiny_cxl_gate(self):
        args = self.runner.parse_args([
            "--stage", "tiny",
            "--server-count", "1",
            "--client-count", "2",
            "--serving-transport", "cxl",
            "--fault-profile", "reject-active-clean-retirement",
        ])
        self.assertEqual(args.fault_profile, "reject-active-clean-retirement")
        bench = (ROOT / "components/legofs/badfs-bench/src/main.rs").read_text(
            encoding="utf-8"
        )
        self.assertIn('"hold" =>', bench)
        self.assertIn("badfs_clean_restart_hold_ready", bench)

        record = {
            "retirement": {"active_client_lanes": 1, "retired_lanes": 3},
            "transport": {
                "filesystem_tcp_requests_after_cxl_ready": 0,
                "legacy_tarpc_calls_after_cxl_ready": 0,
                "blob_tcp_bytes_after_cxl_ready": 0,
                "transport_fallbacks_after_cxl_ready": 0,
            },
        }

        class FakeConsole:
            def __init__(self, index):
                self.index = index
                self.output = ""

            def send(self, command):
                if command == "LEGOFS_CLEAN_RESTART_COHORT hold 42":
                    self.output += "badfs_clean_restart_hold_ready hold_ms=30000\n"
                    self.output += (
                        "LEGOFS_IO500_CLEAN_RESTART_COHORT_EXIT index=0 "
                        "action=hold endpoint=42 generation=1 rc=0\n"
                    )
                elif command == "LEGOFS_INSPECT_ENDPOINT 41":
                    self.output += (
                        "badfs_recovery_control_checkpoint "
                        + json.dumps(record)
                        + "\nLEGOFS_IO500_INSPECT_ENDPOINT_EXIT "
                        "index=1 endpoint=41 rc=0\n"
                    )

            def wait(self, marker, timeout, start=0):
                if marker not in self.output[start:]:
                    raise AssertionError(f"missing marker {marker!r}")

        original_parser = self.runner.parse_server_inspection
        self.runner.parse_server_inspection = lambda *args, **kwargs: {
            "recovery_control": record
        }
        try:
            evidence = self.runner.run_active_lane_retirement_probe(
                [FakeConsole(0), FakeConsole(1)], 1
            )
        finally:
            self.runner.parse_server_inspection = original_parser
        self.assertEqual(
            evidence["lifecycle_inspection"]["recovery_control"]
            ["retirement"]["active_client_lanes"],
            1,
        )

    def test_cxlmemsim_tcp_port_is_reserved_with_listener_address_scope(self):
        runner = RUNNER.read_text(encoding="utf-8")
        self.assertIn('tcp.bind(("0.0.0.0", 0))', runner)

    def test_clean_restart_fault_orders_cohorts_and_requires_exact_cxl_opcodes(self):
        control = {
            "transport": {
                "filesystem_tcp_requests_after_cxl_ready": 0,
                "legacy_tarpc_calls_after_cxl_ready": 0,
                "blob_tcp_bytes_after_cxl_ready": 0,
                "transport_fallbacks_after_cxl_ready": 0,
                "dispatches_by_opcode": {"2": 1, "3": 1, "4": 1},
            }
        }

        class FakeConsole:
            def __init__(self, index):
                self.index = index
                self.output = ""
                self.commands = []

            def send(self, command):
                self.commands.append(command)
                if command.startswith("LEGOFS_CLEAN_RESTART_COHORT"):
                    _, action, endpoint = command.split()
                    generation = 1 if action in ("produce", "observe") else 2
                    self.output += (
                        "LEGOFS_IO500_CLEAN_RESTART_COHORT_EXIT "
                        f"index={self.index} action={action} endpoint={endpoint} "
                        f"generation={generation} rc=0\n"
                    )
                elif command.startswith("LEGOFS_CLEAN_RESTART_CONTROL"):
                    self.output += "badfs_clean_restart_control " + json.dumps(control) + "\n"
                    self.output += (
                        "LEGOFS_IO500_CLEAN_RESTART_CONTROL_EXIT "
                        "index=0 target_generation=2 rc=0\n"
                    )
                elif command == "LEGOFS_SERVER_CLEAN_RESTART 2":
                    self.output += "LEGOFS_IO500_SERVER_RESTARTED index=0 generation=2\n"
                elif command == "LEGOFS_CLIENT_GENERATION 2":
                    self.output += (
                        "LEGOFS_IO500_CLIENT_GENERATION_SET "
                        f"index={self.index} generation=2\n"
                    )

            def wait(self, marker, timeout, start=0):
                if marker not in self.output[start:]:
                    raise AssertionError(f"missing marker {marker!r}")

        server = FakeConsole(0)
        clients = [FakeConsole(0), FakeConsole(1)]
        evidence = self.runner.run_clean_restart_fault(server, clients, 1)
        self.assertTrue(evidence["functional_model_only"])
        self.assertFalse(evidence["physical_hardware_evidence"])
        self.assertEqual(evidence["cohort_a"], ["produce", "observe"])
        self.assertEqual(evidence["cohort_b"], ["recover", "delete"])
        self.assertEqual(server.commands, ["LEGOFS_SERVER_CLEAN_RESTART 2"])
        self.assertEqual(
            clients[0].commands,
            [
                "LEGOFS_CLEAN_RESTART_COHORT produce 42",
                "LEGOFS_CLEAN_RESTART_CONTROL 2 7640891576956012809",
                "LEGOFS_CLIENT_GENERATION 2",
                "LEGOFS_CLEAN_RESTART_COHORT recover 42",
            ],
        )

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
            self.assertIn("cxl-fmw.0.restrictions=0x29", text)
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
        self.assertIn("--coherence-v2-trace=", text)

        counter_text = " ".join(self.runner.server_command(self.paths, 19000, False))
        self.assertNotIn("--coherence-v2-counters", counter_text)
        self.assertNotIn("--coherence-v2-trace=", counter_text)

        full_text = " ".join(
            self.runner.server_command(self.paths, 19000, False, full_trace=True)
        )
        self.assertIn("--coherence-v2-trace=", full_text)
        self.assertNotIn("--coherence-v2-counters", full_text)
        self.assertNotIn("/dev/null", counter_text)

    def test_guests_derive_the_shared_region_identity_from_dax(self):
        init = INIT_SCRIPT.read_text(encoding="utf-8")
        rank = RANK_SCRIPT.read_text(encoding="utf-8")
        self.assertIn('BADFS_LIFECYCLE_DEVICE="$dax_path"', init)
        self.assertIn('BADFS_LIFECYCLE_DEVICES="$lifecycle_devices"', init)
        self.assertIn('BADFS_LIFECYCLE_DEVICES="$(cat /run/lifecycle-devices)"', rank)
        self.assertIn("BADFS_LIFECYCLE_POOL_OFFSET", init)
        self.assertIn("BADFS_LIFECYCLE_REGION_SIZE=68719476736", init)
        self.assertIn('BADFS_SERVING_TRANSPORT="$serving_transport"', init)
        self.assertIn('BADFS_SERVER_INDEX="$index"', init)
        self.assertIn('export BADFS_SERVING_TRANSPORT="$serving_transport"', rank)
        self.assertIn("BADFS_LIFECYCLE_REGION_SIZE=68719476736", rank)
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

    def test_payload_only_mode_is_explicit_and_mutually_exclusive(self):
        runner = TOP_LEVEL_RUNNER.read_text(encoding="utf-8")
        self.assertIn("PAYLOAD_ONLY=0", runner)
        self.assertIn("--payload-only", runner)
        self.assertIn(
            "((BUILD_ONLY + RUN_ONLY + PAYLOAD_ONLY <= 1))",
            runner,
        )
        self.assertIn(
            '"$ROOT/scripts/rebuild_legofs_io500_payload.sh" --jobs "$JOBS"',
            runner,
        )

    def test_payload_only_rebuild_cannot_rebuild_platform(self):
        rebuild = PAYLOAD_REBUILD_SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("build_legofs_type3.sh", rebuild)
        self.assertNotIn("components/qemu", rebuild)
        self.assertNotIn("components/linux", rebuild)
        self.assertNotIn("components/cxlmemsim", rebuild)
        for artifact in (
            "$PLATFORM/qemu-system-riscv64",
            "$PLATFORM/cxlmemsim_server",
            "$PLATFORM/fw_dynamic.bin",
            "$PLATFORM/u-boot.bin",
            "$PLATFORM/linux-io500-Image",
        ):
            self.assertIn(f'require_file "{artifact}"', rebuild)
        self.assertIn("platform_hashes_before=", rebuild)
        self.assertIn("platform artifacts changed during payload-only rebuild", rebuild)

    def test_mutable_guest_control_loop_lives_in_payload(self):
        bootstrap = BOOTSTRAP_INIT_SCRIPT.read_text(encoding="utf-8")
        full_build = BUILD_SCRIPT.read_text(encoding="utf-8")
        payload_build = PAYLOAD_REBUILD_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("/payload/bin/legofs-io500-init", bootstrap)
        self.assertIn("legofs_io500_bootstrap_init.sh", full_build)
        self.assertIn(
            '"$PAYLOAD_TREE/bin/legofs-io500-init"', payload_build
        )
        self.assertNotIn("LEGOFS_SERVER_CLEAN_RESTART", bootstrap)
        bootstrap_rebuild = BOOTSTRAP_REBUILD_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("legofs_io500_bootstrap_init.sh", bootstrap_rebuild)
        self.assertIn("non-Linux platform artifacts changed", bootstrap_rebuild)
        self.assertNotIn("cxlmemsim_server --", bootstrap_rebuild)

    def test_payload_only_rebuild_reuses_dependencies_and_builds_legofs(self):
        rebuild = PAYLOAD_REBUILD_SCRIPT.read_text(encoding="utf-8")
        self.assertIn('RUST_SYSROOT="$(rustc --print sysroot)"', rebuild)
        self.assertIn('$RUST_SYSROOT/lib/rustlib/$RUST_TARGET/lib', rebuild)
        self.assertNotIn("rustup target list --installed", rebuild)
        for reused in (
            "$SOURCES/io500/io500",
            "$SOURCES/io500/io500-verify",
            "$MPICH_PREFIX/bin/mpiexec.hydra",
            "$MPICH_PREFIX/bin/hydra_pmi_proxy",
            "$SYSINT_BUILD_ROOT/build/libsyscall_intercept.so.0",
            "$LIBUNWIND_PREFIX/lib/libunwind.so.1",
        ):
            self.assertIn(f'require_file "{reused}"', rebuild)
        self.assertIn("-p badfs-server -p badfs-bench", rebuild)
        self.assertIn("-p badfs-intercept", rebuild)
        self.assertIn("--features syscall-intercept-backend", rebuild)
        self.assertIn('"${CROSS_COMPILE}strip" --strip-debug', rebuild)
        self.assertIn("require_sifive_u_isa", rebuild)
        self.assertIn("require_syscall_intercept_abi", rebuild)
        self.assertIn("reject_glibc_versions", rebuild)

    def test_payload_only_image_and_manifest_are_atomic_and_hashed(self):
        rebuild = PAYLOAD_REBUILD_SCRIPT.read_text(encoding="utf-8")
        self.assertIn('mktemp -d "$TARGET_ROOT/.payload-build.XXXXXX"', rebuild)
        self.assertIn('mktemp "$IMAGES/.io500-payload.ext2.XXXXXX"', rebuild)
        self.assertIn(
            'mv -f -- "$PAYLOAD_IMAGE_TMP" "$PAYLOAD_IMAGE"',
            rebuild,
        )
        self.assertNotIn('rm -rf -- "$PAYLOAD_ROOT"', rebuild)
        self.assertIn('--source "legofs=$LEGOFS_ROOT"', rebuild)
        self.assertNotIn("--no-artifact-hashes", rebuild)
        for artifact in (
            '"qemu=$PLATFORM/qemu-system-riscv64"',
            '"cxlmemsim_server=$PLATFORM/cxlmemsim_server"',
            '"linux=$PLATFORM/linux-io500-Image"',
            '"payload=$PAYLOAD_IMAGE"',
            '"badfs_server=$PAYLOAD_ROOT/bin/badfs-server.real"',
            '"badfs_bench=$PAYLOAD_ROOT/bin/badfs-bench.real"',
            '"badfs_intercept=$PAYLOAD_ROOT/lib/libbadfs_intercept.so"',
        ):
            self.assertIn(f'--artifact {artifact}', rebuild)

    def test_cxl_inspection_uses_a_fresh_shared_region_lane(self):
        wrapper = BENCH_WRAPPER.read_text(encoding="utf-8")
        rebuild = PAYLOAD_REBUILD_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("io500.serving_transport=", wrapper)
        self.assertIn("io500.client_count=", wrapper)
        self.assertIn('BADFS_CLIENT_ENDPOINT_ID="$client_count"', wrapper)
        self.assertIn("BADFS_SERVING_TRANSPORT=cxl", wrapper)
        self.assertIn("/payload/bin/badfs-bench.real", wrapper)
        self.assertIn('badfs-bench.real"', rebuild)
        self.assertIn('legofs_badfs_bench.sh"', rebuild)

    def test_cxl_result_export_uses_a_disjoint_post_run_lane(self):
        rank = RANK_SCRIPT.read_text(encoding="utf-8")
        self.assertIn('exporter_endpoint="$((2 * client_count + 1))"', rank)
        self.assertIn('BADFS_CLIENT_ENDPOINT_ID="$exporter_endpoint"', rank)
        self.assertIn('BADFS_POSIX_TRACE_DIR=/tmp/posix-export', rank)
        self.assertIn('PMI_RANK= PMIX_RANK= OMPI_COMM_WORLD_RANK=', rank)
        self.assertIn("LEGOFS_IO500_EXPORT_POSIX_SUMMARY", rank)

    def test_post_run_exporter_summary_accepts_supported_client_counts(self):
        for client_count in (2, 4, 6, 10):
            endpoint = 2 * client_count + 1
            record = {
                "schema_version": "badfs.posix.path-summary.v4",
                "mpi_rank": None,
                "endpoint": endpoint,
                "intercept_enabled": True,
                "stats": {"open_ops": 1, "read_ops": 1, "write_ops": 0},
                "syscall_classification": {
                    "totals": {
                        "handled": 2,
                        "rejected": 0,
                        "non_badfs_forward": 1,
                        "forbidden_badfs_forward": 0,
                    },
                    "syscalls": [],
                },
                "cxl_serving_evidence": cxl_serving_evidence(endpoint, 2),
            }
            parsed = self.runner.parse_post_run_exporter_summary(
                "LEGOFS_IO500_EXPORT_POSIX_SUMMARY "
                f"endpoint={endpoint} file=posix-export.json "
                + json.dumps(record, separators=(",", ":"))
                + "\n",
                client_count,
            )
            self.assertEqual(parsed["endpoint"], endpoint)
            self.assertIsNone(parsed["mpi_rank"])

    def test_post_run_exporter_summary_rejects_missing_duplicate_and_wrong_identity(self):
        client_count = 10
        endpoint = 2 * client_count + 1
        record = {
            "schema_version": "badfs.posix.path-summary.v4",
            "mpi_rank": None,
            "endpoint": endpoint,
            "intercept_enabled": True,
            "stats": {"open_ops": 1, "read_ops": 1, "write_ops": 0},
            "syscall_classification": {
                "totals": {
                    "handled": 2,
                    "rejected": 0,
                    "non_badfs_forward": 1,
                    "forbidden_badfs_forward": 0,
                },
                "syscalls": [],
            },
            "cxl_serving_evidence": cxl_serving_evidence(endpoint, 2),
        }
        line = (
            "LEGOFS_IO500_EXPORT_POSIX_SUMMARY "
            f"endpoint={endpoint} file=posix-export.json "
            + json.dumps(record, separators=(",", ":"))
            + "\n"
        )
        with self.assertRaisesRegex(ValueError, "exactly one exporter"):
            self.runner.parse_post_run_exporter_summary("", client_count)
        with self.assertRaisesRegex(ValueError, "exactly one exporter"):
            self.runner.parse_post_run_exporter_summary(line + line, client_count)

        ranked = json.loads(json.dumps(record))
        ranked["mpi_rank"] = 0
        with self.assertRaisesRegex(ValueError, "MPI rank identity"):
            self.runner.validate_post_run_exporter_summary(ranked, client_count)

        stale = json.loads(json.dumps(record))
        stale["endpoint"] -= 1
        with self.assertRaisesRegex(ValueError, "endpoint mismatch"):
            self.runner.validate_post_run_exporter_summary(stale, client_count)

        fallback = json.loads(json.dumps(record))
        fallback["cxl_serving_evidence"][0][
            "transport_fallbacks_after_cxl_ready"
        ] = 1
        with self.assertRaisesRegex(ValueError, "nonzero forbidden"):
            self.runner.validate_post_run_exporter_summary(fallback, client_count)

    def test_manifest_checks_paths_sizes_and_optional_hashes_without_head_gate(self):
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

        manifest = json.loads(self.paths.manifest.read_text(encoding="utf-8"))
        manifest["artifacts"]["cxlmemsim_server"]["sha256"] = "0" * 64
        self.paths.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "artifact hash mismatch"):
            self.runner.verify_manifest(self.paths)

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

    def test_live_coherence_snapshot_ignores_only_unterminated_tail(self):
        trace = pathlib.Path(self.temporary.name) / "coherence.jsonl"
        complete = {"schema_version": 1, "event": "registration"}
        trace.write_bytes(
            (json.dumps(complete) + "\n").encode("utf-8")
            + b'{"schema_version":1,"event":"request"'
        )
        self.assertEqual(self.runner.parse_trace(trace), [complete])

        trace.write_bytes(b'{"schema_version":1,"event":}\n')
        with self.assertRaisesRegex(ValueError, "invalid coherence JSONL"):
            self.runner.parse_trace(trace)

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
        self.assertIn('[ "$dax_align" -ge 4096 ]', init)
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
        serial_scc = self.runner.classify_io500_verifier(
            "scc", 0, "Verbosity: 1\r\r\n[OK]\r\r\n"
        )
        self.assertIn("rc=0", serial_scc)
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

    def test_result_extraction_rejects_empty_find_even_for_tiny(self):
        source = RUNNER.read_text()
        self.assertIn('raise ValueError("IO500 find phase did not match any file")', source)
        self.assertIn('if "[find]" in text', source)

    def test_tiny_hard_mdtest_completes_an_io500_find_candidate(self):
        config = (ROOT / "configs" / "io500-tiny.ini").read_text()
        hard = config.split("[mdtest-hard]\n", 1)[1].split(
            "[mdtest-hard-write]\n", 1
        )[0]
        # pfind's official pattern is `*01*`; mdtest's first deterministic
        # matching basename is file.mdtest.<rank>.101.
        self.assertIn("n = 102\n", hard)
        self.assertIn("files-per-dir = 102\n", hard)
        self.assertIn("[find]\nrun = TRUE\n", config)

    def test_hard_smoke_is_measurement_only_and_runs_both_shared_file_phases(self):
        config = (ROOT / "configs" / "io500-hard-smoke.ini").read_text()
        self.assertIn("[ior-hard]\n", config)
        self.assertIn("stonewall-time = 300\n", config)
        self.assertIn("segmentCount = 10000000\n", config)
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
                "schema_version": "badfs.posix.path-summary.v4",
                "mpi_rank": rank,
                "endpoint": rank,
                "intercept_enabled": True,
                "cxl_serving_evidence": cxl_serving_evidence(rank),
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
                "schema_version": "badfs.posix.path-summary.v4",
                "mpi_rank": rank,
                "endpoint": rank,
                "intercept_enabled": True,
                "cxl_serving_evidence": cxl_serving_evidence(rank),
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

    def test_posix_summary_gate_rejects_unbalanced_cxl_lane_and_fallback(self):
        records = []
        for rank in range(10):
            records.append({
                "schema_version": "badfs.posix.path-summary.v4",
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
                "cxl_serving_evidence": cxl_serving_evidence(rank, 2),
            })

        records[3]["cxl_serving_evidence"][0]["shared_sequences"]["sq_consumed"] = 1
        with self.assertRaisesRegex(ValueError, "shared CXL SQ/CQ"):
            self.runner.validate_posix_summaries(records)
        records[3]["cxl_serving_evidence"] = cxl_serving_evidence(3, 2)
        records[3]["cxl_serving_evidence"][0]["transport_fallbacks_after_cxl_ready"] = 1
        with self.assertRaisesRegex(ValueError, "nonzero forbidden"):
            self.runner.validate_posix_summaries(records)

        records[3]["cxl_serving_evidence"] = cxl_serving_evidence(3, 2)
        del records[3]["cxl_serving_evidence"][0]["authority_timing"]
        with self.assertRaisesRegex(ValueError, "authority timing evidence"):
            self.runner.validate_posix_summaries(records)

        records[3]["cxl_serving_evidence"] = cxl_serving_evidence(3, 2)
        del records[3]["cxl_serving_evidence"][0]["bootstrap_tcp_bytes"]
        with self.assertRaisesRegex(ValueError, "bootstrap byte evidence"):
            self.runner.validate_posix_summaries(records)

        records[3]["cxl_serving_evidence"] = cxl_serving_evidence(3, 2)
        records[3]["cxl_serving_evidence"][0]["lane_role"] = "authority_internal"
        with self.assertRaisesRegex(ValueError, "not CLIENT_FS"):
            self.runner.validate_posix_summaries(records)

    def test_fixed_stage_roots_refuse_overwrite(self):
        self.paths.run.mkdir(parents=True)
        with self.assertRaisesRegex(FileExistsError, "already exists"):
            self.runner.prepare_paths(self.paths)

    def test_io500_result_parser_preserves_phase_and_aggregate_scores(self):
        metrics = self.runner.parse_io500_metrics(
            """
[ior-easy-write]
score = 1.250000
t_delta = 300.5000
[mdtest-hard-stat]
score = 42.000000
t_delta = 2.5000
[SCORE]
MD = 3.000000
BW = 2.000000
SCORE = 2.449490
hash = ABCD1234
[SCOREX]
MD = 4.000000
BW = 1.000000
SCORE = 2.000000
hash = DCBA4321
"""
        )
        self.assertEqual(metrics["official"]["score"], 2.44949)
        self.assertEqual(metrics["extended"]["hash"], "DCBA4321")
        self.assertEqual(metrics["phases"][0]["unit"], "GiB/s")
        self.assertEqual(metrics["phases"][1]["unit"], "kIOPS")

    def test_host_process_cost_is_explicitly_inclusive(self):
        before = {
            "client0_qemu": {
                "pid": 10, "user_ns": 10, "system_ns": 20, "cpu_ns": 30,
                "read_bytes": 100, "write_bytes": 200,
            },
            "cxlmemsim": {
                "pid": 11, "user_ns": 5, "system_ns": 5, "cpu_ns": 10,
                "read_bytes": 0, "write_bytes": 0,
            },
        }
        after = {
            "client0_qemu": {
                "pid": 10, "user_ns": 70, "system_ns": 40, "cpu_ns": 110,
                "read_bytes": 400, "write_bytes": 700,
            },
            "cxlmemsim": {
                "pid": 11, "user_ns": 15, "system_ns": 15, "cpu_ns": 30,
                "read_bytes": 1000, "write_bytes": 2000,
            },
        }
        cost = self.runner.host_process_cost_window(before, after, 100, 200)
        self.assertEqual(cost["groups"]["qemu_inclusive"]["cpu_ns"], 80)
        self.assertEqual(cost["groups"]["cxlmemsim"]["cpu_ns"], 20)
        self.assertEqual(cost["groups"]["qemu_inclusive"]["sampled_cpu_share"], 0.8)
        self.assertIn("inclusive", cost["interpretation"])

    def test_legofs_timing_ratios_use_rank_wall_budget(self):
        summaries = [
            {
                "stats": {
                    "direct_read_acquire_ns": 10,
                    "lifecycle_metadata_lookup_ns": 20,
                    "lifecycle_write_arena_commit_ns": 30,
                }
            },
            {
                "stats": {
                    "direct_read_acquire_ns": 10,
                    "lifecycle_metadata_lookup_ns": 20,
                    "lifecycle_write_arena_commit_ns": 30,
                }
            },
        ]
        timing = self.runner.legofs_timing_breakdown(
            summaries,
            {
                "audit": {
                    "direct_write_total_ns": 25,
                    "state_deferred_updates": 7,
                    "state_validation_ops": 3,
                    "state_validation_ns": 10,
                    "arena_acquire_calls": 2,
                    "arena_acquire_slots": 128,
                    "arena_acquire_backend_reserve_ns": 40,
                    "arena_acquire_state_persist_ns": 30,
                    "arena_acquire_total_ns": 90,
                }
            },
            wall_ns=100,
            client_count=2,
        )
        self.assertEqual(timing["client_timed_intervals_ns"], 120)
        self.assertEqual(timing["client_timed_share_of_rank_wall"], 0.6)
        self.assertEqual(timing["server_direct_write_commit_ns"], 25)
        self.assertEqual(timing["server_state_deferred_updates"], 7)
        self.assertEqual(timing["server_state_validation_ops"], 3)
        self.assertEqual(timing["server_state_validation_ns"], 10)
        self.assertEqual(timing["server_state_validation_share_of_rank_wall"], 0.05)
        self.assertEqual(timing["arena_acquire"]["arena_acquire_calls"], 2)
        self.assertEqual(
            timing["arena_acquire"]["arena_acquire_backend_reserve_ns"], 40
        )
        self.assertEqual(timing["arena_acquire"]["arena_acquire_total_ns"], 90)
        self.assertIn("nested", timing["interpretation"])

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
        unrelated_serial_noise = json.dumps({
            "host_capture_ns": 90,
            "line": (
                'BADFS_DIRECT_MAP_TRACE_JSON {"event":"mmap","op_'
                '[   62.908134] hrtimer: interrupt took 93077000 ns'
            ),
        })
        persisted_event = json.dumps({
                "host_capture_ns": 100,
                "line": "BADFS_DIRECT_MAP_TRACE_JSON "
                + json.dumps(direct, separators=(",", ":")),
            })
        self.paths.event_log("client0").write_text(
            unrelated_serial_noise + "\n" + persisted_event + "\n",
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

    def test_strict_persistency_proof_rejects_malformed_candidate_event(self):
        self.paths.bundle.mkdir(parents=True)
        self.paths.event_log("client0").write_text(
            json.dumps({
                "host_capture_ns": 100,
                "line": (
                    'BADFS_DIRECT_MAP_TRACE_JSON {"event":"persisted","op_'
                    '[   62.908134] hrtimer: interrupt took 93077000 ns'
                ),
            }) + "\n",
            encoding="utf-8",
        )
        self.paths.event_log("server0").write_text("", encoding="utf-8")
        self.paths.coherence.write_text("", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "no exact writer-persisted"):
            self.runner.strict_persistency_proof(
                self.paths, [{"owner": 77, "endpoint": 3}], 1
            )

    def test_strict_persistency_proof_drops_corrupt_redundant_candidate(self):
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
        client_lines = [
            json.dumps({
                "host_capture_ns": 90,
                "line": 'BADFS_DIRECT_MAP_TRACE_JSON {"event":"persisted","op_',
            }),
            json.dumps({
                "host_capture_ns": 100,
                "line": "BADFS_DIRECT_MAP_TRACE_JSON "
                + json.dumps(direct, separators=(",", ":"))
                + "LEGOFS_SET_TIME 123",
            }),
        ]
        self.paths.event_log("client0").write_text(
            "\n".join(client_lines) + "\n", encoding="utf-8"
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
        self.assertEqual(proof["writer_persisted"]["count"], 1)

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
        with self.assertRaisesRegex(ValueError, "line PUTM"):
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
