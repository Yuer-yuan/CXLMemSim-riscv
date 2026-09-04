import importlib.util
import base64
import gzip
import hashlib
import json
import os
import pathlib
import subprocess
import struct
import tempfile
import threading
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
TOP_LEVEL_RUNNER = ROOT / "run-legofs-io500.sh"
RUNNER = ROOT / "scripts" / "legofs_io500.py"
BUILD_SCRIPT = ROOT / "scripts" / "build_legofs_io500.sh"
SYSINT_COMPAT_BUILD_SCRIPT = ROOT / "scripts" / "build_syscall_intercept_riscv.sh"
PAYLOAD_REBUILD_SCRIPT = ROOT / "scripts" / "rebuild_legofs_io500_payload.sh"
BOOTSTRAP_REBUILD_SCRIPT = ROOT / "scripts" / "rebuild_legofs_io500_bootstrap.sh"
NOV_GCC = ROOT / "scripts" / "riscv64-nov-gcc"
RANK_SCRIPT = ROOT / "guest" / "legofs_io500_rank.sh"
BENCH_WRAPPER = ROOT / "guest" / "legofs_badfs_bench.sh"
SERVER_WRAPPER = ROOT / "guest" / "legofs_badfs_server.sh"
INIT_SCRIPT = ROOT / "guest" / "legofs_io500_init.sh"
BOOTSTRAP_INIT_SCRIPT = ROOT / "guest" / "legofs_io500_bootstrap_init.sh"
SYSTEM_SYNC_PROBE_SOURCE = ROOT / "guest" / "system_sync_probe.c"
LEGOFS_SERVER_SOURCE = ROOT / "components" / "legofs" / "badfs-server" / "src" / "lib.rs"


def load_runner():
    spec = importlib.util.spec_from_file_location("legofs_io500", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cxl_serving_evidence(
    endpoint,
    submitted=1,
    cursor_mode="owned",
    cq_wait_mode="timer_sleep",
):
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
        "persistence_handoff": {
            "request_generation": 0,
            "release_generation": 0,
            "grant_generation": 0,
        },
        "persistence_handoff_requests": 0,
        "persistence_handoff_grants": 0,
        "persistence_handoff_releases": 0,
        "persistence_handoff_wait_ns": 0,
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
            "successful_request_poll_ns": submitted,
            "dispatcher_backend_ns": submitted,
            "completion_publication_wait_ns": submitted,
            "cqe_publish_ns": submitted,
        },
        "client_timing": {
            "calls": submitted,
            "call_gate_wait_ns": submitted,
            "syscall_prepare_ns": submitted,
            "sq_credit_wait_ns": 0,
            "sq_publish_ns": submitted,
            "cq_wait_ns": submitted,
            "cq_empty_polls": 0,
            "cq_spin_polls": 0,
            "cq_cooperative_yields": 0,
            "cq_timer_sleeps": 0,
        },
        "cursor_mode": cursor_mode,
        "cq_wait_mode": cq_wait_mode,
        "open_mode": "fused_pin",
        "fused_open_attempts": submitted,
        "fused_open_pins": submitted,
        "fused_open_fallbacks": 0,
        "fused_close_attempts": submitted,
        "fused_close_snapshot_releases": submitted,
        "batched_close_commands": 1,
        "batched_close_items": submitted,
        "direct_metadata_capability": False,
        "direct_metadata_attempts": 0,
        "direct_metadata_hits": 0,
        "direct_metadata_not_found_hits": 0,
        "direct_metadata_no_hint": 0,
        "direct_metadata_stale": 0,
        "direct_metadata_cold_attempts": 0,
        "direct_metadata_cold_hits": 0,
        "direct_metadata_cold_not_found_hits": 0,
        "direct_metadata_cold_fallbacks": 0,
        "direct_metadata_fallback_commands": 0,
        "direct_metadata_commands_elided": 0,
        "direct_metadata_read_ns": 0,
        "direct_metadata_gate_loads": 0,
        "direct_metadata_root_loads": 0,
        "direct_metadata_dentry_cell_loads": 0,
        "direct_metadata_inode_record_loads": 0,
        "direct_metadata_unstable_read_retries": 0,
        "direct_metadata_unstable_read_recoveries": 0,
        "direct_metadata_unstable_read_exhaustions": 0,
        "direct_metadata_unstable_root_unavailable": 0,
        "direct_metadata_unstable_dentry_decode": 0,
        "direct_metadata_unstable_inode_decode": 0,
        "direct_metadata_unstable_snapshot_changed": 0,
        "direct_mutation_lease_installs": 0,
        "direct_mutation_lease_replacements": 0,
        "direct_mutation_generic_close_handoffs": 0,
        "direct_create_attempts": 0,
        "direct_create_hits": 0,
        "direct_create_fallbacks": 0,
        "direct_create_commands_elided": 0,
        "direct_create_post_grant_retry_hits": 0,
        "direct_create_fallback_ineligible": 0,
        "direct_create_fallback_no_parent_writer": 0,
        "direct_create_fallback_existing": 0,
        "direct_create_fallback_inode_exhausted": 0,
        "direct_create_errors": 0,
        "direct_unlink_attempts": 0,
        "direct_unlink_commands_elided": 0,
        "direct_unlink_base_commands_elided": 0,
        "direct_unlink_peer_applied_commands_elided": 0,
        "direct_unlink_fallbacks": 0,
        "direct_unlink_fallback_ineligible": 0,
        "direct_unlink_fallback_no_parent_writer": 0,
        "direct_unlink_fallback_unapplied_peer": 0,
        "direct_unlink_fallback_base_unavailable": 0,
        "direct_unlink_fallback_overlay_changed": 0,
        "direct_unlink_fallback_unsupported_target": 0,
        "direct_unlink_fallback_live_ofd": 0,
        "direct_unlink_not_found": 0,
        "direct_unlink_membership_changing": 0,
        "direct_unlink_lease_revoked": 0,
        "client_lane_access": {
            "submit_calls": submitted,
            "completion_polls": submitted,
            "request_polls": 0,
            "completions": submitted,
            "ready_loads": 0,
            "peer_publication_loads": submitted,
            "self_cursor_shared_loads": 0 if cursor_mode == "owned" else 2 * submitted,
            "capacity_refreshes": 0,
            "shared_identity_rereads": 0 if cursor_mode == "owned" else submitted,
        },
        "dispatches_by_opcode": {"35": 1} if submitted == 1 else {
            "1": submitted - 1,
            "35": 1,
        },
        "cq_wait_ns_by_opcode": {"35": 1} if submitted == 1 else {
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
                "successful_request_poll_ns": 1,
                "dispatcher_backend_ns": 1,
                "completion_publication_wait_ns": 0,
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
            "cq_wait_ns_by_opcode": {"1": 1},
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
            [
                "--stage", "tiny",
                "--serving-transport", "cxl",
                "--cursor-mode", "legacy_shared",
                "--cq-wait-mode", "timer_sleep",
            ]
        )
        self.assertEqual(cxl.serving_transport, "cxl")
        self.assertEqual(cxl.cursor_mode, "legacy_shared")
        self.assertEqual(cxl.cq_wait_mode, "timer_sleep")
        self.assertEqual(cxl.packed_small_segments, "on")
        self.assertFalse(hasattr(cxl, "direct_metadata_mode"))

        packed = self.runner.parse_args(
            [
                "--stage", "tiny",
                "--serving-transport", "cxl",
                "--packed-small-segments", "on",
                "--small-segment-count", "16",
            ]
        )
        self.assertEqual(packed.packed_small_segments, "on")
        self.assertEqual(packed.small_segment_count, 16)

        init = INIT_SCRIPT.read_text(encoding="utf-8")
        rank = RANK_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("io500.client_count=", RUNNER.read_text(encoding="utf-8"))
        self.assertIn('-n "$client_count"', init)
        self.assertIn('> /run/cursor-mode', init)
        self.assertIn('> /run/cq-wait-mode', init)
        self.assertNotIn('/run/direct-metadata-mode', init)
        self.assertNotIn('/run/open-mode', init)
        self.assertIn('BADFS_SERVING_CURSOR_MODE="$cursor_mode"', rank)
        self.assertIn('BADFS_SERVING_CQ_WAIT_MODE="$cq_wait_mode"', rank)
        self.assertNotIn('BADFS_DIRECT_METADATA_MODE', rank)
        self.assertNotIn('BADFS_SERVING_OPEN_MODE', rank)
        self.assertIn('if [ "$endpoint_id" -eq 0 ]; then', rank)
        self.assertIn(
            'LEGOFS_IO500_PREFLIGHT_MATRIX endpoint=$endpoint_id mode=full',
            rank,
        )
        self.assertIn('mode=covered-by-endpoint-0', rank)
        self.assertNotIn(
            "--open-mode", TOP_LEVEL_RUNNER.read_text(encoding="utf-8")
        )
        self.assertNotIn(
            "--direct-metadata-mode", TOP_LEVEL_RUNNER.read_text(encoding="utf-8")
        )
        server = LEGOFS_SERVER_SOURCE.read_text(encoding="utf-8")
        self.assertNotIn("BADFS_DIRECT_METADATA_MODE", server)
        self.assertIn(
            "if serving_profile == CxlServingProfile::SingleAuthorityMinimal",
            server,
        )
        self.assertIn("CxlDirectMetadataStore::initialize", server)

    def observation_arena(self, run_id="obs-contract", role=1, endpoint=0, pid=1234):
        data = bytearray(4096)
        data[0:8] = b"LEGOFSO1"
        struct.pack_into("<HHHHQ", data, 8, 1, 0, 4096, 64, len(data))
        data[24] = 1
        data[25] = 1
        data[26] = role
        data[27] = 4
        struct.pack_into("<HHII", data, 28, 8, 384, endpoint, pid)
        struct.pack_into("<QQQQ", data, 40, 91, 1, 0b11, self.runner.observation_run_hash(run_id))
        struct.pack_into("<QQQQQ", data, 72, 1_000_000, 2_000_000, 10, 20, 30)
        struct.pack_into("<IIQQQ", data, 112, 1, 4096, 4096, 4096, 0)
        struct.pack_into("<Q", data, 248, 8195)
        struct.pack_into(
            "<Q", data, 416, int(self.runner.OBSERVATION_SCHEMA_DIGEST, 16)
        )
        identity = 4096 - 0  # identity table starts immediately after the header
        # Extend only for this parser contract; runner validation reads the first
        # identity record at offset 4096, as production arenas do.
        data.extend(b"\0" * 64)
        data[identity] = role
        struct.pack_into("<IIIQQ", data, identity + 4, endpoint, pid,
                         endpoint if role == 1 else 0xFFFFFFFF, 91, 1)
        struct.pack_into("<Q", data, 16, len(data))
        return bytes(data)

    def observation_frame_text(self, payload, run_id="obs-contract", role="client-rank", endpoint=0, pid=1234):
        filename = f"observation-v1-{run_id}-{role}-{endpoint}-{pid}.bin"
        digest = hashlib.sha256(payload).hexdigest()
        encoded = base64.b64encode(payload).decode("ascii")
        lines = [encoded[index:index + 76] for index in range(0, len(encoded), 76)]
        return "\n".join([
            f"LEGOFS_OBSERVATION_BEGIN role={role} endpoint={endpoint} file={filename} bytes={len(payload)} sha256={digest}",
            *lines,
            f"LEGOFS_OBSERVATION_END role={role} endpoint={endpoint} file={filename}",
        ]) + "\n"

    def compressed_observation_frame_text(
        self, payload, run_id="obs-contract", role="client-rank", endpoint=0,
        pid=1234, corrupt_first_copy=None,
    ):
        filename = f"observation-v1-{run_id}-{role}-{endpoint}-{pid}.bin"
        digest = hashlib.sha256(payload).hexdigest()
        compressed = gzip.compress(payload, compresslevel=1, mtime=0)
        compressed_digest = hashlib.sha256(compressed).hexdigest()
        encoded = base64.b64encode(compressed).decode("ascii")
        chunks = [encoded[index:index + 1024] for index in range(0, len(encoded), 1024)]
        lines = [
            f"LEGOFS_OBSERVATION_BEGIN role={role} endpoint={endpoint} "
            f"file={filename} bytes={len(payload)} sha256={digest} "
            f"encoding=gzip-base64-v1 compressed_bytes={len(compressed)} "
            f"compressed_sha256={compressed_digest} chunks={len(chunks)}",
        ]
        for sequence, chunk in enumerate(chunks):
            first = chunk
            if corrupt_first_copy == sequence:
                first += "[  78.658516] harmless asynchronous kernel diagnostic"
            lines.extend([
                f"LEGOFS_OBSERVATION_CHUNK seq={sequence} data={first}",
                f"LEGOFS_OBSERVATION_CHUNK seq={sequence} data={chunk}",
            ])
        lines.append(
            f"LEGOFS_OBSERVATION_END role={role} endpoint={endpoint} file={filename}"
        )
        return "\n".join(lines) + "\n"

    def test_observation_cli_profile_and_guest_contract(self):
        args = self.runner.parse_args([
            "--stage", "tiny", "--observation-mode", "aggregate",
            "--observation-sample-shift", "6", "--observation-arena-mib", "12",
            "--host-profiler", "stat",
        ])
        config = self.runner.resolve_observation_config(
            args, pathlib.Path(self.temporary.name), "obs-contract"
        )
        self.assertEqual(config["mode"], "aggregate")
        self.assertEqual(config["sample_shift"], 6)
        self.assertEqual(config["arena_bytes"], 12 * 1024**2)

        default = self.runner.resolve_observation_config(
            self.runner.parse_args(["--stage", "tiny"]),
            pathlib.Path(self.temporary.name),
            "obs-default",
        )
        self.assertEqual(default["mode"], "default")
        init = INIT_SCRIPT.read_text(encoding="utf-8")
        self.assertIn('unset BADFS_OBSERVATION_MODE', init)
        self.assertIn('BADFS_OBSERVATION_WORKLOAD_ENDPOINTS', init)
        self.assertIn('LEGOFS_DUMP_OBSERVATION', init)
        self.assertIn('/bin/busybox wc -c', init)
        self.assertIn('/bin/busybox sha256sum', init)
        self.assertIn('/bin/busybox base64', init)
        self.assertIn('/bin/busybox gzip -1 -c', init)
        self.assertIn('LEGOFS_OBSERVATION_CHUNK seq=', init)
        self.assertNotIn('$(wc -c', init)
        runner_source = RUNNER.read_text(encoding="utf-8")
        self.assertIn("o=", runner_source)
        compact_run_id = self.runner.observation_run_id_for_label("x" * 96)
        self.assertRegex(compact_run_id, r"^r[0-9a-f]{16}$")
        boot_token = self.runner.observation_boot_token({
            **config,
            "run_id": compact_run_id,
        })
        self.assertLessEqual(len(boot_token), 40)
        for transport, durability in (
            ("legacy", "d-before-v"),
            ("cxl", "coherent-seal-no-writeback"),
            ("cxl", "coherent-seal-needs-writeback"),
        ):
            payload_owner = (
                "authority-bi-acquire"
                if durability == "coherent-seal-no-writeback"
                else "writer-before-visibility"
            )
            metadata_bootargs = self.runner.guest_bootargs(
                "server", 9, "metadata-smoke", 2, 10, transport, "owned",
                "timer_sleep", durability, payload_owner, "msync", "on", 32767, "batched",
                {**config, "run_id": compact_run_id},
            )
            self.assertLessEqual(
                len("setenv bootargs ''") + len(metadata_bootargs), 255
            )
        zicbom_bootargs = self.runner.guest_bootargs(
            "client", 0, "tiny", 1, 2, "cxl", "owned", "timer_sleep",
            "coherent-seal-no-writeback", "placement-routed",
            "riscv-zicbom-dax", "on", 2048, "batched",
            {**config, "run_id": compact_run_id},
        )
        self.assertIn(" u=H ", zicbom_bootargs)
        self.assertNotIn(" z=", zicbom_bootargs)

        for shift in (-1, 21):
            bad = self.runner.parse_args([
                "--stage", "tiny", "--observation-sample-shift", str(shift)
            ])
            with self.assertRaisesRegex(ValueError, "between 0 and 20"):
                self.runner.resolve_observation_config(
                    bad, pathlib.Path(self.temporary.name), "bad"
                )
        for arena in (7, 65):
            bad = self.runner.parse_args([
                "--stage", "tiny", "--observation-arena-mib", str(arena)
            ])
            with self.assertRaisesRegex(ValueError, "between 8 and 64"):
                self.runner.resolve_observation_config(
                    bad, pathlib.Path(self.temporary.name), "bad"
                )

    def test_observation_profile_hash_is_checked_before_execute(self):
        profile = {
            "schema_version": "legofs.observation.profile.v1",
            "name": "o1-aggregate-v1",
            "mode": "aggregate",
            "sample_shift": 4,
            "arena_mib": 12,
            "producer_slots": 8,
            "histogram": {"bins": 576},
            "idle_sample_stride": 1024,
            "observation_schema": {
                "major": 1, "minor": self.runner.OBSERVATION_SCHEMA_MINOR,
                "event_record_bytes": 64,
                "digest": self.runner.OBSERVATION_SCHEMA_DIGEST,
            },
        }
        profile["profile_sha256"] = self.runner.observation_profile_digest(profile)
        path = pathlib.Path(self.temporary.name) / "profile.json"
        path.write_text(json.dumps(profile), encoding="utf-8")
        loaded = self.runner.load_observation_profile(path)
        self.assertEqual(loaded["sample_shift"], 4)
        profile["sample_shift"] = 5
        path.write_text(json.dumps(profile), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.runner.load_observation_profile(path)

        args = self.runner.parse_args([
            "--stage", "tiny", "--observation-profile-manifest", str(path),
            "--observation-mode", "aggregate",
        ])
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            self.runner.resolve_observation_config(
                args, pathlib.Path(self.temporary.name), "obs-contract"
            )

    def test_observation_frame_rejects_truncation_digest_and_identity_mismatch(self):
        payload = self.observation_arena()
        text = self.observation_frame_text(payload)
        frames = self.runner.parse_observation_frames(text)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["payload"], payload)
        header = self.runner.validate_observation_frame(
            frames[0], expected_role="client-rank", expected_endpoint=0,
            config={
                "mode": "aggregate", "sample_shift": 4,
                "producer_slots": 8, "arena_bytes": len(payload),
                "run_id": "obs-contract",
            },
            client_count=2,
        )
        self.assertTrue(header["snapshot_complete"])
        self.assertTrue(header["runtime_valid"])

        abandoned = bytearray(payload)
        struct.pack_into("<Q", abandoned, 224, 1)
        abandoned_frame = self.runner.parse_observation_frames(
            self.observation_frame_text(bytes(abandoned))
        )[0]
        abandoned_header = self.runner.validate_observation_frame(
            abandoned_frame, expected_role="client-rank", expected_endpoint=0,
            config={
                "mode": "aggregate", "sample_shift": 4,
                "producer_slots": 8, "arena_bytes": len(abandoned),
                "run_id": "obs-contract",
            },
            client_count=2,
        )
        self.assertFalse(abandoned_header["runtime_valid"])

        with self.assertRaisesRegex(ValueError, "truncated"):
            self.runner.parse_observation_frames(text.rsplit("\n", 2)[0] + "\n")
        bad_digest = text.replace(hashlib.sha256(payload).hexdigest(), "0" * 64)
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            self.runner.parse_observation_frames(bad_digest)
        bad_end = text.replace(
            "LEGOFS_OBSERVATION_END role=client-rank endpoint=0",
            "LEGOFS_OBSERVATION_END role=client-rank endpoint=1",
        )
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            self.runner.parse_observation_frames(bad_end)

    def test_compressed_observation_frame_survives_one_interrupted_chunk_copy(self):
        payload = self.observation_arena()
        text = self.compressed_observation_frame_text(
            payload, corrupt_first_copy=0
        )
        frames = self.runner.parse_observation_frames(text)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["payload"], payload)

        missing = "\n".join(
            line for line in text.splitlines()
            if not line.startswith("LEGOFS_OBSERVATION_CHUNK seq=0 ")
        ) + "\n"
        with self.assertRaisesRegex(ValueError, "missing chunks"):
            self.runner.parse_observation_frames(missing)

    def test_observation_failure_domain_does_not_raise(self):
        result = self.runner.dump_observations_safely(
            config={"mode": "aggregate"}
        )
        self.assertFalse(result["observation_valid"])
        self.assertIsNotNone(result["observation_error"])

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

    def test_system_sync_probe_is_an_explicit_metadata_cxl_diagnostic(self):
        args = self.runner.parse_args([
            "--stage", "metadata-smoke",
            "--server-count", "1",
            "--client-count", "2",
            "--serving-transport", "cxl",
            "--fault-profile", "diagnose-system-sync",
        ])
        self.assertEqual(args.fault_profile, "diagnose-system-sync")
        source = SYSTEM_SYNC_PROBE_SOURCE.read_text(encoding="utf-8")
        self.assertIn("raw_status=%d", source)
        self.assertIn("child_reached=%d", source)
        self.assertIn("WIFSIGNALED", source)
        rebuild = PAYLOAD_REBUILD_SCRIPT.read_text(encoding="utf-8")
        self.assertIn('"$PAYLOAD_TREE/bin/system-sync-probe"', rebuild)

        output = (
            "LEGOFS_SYSTEM_SYNC_PROBE_RESULT label=preload-all "
            "raw_status=9 errno=10 child_reached=0 exited=0 exit_status=-1 "
            "signaled=1 term_signal=9\n"
            "LEGOFS_IO500_PROXY_EXIT index=0 rc=1\n"
        )
        record = self.runner.parse_system_sync_probe(output, "preload-all")
        self.assertTrue(record["program_reached"])
        self.assertEqual(record["raw_status"], 9)
        self.assertEqual(record["errno"], 10)
        self.assertEqual(record["child_reached"], 0)
        self.assertEqual(record["term_signal"], 9)
        self.assertEqual(record["proxy_rc"], 1)

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
        self.assertIn("--mattr=+m,+a,+f,+d,+c,+zicsr,+zifencei,+zicbom", build)
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
            'SYSINT_ROOT="$LEGOFS_ROOT/third_party/syscall-intercept-riscv"',
            build,
        )
        self.assertIn(
            '"$LEGOFS_ROOT/scripts/build-syscall-intercept-riscv.sh"',
            build,
        )
        self.assertIn('SYSINT_MUSL_LIBC="$MUSL_PREFIX/lib/libc.so"', build)
        self.assertNotIn("LEGOFS_TOOL_ROOT", build)
        self.assertNotIn(
            '"$ROOT/scripts/build_syscall_intercept_riscv.sh"',
            build,
        )
        self.assertIn(
            'cargo build --manifest-path "$LEGOFS_ROOT/Cargo.toml"',
            build,
        )
        self.assertIn("LLVM_COMMIT=87f0227cb60147a26a1eeb4fb06e3b505e9c7261", build)
        self.assertIn("/lib/ld-musl-riscv64.so.1", build)
        self.assertNotIn("/usr/riscv64-linux-gnu/lib/*.so", build)

    def test_workspace_syscall_intercept_entry_forwards_to_legofs(self):
        wrapper = SYSINT_COMPAT_BUILD_SCRIPT.read_text(encoding="utf-8")
        self.assertIn(
            "components/legofs/scripts/build-syscall-intercept-riscv.sh",
            wrapper,
        )
        self.assertNotIn(".cxl-bi-tools", wrapper)
        self.assertNotIn("cmake -S", wrapper)

    def test_payload_only_mode_is_explicit_and_mutually_exclusive(self):
        runner = TOP_LEVEL_RUNNER.read_text(encoding="utf-8")
        self.assertIn("PAYLOAD_ONLY=0", runner)
        self.assertIn("--payload-only", runner)
        self.assertIn(
            "((BUILD_ONLY + RUN_ONLY + PAYLOAD_ONLY <= 1))",
            runner,
        )
        self.assertIn("BUILD_ONLY == 0 && PAYLOAD_ONLY == 0", runner)
        self.assertIn(
            '"$ROOT/scripts/rebuild_legofs_io500_payload.sh" --jobs "$JOBS"',
            runner,
        )

    def test_top_level_runner_forwards_explicit_packed_layout_selector(self):
        with tempfile.TemporaryDirectory() as temporary:
            tool_dir = pathlib.Path(temporary)
            for tool in ("bash", "cat", "debugfs", "mke2fs"):
                stub = tool_dir / tool
                stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                stub.chmod(0o755)
            getconf = tool_dir / "getconf"
            getconf.write_text("#!/bin/sh\nprintf '64\\n'\n", encoding="utf-8")
            getconf.chmod(0o755)
            python = tool_dir / "python3"
            python.write_text(
                "#!/bin/sh\nprintf 'FORWARDED:%s\\n' \"$*\"\n",
                encoding="utf-8",
            )
            python.chmod(0o755)
            environment = os.environ.copy()
            environment["LEGOFS_TOOLCHAIN_PATH"] = str(tool_dir)
            run = subprocess.run(
                [
                    str(TOP_LEVEL_RUNNER),
                    "--run-only",
                    "--stage", "tiny",
                    "--server-count", "1",
                    "--client-count", "2",
                    "--serving-transport", "cxl",
                    "--packed-small-segments", "on",
                    "--small-segment-count", "16",
                ],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn("--packed-small-segments on", run.stdout)
            self.assertIn("--small-segment-count 16", run.stdout)

            product_default = subprocess.run(
                [
                    str(TOP_LEVEL_RUNNER),
                    "--run-only",
                    "--stage", "tiny",
                    "--server-count", "1",
                    "--client-count", "2",
                    "--serving-transport", "cxl",
                ],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(product_default.returncode, 0, product_default.stderr)
            self.assertIn("--packed-small-segments on", product_default.stdout)
            self.assertIn("--small-segment-count 2048", product_default.stdout)

            rejected = subprocess.run(
                [
                    str(TOP_LEVEL_RUNNER),
                    "--run-only",
                    "--packed-small-segments", "off",
                    "--small-segment-count", "16",
                ],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(rejected.returncode, 2)
            self.assertIn(
                "--small-segment-count requires --packed-small-segments on",
                rejected.stderr,
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
            "$LIBUNWIND_PREFIX/lib/libunwind.so.1",
        ):
            self.assertIn(f'require_file "{reused}"', rebuild)
        self.assertNotIn(
            'require_file "$SYSINT_BUILD_ROOT/build/libsyscall_intercept.so.0"\n'
            'require_file "$LIBUNWIND_PREFIX/lib/libunwind.so.1"',
            rebuild,
        )
        sysint_build = rebuild.index(
            '"$LEGOFS_ROOT/scripts/build-syscall-intercept-riscv.sh"'
        )
        intercept_build = rebuild.index("-p badfs-intercept")
        self.assertLess(sysint_build, intercept_build)
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

    def test_qemu_server_uses_one_ordered_cxl_issuer(self):
        wrapper = SERVER_WRAPPER.read_text(encoding="utf-8")
        self.assertIn("BADFS_SERVER_RUNTIME_WORKERS=1", wrapper)
        self.assertIn("BADFS_SERVING_DISPATCH_WORKERS=4", wrapper)
        self.assertIn("Production launchers omit these overrides", wrapper)

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

    def test_measured_mpi_window_never_mutates_guest_clocks(self):
        source = RUNNER.read_text(encoding="utf-8")
        launch = source[
            source.index("def launch_mpi("):
            source.index("def parse_hello(")
        ]
        self.assertEqual(launch.count("synchronize_guest_clocks("), 1)
        self.assertNotIn("maintain_guest_clocks", launch)
        self.assertIn(
            '"reason": "measurement-window clock mutation is forbidden"',
            launch,
        )

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

        console.output = "LEGOFS_IO500_MPI_EXIT stage=metadata-smoke rc=0\r\n"
        server = FakeConsole("rcu: rcu_sched detected stalls on CPUs/tasks:\r\n")
        self.assertEqual(
            self.runner.wait_mpi_exit(
                console,
                "metadata-smoke",
                timeout=3600,
                start=0,
                monitored_guests=[
                    ("client0", console, 0),
                    ("server0", server, 0),
                ],
            ),
            0,
        )

        console.output = ""
        server.output = "watchdog: BUG: soft lockup - CPU#0 stuck\r\n"
        with self.assertRaisesRegex(RuntimeError, "INCONCLUSIVE_RACE.*server0"):
            self.runner.wait_mpi_exit(
                console,
                "metadata-smoke",
                timeout=3600,
                start=0,
                monitored_guests=[
                    ("client0", console, 0),
                    ("server0", server, 0),
                ],
            )

        server.output = (
            "epc : 000000000022f09a badaddr: 00007fff90600040 "
            "cause: 0000000000000005\r\n"
        )
        with self.assertRaisesRegex(RuntimeError, "server0.*fatal process fault"):
            self.runner.wait_mpi_exit(
                console,
                "metadata-smoke",
                timeout=3600,
                start=0,
                monitored_guests=[
                    ("client0", console, 0),
                    ("server0", server, 0),
                ],
            )

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

        console = FakeConsole("LEGOFS_IO500_FATAL step=server-exit rc=78\r\n")
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
        self.assertIn('if find_enabled and (', source)
        self.assertTrue(
            self.runner.io500_phase_enabled("[find]\nrun = TRUE\n", "find")
        )
        self.assertFalse(
            self.runner.io500_phase_enabled("[find]\nrun = FALSE\n", "find")
        )

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
        self.assertIn(
            "[mdtest-hard]\nAPI = POSIX\nn = 32\nfiles-per-dir = 32\nrun = TRUE\n",
            metadata,
        )
        self.assertIn("[mdtest-hard-write]\nAPI = POSIX\nrun = TRUE\n", metadata)
        self.assertIn("[mdtest-hard-read]\nAPI = POSIX\nrun = TRUE\n", metadata)
        self.assertIn("[mdtest-hard-stat]\nAPI = POSIX\nrun = TRUE\n", metadata)
        for disabled in (
            "[mdtest-easy]\nrun = FALSE\n",
            "[find]\nrun = FALSE\n",
            "[mdtest-hard-delete]\nrun = FALSE\n",
        ):
            self.assertIn(disabled, metadata)
        self.assertIn("[ior-easy]\nrun = FALSE\n", metadata)
        rank_script = RANK_SCRIPT.read_text()
        self.assertIn(
            "metadata-smoke) export BADFS_LIFECYCLE_WRITE_ARENA_SLOTS=32 ;;",
            rank_script,
        )
        self.assertIn(
            "*) export BADFS_LIFECYCLE_WRITE_ARENA_SLOTS=64 ;;", rank_script
        )

    def test_payload_persistence_owner_auto_uses_measured_provider_default(self):
        self.assertEqual(
            self.runner.resolve_payload_persistence_owner(
                "coherent-seal-no-writeback", "auto"
            ),
            "placement-routed",
        )
        self.assertEqual(
            self.runner.resolve_payload_persistence_owner("d-before-v", "auto"),
            "writer-before-visibility",
        )
        self.assertEqual(
            self.runner.resolve_payload_persistence_owner(
                "coherent-seal-no-writeback", "authority-bi-acquire"
            ),
            "authority-bi-acquire",
        )
        self.assertEqual(
            self.runner.resolve_payload_persistence_owner(
                "coherent-seal-no-writeback", "placement-routed"
            ),
            "placement-routed",
        )
        with self.assertRaisesRegex(ValueError, "incompatible"):
            self.runner.resolve_payload_persistence_owner(
                "d-before-v", "authority-bi-acquire"
            )

        for script in (INIT_SCRIPT, ROOT / "guest" / "legofs_badfs_server.sh"):
            self.assertIn(
                "h) payload_persistence_owner=placement_routed; writer_persist_provider=msync ;;",
                script.read_text(),
            )
            self.assertIn(
                "H) payload_persistence_owner=placement_routed; writer_persist_provider=riscv-zicbom-dax ;;",
                script.read_text(),
            )

    def test_lifecycle_payload_evidence_accepts_only_closed_durability_paths(self):
        base = {
            "payload_persistence_owner": "WRITER_BEFORE_VISIBILITY",
            "direct_write_commit_items": 2,
            "writer_persisted_direct_items": 0,
            "writer_persisted_direct_bytes": 0,
            "host_persist_receipts_accepted": 0,
            "host_persist_receipts_duplicate": 0,
            "host_persist_receipts_rejected": 0,
            "pending_payload_dependencies": 0,
            "provider_payload_barriers": 1,
            "provider_payload_bytes": 8192,
            "authority_coherent_acquire_jobs": 0,
            "authority_coherent_acquire_ranges": 0,
            "authority_coherent_acquire_bytes": 0,
            "authority_coherent_acquire_ns": 0,
            "authority_payload_persist_jobs": 0,
            "authority_payload_persist_ranges": 0,
            "authority_payload_persist_bytes": 0,
            "authority_payload_persist_ns": 0,
        }
        provider = self.runner.lifecycle_payload_persistence_evidence(base, 0)
        self.assertTrue(provider["provider_payload_barrier"])
        self.assertFalse(provider["writer_persisted_complete"])

        writer = dict(base)
        writer.update({
            "writer_persisted_direct_items": 2,
            "writer_persisted_direct_bytes": 7802,
            "provider_payload_barriers": 0,
            "provider_payload_bytes": 0,
        })
        writer_evidence = self.runner.lifecycle_payload_persistence_evidence(
            writer, 0
        )
        self.assertFalse(writer_evidence["provider_payload_barrier"])
        self.assertTrue(writer_evidence["writer_persisted_complete"])

        receipt = dict(base)
        receipt.update({
            "payload_persistence_owner": "WRITER_RECEIPT",
            "provider_payload_barriers": 0,
            "provider_payload_bytes": 0,
            "host_persist_receipts_accepted": 2,
        })
        receipt_evidence = self.runner.lifecycle_payload_persistence_evidence(
            receipt, 0
        )
        self.assertTrue(receipt_evidence["writer_host_receipt_complete"])

        authority = dict(base)
        authority.update({
            "payload_persistence_owner": "AUTHORITY_BI_ACQUIRE",
            "authority_coherent_acquire_jobs": 1,
            "authority_coherent_acquire_ranges": 2,
            "authority_coherent_acquire_bytes": 8192,
            "authority_coherent_acquire_ns": 100,
            "authority_payload_persist_jobs": 1,
            "authority_payload_persist_ranges": 2,
            "authority_payload_persist_bytes": 8192,
            "authority_payload_persist_ns": 200,
        })
        authority_evidence = self.runner.lifecycle_payload_persistence_evidence(
            authority, 0
        )
        self.assertTrue(authority_evidence["authority_complete"])
        self.assertEqual(authority_evidence["authority_dependency_items"], 2)

        placement = dict(
            authority,
            payload_persistence_owner="PLACEMENT_ROUTED",
            host_persist_receipts_accepted=1,
            authority_coherent_acquire_ranges=1,
            authority_payload_persist_ranges=1,
        )
        placement_evidence = self.runner.lifecycle_payload_persistence_evidence(
            placement, 0
        )
        self.assertTrue(placement_evidence["placement_routed_complete"])
        self.assertFalse(placement_evidence["writer_host_receipt_complete"])
        self.assertEqual(placement_evidence["authority_dependency_items"], 1)

        undercovered_authority = dict(
            authority,
            authority_coherent_acquire_ranges=1,
            authority_payload_persist_ranges=1,
        )
        with self.assertRaisesRegex(ValueError, "authority-owned payload"):
            self.runner.lifecycle_payload_persistence_evidence(
                undercovered_authority, 0
            )

        mixed_authority = dict(authority, host_persist_receipts_accepted=1)
        with self.assertRaisesRegex(ValueError, "mixed writer receipts"):
            self.runner.lifecycle_payload_persistence_evidence(mixed_authority, 0)
        mixed_writer = dict(
            receipt,
            authority_coherent_acquire_jobs=1,
            authority_coherent_acquire_ranges=1,
            authority_coherent_acquire_bytes=4096,
            authority_coherent_acquire_ns=1,
            authority_payload_persist_jobs=1,
            authority_payload_persist_ranges=1,
            authority_payload_persist_bytes=4096,
            authority_payload_persist_ns=1,
        )
        with self.assertRaisesRegex(ValueError, "used authority persistence"):
            self.runner.lifecycle_payload_persistence_evidence(mixed_writer, 0)

        missing = dict(writer, writer_persisted_direct_items=1)
        with self.assertRaisesRegex(ValueError, "without payload persistence"):
            self.runner.lifecycle_payload_persistence_evidence(missing, 0)
        rejected_receipt = dict(receipt, host_persist_receipts_rejected=1)
        with self.assertRaisesRegex(ValueError, "lacks payload persistence"):
            self.runner.lifecycle_payload_persistence_evidence(rejected_receipt, 0)
        impossible = dict(writer, writer_persisted_direct_items=3)
        with self.assertRaisesRegex(ValueError, "more writer-persisted"):
            self.runner.lifecycle_payload_persistence_evidence(impossible, 0)

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

    def test_writer_receipt_gate_requires_completed_cxl_persistence_handoff(self):
        records = []
        for rank in range(2):
            evidence = cxl_serving_evidence(rank)[0]
            evidence.update({
                "persistence_handoff": {
                    "request_generation": 1,
                    "release_generation": 1,
                    "grant_generation": 1,
                },
                "persistence_handoff_requests": 1,
                "persistence_handoff_grants": 1,
                "persistence_handoff_releases": 1,
                "persistence_handoff_wait_ns": 10,
            })
            records.append({
                "schema_version": "badfs.posix.path-summary.v4",
                "mpi_rank": rank,
                "endpoint": rank,
                "intercept_enabled": True,
                "cxl_serving_evidence": [evidence],
                "stats": {"open_ops": 1, "write_ops": 1},
                "syscall_classification": {
                    "totals": {
                        "handled": 1,
                        "rejected": 0,
                        "non_badfs_forward": 0,
                        "forbidden_badfs_forward": 0,
                    },
                    "syscalls": [],
                },
            })

        self.runner.validate_posix_summaries(
            records,
            client_count=2,
            expected_payload_persistence_owner="WRITER_RECEIPT",
        )

        missing = json.loads(json.dumps(records))
        evidence = missing[1]["cxl_serving_evidence"][0]
        evidence.update({
            "persistence_handoff": {
                "request_generation": 0,
                "release_generation": 0,
                "grant_generation": 0,
            },
            "persistence_handoff_requests": 0,
            "persistence_handoff_grants": 0,
            "persistence_handoff_releases": 0,
            "persistence_handoff_wait_ns": 0,
        })
        with self.assertRaisesRegex(ValueError, r"active ranks \[1\]"):
            self.runner.validate_posix_summaries(
                missing,
                client_count=2,
                expected_payload_persistence_owner="WRITER_RECEIPT",
            )

        unbalanced = json.loads(json.dumps(records))
        unbalanced[0]["cxl_serving_evidence"][0][
            "persistence_handoff_releases"
        ] = 0
        with self.assertRaisesRegex(ValueError, "generations are not balanced"):
            self.runner.validate_posix_summaries(
                unbalanced,
                client_count=2,
                expected_payload_persistence_owner="WRITER_RECEIPT",
            )

    def test_posix_summary_gate_distinguishes_immediate_and_batched_close(self):
        records = []
        for rank in range(2):
            evidence = cxl_serving_evidence(rank)[0]
            evidence["batched_close_commands"] = 0
            evidence["batched_close_items"] = 0
            records.append({
                "schema_version": "badfs.posix.path-summary.v4",
                "mpi_rank": rank,
                "endpoint": rank,
                "intercept_enabled": True,
                "cxl_serving_evidence": [evidence],
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
            })

        self.runner.validate_posix_summaries(
            records, client_count=2, expected_close_mode="immediate"
        )
        with self.assertRaisesRegex(ValueError, "no bounded close batch"):
            self.runner.validate_posix_summaries(
                records, client_count=2, expected_close_mode="batched"
            )

        direct_records = json.loads(json.dumps(records))
        for record in direct_records:
            record["stats"].update({
                "direct_ro_open_attempts": 1,
                "direct_ro_open_hits": 1,
                "direct_ro_open_fallbacks": 0,
                "direct_ro_layout_roots_loaded": 1,
                "direct_ro_layout_entries_loaded": 1,
                "direct_ro_open_commands_elided": 1,
                "direct_ro_close_commands_elided": 1,
                "direct_ro_epoch_acquires": 1,
                "direct_ro_epoch_releases": 1,
            })
            evidence = record["cxl_serving_evidence"][0]
            evidence["fused_close_attempts"] = 0
            evidence["fused_close_snapshot_releases"] = 0
        self.runner.validate_posix_summaries(
            direct_records, client_count=2, expected_close_mode="batched"
        )

        broken = json.loads(json.dumps(direct_records))
        broken[0]["stats"]["direct_ro_epoch_releases"] = 0
        with self.assertRaisesRegex(ValueError, "direct RO POSIX evidence"):
            self.runner.validate_posix_summaries(
                broken, client_count=2, expected_close_mode="batched"
            )

    def test_direct_metadata_gate_requires_capability_and_exact_accounting(self):
        records = []
        for rank in range(2):
            evidence = cxl_serving_evidence(rank, 2)[0]
            evidence.update({
                "format_generation": self.runner.DIRECT_METADATA_FORMAT_GENERATION,
                "direct_metadata_capability": True,
                "direct_metadata_attempts": 2,
                "direct_metadata_hits": 1,
                "direct_metadata_not_found_hits": 0,
                "direct_metadata_no_hint": 1,
                "direct_metadata_stale": 0,
                "direct_metadata_cold_attempts": 1,
                "direct_metadata_cold_hits": 0,
                "direct_metadata_cold_not_found_hits": 0,
                "direct_metadata_cold_fallbacks": 1,
                "direct_metadata_fallback_commands": 1,
                "direct_metadata_commands_elided": 1,
                "direct_metadata_read_ns": 10,
                "direct_metadata_gate_loads": 2,
                "direct_metadata_root_loads": 2,
                "direct_metadata_dentry_cell_loads": 3,
                "direct_metadata_inode_record_loads": 1,
                "dispatches_by_opcode": {"2": 1, "35": 1},
                "cq_wait_ns_by_opcode": {"2": 1, "35": 1},
            })
            records.append({
                "schema_version": "badfs.posix.path-summary.v4",
                "mpi_rank": rank,
                "endpoint": rank,
                "intercept_enabled": True,
                "cxl_serving_evidence": [evidence],
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
            })

        self.runner.validate_posix_summaries(
            records,
            client_count=2,
            expected_direct_metadata_mode="cxl",
        )

        warm_fallback_records = []
        for rank in range(2):
            evidence = cxl_serving_evidence(rank, 3)[0]
            evidence.update({
                "format_generation": self.runner.DIRECT_METADATA_FORMAT_GENERATION,
                "direct_metadata_capability": True,
                "direct_metadata_attempts": 3,
                "direct_metadata_hits": 1,
                "direct_metadata_not_found_hits": 0,
                "direct_metadata_no_hint": 1,
                "direct_metadata_stale": 0,
                "direct_metadata_cold_attempts": 1,
                "direct_metadata_cold_hits": 0,
                "direct_metadata_cold_not_found_hits": 0,
                "direct_metadata_cold_fallbacks": 1,
                "direct_metadata_fallback_commands": 2,
                "direct_metadata_commands_elided": 1,
                "direct_metadata_read_ns": 10,
                "direct_metadata_gate_loads": 3,
                "direct_metadata_root_loads": 3,
                "direct_metadata_dentry_cell_loads": 3,
                "direct_metadata_inode_record_loads": 1,
                "dispatches_by_opcode": {"2": 2, "35": 1},
                "cq_wait_ns_by_opcode": {"2": 2, "35": 1},
            })
            warm_fallback_records.append({
                "schema_version": "badfs.posix.path-summary.v4",
                "mpi_rank": rank,
                "endpoint": rank,
                "intercept_enabled": True,
                "cxl_serving_evidence": [evidence],
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
            })
        self.runner.validate_posix_summaries(
            warm_fallback_records,
            client_count=2,
            expected_direct_metadata_mode="cxl",
        )

        broken_warm = json.loads(json.dumps(warm_fallback_records))
        for record in broken_warm:
            evidence = record["cxl_serving_evidence"][0]
            evidence["direct_metadata_no_hint"] = 3
            evidence["direct_metadata_cold_attempts"] = 3
            evidence["direct_metadata_cold_fallbacks"] = 3
        with self.assertRaisesRegex(ValueError, "fallback accounting"):
            self.runner.validate_posix_summaries(
                broken_warm,
                client_count=2,
                expected_direct_metadata_mode="cxl",
            )

        broken = json.loads(json.dumps(records))
        broken[0]["cxl_serving_evidence"][0][
            "direct_metadata_commands_elided"
        ] = 0
        with self.assertRaisesRegex(ValueError, "command-elision"):
            self.runner.validate_posix_summaries(
                broken,
                client_count=2,
                expected_direct_metadata_mode="cxl",
            )

        broken = json.loads(json.dumps(records))
        broken[0]["cxl_serving_evidence"][0][
            "direct_metadata_capability"
        ] = False
        with self.assertRaisesRegex(ValueError, "capability absent"):
            self.runner.validate_posix_summaries(
                broken,
                client_count=2,
                expected_direct_metadata_mode="cxl",
            )

        rpc_records = json.loads(json.dumps(records))
        for record in rpc_records:
            evidence = record["cxl_serving_evidence"][0]
            evidence["direct_metadata_capability"] = False
            for field in self.runner.DIRECT_METADATA_CLIENT_COUNTERS:
                evidence[field] = 0
            evidence["dispatches_by_opcode"] = {"1": 1, "35": 1}
            evidence["cq_wait_ns_by_opcode"] = {"1": 1, "35": 1}
        self.runner.validate_posix_summaries(
            rpc_records,
            client_count=2,
            expected_direct_metadata_mode="rpc",
        )

    def test_direct_metadata_result_breakdown_preserves_command_demand(self):
        evidence = cxl_serving_evidence(0, 2)[0]
        evidence.update({
            "direct_metadata_capability": True,
            "direct_metadata_attempts": 3,
            "direct_metadata_hits": 2,
            "direct_metadata_not_found_hits": 0,
            "direct_metadata_no_hint": 1,
            "direct_metadata_stale": 0,
            "direct_metadata_cold_attempts": 1,
            "direct_metadata_cold_hits": 0,
            "direct_metadata_cold_not_found_hits": 0,
            "direct_metadata_cold_fallbacks": 1,
            "direct_metadata_fallback_commands": 1,
            "direct_metadata_commands_elided": 2,
            "direct_metadata_read_ns": 30,
            "direct_metadata_gate_loads": 4,
            "direct_metadata_root_loads": 4,
            "direct_metadata_dentry_cell_loads": 7,
            "direct_metadata_inode_record_loads": 2,
            "dispatches_by_opcode": {"2": 1, "35": 1},
        })
        authority = {
            "publication_batches": 3,
            "dentry_writes": 4,
            "inode_writes": 3,
            "record_prepare_ns": 40,
            "root_odd_ns": 12,
            "root_odd_max_ns": 7,
            "registered_hints": 2,
            "capacity_failures": 0,
            "publication_failures": 0,
            "dentry_high_water": 4,
            "dentry_capacity": 1024,
            "inode_capacity": 1024,
        }
        result = self.runner.direct_metadata_breakdown(
            [{"cxl_serving_evidence": [evidence]}],
            {"audit": {"direct_metadata": authority}},
            "cxl",
        )
        self.assertEqual(result["read_metadata_commands"], 1)
        self.assertEqual(result["baseline_equivalent_read_metadata_demand"], 3)
        self.assertEqual(result["client"]["direct_metadata_commands_elided"], 2)
        self.assertAlmostEqual(result["hit_ratio"], 2 / 3)
        self.assertEqual(result["authority"], authority)

    def test_direct_mutation_breakdown_preserves_elided_create_demand(self):
        evidence = cxl_serving_evidence(0, 2)[0]
        evidence.update({
            "direct_mutation_lease_installs": 1,
            "direct_create_attempts": 65,
            "direct_create_hits": 64,
            "direct_create_fallbacks": 1,
            "direct_create_commands_elided": 64,
            "direct_create_fallback_no_parent_writer": 1,
            "dispatches_by_opcode": {"61": 1, "35": 1},
        })
        result = self.runner.direct_mutation_breakdown(
            [{"cxl_serving_evidence": [evidence]}]
        )
        self.assertEqual(result["open_or_create_commands"], 1)
        self.assertEqual(result["baseline_equivalent_create_demand"], 65)
        self.assertEqual(result["client"]["direct_create_hits"], 64)
        self.assertAlmostEqual(result["hit_ratio"], 64 / 65)

    def test_direct_mutation_breakdown_does_not_count_post_grant_retry_as_elision(self):
        evidence = cxl_serving_evidence(0, 2)[0]
        evidence.update({
            "direct_mutation_lease_installs": 1,
            "direct_create_attempts": 3,
            "direct_create_hits": 1,
            "direct_create_fallbacks": 2,
            "direct_create_commands_elided": 0,
            "direct_create_post_grant_retry_hits": 1,
            "direct_create_fallback_no_parent_writer": 2,
            "dispatches_by_opcode": {"61": 2, "35": 1},
        })
        result = self.runner.direct_mutation_breakdown(
            [{"cxl_serving_evidence": [evidence]}]
        )
        self.assertEqual(result["open_or_create_commands"], 2)
        self.assertEqual(result["baseline_equivalent_create_demand"], 2)
        self.assertEqual(result["client"]["direct_create_hits"], 1)
        self.assertEqual(result["hit_ratio"], 0.0)
        self.assertAlmostEqual(result["attempt_resolution_hit_ratio"], 1 / 3)

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

        records[3]["cxl_serving_evidence"] = cxl_serving_evidence(3, 2)
        records[3]["cxl_serving_evidence"][0]["cq_wait_mode"] = "timer_sleep"
        records[3]["cxl_serving_evidence"][0]["client_timing"][
            "cq_cooperative_yields"
        ] = 1
        records[3]["cxl_serving_evidence"][0]["client_timing"]["cq_empty_polls"] = 1
        records[3]["cxl_serving_evidence"][0]["client_lane_access"][
            "completion_polls"
        ] = 3
        records[3]["cxl_serving_evidence"][0]["client_lane_access"][
            "peer_publication_loads"
        ] = 3
        with self.assertRaisesRegex(ValueError, "timer CQ wait"):
            self.runner.validate_posix_summaries(records)

        records[3]["cxl_serving_evidence"] = cxl_serving_evidence(3, 2)
        records[3]["cxl_serving_evidence"][0]["client_timing"][
            "cq_empty_polls"
        ] = 1
        with self.assertRaisesRegex(ValueError, "lane-access evidence"):
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

    def test_observation_phase_projection_marks_boundary_uncertainty(self):
        phases = [
            {"name": "first", "start_realtime_ns": 10_000, "end_realtime_ns": 20_000},
            {"name": "second", "start_realtime_ns": 20_000, "end_realtime_ns": 30_000},
        ]
        header = {
            "realtime_anchor_ns": 10_000,
            "monotonic_anchor_ns": 1_000,
            "anchor_span_ns": 10,
        }
        inside = self.runner.project_observation_timestamp_to_phase(
            6_000, header, phases, guest_sync_error_ns=10,
            io500_timestamp_resolution_ns=10,
        )
        self.assertEqual(inside["phase"], "first")
        self.assertFalse(inside["phase_ambiguous"])

        boundary = self.runner.project_observation_timestamp_to_phase(
            11_000, header, phases, guest_sync_error_ns=10,
            io500_timestamp_resolution_ns=10,
        )
        self.assertIsNone(boundary["phase"])
        self.assertTrue(boundary["phase_ambiguous"])
        self.assertEqual(boundary["overlapping_phases"], ["first", "second"])

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
                    "read_ops": 2,
                    "read_ns": 30,
                    "read_bytes": 4096,
                    "write_ops": 3,
                    "write_ns": 40,
                    "write_bytes": 8192,
                    "direct_read_acquire_ns": 10,
                    "lifecycle_metadata_lookup_ns": 20,
                    "lifecycle_write_arena_commit_ns": 30,
                    "small_segment_cache_hits": 7,
                    "small_segment_dynamic_mmap_calls": 1,
                    "small_segment_dynamic_mmap_ns": 11,
                    "small_segment_mapped_owner_segments": 1,
                    "small_segment_protection_calls": 9,
                    "small_segment_protection_ns": 13,
                    "writer_host_persist_jobs_queued": 10,
                    "writer_host_persist_jobs_completed": 10,
                    "writer_host_persist_jobs_failed": 0,
                    "writer_host_local_persist_ns": 21,
                    "writer_host_receipt_submit_ns": 31,
                    "writer_host_queue_wait_ns": 4,
                    "writer_host_msync_persist_jobs": 1,
                    "writer_host_local_persist_ranges": 10,
                    "writer_host_local_persist_bytes": 40960,
                    "writer_host_receipt_batches_submitted": 1,
                    "writer_host_receipt_items_submitted": 10,
                    "writer_host_authority_apply_wait_polls": 7,
                    "writer_host_authority_apply_wait_ns": 41,
                }
            },
            {
                "stats": {
                    "read_ops": 2,
                    "read_ns": 20,
                    "read_bytes": 4096,
                    "write_ops": 3,
                    "write_ns": 30,
                    "write_bytes": 8192,
                    "direct_read_acquire_ns": 10,
                    "lifecycle_metadata_lookup_ns": 20,
                    "lifecycle_write_arena_commit_ns": 30,
                    "small_segment_cache_hits": 5,
                    "small_segment_dynamic_mmap_calls": 1,
                    "small_segment_dynamic_mmap_ns": 17,
                    "small_segment_mapped_owner_segments": 1,
                    "small_segment_protection_calls": 8,
                    "small_segment_protection_ns": 19,
                    "writer_host_persist_jobs_queued": 6,
                    "writer_host_persist_jobs_completed": 6,
                    "writer_host_persist_jobs_failed": 0,
                    "writer_host_local_persist_ns": 11,
                    "writer_host_receipt_submit_ns": 19,
                    "writer_host_queue_wait_ns": 3,
                    "writer_host_msync_persist_jobs": 1,
                    "writer_host_local_persist_ranges": 6,
                    "writer_host_local_persist_bytes": 24576,
                    "writer_host_receipt_batches_submitted": 1,
                    "writer_host_receipt_items_submitted": 6,
                    "writer_host_authority_apply_wait_polls": 5,
                    "writer_host_authority_apply_wait_ns": 29,
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
                    "direct_write_commit_groups": 2,
                    "direct_write_commit_items": 16,
                    "direct_write_prepare_ns": 2,
                    "direct_write_payload_persist_ns": 3,
                    "direct_write_state_build_ns": 4,
                    "direct_write_state_persist_ns": 5,
                    "direct_write_publish_ns": 6,
                    "direct_write_trace_ns": 1,
                    "small_segment_claims": 1,
                    "small_segment_claim_persist_ns": 23,
                    "small_cell_grants": 16,
                    "small_cell_commits": 16,
                    "small_cell_payload_bytes": 62416,
                    "small_cell_payload_persist_bytes": 0,
                    "small_cell_allocator_persist_barriers": 0,
                    "legacy_small_arena_reserve_calls": 0,
                    "legacy_small_arena_reserve_ns": 0,
                }
            },
            wall_ns=100,
            client_count=2,
        )
        self.assertEqual(timing["client_timed_intervals_ns"], 120)
        self.assertEqual(timing["client_timed_share_of_rank_wall"], 0.6)
        self.assertEqual(timing["client_io_envelope"]["read_ns"], 50)
        self.assertEqual(timing["client_io_envelope"]["write_ns"], 70)
        self.assertEqual(
            timing["client_io_envelope"]["write_share_of_rank_wall"], 0.35
        )
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
        self.assertEqual(timing["direct_write"]["direct_write_total_ns"], 25)
        self.assertEqual(timing["direct_write"]["direct_write_prepare_ns"], 2)
        self.assertEqual(timing["packed_small_segment"]["small_segment_claims"], 1)
        self.assertEqual(timing["packed_small_segment"]["small_cell_grants"], 16)
        self.assertEqual(timing["packed_small_segment"]["client_cache_hits"], 12)
        self.assertEqual(timing["packed_small_segment"]["client_dynamic_mmap_calls"], 2)
        self.assertEqual(timing["packed_small_segment"]["client_dynamic_mmap_ns"], 28)
        self.assertEqual(timing["packed_small_segment"]["client_protection_calls"], 17)
        self.assertEqual(timing["packed_small_segment"]["client_protection_ns"], 32)
        writer_host = timing["persistence"]["writer_host"]
        self.assertEqual(writer_host["writer_host_persist_jobs_queued"], 16)
        self.assertEqual(writer_host["writer_host_persist_jobs_completed"], 16)
        self.assertEqual(writer_host["writer_host_msync_persist_jobs"], 2)
        self.assertEqual(writer_host["writer_host_local_persist_ranges"], 16)
        self.assertEqual(writer_host["writer_host_local_persist_bytes"], 65536)
        self.assertEqual(writer_host["writer_host_receipt_batches_submitted"], 2)
        self.assertEqual(writer_host["writer_host_receipt_items_submitted"], 16)
        self.assertEqual(writer_host["writer_host_authority_apply_wait_polls"], 12)
        self.assertEqual(writer_host["writer_host_authority_apply_wait_ns"], 70)
        self.assertIn("nested", timing["interpretation"])

    def test_packed_small_segment_evidence_distinguishes_v6_and_v7(self):
        packed_audit = {
            "backend_fresh_format_scan_bytes": 0,
            "backend_publication_slots_reset": 0,
            "startup_small_runtime_segments": 0,
            "startup_small_runtime_cells": 0,
            "small_segment_claims": 1,
            "small_segment_claim_persist_ns": 10,
            "small_cell_grants": 8,
            "small_cell_commits": 8,
            "small_cell_payload_bytes": 8 * 3901,
            "small_cell_payload_persist_bytes": 0,
            "small_cell_allocator_persist_barriers": 0,
            "legacy_small_arena_reserve_calls": 0,
            "legacy_small_arena_reserve_ns": 0,
            "writer_persisted_direct_bytes": 8 * 3901,
        }
        self.runner.validate_packed_small_segment_audit(
            packed_audit, 0, expected_packed_small_segments=True
        )
        with self.assertRaisesRegex(ValueError, "packed small-cell grants"):
            self.runner.validate_packed_small_segment_audit(
                {**packed_audit, "small_cell_grants": 0},
                0,
                expected_packed_small_segments=True,
            )
        with self.assertRaisesRegex(ValueError, "payload durability"):
            self.runner.validate_packed_small_segment_audit(
                {**packed_audit, "writer_persisted_direct_bytes": 0},
                0,
                expected_packed_small_segments=True,
            )

        legacy_audit = {
            "backend_fresh_format_scan_bytes": 0,
            "backend_publication_slots_reset": 0,
            "startup_small_runtime_segments": 0,
            "startup_small_runtime_cells": 0,
            "small_segment_claims": 0,
            "small_segment_claim_persist_ns": 0,
            "small_cell_grants": 0,
            "small_cell_commits": 0,
            "small_cell_payload_bytes": 0,
            "small_cell_payload_persist_bytes": 0,
            "small_cell_allocator_persist_barriers": 0,
            "legacy_small_arena_reserve_calls": 2,
            "legacy_small_arena_reserve_ns": 100,
            "writer_persisted_direct_bytes": 8 * 3901,
        }
        self.runner.validate_packed_small_segment_audit(
            legacy_audit, 0, expected_packed_small_segments=False
        )
        with self.assertRaisesRegex(ValueError, "legacy 4 KiB reserve"):
            self.runner.validate_packed_small_segment_audit(
                {**legacy_audit, "legacy_small_arena_reserve_calls": 0},
                0,
                expected_packed_small_segments=False,
            )

        packed_summaries = [{"stats": {
            "small_segment_cache_hits": 7,
            "small_segment_dynamic_mmap_calls": 1,
            "small_segment_dynamic_mmap_ns": 10,
            "small_segment_mapped_owner_segments": 1,
            "small_segment_protection_calls": 16,
            "small_segment_protection_ns": 20,
        }}]
        self.runner.validate_packed_small_segment_client_evidence(
            packed_summaries, expected_packed_small_segments=True
        )
        with self.assertRaisesRegex(ValueError, "duplicate dynamic mmap"):
            self.runner.validate_packed_small_segment_client_evidence(
                [{"stats": {
                    **packed_summaries[0]["stats"],
                    "small_segment_dynamic_mmap_calls": 2,
                }}],
                expected_packed_small_segments=True,
            )
        self.runner.validate_packed_small_segment_client_evidence(
            [{"stats": {}}], expected_packed_small_segments=False
        )

    def test_packed_small_validation_is_only_required_for_small_file_stages(self):
        for stage in ("easy-smoke", "hard-smoke", "rnd4k"):
            self.assertIsNone(
                self.runner.packed_small_validation_expectation(stage, "on", "none")
            )
        for stage in ("tiny", "metadata-smoke", "scc", "standard"):
            self.assertTrue(
                self.runner.packed_small_validation_expectation(stage, "on", "none")
            )
            self.assertFalse(
                self.runner.packed_small_validation_expectation(stage, "off", "none")
            )
        self.assertIsNone(
            self.runner.packed_small_validation_expectation(
                "tiny", "on", "reject-active-clean-retirement"
            )
        )

    def test_host_cost_window_closes_once_before_observation_export(self):
        result = {}
        self.assertTrue(self.runner.record_host_process_cost_window(
            result, {}, {}, 100, 200
        ))
        self.assertFalse(self.runner.record_host_process_cost_window(
            result, {}, {}, 100, 900
        ))
        self.assertEqual(result["host_process_cost"]["wall_ns"], 100)

        source = RUNNER.read_text(encoding="utf-8")
        launch = source.index('result["commands"]["mpi"] = launch_mpi(')
        close = source.index(
            "record_host_process_cost_window(", launch
        )
        export = source.index("dump_observations_safely(", launch)
        self.assertLess(close, export)

    def test_cq_attribution_keeps_inner_intervals_nested(self):
        evidence = cxl_serving_evidence(0)[0]
        evidence["client_timing"].update({"calls": 1, "cq_wait_ns": 100})
        evidence["authority_timing"].update({
            "successful_request_poll_ns": 20,
            "authority_queue_wait_ns": 10,
            "dispatcher_backend_ns": 15,
            "completion_publication_wait_ns": 1,
            "cqe_publish_ns": 4,
        })
        timing = self.runner.legofs_timing_breakdown(
            [{"stats": {}, "cxl_serving_evidence": [evidence]}],
            {"audit": {}},
            wall_ns=100,
            client_count=1,
        )
        attribution = timing["transport_cq_attribution"]
        self.assertEqual(attribution["outer_cq_wait_ns"], 100)
        self.assertEqual(attribution["inner_authority_ns"], 50)
        self.assertEqual(attribution["residual_ns"], 50)
        self.assertEqual(attribution["residual_share_of_outer"], 0.5)
        self.assertIn("nested", attribution["interpretation"])

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

    def test_strict_persistency_proof_accepts_writer_host_receipt_chain(self):
        self.paths.bundle.mkdir(parents=True)
        direct = []
        for capture, event in ((100, "coherent_sealed"), (400, "writer_host_persisted")):
            direct.append(json.dumps({
                "host_capture_ns": capture,
                "line": "BADFS_DIRECT_MAP_TRACE_JSON " + json.dumps({
                    "schema_version": "badfs.direct-map-trace.v1",
                    "event": event,
                    "access": "read_write",
                    "rc": 0,
                    "owner": 77,
                    "op_id": 9,
                    "offset": 4096,
                    "length": 4096,
                }, separators=(",", ":")),
            }))
        self.paths.event_log("client0").write_text(
            "\n".join(direct) + "\n", encoding="utf-8"
        )
        server_lines = []
        for capture, event in ((200, "store_direct_begin"), (300, "store_direct_success")):
            server_lines.append(json.dumps({
                "host_capture_ns": capture,
                "line": "BADFS_LIFECYCLE_TRACE_JSON " + json.dumps({
                    "schema_version": "badfs.lifecycle.v1",
                    "event": event,
                    "owner": 77,
                    "op_id": 9,
                    "mapping_offset": 4096,
                    "mapping_length": 4096,
                    "fault_point": "coherent_seal",
                }, separators=(",", ":")),
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
        self.assertEqual(
            proof["writer_persisted"]["first"]["proof_kind"],
            "writer_host_receipt",
        )

        late = json.loads(server_lines[1])
        late["host_capture_ns"] = 500
        self.paths.event_log("server0").write_text(
            server_lines[0] + "\n" + json.dumps(late) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "no exact writer-persisted"):
            self.runner.strict_persistency_proof(
                self.paths, [{"owner": 77, "endpoint": 3}], 1
            )

    def test_strict_persistency_proof_accepts_exact_authority_dependency_chain(self):
        self.paths.bundle.mkdir(parents=True)
        direct = {
            "schema_version": "badfs.direct-map-trace.v1",
            "event": "coherent_sealed",
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
        lifecycle_base = {
            "schema_version": "badfs.lifecycle.v1",
            "owner": 77,
            "op_id": 9,
            "extent_id": 11,
            "extent_generation": 2,
            "layout_epoch": 5,
        }
        dependency_base = {
            **lifecycle_base,
            "mapping_offset": 4224,
            "mapping_length": 64,
            "dependency_range_index": 0,
            "dependency_range_count": 1,
            "dependency_range_bytes": 64,
            "dependency_range_set_digest": "ab" * 32,
        }
        lifecycle = [
            (200, {
                **lifecycle_base,
                "event": "store_direct_begin",
                "mapping_offset": 4096,
                "mapping_length": 4096,
                "fault_point": "coherent_seal",
            }),
            (300, {
                **lifecycle_base,
                "event": "store_direct_success",
                "mapping_offset": 4096,
                "mapping_length": 4096,
                "fault_point": "dirty_range_ownership",
            }),
            (700, {
                **dependency_base,
                "event": "payload_dependency_acquired",
                "fault_point": "authority_coherent_acquire",
                "persist_state": "not_persisted",
            }),
            (1000, {
                **dependency_base,
                "event": "payload_dependency_persisted",
                "fault_point": "authority_payload_persist",
                "persist_state": "persisted",
            }),
        ]
        self.paths.event_log("server0").write_text(
            "".join(json.dumps({
                "host_capture_ns": capture,
                "line": "BADFS_LIFECYCLE_TRACE_JSON "
                + json.dumps(record, separators=(",", ":")),
            }) + "\n" for capture, record in lifecycle),
            encoding="utf-8",
        )
        coherence = [
            {"event": "request", "opcode": "GETS", "src_host": 0,
             "line_address": 4224, "status": "OK", "monotonic_ns": 500},
            {"event": "snoop_send", "opcode": "SNP_DATA_DOWNGRADE", "dst_host": 4,
             "line_address": 4224, "snoop_id": 6, "session_id": 8,
             "epoch": 2, "monotonic_ns": 510},
            {"event": "snoop_ack", "opcode": "SNOOP_ACK", "src_host": 4,
             "line_address": 4224, "snoop_id": 6, "session_id": 8,
             "epoch": 2, "ack_strength": "MODEL", "dirty_data": True,
             "payload_len": 64, "status": "OK", "monotonic_ns": 550},
            {"event": "dirty_completion", "opcode": "SNOOP_ACK", "src_host": 4,
             "line_address": 4224, "snoop_id": 6, "session_id": 8,
             "epoch": 2, "ack_strength": "MODEL", "dirty_data": True,
             "payload_len": 64, "status": "OK", "monotonic_ns": 560},
            {"event": "request", "opcode": "FENCE", "src_host": 0,
             "session_id": 1, "request_id": 22, "status": "OK",
             "monotonic_ns": 800},
            {"event": "persistence_fence_completion", "opcode": "FENCE",
             "src_host": 0, "session_id": 1, "request_id": 22,
             "status": "OK", "monotonic_ns": 900},
        ]
        coherence = [{"schema_version": 1, **record} for record in coherence]
        self.paths.coherence.write_text(
            "".join(json.dumps(record) + "\n" for record in coherence),
            encoding="utf-8",
        )

        proof = self.runner.strict_persistency_proof(
            self.paths, [{"owner": 77, "endpoint": 3}], 1
        )
        self.assertEqual(proof["authority_acquired"]["count"], 1)
        self.assertEqual(proof["authority_acquired"]["dirty_transfer_count"], 1)
        self.assertEqual(
            proof["authority_acquired"]["first"]["dependency_range_set_digest"],
            "ab" * 32,
        )

        # A coherent GETS remains the authority's correctness contract when
        # the home agent can satisfy it from already-current memory and emits
        # no implementation-specific back-invalidation transaction.
        no_snoop = [
            record for record in coherence
            if record["event"] in ("request", "persistence_fence_completion")
        ]
        self.paths.coherence.write_text(
            "".join(json.dumps(record) + "\n" for record in no_snoop),
            encoding="utf-8",
        )
        no_snoop_proof = self.runner.strict_persistency_proof(
            self.paths, [{"owner": 77, "endpoint": 3}], 1
        )
        self.assertEqual(no_snoop_proof["authority_acquired"]["count"], 1)
        self.assertEqual(
            no_snoop_proof["authority_acquired"]["dirty_transfer_count"], 0
        )

        lifecycle[-1][1]["dependency_range_set_digest"] = "cd" * 32
        self.paths.event_log("server0").write_text(
            "".join(json.dumps({
                "host_capture_ns": capture,
                "line": "BADFS_LIFECYCLE_TRACE_JSON "
                + json.dumps(record, separators=(",", ":")),
            }) + "\n" for capture, record in lifecycle),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "no exact writer-persisted"):
            self.runner.strict_persistency_proof(
                self.paths, [{"owner": 77, "endpoint": 3}], 1
            )

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
            "authority_acquired": {"count": 0, "dirty_transfer_count": 0},
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

        authority = {
            "writer_persisted": {"count": 0},
            "authority_acquired": {"count": 1, "dirty_transfer_count": 1},
            "backinvalidation": {"count": 0},
        }
        self.runner.validate_tiny_provider_counters(
            {
                "gets": 1,
                "request_fence": 1,
                "persistence_fence_completions": 1,
                "snp_data_downgrade": 1,
                "model_acks": 1,
                "dirty_data_completions": 1,
            },
            authority,
        )
        with self.assertRaisesRegex(ValueError, "dirty-data snoop"):
            self.runner.validate_tiny_provider_counters(
                {
                    "gets": 1,
                    "request_fence": 1,
                    "persistence_fence_completions": 1,
                    "model_acks": 1,
                    "dirty_data_completions": 1,
                },
                authority,
            )

        inspection = {"audit": {"writer_persisted_direct_items": 2}}
        self.runner.validate_inspection_provider_counters(
            {"request_fence": 1, "persistence_fence_completions": 1},
            inspection,
        )
        with self.assertRaisesRegex(ValueError, "persistence_fence_completions"):
            self.runner.validate_inspection_provider_counters(
                {"request_fence": 1}, inspection
            )


if __name__ == "__main__":
    unittest.main()
