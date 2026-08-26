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
SYSINT_BUILD_SCRIPT = ROOT / "scripts" / "build_syscall_intercept_riscv.sh"
SYSINT_MUSL_PATCH = (
    ROOT
    / "scripts"
    / "patches"
    / "syscall-intercept-riscv"
    / "0001-musl-constructor-uses-auxv.patch"
)
SYSINT_MUSL_LIBC_PATCH = (
    ROOT
    / "scripts"
    / "patches"
    / "syscall-intercept-riscv"
    / "0002-musl-loader-is-libc.patch"
)
SYSINT_PREBUILT_CAPSTONE_PATCH = (
    ROOT
    / "scripts"
    / "patches"
    / "syscall-intercept-riscv"
    / "0003-prebuilt-capstone-c-only.patch"
)
MUSL_HOTPATCH_PADDING_PATCH = (
    ROOT
    / "scripts"
    / "patches"
    / "musl"
    / "0001-riscv64-syscall-hotpatch-padding.patch"
)


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

    def test_model_and_physical_evidence_modes_are_exclusive(self):
        model = {
            "functional_model_only": True,
            "physical_hardware_evidence": False,
        }
        self.runner.validate_evidence_mode(model)
        for invalid in (
            {"functional_model_only": True, "physical_hardware_evidence": True},
            {"functional_model_only": False, "physical_hardware_evidence": False},
        ):
            with self.assertRaisesRegex(ValueError, "must be exclusive"):
                self.runner.validate_evidence_mode(invalid)

    def test_local_candidate_gate_is_explicit_and_cannot_claim_hardware_or_official(self):
        args = self.runner.parse_args(["--stage", "tiny", "--local-candidate-gate"])
        self.assertTrue(args.local_candidate_gate)
        source = (ROOT / "run-legofs-io500.sh").read_text(encoding="utf-8")
        self.assertIn("--local-candidate-gate", source)
        self.assertIn("never grants C3/C4", source)

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
        self.assertIn("BADFS_CONTROL_TRANSPORT=cxl", init + rank)
        self.assertIn("BADFS_CXL_CONTROL_RING_SIZE=262144", init + rank)
        self.assertNotIn("BADFS_CXL_HEARTBEAT_TIMEOUT_MS", init + rank)
        self.assertIn('BADFS_CXL_CLIENT_SLOT="$endpoint_id"', rank)
        self.assertNotIn("BADFS_SERVERS=", init + rank)
        self.assertNotIn("nc -z -w 1 127.0.0.1 3345", init)
        self.assertNotIn('BADFS_FABRIC_REGION_ID=', init + rank)

    def test_cxl_control_prefix_is_disjoint_from_allocator_partitions(self):
        layout = self.runner.cxl_control_layout(2, 10)
        self.assertEqual(layout["transport"], "cxl-dax-ring")
        self.assertEqual(layout["control_region_bytes"] % (2 * 1024**2), 0)
        partitions = layout["allocator_partitions"]
        self.assertEqual(partitions[0]["offset"], layout["control_region_bytes"])
        self.assertLessEqual(
            partitions[-1]["offset"] + partitions[-1]["length"],
            self.runner.ENDPOINT_BYTES,
        )
        self.assertLessEqual(
            partitions[0]["offset"] + partitions[0]["length"],
            partitions[1]["offset"],
        )

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
            'SYSINT_ROOT="$SOURCES/syscall-intercept"',
            build,
        )
        self.assertIn(
            'cargo build --manifest-path "$LEGOFS_ROOT/Cargo.toml"',
            build,
        )
        self.assertIn("LLVM_COMMIT=87f0227cb60147a26a1eeb4fb06e3b505e9c7261", build)
        self.assertIn("/lib/ld-musl-riscv64.so.1", build)
        self.assertNotIn("/usr/riscv64-linux-gnu/lib/*.so", build)

    def test_manifest_checks_artifact_hashes_and_source_provenance(self):
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
        artifacts.update(
            self.runner.expected_mode_artifacts(
                self.paths,
                "legacy-cxl-reference",
            )
        )
        for path in artifacts.values():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")
        self.paths.manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest = {
                "schema_version": 2,
                "superproject_commit": "development-tree-may-be-dirty",
                "sources": {
                    "legofs": {
                        "commit": "a" * 40,
                        "tree": "b" * 40,
                        "dirty_diff_sha256": "c" * 64,
                        "status_sha256": "d" * 64,
                    }
                },
                "artifacts": {
                    name: {
                        "path": str(path),
                        "size": 1,
                        "sha256": self.runner.sha256_file(path),
                    }
                    for name, path in artifacts.items()
                },
            }
        self.paths.manifest.write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )
        build = self.runner.verify_manifest(self.paths)
        self.assertEqual(build["manifest"], manifest)
        artifacts["payload"].write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.runner.verify_manifest(self.paths)

    def test_filesystem_modes_are_closed_and_result_roots_are_disjoint(self):
        reference = self.runner.parse_args(
            ["--stage", "hello", "--filesystem-mode", "legacy-cxl-reference"]
        )
        candidate = self.runner.parse_args(
            ["--stage", "hello", "--filesystem-mode", "rdwo-candidate"]
        )
        self.assertEqual(reference.filesystem_mode, "legacy-cxl-reference")
        self.assertEqual(candidate.filesystem_mode, "rdwo-candidate")
        reference_paths = self.runner.Paths(
            pathlib.Path(self.temporary.name),
            "hello",
            filesystem_mode=reference.filesystem_mode,
        )
        candidate_paths = self.runner.Paths(
            pathlib.Path(self.temporary.name),
            "hello",
            filesystem_mode=candidate.filesystem_mode,
        )
        self.assertNotEqual(reference_paths.run, candidate_paths.run)
        self.assertNotEqual(reference_paths.bundle, candidate_paths.bundle)
        self.assertIn("legacy-cxl-reference", reference_paths.run.parts)
        self.assertIn("rdwo-candidate", candidate_paths.run.parts)

    def test_candidate_manifest_cannot_reuse_legacy_artifacts(self):
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
        artifacts.update(
            self.runner.expected_mode_artifacts(
                self.paths,
                "legacy-cxl-reference",
            )
        )
        for path in artifacts.values():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")
        self.paths.manifest.parent.mkdir(parents=True, exist_ok=True)
        self.paths.manifest.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "sources": {
                        "legofs": {
                            "commit": "a" * 40,
                            "tree": "b" * 40,
                            "dirty_diff_sha256": "c" * 64,
                            "status_sha256": "d" * 64,
                        }
                    },
                    "artifacts": {
                        name: {
                            "path": str(path),
                            "size": 1,
                            "sha256": self.runner.sha256_file(path),
                        }
                        for name, path in artifacts.items()
                    },
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(FileNotFoundError, "rdwo_client"):
            self.runner.verify_manifest(self.paths, "rdwo-candidate")

    def test_candidate_uses_normal_mpi_branch_without_legacy_environment(self):
        init = INIT_SCRIPT.read_text(encoding="utf-8")
        rank = RANK_SCRIPT.read_text(encoding="utf-8")
        runner = RUNNER.read_text(encoding="utf-8")
        self.assertIn("io500.filesystem_mode=", runner)
        self.assertIn("legacy-cxl-reference|rdwo-candidate", init + rank)
        self.assertIn("/payload/bin/badfs-rdwo-client --", rank)
        self.assertIn("/payload/bin/badfs-rdwo-host-agent", init)
        self.assertIn("/payload/bin/badfs-rdwo-server", init)
        candidate_branch = rank.split("\nrdwo-candidate)\n", 1)[1].split(
            "\nesac",
            1,
        )[0]
        self.assertNotIn("BADFS_POSIX_DATA_PATH", candidate_branch)
        self.assertNotIn("libbadfs_intercept.so", candidate_branch)
        self.assertNotIn("legofs.v2=1", rank)

    def test_evaluation_manifest_binds_mode_topology_build_and_payload_config(self):
        self.paths.manifest.parent.mkdir(parents=True, exist_ok=True)
        self.paths.manifest.write_text('{"schema_version":2}\n', encoding="utf-8")
        self.paths.payload_config.parent.mkdir(parents=True, exist_ok=True)
        self.paths.payload_config.write_text(
            "[global]\nscc = TRUE\n",
            encoding="utf-8",
        )
        effective = pathlib.Path(self.temporary.name) / "effective.ini"
        effective.write_bytes(self.paths.payload_config.read_bytes())
        artifact = pathlib.Path(self.temporary.name) / "io500"
        artifact.write_bytes(b"binary")
        manifest = {
            "schema_version": "legofs.evaluation-run-manifest.v1",
            "filesystem_mode": "legacy-cxl-reference",
            "io500_mode": "standard",
            "product_consumes_this_manifest": False,
            "official_candidate": False,
            "topology": {
                "server_guests": 1,
                "client_guests": 2,
                "mpi_ranks": 2,
                "shared_cxl_type3_region_bytes": self.runner.ENDPOINT_BYTES,
                "hdm_db_bi_required": True,
            },
            "phase_evidence": {
                "schema_version": "legofs.phase-evidence-manifest.v1",
                "external_only": True,
                "product_visible_phase_identity": False,
                "records": [
                    {
                        "phase": "ior-easy-write",
                        "required_metric_units": ["GiB/s", "kIOPS"],
                    },
                    {
                        "phase": "mdtest-easy-write",
                        "required_metric_units": ["kIOPS"],
                    },
                ],
            },
            "build_identity": {
                "build_manifest_sha256": self.runner.sha256_file(
                    self.paths.manifest
                )
            },
            "artifacts": {
                "io500": {
                    "path": str(artifact),
                    "size": artifact.stat().st_size,
                    "sha256": self.runner.sha256_file(artifact),
                }
            },
            "effective_config": {
                "path": str(effective),
                "sha256": self.runner.sha256_file(effective),
            },
        }
        manifest["manifest_digest"] = self.runner.canonical_digest(manifest)
        manifest_path = pathlib.Path(self.temporary.name) / "evaluation.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        validated = self.runner.validate_evaluation_manifest(
            manifest_path,
            self.paths,
            {"manifest": {"schema_version": 2}},
            "legacy-cxl-reference",
            1,
            2,
        )
        self.assertEqual(validated["manifest_digest"], manifest["manifest_digest"])

        effective.write_text("tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "config digest mismatch"):
            self.runner.validate_evaluation_manifest(
                manifest_path,
                self.paths,
                {"manifest": {"schema_version": 2}},
                "legacy-cxl-reference",
                1,
                2,
            )

    def test_syscall_interceptor_has_reproducible_musl_constructor_fix(self):
        builder = SYSINT_BUILD_SCRIPT.read_text(encoding="utf-8")
        patch = SYSINT_MUSL_PATCH.read_text(encoding="utf-8")
        libc_patch = SYSINT_MUSL_LIBC_PATCH.read_text(encoding="utf-8")
        io500_builder = BUILD_SCRIPT.read_text(encoding="utf-8")

        self.assertIn('archive --format=tar "$SYSINT_COMMIT"', builder)
        self.assertIn("GIT_DIR=/dev/null git apply", builder)
        self.assertIn("grep -q 'intercept(void)'", builder)
        self.assertIn("grep -q 'getauxval(AT_EXECFN)'", builder)
        self.assertIn("grep -q 'ld-musl-'", builder)
        self.assertIn("+intercept(void)", patch)
        self.assertIn(
            "+\tcmdline = (const char *)(uintptr_t)getauxval(AT_EXECFN);",
            patch,
        )
        self.assertIn("-\tcmdline = argv[0];", patch)
        self.assertIn('+\tstatic const char musl[] = "ld-musl-";', libc_patch)
        self.assertIn("+\t\tstrncmp(name, musl, sizeof(musl) - 1) == 0", libc_patch)
        self.assertIn(
            "syscall_intercept_manifest=$SYSINT_BUILD_ROOT/manifest.txt",
            io500_builder,
        )

    def test_syscall_interceptor_uses_pinned_c_only_riscv_capstone(self):
        builder = SYSINT_BUILD_SCRIPT.read_text(encoding="utf-8")
        patch = SYSINT_PREBUILT_CAPSTONE_PATCH.read_text(encoding="utf-8")

        self.assertIn('archive --format=tar "$CAPSTONE_COMMIT"', builder)
        self.assertIn("CAPSTONE_ARCHS=riscv", builder)
        self.assertIn("CAPSTONE_BUILD_CORE_ONLY=yes", builder)
        self.assertIn("CAPSTONE_STATIC=yes", builder)
        self.assertIn("CAPSTONE_SHARED=no", builder)
        self.assertIn("-DBUILD_CPP_TEST=OFF", builder)
        self.assertIn("-DUSE_PREBUILT_CAPSTONE=ON", builder)
        self.assertNotIn("RISCV_CXX", builder)
        self.assertIn("+option(BUILD_CPP_TEST", patch)
        self.assertIn("+option(USE_PREBUILT_CAPSTONE", patch)
        self.assertIn('"${PREBUILT_CAPSTONE_LIBRARY}"', patch)

    def test_musl_ecalls_have_reproducible_hotpatch_padding_gate(self):
        builder = BUILD_SCRIPT.read_text(encoding="utf-8")
        patch = MUSL_HOTPATCH_PADDING_PATCH.read_text(encoding="utf-8")

        self.assertIn(
            "MUSL_TARBALL_SHA256="
            "a9a118bbe84d8764da0ea0d28b3ab3fae8477fc7e4085d90102b8596fc7c75e4",
            builder,
        )
        self.assertIn('MUSL_SOURCE="${TARGET_ROOT}/musl-', builder)
        self.assertIn("GIT_DIR=/dev/null git apply", builder)
        self.assertIn("require_musl_hotpatch_padding", builder)
        self.assertIn('previous_encoding != "00000013"', builder)
        self.assertIn("exit total == 0 || bad != 0", builder)
        self.assertIn("musl_ecall_gate=$MUSL_ECALL_GATE", builder)
        self.assertIn("musl_hotpatch_padding_patch=$MUSL_HOTPATCH_PADDING_PATCH", builder)
        self.assertIn("+\t.option norvc", patch)
        self.assertIn("+\tnop", patch)
        self.assertNotIn("INTERCEPT_DEBUG_DUMP", RANK_SCRIPT.read_text())

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
        self.assertIn('[ "$dax_align" -ge 4096 ]', init)
        self.assertIn('export BADFS_CXL_MAP_ALIGNMENT="$dax_align"', init)
        self.assertIn('export BADFS_CXL_MAP_ALIGNMENT="$(cat /run/dax-align)"', rank)
        self.assertIn("export INTERCEPT_ALL_OBJS=1", rank)

    def test_guest_command_loop_captures_expected_failures_without_errexit_leak(self):
        init = INIT_SCRIPT.read_text()
        self.assertNotIn("set -e", init)
        self.assertIn(
            'if /payload/bin/io500-verify "/results/$verify_stage/config.ini"',
            init,
        )
        self.assertIn(
            'echo "LEGOFS_IO500_VERIFY_EXIT stage=$verify_stage rc=$rc"',
            init,
        )
        self.assertIn(
            "if BADFS_BENCH_MODE=inspect /payload/bin/badfs-bench; then",
            init,
        )

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

    def test_guest_readiness_fatal_is_reported_immediately(self):
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
            "LEGOFS_IO500_FATAL step=server-exit rc=78\r\n"
        )
        with self.assertRaisesRegex(RuntimeError, "fatal marker"):
            self.runner.wait_guest_marker(
                console, "LEGOFS_IO500_SERVER_READY index=0", timeout=3600
            )

        console.output = (
            "LEGOFS_IO500_SERVER_READY index=0\r\n"
            "LEGOFS_IO500_FATAL step=server-exit rc=78\r\n"
        )
        with self.assertRaisesRegex(RuntimeError, "fatal marker"):
            self.runner.wait_guest_marker(
                console, "LEGOFS_IO500_SERVER_READY index=0", timeout=3600
            )

        console.output = "LEGOFS_IO500_SERVER_READY index=0\r\n"
        self.runner.wait_guest_marker(
            console, "LEGOFS_IO500_SERVER_READY index=0", timeout=1
        )

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
                "schema_version": "badfs.posix.path-summary.v2",
                "mpi_rank": rank,
                "endpoint": rank,
                "intercept_enabled": True,
                "control_transport": "cxl-dax-ring",
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
                "control_transport": "cxl-dax-ring",
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

    def test_strict_proof_discards_but_does_not_reconstruct_printk_interleave(self):
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
                "host_capture_ns": 50,
                "line": (
                    "BADFS_DIRECT_MAP_TRACE_JSON "
                    '{"offset":409[   1.234567] hrtimer: interrupt took 12 ns}'
                ),
            }),
            json.dumps({
                "host_capture_ns": 100,
                "line": "BADFS_DIRECT_MAP_TRACE_JSON "
                + json.dumps(direct, separators=(",", ":")),
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

        diagnostics = proof["serial_trace_diagnostics"]
        self.assertEqual(proof["count"], 1)
        self.assertEqual(diagnostics["discarded_uart_interleaved_records"], 1)
        self.assertEqual(
            diagnostics["discarded_records"][0]["classification"],
            "guest-uart-kernel-printk-interleave",
        )

    def test_marked_console_json_rejects_unclassified_malformed_record(self):
        events = [{
            "host_capture_ns": 1,
            "line": "BADFS_DIRECT_MAP_TRACE_JSON {not-json}",
        }]
        with self.assertRaisesRegex(ValueError, "invalid BADFS_DIRECT_MAP_TRACE_JSON"):
            self.runner.parse_marked_console_json(
                events,
                "BADFS_DIRECT_MAP_TRACE_JSON ",
                pathlib.Path("client0-events.jsonl"),
            )

    def test_marked_console_json_discards_split_hrtimer_printk(self):
        events = [{
            "host_capture_ns": 2,
            "line": (
                "[   84.441635] hBADFS_DIRECT_MAP_TRACE_JSON "
                '{"offset":316669952,"ortimer: interrupt took 9281000 ns'
            ),
        }]

        records, discarded = self.runner.parse_marked_console_json(
            events,
            "BADFS_DIRECT_MAP_TRACE_JSON ",
            pathlib.Path("client0-events.jsonl"),
        )

        self.assertEqual(records, [])
        self.assertEqual(len(discarded), 1)
        self.assertEqual(
            discarded[0]["classification"],
            "guest-uart-kernel-printk-interleave",
        )
        self.assertIn("interrupt took 9281000 ns", discarded[0]["kernel_printk_prefix"])

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
