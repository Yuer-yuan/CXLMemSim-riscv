import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "legofs_v2_qemu_bi.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("legofs_v2_qemu_bi", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class V2QemuBiPublicationTransferModeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runner = load_runner()

    def test_exact_prefix_keeps_the_production_artifact_paths(self):
        paths = self.runner.Paths(ROOT, "candidate", "exact-prefix")
        self.assertEqual(
            paths.v2_build,
            ROOT / "target/build/riscv-v2-qemu/payload",
        )
        self.assertEqual(
            paths.bundle_manifest,
            ROOT
            / "target/build/riscv-v2-guest/bundle/evidence/build-manifest.json",
        )
        self.assertEqual(paths.publication_transfer_mode, "exact-prefix")
        self.assertEqual(paths.durable_progress_mode, "anchor")

    def test_full_slot_baseline_uses_an_isolated_artifact_tree(self):
        paths = self.runner.Paths(ROOT, "baseline", "full-slot-baseline")
        self.assertEqual(
            paths.v2_build,
            ROOT / "target/build/riscv-v2-qemu-full-slot-baseline/payload",
        )
        self.assertEqual(
            paths.bundle_manifest,
            ROOT
            / "target/build/riscv-v2-guest-full-slot-baseline"
            / "bundle/evidence/build-manifest.json",
        )
        self.assertEqual(paths.publication_transfer_mode, "full-slot-baseline")

    def test_full_root_durable_baseline_uses_an_isolated_artifact_tree(self):
        paths = self.runner.Paths(
            ROOT, "durable-baseline", "exact-prefix", "full-root-baseline"
        )
        self.assertEqual(
            paths.v2_build,
            ROOT
            / "target/build/riscv-v2-qemu-durable-full-root-baseline/payload",
        )
        self.assertEqual(
            paths.bundle_manifest,
            ROOT
            / "target/build/riscv-v2-guest-durable-full-root-baseline"
            / "bundle/evidence/build-manifest.json",
        )
        self.assertEqual(paths.publication_transfer_mode, "exact-prefix")
        self.assertEqual(paths.durable_progress_mode, "full-root-baseline")

    def test_anchor_eager_baseline_uses_an_isolated_artifact_tree(self):
        paths = self.runner.Paths(
            ROOT, "durable-eager", "exact-prefix", "anchor-eager-baseline"
        )
        self.assertEqual(
            paths.v2_build,
            ROOT
            / "target/build/riscv-v2-qemu-durable-anchor-eager-baseline/payload",
        )
        self.assertEqual(
            paths.bundle_manifest,
            ROOT
            / "target/build/riscv-v2-guest-durable-anchor-eager-baseline"
            / "bundle/evidence/build-manifest.json",
        )
        self.assertEqual(paths.durable_progress_mode, "anchor-eager-baseline")

    def test_redundant_file_version_baseline_uses_an_isolated_artifact_tree(self):
        paths = self.runner.Paths(
            ROOT,
            "metadata-baseline",
            "exact-prefix",
            "anchor",
            12,
            "redundant-file-version-baseline",
        )
        self.assertEqual(
            paths.v2_build,
            ROOT
            / "target/build/riscv-v2-qemu-redundant-file-version-baseline-workload-i12/payload",
        )
        self.assertEqual(
            paths.bundle_manifest,
            ROOT
            / "target/build/riscv-v2-guest-redundant-file-version-baseline/bundle/evidence/build-manifest.json",
        )
        self.assertEqual(paths.metadata_index_mode, "redundant-file-version-baseline")

    def test_long_workload_uses_an_isolated_payload_but_the_same_rv64_bundle(self):
        paths = self.runner.Paths(
            ROOT, "long", "exact-prefix", "anchor", 12
        )
        self.assertEqual(
            paths.v2_build,
            ROOT / "target/build/riscv-v2-qemu-workload-i12/payload",
        )
        self.assertEqual(
            paths.bundle_manifest,
            ROOT
            / "target/build/riscv-v2-guest/bundle/evidence/build-manifest.json",
        )
        self.assertEqual(paths.workload_iteration_envelope, 12)

    def test_vd_bi_calibration_uses_an_isolated_artifact_tree(self):
        paths = self.runner.Paths(
            ROOT, "calibration", "exact-prefix", "anchor", 8, "inode-ref", True
        )
        self.assertEqual(
            paths.v2_build,
            ROOT / "target/build/riscv-v2-qemu-vd-bi-calibration/payload",
        )
        self.assertEqual(
            paths.bundle_manifest,
            ROOT
            / "target/build/riscv-v2-guest-vd-bi-calibration"
            / "bundle/evidence/build-manifest.json",
        )
        self.assertTrue(paths.vd_bi_calibration)

    def test_unknown_mode_and_noncanonical_evidence_fail_closed(self):
        with self.assertRaises(ValueError):
            self.runner.Paths(ROOT, "invalid", "legacy")
        with self.assertRaises(ValueError):
            self.runner.Paths(ROOT, "invalid", "exact-prefix", "runtime-toggle")

        with tempfile.TemporaryDirectory() as temporary:
            evidence = pathlib.Path(temporary) / "mode.txt"
            evidence.write_text("exact-prefix\n", encoding="ascii")
            self.runner.verify_publication_transfer_mode(evidence, "exact-prefix")

            evidence.write_text("exact-prefix \n", encoding="ascii")
            with self.assertRaises(ValueError):
                self.runner.verify_publication_transfer_mode(evidence, "exact-prefix")

            evidence.write_text("full-slot-baseline\n", encoding="ascii")
            with self.assertRaises(ValueError):
                self.runner.verify_publication_transfer_mode(evidence, "exact-prefix")

            evidence.write_text("anchor\n", encoding="ascii")
            self.runner.verify_durable_progress_mode(evidence, "anchor")
            evidence.write_text("full-root-baseline \n", encoding="ascii")
            with self.assertRaises(ValueError):
                self.runner.verify_durable_progress_mode(evidence, "full-root-baseline")

            evidence.write_text("inode-ref\n", encoding="ascii")
            self.runner.verify_metadata_index_mode(evidence, "inode-ref")
            evidence.write_text("inode-ref \n", encoding="ascii")
            with self.assertRaises(ValueError):
                self.runner.verify_metadata_index_mode(evidence, "inode-ref")

            evidence.write_text("1\n", encoding="ascii")
            self.runner.verify_vd_bi_calibration_mode(evidence, True)
            evidence.write_text("true\n", encoding="ascii")
            with self.assertRaises(ValueError):
                self.runner.verify_vd_bi_calibration_mode(evidence, True)

            evidence.write_text("12\n", encoding="ascii")
            self.runner.verify_workload_iteration_envelope(evidence, 12)
            evidence.write_text("012\n", encoding="ascii")
            with self.assertRaises(ValueError):
                self.runner.verify_workload_iteration_envelope(evidence, 12)

    def test_authority_telemetry_proves_candidate_and_eager_paths(self):
        candidate = {
            "schema": "legofs.v2.authority-telemetry.v1",
            "durable_progress_deferred_advances": 3,
            "durable_progress_root_piggybacks": 2,
            "durable_progress_forced_publications": 1,
            "persistence_stable_copy_ns": 120,
            "persistence_runtime_publication_ns": 90,
            "persistence_runtime_root_publications": 0,
            "persistence_runtime_publication_elisions": 4,
        }
        self.runner.validate_authority_telemetry(candidate, "anchor", True)
        eager = dict(candidate)
        eager.update(
            {
                "durable_progress_deferred_advances": 0,
                "durable_progress_root_piggybacks": 0,
                "persistence_runtime_root_publications": 4,
                "persistence_runtime_publication_elisions": 0,
            }
        )
        self.runner.validate_authority_telemetry(
            eager, "anchor-eager-baseline", True
        )
        broken = dict(candidate)
        broken["persistence_runtime_root_publications"] = 1
        with self.assertRaises(ValueError):
            self.runner.validate_authority_telemetry(broken, "anchor", True)
        idle = {field: 0 for field in candidate if field != "schema"}
        idle["schema"] = "legofs.v2.authority-telemetry.v1"
        self.runner.validate_authority_telemetry(idle, "anchor", False)

    def test_calibration_authority_gate_requires_real_exact_batches_and_samples(self):
        events = {
            name: {"samples_ns": [1, 2, 3, 4]}
            for name in (
                "page_persist",
                "tail_persist",
                "root_publication",
                "durable_anchor_publication",
                "cow_path",
                "append_1",
                "append_2",
                "append_4",
                "append_8",
            )
        }
        telemetry = {
            "schema": "legofs.v2.authority-telemetry.v1",
            "commands": 90,
            "mutations": 90,
            "mutation_failures": 0,
            "read_failures": 0,
            "mutation_durable_batches": 24,
            "mutation_durable_batch_items": 90,
            "mutation_durable_batch_max_items": 8,
            "mutation_visible_ahead_returns": 0,
            "vd_calibration_samples": {
                "schema": "legofs.vd-authority-calibration.v1",
                "events": events,
            },
        }
        self.runner.validate_calibration_authority_telemetry(telemetry)
        broken = dict(telemetry)
        broken["mutation_durable_batch_max_items"] = 4
        with self.assertRaisesRegex(ValueError, "eight-item"):
            self.runner.validate_calibration_authority_telemetry(broken)
        events["append_8"] = {"samples_ns": [1, 2, 3]}
        with self.assertRaisesRegex(ValueError, "append_8"):
            self.runner.validate_calibration_authority_telemetry(telemetry)

    def test_bounded_client_start_batches_cover_each_client_once(self):
        self.assertEqual(
            [list(batch) for batch in self.runner.bounded_start_batches(10, 2)],
            [[0, 1], [2, 3], [4, 5], [6, 7], [8, 9]],
        )
        self.assertEqual(
            [list(batch) for batch in self.runner.bounded_start_batches(3, 4)],
            [[0, 1, 2]],
        )
        with self.assertRaises(ValueError):
            self.runner.bounded_start_batches(10, 0)

    def test_exact_cohort_commands_are_all_dispatched_before_ready_wait(self):
        class FakeConsole:
            def __init__(self, output):
                self.output = output
                self.commands = []

            def send(self, command):
                self.commands.append(command)

        consoles = [FakeConsole("server")] + [
            FakeConsole(f"client-{slot}") for slot in range(6)
        ]
        command, starts = self.runner.dispatch_bounded_client_commands(
            consoles, 6, False, 12
        )
        self.assertEqual(command, "bounded-client-start")
        self.assertEqual(starts, [8] * 6)
        self.assertEqual(
            [console.commands for console in consoles[1:]],
            [[f"/payload/bin/legofs-v2-qemu-node bounded-client-start {slot} 12"]
             for slot in range(6)],
        )
        with self.assertRaises(ValueError):
            self.runner.dispatch_bounded_client_commands(consoles[:-1], 6, False, 12)

    def test_calibration_dispatch_is_explicit_and_excludes_admission_only(self):
        class FakeConsole:
            def __init__(self):
                self.output = ""
                self.commands = []

            def send(self, command):
                self.commands.append(command)

        consoles = [FakeConsole(), FakeConsole()]
        command, starts = self.runner.dispatch_bounded_client_commands(
            consoles, 1, False, 7, True
        )
        self.assertEqual(command, "bounded-client-calibrate")
        self.assertEqual(starts, [0])
        self.assertEqual(
            consoles[1].commands,
            ["/payload/bin/legofs-v2-qemu-node bounded-client-calibrate 0 7"],
        )
        with self.assertRaises(ValueError):
            self.runner.dispatch_bounded_client_commands(
                consoles, 1, True, 7, True
            )

    def test_vd_probe_bundle_uses_only_raw_source_samples(self):
        authority_events = {
            name: {"samples_ns": [1, 2, 3, 4]}
            for name in (
                "page_persist",
                "tail_persist",
                "root_publication",
                "durable_anchor_publication",
                "cow_path",
                "append_1",
                "append_2",
                "append_4",
                "append_8",
            )
        }
        authority = {
            "vd_calibration_samples": {
                "schema": "legofs.vd-authority-calibration.v1",
                "events": authority_events,
            }
        }
        clients = [
            {
                "schema": "legofs.vd-client-calibration.v1",
                "events": {"sq_cq": {"samples_ns": [4, 5, 6]}},
            }
        ]
        coherence = {
            "schema": "legofs.cxlmemsim.request-completion-timing.v1",
            "instrumentation_available": True,
            "events": {
                name: {"samples_ns": [7, 8, 9, 10]}
                for name in ("gets", "getm", "upgrade", "putm")
            },
        }
        probes = self.runner.build_vd_calibration_probes(
            authority, clients, coherence
        )
        self.assertEqual(probes["schema"], "legofs.vd-bi-calibration-probes.v1")
        self.assertEqual(probes["events"]["sq_cq"]["samples_ns"], [4, 5, 6])
        self.assertEqual(probes["events"]["sq_cq"]["warmup_samples_discarded"], 16)
        self.assertEqual(probes["events"]["gets"]["samples_ns"], [8, 9, 10])
        self.assertEqual(probes["events"]["append_8"]["samples_ns"], [2, 3, 4])
        self.assertEqual(
            set(probes["events"]),
            {
                "sq_cq",
                "gets",
                "getm",
                "upgrade",
                "putm",
                "page_persist",
                "tail_persist",
                "root_publication",
                "durable_anchor_publication",
                "cow_path",
                "append_1",
                "append_2",
                "append_4",
                "append_8",
            },
        )

    def test_startup_pipeline_overlaps_after_layout_but_gates_final_admission(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)

            class EventPaths:
                def event_log(self, role):
                    return root / f"{role}-events.jsonl"

            def write(role, events):
                with EventPaths().event_log(role).open("w", encoding="utf-8") as output:
                    for host_capture_ns, line in events:
                        json.dump(
                            {"host_capture_ns": host_capture_ns, "line": line}, output
                        )
                        output.write("\n")

            write(
                "server",
                [
                    (100, "/payload/bin/legofs-v2-qemu-node server-start"),
                    (200, "LEGOFS_V2_SERVER_LAYOUT_READY clients=2"),
                    (700, "LEGOFS_V2_SERVER_RECOVERING_READY provider_pid=9"),
                    (850, "/payload/bin/legofs-v2-qemu-node server-admit"),
                    (900, "LEGOFS_V2_SERVER_READY provider_pid=9"),
                ],
            )
            for slot, begin, ready in ((0, 250, 600), (1, 260, 620)):
                write(
                    f"client{slot}",
                    [
                        (
                            begin,
                            f"/payload/bin/legofs-v2-qemu-node bounded-client-start {slot} 3",
                        ),
                        (
                            ready,
                            f"LEGOFS_V2_BOUNDED_WORKLOAD_WAITING slot={slot}",
                        ),
                    ],
                )
            evidence = self.runner.build_startup_pipeline_evidence(
                EventPaths(), 2, False
            )
            self.assertEqual(evidence["provider_client_overlap_ns"], 370)
            self.assertTrue(
                evidence["client_started_before_provider_recovering_ready"]
            )
            self.assertEqual(evidence["server_start_to_serving_ns"], 800)

            write(
                "server",
                [
                    (100, "/payload/bin/legofs-v2-qemu-node server-start"),
                    (200, "LEGOFS_V2_SERVER_LAYOUT_READY clients=2"),
                    (700, "LEGOFS_V2_SERVER_RECOVERING_READY provider_pid=9"),
                    (610, "/payload/bin/legofs-v2-qemu-node server-admit"),
                    (900, "LEGOFS_V2_SERVER_READY provider_pid=9"),
                ],
            )
            with self.assertRaises(ValueError):
                self.runner.build_startup_pipeline_evidence(EventPaths(), 2, False)

    def test_recorded_console_wait_closes_the_jsonl_watermark(self):
        class FakeConsole:
            def __init__(self):
                self.recorded = None

            def wait_event(self, marker, timeout):
                self.recorded = (marker, timeout)

        console = FakeConsole()
        with mock.patch.object(self.runner, "wait_console") as wait_console:
            self.runner.wait_recorded_console(console, "READY", 2.0, 17)
        wait_console.assert_called_once_with(console, "READY", 2.0, 17)
        self.assertEqual(console.recorded[0], "READY")
        self.assertGreater(console.recorded[1], 0)
        self.assertLessEqual(console.recorded[1], 2.0)

    def test_coherence_completion_timing_pairs_exact_requests_and_reports_rmad(self):
        def item(event, opcode, request_id, timestamp, duration=0, status="OK"):
            return {
                "schema_version": 1,
                "event": event,
                "monotonic_ns": timestamp,
                "opcode": opcode,
                "src_host": 1,
                "session_id": 9,
                "request_id": request_id,
                "status": status,
                "duration_ns": duration,
            }

        records = []
        for index, duration in enumerate((100, 110, 120), 1):
            records.append(item("request", "GETS", index, index * 1000))
            records.append(
                item("request_completion", "GETS", index, index * 1000 + 500, duration)
            )
        timing = self.runner.coherence_request_completion_timing(
            records, 0, 10_000
        )
        self.assertTrue(timing["instrumentation_available"])
        self.assertEqual(timing["request_count"], 3)
        self.assertEqual(timing["completion_count"], 3)
        self.assertEqual(timing["events"]["gets"]["samples_ns"], [100, 110, 120])
        self.assertAlmostEqual(
            timing["events"]["gets"]["rmad"], 1.4826 * 10 / 110
        )

    def test_coherence_completion_timing_fails_closed_on_unpaired_or_failed_data(self):
        request = {
            "schema_version": 1,
            "event": "request",
            "monotonic_ns": 100,
            "opcode": "GETM",
            "src_host": 2,
            "session_id": 3,
            "request_id": 4,
            "status": "OK",
            "duration_ns": 0,
        }
        completion = dict(request)
        completion.update(
            event="request_completion",
            monotonic_ns=200,
            duration_ns=50,
            request_id=5,
        )
        with self.assertRaises(ValueError):
            self.runner.coherence_request_completion_timing(
                [request, completion], 0, 1000
            )
        completion["request_id"] = 4
        completion["status"] = "IO_ERROR"
        with self.assertRaises(ValueError):
            self.runner.coherence_request_completion_timing(
                [request, completion], 0, 1000
            )

    def workload(self, slot=0, iterations=2):
        records = []
        for index, phase in enumerate(self.runner.WORKLOAD_PHASES):
            begin = 1_000_000_000 + index * 2_000_000_000
            elapsed = 1_000_000_000
            logical_bytes = (
                self.runner.WORKLOAD_PHASE_BYTES_PER_ITERATION[phase] * iterations
            )
            rates = self.runner._derived_rates(iterations, logical_bytes, elapsed)
            records.append(
                {
                    "workload_name": "v2-io500-interface",
                    "phase_name": phase,
                    "operation_kind": f"kind-{phase}",
                    "requested_operation_size_bytes": 3901
                    if logical_bytes
                    else 0,
                    "attempted_operations": iterations,
                    "successful_operations": iterations,
                    "failed_operations": 0,
                    "logical_bytes_successfully_transferred": logical_bytes,
                    "phase_begin_ns": begin,
                    "phase_end_ns": begin + elapsed,
                    "wall_elapsed_ns": elapsed,
                    "measured_elapsed_ns": elapsed,
                    **rates,
                    "p50_us": 200000.0,
                    "p95_us": 200000.0,
                    "p99_us": 200000.0,
                    "max_us": 200000.0,
                    "tool_reported_value": None,
                    "tool_reported_unit": None,
                    "measurement_source": "guest-clock-monotonic-raw",
                    "latencies_ns": [100_000_000, 200_000_000],
                }
            )
        return {
            "schema": self.runner.WORKLOAD_SCHEMA,
            "guest_client_slot": slot,
            "config": {
                "iterations": iterations,
                "payload_bytes": 3901,
                "first_write_bytes": 2048,
            },
            "records": records,
        }

    def test_workload_v2_rates_are_independently_recomputed(self):
        workload = self.workload()
        phases = self.runner.validate_workload_v2(workload, 2, 0)
        self.assertEqual(phases["write_close"]["successful_operations"], 2)
        phases["write_close"]["bandwidth_bytes_per_second"] *= 1.01
        with self.assertRaises(ValueError):
            self.runner.validate_workload_v2(workload, 2, 0)

    def test_aggregate_uses_global_host_makespan_not_rate_sum(self):
        workloads = [
            {"client_slot": slot, **self.workload(slot)} for slot in range(2)
        ]
        timelines = {}
        for slot in range(2):
            timelines[slot] = {
                phase: {
                    "begin_ns": 10_000_000_000 + slot * 500_000_000,
                    "end_ns": 12_000_000_000 - slot * 500_000_000,
                }
                for phase in self.runner.WORKLOAD_PHASES
            }
        records, summary = self.runner.build_performance_records(
            "unit-run", workloads, timelines, 2
        )
        self.assertEqual(len(records), len(self.runner.WORKLOAD_PHASES) * 3)
        self.assertEqual(summary["metric_scope"], "authoritative-global-host-makespan")
        self.assertEqual(summary["create_iops"], 2.0)
        self.assertEqual(summary["payload_write_bw_bytes_s"], 7802.0)
        self.assertEqual(
            summary["per_client_rate_sum_cross_check"]["create"][
                "per_client_iops_sum"
            ],
            4.0,
        )

    def test_phase_timeline_binds_host_events_to_guest_record(self):
        workload = self.workload()
        with tempfile.TemporaryDirectory() as temporary:
            path = pathlib.Path(temporary) / "events.jsonl"
            with path.open("w", encoding="utf-8") as output:
                for record in workload["records"]:
                    phase = record["phase_name"]
                    for boundary, field in (
                        ("BEGIN", "phase_begin_ns"),
                        ("END", "phase_end_ns"),
                    ):
                        json.dump(
                            {
                                "host_capture_ns": record[field] + 1000,
                                "line": (
                                    f"LEGOFS_V2_PHASE_{boundary} slot=0 "
                                    f"phase={phase} guest_ns={record[field]}"
                                ),
                            },
                            output,
                        )
                        output.write("\n")
            timeline = self.runner.read_phase_timeline(path, 0, workload)
            self.assertEqual(
                timeline["full"]["begin_ns"],
                next(
                    record["phase_begin_ns"]
                    for record in workload["records"]
                    if record["phase_name"] == "full"
                )
                + 1000,
            )

if __name__ == "__main__":
    unittest.main()
