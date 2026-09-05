"""Fixed-work pressure smokes must prove boundary coverage, not just MPI rc=0."""
import configparser
import importlib.util
import pathlib
import threading
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("pressure_runner", ROOT / "scripts/legofs_io500.py")
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


class PressureSmokeTests(unittest.TestCase):
    def config(self, stage):
        parser = configparser.ConfigParser()
        self.assertTrue(parser.read(ROOT / "configs" / f"io500-{stage}.ini"))
        return parser

    def test_fixed_work_and_real_result_path(self):
        stress = self.config("stress-tiny")
        # IO500 sets IOR stoneWallingWearOut=1, which rejects -D0. A deadline
        # beyond the independent 600 s gate prevents truncating a passing run.
        self.assertEqual(stress["debug"]["stonewall-time"], "601")
        self.assertEqual(stress["ior-easy"]["transferSize"], "1m")
        self.assertEqual(stress["ior-easy"]["blockSize"], "5m")
        self.assertEqual(stress["ior-hard"]["segmentCount"], "65")
        self.assertEqual(stress["mdtest-easy"]["n"], "64")
        self.assertEqual(stress["mdtest-hard"]["n"], "102")
        self.assertEqual(stress["mdtest-hard"]["files-per-dir"], "102")
        self.assertEqual(stress["global"]["resultdir"], "/badfs/io500-stress-tiny-results")
        for phase in RUNNER.STANDARD_IO500_PHASES - {"ior-rnd4K-easy-read"}:
            self.assertTrue(stress.getboolean(phase, "run"), phase)
        self.assertFalse(stress.getboolean("ior-rnd4K-easy-read", "run"))
        rollover = self.config("rollover-smoke")
        self.assertEqual(rollover["debug"]["stonewall-time"], "601")
        self.assertEqual(rollover["mdtest-hard"]["n"], "1024")
        self.assertEqual(rollover["mdtest-hard"]["files-per-dir"], "1024")
        self.assertFalse(rollover.getboolean("ior-easy-write", "run"))
        for phase in ("mdtest-hard-write", "mdtest-hard-stat", "mdtest-hard-read", "mdtest-hard-delete", "find"):
            self.assertTrue(rollover.getboolean(phase, "run"))
        # Existing fast and official workload thresholds are not rewritten.
        self.assertEqual(self.config("tiny")["debug"]["stonewall-time"], "1")
        self.assertEqual(self.config("standard")["debug"]["stonewall-time"], "300")

    def test_profiles_are_diagnostic_and_plumbed_through_payload(self):
        for stage in ("stress-tiny", "rollover-smoke"):
            self.assertIn(stage, RUNNER.SEMANTIC_SMOKE_STAGES)
            self.assertIn(stage, RUNNER.PACKED_SMALL_WORKLOAD_STAGES)
            self.assertEqual(RUNNER.parse_args(["--stage", stage]).stage, stage)
            self.assertIn("expected INVALID", RUNNER.classify_io500_verifier(stage, 1, "[OK] But this is an invalid run!"))
            for path in ("run-legofs-io500.sh", "guest/legofs_io500_init.sh", "guest/legofs_io500_rank.sh", "scripts/build_legofs_io500.sh", "scripts/rebuild_legofs_io500_payload.sh"):
                self.assertIn(stage, (ROOT / path).read_text(), path)
        self.assertIn('strcmp(argv[1], "stress-tiny")', (ROOT / "guest/export_io500_results.c").read_text())
        rank = (ROOT / "guest/legofs_io500_rank.sh").read_text()
        early_exec = rank.split('case "$stage" in\n', 1)[1].split('set +e', 1)[0]
        self.assertNotIn("stress-tiny", early_exec)
        self.assertIn("rollover-smoke", early_exec)

    def test_topology_and_provider_cannot_silently_reduce_coverage(self):
        for stage, ranks in (("stress-tiny", 10), ("rollover-smoke", 2)):
            args = RUNNER.parse_args(["--stage", stage, "--client-count", str(ranks), "--serving-transport", "cxl", "--durability-profile", "coherent-seal-no-writeback", "--payload-persistence-owner", "writer-receipt", "--writer-persist-provider", "riscv-zicbom-dax"])
            RUNNER.validate_pressure_smoke_options(args)
            args.client_count = 1
            with self.assertRaisesRegex(ValueError, "requires"):
                RUNNER.validate_pressure_smoke_options(args)
            args.client_count = ranks
            args.writer_persist_provider = "msync"
            with self.assertRaisesRegex(ValueError, "requires"):
                RUNNER.validate_pressure_smoke_options(args)

    def summaries(self, count):
        return [{"mpi_rank": rank, "owner": 100 + rank,
                 "stats": {"write_bytes": 5 * 1024**2, "write_size_le_4k_ops": 1024,
                           "writer_host_max_pending_jobs": 64},
                 "cxl_serving_evidence": [{"direct_create_fallback_inode_exhausted": 1}]}
                for rank in range(count)]

    def flushes(self, count):
        return "\n".join(f"LEGOFS_WRITER_FLUSH_TIMING owner={100 + rank} jobs=4 bytes={size} handoff_ns=1 drain_ns=1 finish_ns=1 release_ns=1 total_ns=5 success=true"
                         for rank in range(count) for size in (4194304, 1048576))

    def apply_log(self, records=32):
        return f"BADFS_DIRECT_MUTATION_APPLY_TIMING records={records} creates=16 terminals=16 unlinks=0 max_in_flight=16 create_ns=1 terminal_ns=1 unlink_ns=1 total_ns=3\n"

    def coverage(self, stage, summaries, client_output="", server_output="", exporter=True):
        return RUNNER.pressure_smoke_coverage(stage, summaries, client_output, [server_output], exporter_validated=exporter)

    def test_stress_requires_each_rank_full_and_tail_and_exporter(self):
        rows, flushes = self.summaries(10), self.flushes(10)
        report = self.coverage("stress-tiny", rows, flushes, self.apply_log())
        self.assertTrue(report["passed"], report)
        for damaged in (rows[:-1], rows + rows[:1]):
            self.assertFalse(self.coverage("stress-tiny", damaged, flushes, self.apply_log())["passed"])
        for damaged in (flushes.replace("owner=109", "owner=999"), flushes.replace("bytes=4194304", "bytes=4194240"), flushes.replace("bytes=1048576", "bytes=4194304"), flushes.replace("success=true", "success=false")):
            self.assertFalse(self.coverage("stress-tiny", rows, damaged, self.apply_log())["passed"])
        self.assertFalse(self.coverage("stress-tiny", rows, flushes, self.apply_log(), exporter=False)["passed"])
        self.assertFalse(self.coverage("stress-tiny", rows, flushes, self.apply_log(1812))["passed"])
        for jobs in (63, 65):
            rows[9]["stats"]["writer_host_max_pending_jobs"] = jobs
            self.assertFalse(self.coverage("stress-tiny", rows, flushes, self.apply_log())["passed"])
        rows[9]["stats"].update(writer_host_max_pending_jobs=64, write_size_le_4k_ops=101)
        self.assertFalse(self.coverage("stress-tiny", rows, flushes, self.apply_log())["passed"])

    def test_rollover_requires_observed_boundaries_not_only_large_config(self):
        rows = self.summaries(2)
        rollover = "\n".join(f"BADFS_DIRECT_MUTATION_ROLLOVER_COMPLETE lane={lane} mode=fresh-lane applied=0 durable=0 apply_ns=1 durable_ns=1 grace_ns=1 total_ns=3" for lane in (2, 3))
        report = self.coverage("rollover-smoke", rows, server_output=self.apply_log() + rollover)
        self.assertTrue(report["passed"], report)
        for damaged in ("", rollover.replace("lane=3", "lane=2"), rollover.replace("applied=0", "applied=1812"), rollover.replace("applied=0", "applied=1")):
            self.assertFalse(self.coverage("rollover-smoke", rows, server_output=self.apply_log() + damaged)["passed"])
        rows[1]["cxl_serving_evidence"][0]["direct_create_fallback_inode_exhausted"] = 0
        self.assertFalse(self.coverage("rollover-smoke", rows, server_output=self.apply_log() + rollover)["passed"])

    def test_each_pressure_stage_uses_600_second_phase_gate_before_mpi_success(self):
        for stage in ("stress-tiny", "rollover-smoke"):
            console = mock.Mock()
            console.output = f"[RESULT] mdtest-hard-write 1.0 kIOPS : time 601.0 seconds\nLEGOFS_IO500_MPI_EXIT stage={stage} rc=0\n"
            console.condition = threading.Condition()
            with self.assertRaisesRegex(TimeoutError, "exceeded 600"):
                RUNNER.wait_mpi_exit(console, stage, timeout=9000, start=0)

    def test_final_metrics_independently_require_enabled_phases(self):
        for stage in ("stress-tiny", "rollover-smoke"):
            names = RUNNER.STANDARD_IO500_PHASES - {"ior-rnd4K-easy-read"} if stage == "stress-tiny" else {"mdtest-hard-write", "mdtest-hard-stat", "mdtest-hard-read", "mdtest-hard-delete", "find"}
            phases = [{"name": name, "seconds": 1.0} for name in names]
            RUNNER.validate_pressure_phase_metrics(stage, {"phases": phases})
            with self.assertRaises(ValueError):
                RUNNER.validate_pressure_phase_metrics(stage, {"phases": phases[:-1]})
            for bad in (0, -1, 600.001, float("nan"), float("inf")):
                with self.assertRaises(ValueError):
                    RUNNER.validate_pressure_phase_metrics(stage, {"phases": [dict(phases[0], seconds=bad)] + phases[1:]})

    def test_phase_label_detached_by_stderr_does_not_hide_completion(self):
        gate = RUNNER.IO500PhaseWatchdog()
        output = "IO500 version test\n[RESULT] mdtest-hard-read 1.0 kIOPS : time 37.619 seconds\n"
        gate.observe(output, 0.0)
        output += "[RESULT]LEGOFS_WRITER_CLEAN_TIMING jobs=1 bytes=4096 wall_ns=59220000\n"
        gate.observe(output, 180.0)
        self.assertEqual(gate.completed, {"mdtest-hard-read"})
        output += "   mdtest-hard-delete        0.022785 kIOPS : time 181.090 seconds\n"
        gate.observe(output, 181.0)
        self.assertEqual(gate.completed, {"mdtest-hard-read", "mdtest-hard-delete"})
        gate.observe(output, 700.0)  # 519 s in this phase, not 700 s across two.
        with self.assertRaisesRegex(TimeoutError, "after mdtest-hard-delete"):
            gate.observe(output, 782.0)

    def test_pressure_gate_tracks_only_configured_fixed_work_without_changing_standard(self):
        required = RUNNER.PRESSURE_SMOKE_PHASES["stress-tiny"]
        self.assertEqual(len(required), 12)
        gate = RUNNER.IO500PhaseWatchdog(required)
        output = "IO500 version test\n" + "".join(f"[RESULT] {name} 1.0 kIOPS : time 1.0 seconds\n" for name in required)
        gate.observe(output, 0.0)
        gate.observe(output, 9000.0)
        self.assertEqual(gate.completed, required)
        standard = RUNNER.IO500PhaseWatchdog()
        standard.observe(output, 0.0)
        with self.assertRaisesRegex(TimeoutError, "completed=12/13"):
            standard.observe(output, 601.0)


if __name__ == "__main__":
    unittest.main()
