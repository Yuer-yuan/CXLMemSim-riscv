import importlib.util
import json
import pathlib
import struct
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "legofs_type3_2node.py"
OBSERVER = ROOT / "scripts" / "legofs_observe.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("legofs_type3_2node_evidence", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_observer():
    spec = importlib.util.spec_from_file_location("legofs_observe_contract", OBSERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LegofsEvidenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runner = load_runner()
        cls.observer = load_observer()

    def test_observation_profile_digest_and_uninstrumented_coverage_are_explicit(self):
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
                "major": 1, "minor": self.observer.SCHEMA_MINOR,
                "event_record_bytes": 64,
                "digest": self.observer.SCHEMA_DIGEST,
            },
        }
        digest = self.observer.profile_digest(profile)
        profile["profile_sha256"] = digest
        self.assertEqual(self.observer.profile_digest(profile), digest)

        summary = self.observer.component_summary([], {"mode": "off"})
        self.assertTrue(summary["observation_valid"])
        self.assertTrue(all(
            item["metrics"] is None
            for item in summary["components"]
            if item["coverage"] == "observation_off"
        ))

    def test_observation_histogram_percentiles_are_decoded_not_invented(self):
        data = bytearray(self.observer.METRIC_RECORD_BYTES)
        struct.pack_into("<Q", data, 0, 100)
        struct.pack_into("<Q", data, 8, 100)
        struct.pack_into("<H", data, self.observer.METRIC_HEADER_BYTES, 50)
        struct.pack_into("<H", data, self.observer.METRIC_HEADER_BYTES + 20, 45)
        struct.pack_into("<H", data, self.observer.METRIC_HEADER_BYTES + 40, 5)
        metric = self.observer.decode_metric(bytes(data), 0)
        percentiles = self.observer.metric_percentiles(metric)
        self.assertEqual(percentiles["status"], "ok")
        self.assertEqual(percentiles["p50_ns"], 102)
        self.assertEqual(percentiles["p95_ns"], 152)
        self.assertEqual(percentiles["p99_ns"], 227)

        corrupt = dict(metric)
        corrupt["duration_samples"] = 101
        self.assertEqual(
            self.observer.metric_percentiles(corrupt)["status"],
            "histogram_count_mismatch",
        )

        compact = self.observer.decode_compact_metric(bytes(64), 0)
        merged = {}
        self.observer.add_metric(merged, metric)
        self.observer.add_metric(merged, compact)
        self.assertEqual(
            self.observer.metric_percentiles(merged)["status"],
            "not_available_compact_metric",
        )
        self.assertEqual(
            self.observer.metric_percentiles({})["status"], "no_samples"
        )

    def test_observation_metric_banks_keep_stable_stage_ownership(self):
        self.assertEqual(len(self.observer.STAGES), 61)
        self.assertEqual(len(self.observer.STAGE_COMPONENTS), 61)
        expected = {
            "hook_to_client": "syscall_intercept",
            "namespace_resolution": "client_semantics",
            "cq_detection_bound": "cxl_client_transport",
            "scheduler_wait": "authority_progress",
            "metadata_lock_hold": "namespace_metadata",
            "arena_state_persist": "data_arena_extent",
            "persistence_barrier": "persistence",
            "startup_lane_provision": "startup_recovery",
            "transport_remote_wait": "cxl_client_transport",
            "authority_request_service": "authority_progress",
            "completion_publication_wait": "authority_progress",
        }
        ownership = dict(zip(
            self.observer.STAGES, self.observer.STAGE_COMPONENTS
        ))
        for stage, component in expected.items():
            self.assertEqual(ownership[stage], component)

    def test_observation_report_accepts_disabled_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.observer.analyze_observation_files(
                [], pathlib.Path(directory), config={"mode": "default"}
            )
            self.assertTrue(result["observation_valid"])
            report = (pathlib.Path(directory) / "report.md").read_text()
            self.assertIn("SQ detection upper-bound sum: `0` ns", report)
            self.assertIn("CQ detection upper-bound sum: `0` ns", report)

    def test_o2_transport_ledger_requires_four_equal_request_multisets(self):
        digest = {
            "count": 2, "xor0": 3, "xor1": 5, "sum0": 11, "sum1": 13,
        }
        empty = {
            "count": 0, "xor0": 0, "xor1": 0, "sum0": 0, "sum1": 0,
        }

        def metric(stage, duration):
            return {
                "component": "authority_progress",
                "stage": stage,
                "calls": 2,
                "duration_samples": 2,
                "sampled_duration_sum_ns": duration,
                "sampled_min_ns": duration // 2,
                "sampled_max_ns": duration // 2,
                "objects": 2,
                "bytes": 0,
                "successes": 2,
                "errors": 0,
                "retries": 0,
                "histogram_underflow": 2,
                "histogram_overflow": 0,
                "histogram_saturation": 0,
                "histogram_bins": [0] * self.observer.HISTOGRAM_BINS,
            }

        client = {
            "header": {
                "role": "client-rank",
                "request_digests": {
                    "submit": dict(digest), "observe": dict(empty),
                    "publish": dict(empty), "consume": dict(digest),
                },
            },
            "stage_matrix": [
                {
                    **metric("transport_remote_wait", 1000),
                    "component": "cxl_client_transport",
                },
                {
                    **metric("cq_active_poll", 350),
                    "component": "cxl_client_transport",
                },
                {
                    **metric("cq_sleep", 550),
                    "component": "cxl_client_transport",
                },
            ],
        }
        server_stages = [metric("authority_request_service", 600)]
        server_stages.extend(metric(stage, duration) for stage, duration in (
            ("admission", 20), ("scheduler_wait", 30),
            ("dispatcher_decode", 40), ("dispatcher_route", 400),
            ("dispatcher_encode", 50), ("completion_publication_wait", 30),
            ("cqe_publish", 30),
        ))
        server = {
            "header": {
                "role": "server",
                "request_digests": {
                    "submit": dict(empty), "observe": dict(digest),
                    "publish": dict(digest), "consume": dict(empty),
                },
            },
            "stage_matrix": server_stages,
        }
        ledger = self.observer.transport_ledger([client, server])
        self.assertTrue(ledger["observation_valid"])
        self.assertTrue(ledger["request_multiset_match"])
        self.assertEqual(ledger["client_remote_wait_ns"], 1000)
        self.assertEqual(ledger["correlated_server_request_ns"], 600)
        self.assertEqual(ledger["transport_handoff_ns"], 400)
        self.assertEqual(
            ledger["authority_exclusive_ledger"]["unaccounted_ns"], 0
        )
        self.assertEqual(
            ledger["client_wait_exclusive_ledger"]["unaccounted_ns"], 100
        )

        server["header"]["request_digests"]["observe"]["count"] = 1
        rejected = self.observer.transport_ledger([client, server])
        self.assertFalse(rejected["observation_valid"])
        self.assertIn("request_multiset_mismatch", rejected["validity_reasons"])

    def test_o2_request_digest_combination_preserves_u64_wrapping(self):
        maximum = (1 << 64) - 1
        empty = {
            "count": 0, "xor0": 0, "xor1": 0, "sum0": 0, "sum1": 0,
        }

        def arena(sum0, sum1):
            digest = {
                "count": 1, "xor0": 7, "xor1": 9,
                "sum0": sum0, "sum1": sum1,
            }
            return {
                "header": {
                    "request_digests": {
                        "submit": digest, "observe": dict(empty),
                        "publish": dict(empty), "consume": dict(empty),
                    },
                },
            }

        combined = self.observer._combined_request_digests([
            arena(maximum, maximum - 1), arena(maximum, maximum),
        ])
        self.assertEqual(combined["submit"]["count"], 2)
        self.assertEqual(combined["submit"]["xor0"], 0)
        self.assertEqual(combined["submit"]["xor1"], 0)
        self.assertEqual(combined["submit"]["sum0"], maximum - 1)
        self.assertEqual(combined["submit"]["sum1"], maximum - 2)

    def test_paired_gate_uses_median_mad_and_relative_mad(self):
        ratios = [0.98, 1.0, 1.01, 1.02, 1.20]
        stats = self.observer.paired_metric_stats(ratios)
        self.assertAlmostEqual(stats["median_ratio"], 1.01)
        self.assertAlmostEqual(stats["mad"], 0.01)
        self.assertAlmostEqual(stats["rMAD"], 1.4826 * 0.01 / 1.01)
        self.assertEqual(stats["median_regression"], 0.0)
        self.assertEqual(
            stats["primary_improvement_threshold"],
            max(0.05, 2 * stats["rMAD"]),
        )

        regressed = self.observer.paired_metric_stats(
            [0.94, 0.95, 0.95, 0.96, 0.97]
        )
        self.assertAlmostEqual(regressed["median_regression"], 0.05)

    def test_paired_gate_rejects_nonpositive_or_empty_ratios(self):
        for ratios in ([], [1.0, 0.0], [-1.0, 1.0]):
            with self.subTest(ratios=ratios):
                with self.assertRaises(ValueError):
                    self.observer.paired_metric_stats(ratios)

    def test_arm_rmad_is_computed_from_each_side(self):
        baseline = self.observer.arm_metric_stats([80.0, 100.0, 120.0])
        candidate = self.observer.arm_metric_stats([120.0, 150.0, 180.0])
        self.assertEqual(baseline["median"], 100.0)
        self.assertEqual(candidate["median"], 150.0)
        self.assertAlmostEqual(baseline["rMAD"], 1.4826 * 20.0 / 100.0)
        self.assertAlmostEqual(candidate["rMAD"], 1.4826 * 30.0 / 150.0)

        for values in ([], [1.0, 0.0], [-1.0, 1.0]):
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    self.observer.arm_metric_stats(values)

    def test_candidate_work_signature_includes_intercepted_syscalls(self):
        def result(handled):
            return {"posix_path_summaries": [{
                "mpi_rank": 0,
                "stats": {
                    "open_ops": 1, "close_ops": 1,
                    "read_ops": 0, "write_ops": 0,
                },
                "syscall_classification": {"syscalls": [{
                    "number": 79,
                    "name": "newfstatat",
                    "handled": handled,
                    "rejected": 0,
                    "forbidden_badfs_forward": 0,
                    "non_badfs_forward": 99,
                }]},
            }]}

        baseline = self.observer._candidate_work_signature(result(35))
        same_work = self.observer._candidate_work_signature(result(35))
        less_work = self.observer._candidate_work_signature(result(34))
        self.assertEqual(baseline, same_work)
        self.assertNotEqual(baseline, less_work)

    def test_candidate_gate_accepts_signal_and_explained_protected_noise(self):
        platform_names = (
            "cxlmemsim_server", "io500", "linux", "opensbi", "qemu", "u_boot",
        )

        def result(mode, read_ratio, write_elapsed, arena_seconds):
            fused = mode == "candidate"
            evidence = {
                "filesystem_tcp_requests_after_cxl_ready": 0,
                "legacy_tarpc_calls_after_cxl_ready": 0,
                "blob_tcp_bytes_after_cxl_ready": 0,
                "transport_fallbacks_after_cxl_ready": 0,
                "unsupported_serving_calls_after_cxl_ready": 0,
                "fused_open_attempts": 64 if fused else 0,
                "fused_open_pins": 64 if fused else 0,
                "fused_open_fallbacks": 0,
                "fused_close_attempts": 64 if fused else 0,
                "fused_close_snapshot_releases": 64 if fused else 0,
                "dispatches_by_opcode": {"59": 64, "60": 64} if fused else {"30": 64},
            }
            summaries = []
            for rank in range(2):
                summaries.append({
                    "mpi_rank": rank,
                    "stats": {
                        "open_ops": 64, "close_ops": 64,
                        "read_ops": 32, "write_ops": 32,
                        "read_bytes": 124832, "write_bytes": 124832,
                        "unlink_ops": 1, "rename_ops": 0,
                    },
                    "cxl_serving_evidence": [evidence],
                })
            artifacts = {
                name: {"sha256": f"frozen-{name}"}
                for name in platform_names
            }
            artifacts["payload"] = {"sha256": "frozen-payload"}
            return {
                "schema_version": "legofs.riscv.io500.v2",
                "status": "passed",
                "filesystem_valid": True,
                "observation_valid": True,
                "guest_visible_cxl_evidence": True,
                "functional_model_only": True,
                "physical_hardware_evidence": False,
                "stage": "metadata-smoke",
                "topology": {
                    "client_guests": 2,
                    "server_guests": 1,
                    "legofs_open_mode": "fused_pin" if fused else "legacy_split",
                    "legofs_direct_metadata_mode": "cxl" if fused else "rpc",
                },
                "build": {"manifest": {"artifacts": artifacts}},
                "io500": {"metrics": {"phases": [
                    {
                        "name": "mdtest-hard-read", "unit": "kIOPS",
                        "score": read_ratio, "seconds": 30.0 / read_ratio,
                    },
                    {
                        "name": "mdtest-hard-write", "unit": "kIOPS",
                        "score": 1.0 / write_elapsed, "seconds": write_elapsed,
                    },
                ]}},
                "posix_path_summaries": summaries,
                "coherence_final_stats": {
                    "timeouts": 0, "protocol_errors": 0,
                    "delivery_failures": 0, "active_bindings": 0,
                    "request_fence": 8,
                    "persistence_fence_completions": 8,
                },
                "lifecycle_inspection": {"audit": {
                    "pending_operations": 0, "pending_arena_slots": 0,
                    "active_read_leases": 0, "pending_payload_dependencies": 0,
                    "direct_write_commit_items": 64,
                    "writer_persisted_direct_items": 64,
                    "visibility_durability": {"published_v": 8, "durable_d": 8},
                }},
                "legofs_timing": {
                    "aggregate_rank_wall_ns": int(write_elapsed * 2.5e9),
                    "client_timed_intervals": {
                        "metadata_rpc_wait_ns": int(write_elapsed * 0.25e9),
                        "read_control_rpc_wait_ns": int(write_elapsed * 0.50e9),
                        "write_control_rpc_wait_ns": int(write_elapsed * 0.75e9),
                    },
                    "client_timed_intervals_ns": int(write_elapsed * 1.5e9),
                    "arena_acquire": {
                        "arena_acquire_backend_reserve_ns": int(arena_seconds * 1e9),
                        "arena_acquire_total_ns": int((arena_seconds + 1.0) * 1e9),
                        "arena_acquire_calls": 2,
                    },
                    "direct_write": {
                        "direct_write_commit_groups": 8,
                        "direct_write_commit_items": 64,
                        "direct_write_prepare_ns": int(write_elapsed * 0.02e9),
                        "direct_write_payload_persist_ns": int(write_elapsed * 0.03e9),
                        "direct_write_state_build_ns": int(write_elapsed * 0.02e9),
                        "direct_write_state_persist_ns": int(write_elapsed * 0.04e9),
                        "direct_write_publish_ns": int(write_elapsed * 0.01e9),
                        "direct_write_trace_ns": int(write_elapsed * 0.01e9),
                        "direct_write_total_ns": int(write_elapsed * 0.18e9),
                    },
                    "transport_client": {
                        "calls": 547 if fused else 675,
                        "cq_wait_ns": int(write_elapsed * 1.25e9),
                    },
                    "transport_authority": {
                        "sqe_consumed": 547 if fused else 675,
                        "cqe_published": 547 if fused else 675,
                        "authority_queue_wait_ns": int(write_elapsed * 0.10e9),
                        "successful_request_poll_ns": int(write_elapsed * 0.20e9),
                        "dispatcher_backend_ns": int(write_elapsed * 0.30e9),
                        "completion_publication_wait_ns": int(write_elapsed * 0.05e9),
                        "cqe_publish_ns": int(write_elapsed * 0.10e9),
                    },
                },
                "cleanup": {"owned_processes_remaining": []},
            }

        baseline_write = [71.5833, 56.6571, 78.6281, 77.8789, 65.3101]
        candidate_write = [58.5793, 67.1505, 77.0287, 54.0768, 68.1776]
        baseline_arena = [42.380068, 25.658421, 48.682632, 46.091194, 34.504681]
        candidate_arena = [28.139965, 35.907564, 46.448529, 23.744040, 35.241331]
        read_ratios = [1.589486, 1.541650, 1.494882, 1.528983, 1.600828]
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            baseline_paths = []
            candidate_paths = []
            for index in range(5):
                baseline_path = root / f"baseline-{index}.json"
                candidate_path = root / f"candidate-{index}.json"
                baseline_path.write_text(json.dumps(result(
                    "baseline", 1.0, baseline_write[index], baseline_arena[index]
                )), encoding="utf-8")
                candidate_path.write_text(json.dumps(result(
                    "candidate", read_ratios[index], candidate_write[index],
                    candidate_arena[index]
                )), encoding="utf-8")
                baseline_paths.append(baseline_path)
                candidate_paths.append(candidate_path)

            gate = self.observer.paired_candidate_gate(
                baseline_paths,
                candidate_paths,
                primary_phase="mdtest-hard-read",
                protected_phases=["mdtest-hard-write"],
                explanatory_counters={
                    "mdtest-hard-write": (
                        "legofs_timing.arena_acquire."
                        "arena_acquire_backend_reserve_ns"
                    ),
                },
                minimum_primary_improvement=0.20,
                minimum_command_reduction=128,
            )
            self.assertEqual(gate["classification"], "ACCEPTED")
            self.assertGreater(gate["primary"]["median_improvement"], 0.5)
            self.assertFalse(gate["primary"]["high_noise"])
            self.assertEqual(gate["primary"]["positive_pair_count"], 5)
            self.assertEqual(
                gate["primary"]["acceptance_threshold"],
                max(
                    0.20,
                    2 * max(
                        gate["primary"]["baseline_arm"]["rMAD"],
                        gate["primary"]["candidate_arm"]["rMAD"],
                    ),
                ),
            )
            self.assertTrue(gate["protected"][0]["raw_race_signal"])
            self.assertTrue(gate["protected"][0]["race_explained"])
            self.assertGreater(
                gate["protected"][0]["explanation"]["elapsed_delta_correlation"],
                0.99,
            )
            self.assertEqual(
                gate["mechanism"]["paired_command_reductions"], [128] * 5
            )
            self.assertTrue(gate["time_closure"]["all_pairs_closed"])
            first_closure = gate["time_closure"]["pairs"][0]
            self.assertEqual(
                first_closure["wall_delta_ns"],
                first_closure["client_timed_delta_ns"]
                + first_closure["rank_wall_remainder_delta_ns"],
            )
            self.assertEqual(
                first_closure["baseline"]["client_control"]["remainder_ns"],
                0,
            )
            self.assertTrue(
                first_closure["baseline"]["transport"]["closed"]
            )
            self.assertTrue(first_closure["baseline"]["arena"]["closed"])

            pilot = self.observer.paired_candidate_gate(
                baseline_paths[:1],
                candidate_paths[:1],
                primary_phase="mdtest-hard-read",
                protected_phases=["mdtest-hard-write"],
                minimum_primary_improvement=0.20,
                minimum_command_reduction=128,
            )
            self.assertEqual(pilot["classification"], "PROMISING")

            protected_risk_pilot = self.observer.paired_candidate_gate(
                baseline_paths[1:2],
                candidate_paths[1:2],
                primary_phase="mdtest-hard-read",
                protected_phases=["mdtest-hard-write"],
                minimum_primary_improvement=0.20,
                minimum_command_reduction=128,
            )
            self.assertEqual(
                protected_risk_pilot["classification"],
                "PROMISING_PROTECTED_RISK",
            )

            noisy_two_pair_pilot = self.observer.paired_candidate_gate(
                baseline_paths[:2],
                candidate_paths[:2],
                primary_phase="mdtest-hard-read",
                protected_phases=["mdtest-hard-write"],
                minimum_primary_improvement=0.20,
                minimum_command_reduction=128,
            )
            self.assertNotEqual(
                noisy_two_pair_pilot["classification"], "INCONCLUSIVE_RACE"
            )

            packed_baseline = json.loads(baseline_paths[0].read_text())
            packed_candidate = json.loads(candidate_paths[0].read_text())
            packed_baseline["topology"].update({
                "lifecycle_pool_layout": "v6-variable-extents",
                "small_segment_count_per_authority": 0,
            })
            packed_candidate["topology"].update({
                "lifecycle_pool_layout": "v7-packed-small-segments",
                "small_segment_count_per_authority": 16,
            })
            packed_baseline["legofs_timing"]["packed_small_segment"] = {
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
                "legacy_small_arena_reserve_ns": 1_000,
                "client_cache_hits": 0,
                "client_dynamic_mmap_calls": 0,
                "client_dynamic_mmap_ns": 0,
                "client_mapped_owner_segments": 0,
                "client_protection_calls": 0,
                "client_protection_ns": 0,
            }
            packed_candidate["legofs_timing"]["packed_small_segment"] = {
                "backend_fresh_format_scan_bytes": 0,
                "backend_publication_slots_reset": 0,
                "startup_small_runtime_segments": 0,
                "startup_small_runtime_cells": 0,
                "small_segment_claims": 1,
                "small_segment_claim_persist_ns": 2_000,
                "small_cell_grants": 64,
                "small_cell_commits": 64,
                "small_cell_payload_bytes": 64 * 3901,
                "small_cell_payload_persist_bytes": 0,
                "small_cell_allocator_persist_barriers": 0,
                "legacy_small_arena_reserve_calls": 0,
                "legacy_small_arena_reserve_ns": 0,
                "client_cache_hits": 63,
                "client_dynamic_mmap_calls": 1,
                "client_dynamic_mmap_ns": 3_000,
                "client_mapped_owner_segments": 1,
                "client_protection_calls": 129,
                "client_protection_ns": 4_000,
            }
            packed_candidate["lifecycle_inspection"]["audit"].update({
                "writer_persisted_direct_bytes": 64 * 3901,
            })
            packed_baseline_path = root / "packed-baseline.json"
            packed_candidate_path = root / "packed-candidate.json"
            packed_baseline_path.write_text(
                json.dumps(packed_baseline), encoding="utf-8"
            )
            packed_candidate_path.write_text(
                json.dumps(packed_candidate), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "normalized topology"):
                self.observer.paired_candidate_gate(
                    [packed_baseline_path],
                    [packed_candidate_path],
                    primary_phase="mdtest-hard-read",
                    protected_phases=["mdtest-hard-write"],
                )
            packed_gate = self.observer.paired_candidate_gate(
                [packed_baseline_path],
                [packed_candidate_path],
                primary_phase="mdtest-hard-read",
                protected_phases=["mdtest-hard-write"],
                allowed_topology_differences={
                    "lifecycle_pool_layout",
                    "small_segment_count_per_authority",
                },
            )
            self.assertEqual(
                packed_gate["isolation"]["allowed_topology_differences"],
                ["lifecycle_pool_layout", "small_segment_count_per_authority"],
            )
            self.assertTrue(packed_gate["time_closure"]["all_pairs_closed"])
            self.assertEqual(
                packed_gate["mechanism"]["candidate_path_counters"]
                ["small_cell_commits"],
                64,
            )

            missing_mechanism = json.loads(json.dumps(packed_candidate))
            missing_mechanism["legofs_timing"].pop("packed_small_segment")
            missing_mechanism_path = root / "packed-candidate-missing-mechanism.json"
            missing_mechanism_path.write_text(
                json.dumps(missing_mechanism), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "packed-small-segment mechanism"):
                self.observer.paired_candidate_gate(
                    [packed_baseline_path],
                    [missing_mechanism_path],
                    primary_phase="mdtest-hard-read",
                    protected_phases=["mdtest-hard-write"],
                    allowed_topology_differences={
                        "lifecycle_pool_layout",
                        "small_segment_count_per_authority",
                    },
                )

    def records(self):
        direct = [{
            "schema_version": "badfs.direct-map-trace.v1",
            "event": "drop", "access": "read_write", "op_id": 17,
            "offset": 0x4000, "length": 0x1000, "rc": 0,
        }]
        lifecycle = [
            {
                "schema_version": "badfs.lifecycle.v1",
                "event": "store_direct_begin", "op_id": 17,
                "mapping_offset": 0x4000, "mapping_length": 0x1000,
            },
            {
                "schema_version": "badfs.lifecycle.v1",
                "event": "store_direct_success", "op_id": 17,
                "mapping_offset": 0x4000, "mapping_length": 0x1000,
            },
        ]
        coherence = [
            {
                "schema_version": 1, "event": "snoop_send",
                "opcode": "SNP_DATA_INV", "src_host": 0xFFFF, "dst_host": 1,
                "session_id": 2, "epoch": 7, "snoop_id": 91,
                "line_address": 0x4080, "monotonic_ns": 20,
                "payload_len": 0, "status": "OK", "ack_strength": "NONE",
                "dirty_data": False,
            },
            {
                "schema_version": 1, "event": "snoop_ack",
                "opcode": "SNOOP_ACK", "src_host": 1, "dst_host": 0xFFFF,
                "session_id": 2, "epoch": 7, "snoop_id": 91,
                "line_address": 0x4080, "monotonic_ns": 21,
                "payload_len": 64, "status": "OK", "ack_strength": "MODEL",
                "dirty_data": True,
            },
            {
                "schema_version": 1, "event": "dirty_completion",
                "opcode": "SNOOP_ACK", "src_host": 1, "dst_host": 0xFFFF,
                "session_id": 2, "epoch": 7, "snoop_id": 91,
                "line_address": 0x4080, "monotonic_ns": 22,
                "payload_len": 64, "status": "OK", "ack_strength": "MODEL",
                "dirty_data": True,
            },
        ]
        return direct, lifecycle, coherence

    def test_positive_dirty_backinvalidation_correlation(self):
        direct, lifecycle, coherence = self.records()
        matches = self.runner.correlate_dirty_backinvalidations(
            direct, lifecycle, coherence
        )
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["op_id"], 17)
        self.assertEqual(matches[0]["snoop"]["snoop_id"], 91)

    def test_dirty_correlation_rejects_each_required_invariant(self):
        mutations = {
            "clean ACK": lambda d, l, c: c[1].update(dirty_data=False),
            "native ACK": lambda d, l, c: c[1].update(ack_strength="NATIVE"),
            "short ACK": lambda d, l, c: c[1].update(payload_len=0),
            "wrong snoop": lambda d, l, c: c[1].update(snoop_id=92),
            "wrong completion opcode": lambda d, l, c: c[2].update(opcode="SNP_DATA_INV"),
            "wrong completion host": lambda d, l, c: c[2].update(src_host=0),
            "wrong completion session": lambda d, l, c: c[2].update(session_id=3),
            "outside grant": lambda d, l, c: c[0].update(line_address=0x5000),
            "wrong host": lambda d, l, c: c[0].update(dst_host=0),
            "not invalidation": lambda d, l, c: c[0].update(opcode="SNP_DATA_DOWNGRADE"),
            "missing completion": lambda d, l, c: c.pop(),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                direct, lifecycle, coherence = self.records()
                mutate(direct, lifecycle, coherence)
                with self.assertRaisesRegex(ValueError, "dirty SNP_DATA_INV"):
                    self.runner.correlate_dirty_backinvalidations(
                        direct, lifecycle, coherence
                    )

    def test_strict_jsonl_rejects_duplicate_truncated_and_backwards(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = pathlib.Path(temporary) / "trace.jsonl"
            path.write_text(
                '{"schema_version":1,"event":"x","monotonic_ns":2}\n'
                '{"schema_version":1,"event":"y","monotonic_ns":1}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "backwards"):
                self.runner.read_coherence_trace(path)
            path.write_text('{"schema_version":1,"event":"x"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "truncated"):
                self.runner.read_coherence_trace(path)
            path.write_text(
                '{"schema_version":1,"event":"x","event":"y","monotonic_ns":1}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                self.runner.read_coherence_trace(path)

    def test_host_capture_orders_unmap_through_dirty_completion(self):
        direct, lifecycle, coherence = self.records()
        correlations = self.runner.correlate_dirty_backinvalidations(
            direct, lifecycle, coherence
        )
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.runner.RuntimePaths(temporary, temporary)
            paths.event_log(1).write_text(
                json.dumps({
                    "host_capture_ns": 10,
                    "line": "BADFS_DIRECT_MAP_TRACE_JSON " + json.dumps(direct[0]),
                }) + "\n",
                encoding="utf-8",
            )
            paths.event_log(0).write_text(
                json.dumps({
                    "host_capture_ns": 50,
                    "line": "BADFS_LIFECYCLE_TRACE_JSON " + json.dumps(lifecycle[1]),
                }) + "\n",
                encoding="utf-8",
            )
            ordered = self.runner.validate_host_capture_order(paths, correlations)
            self.assertEqual(ordered["snoop_send_ns"], 20)
            self.assertEqual(ordered["dirty_completion_ns"], 22)

            correlations[0]["completion"]["monotonic_ns"] = 60
            with self.assertRaisesRegex(ValueError, "unmap.*snoop.*completion.*success"):
                self.runner.validate_host_capture_order(paths, correlations)

    def test_benchmark_and_fallback_gate(self):
        bytes_count = 65536
        block_size = min(bytes_count, 1024 * 1024)
        checksum = self.runner.expected_benchmark_checksum(bytes_count, block_size, 1)
        fabric = {
            "trusted_direct_read_ops": 1, "trusted_direct_read_bytes": bytes_count,
            "trusted_direct_write_ops": 1, "trusted_direct_write_bytes": bytes_count,
            "staged_read_ops": 0, "staged_read_bytes": 0,
            "staged_write_ops": 0, "staged_write_bytes": 0,
            "blob_read_ops": 0, "blob_read_bytes": 0,
            "blob_write_ops": 0, "blob_write_bytes": 0,
            "legacy_read_file_block_ops": 0, "legacy_write_file_block_ops": 0,
            "legacy_read_fabric_block_ops": 0, "legacy_write_fabric_block_ops": 0,
            "stale_ref_rejections": 0, "epoch_rejections": 0,
            "checksum_failures": 0, "lease_rejections": 0,
            "quarantine_events": 0, "active_leases": 0, "quarantined_slots": 0,
        }
        audit = {
            "pending_operations": 0, "quarantined_extents": 0,
            "active_read_leases": 0, "direct_mapped_extents": 0,
            "published_ranges": 1, "backend_live_extents": 1,
            "extent_states": {"1": "published"},
            "payload_checksum_bytes": 0, "payload_checksum_scans": 0,
            "payload_persist_bytes": bytes_count,
            "coherent_acquire_bytes": bytes_count,
        }
        output = (
            f"badfs_bench file_size={bytes_count} block_size={block_size} iterations=1 "
            f"written_bytes={bytes_count} read_bytes={bytes_count} write_secs=1.0 "
            f"read_secs=1.0 write_mib_s=1.0 read_mib_s=1.0 checksum={checksum}\n"
            "badfs_lifecycle_inspection "
            + json.dumps({
                "schema_version": "badfs.lifecycle.inspection.v1",
                "server": 0, "fabric": fabric, "audit": audit,
            })
            + "\n"
        )
        result = self.runner.validate_legofs_output(output, bytes_count)
        self.assertEqual(result["benchmark"]["checksum"], checksum)
        fabric["blob_write_ops"] = 1
        bad = output.split("badfs_lifecycle_inspection ", 1)[0] + (
            "badfs_lifecycle_inspection "
            + json.dumps({
                "schema_version": "badfs.lifecycle.inspection.v1",
                "server": 0, "fabric": fabric, "audit": audit,
            })
            + "\n"
        )
        with self.assertRaisesRegex(ValueError, "fallback counter"):
            self.runner.validate_legofs_output(bad, bytes_count)


if __name__ == "__main__":
    unittest.main()
