import json
import pathlib
import tempfile
import unittest

from scripts.cxl_bi_app import (
    RunConfig,
    RuntimePaths,
    analytical_envelope,
    analyze_trace,
    build_bootargs,
    build_qemu_command,
    parse_args,
    parse_guest_records,
    parse_server_stats,
    percentiles_ns,
    require_dirty_handoff,
    validate_guest_evidence,
)


class GuestRecordTests(unittest.TestCase):
    def test_parse_guest_records_ignores_console_noise_and_cr(self):
        console = (
            "OpenSBI noise\r\n"
            'CXLBI_JSON {"schema_version":1,"event":"mapped",'
            '"role":"reader"}\r\n'
            "kernel noise\n"
        )
        self.assertEqual(
            parse_guest_records(console),
            [{"schema_version": 1, "event": "mapped", "role": "reader"}],
        )

    def test_parse_guest_records_rejects_duplicate_json_keys(self):
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            parse_guest_records(
                'CXLBI_JSON {"schema_version":1,"event":"mapped",'
                '"event":"fatal"}\n'
            )

    def test_parse_guest_records_rejects_unsupported_schema(self):
        with self.assertRaisesRegex(ValueError, "schema"):
            parse_guest_records('CXLBI_JSON {"schema_version":2,"event":"mapped"}\n')


class PerformanceMathTests(unittest.TestCase):
    def test_nearest_rank_percentiles(self):
        self.assertEqual(
            percentiles_ns([50, 10, 40, 20, 30]),
            {
                "count": 5,
                "min_ns": 10,
                "mean_ns": 30.0,
                "p50_ns": 30,
                "p95_ns": 50,
                "p99_ns": 50,
                "max_ns": 50,
            },
        )

    def test_percentiles_reject_empty_input(self):
        with self.assertRaisesRegex(ValueError, "empty"):
            percentiles_ns([])

    def test_analytical_envelope_is_explicitly_not_measured(self):
        result = analytical_envelope(
            link_gbps=32.0,
            media_ns=100.0,
            request_ns=150.0,
            bi_ns=200.0,
        )
        self.assertEqual(result["classification"], "analytical_not_measured")
        self.assertEqual(result["dirty_read_latency_ns"], 450.0)
        self.assertEqual(result["clean_transfer_latency_ns"], 350.0)
        self.assertEqual(result["streaming_ceiling_GBps"], 4.0)
        self.assertAlmostEqual(result["serialized_dirty_64B_GBps"], 64.0 / 450.0)

    def test_analytical_envelope_rejects_nonpositive_assumptions(self):
        for field in ("link_gbps", "media_ns", "request_ns", "bi_ns"):
            values = {
                "link_gbps": 32.0,
                "media_ns": 100.0,
                "request_ns": 150.0,
                "bi_ns": 200.0,
            }
            values[field] = 0.0
            with self.subTest(field=field), self.assertRaisesRegex(
                ValueError, "positive"
            ):
                analytical_envelope(**values)


class TraceEvidenceTests(unittest.TestCase):
    @staticmethod
    def _records(line_address=0x200000):
        common = {
            "schema_version": 1,
            "line_address": line_address,
            "session_id": 12,
            "epoch": 7,
            "status": "OK",
        }
        return [
            dict(
                common,
                event="request",
                monotonic_ns=10,
                opcode="GETM",
                src_host=0,
                dst_host=0xFFFF,
                request_id=44,
                snoop_id=0,
                payload_len=0,
                ack_strength="NONE",
                dirty_data=False,
            ),
            dict(
                common,
                event="snoop_send",
                monotonic_ns=20,
                opcode="SNP_DATA_INV",
                src_host=0xFFFF,
                dst_host=1,
                request_id=44,
                snoop_id=91,
                payload_len=0,
                ack_strength="NONE",
                dirty_data=False,
            ),
            dict(
                common,
                event="snoop_ack",
                monotonic_ns=30,
                opcode="SNOOP_ACK",
                src_host=1,
                dst_host=0xFFFF,
                request_id=44,
                snoop_id=91,
                payload_len=64,
                ack_strength="MODEL",
                dirty_data=True,
            ),
            dict(
                common,
                event="dirty_completion",
                monotonic_ns=40,
                opcode="SNOOP_ACK",
                src_host=1,
                dst_host=0xFFFF,
                request_id=44,
                snoop_id=91,
                payload_len=64,
                ack_strength="MODEL",
                dirty_data=True,
            ),
        ]

    def _write(self, records):
        temporary = tempfile.TemporaryDirectory()
        path = pathlib.Path(temporary.name) / "coherence.jsonl"
        path.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )
        self.addCleanup(temporary.cleanup)
        return path

    def test_trace_joins_request_dirty_snoop_ack_and_completion(self):
        evidence = analyze_trace(self._write(self._records()), {0x200018})
        self.assertTrue(evidence["complete"])
        self.assertEqual(evidence["target_lines"], ["0x200000"])
        self.assertEqual(evidence["event_counts"]["snoop_send"], 1)
        self.assertEqual(evidence["event_counts"]["dirty_completion"], 1)
        path = evidence["dirty_paths"][0]
        self.assertEqual(path["line_address"], 0x200000)
        self.assertEqual(path["snoop_id"], 91)
        self.assertEqual(path["request_id"], 44)
        self.assertEqual(path["payload_len"], 64)
        self.assertEqual(require_dirty_handoff(evidence, 1, 0), path)

    def test_required_handoff_rejects_the_reverse_direction(self):
        evidence = analyze_trace(self._write(self._records()), {0x200000})
        with self.assertRaisesRegex(ValueError, "0->1"):
            require_dirty_handoff(evidence, 0, 1)

    def test_trace_reports_unrelated_target_as_incomplete(self):
        evidence = analyze_trace(self._write(self._records()), {0x300000})
        self.assertFalse(evidence["complete"])
        self.assertEqual(evidence["dirty_paths"], [])
        self.assertEqual(evidence["missing_target_lines"], ["0x300000"])

    def test_trace_rejects_short_or_non_model_dirty_ack(self):
        for mutation in (
            {"payload_len": 0},
            {"dirty_data": False},
            {"ack_strength": "NATIVE"},
            {"status": "TIMEOUT"},
            {"snoop_id": 92},
        ):
            records = self._records()
            records[2].update(mutation)
            with self.subTest(mutation=mutation):
                evidence = analyze_trace(self._write(records), {0x200000})
                self.assertFalse(evidence["complete"])

    def test_trace_rejects_truncated_final_record(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = pathlib.Path(temporary.name) / "coherence.jsonl"
        path.write_text('{"schema_version":1', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "truncated"):
            analyze_trace(path, {0x200000})

    def test_trace_rejects_backwards_timestamps(self):
        records = self._records()
        records[2]["monotonic_ns"] = 5
        with self.assertRaisesRegex(ValueError, "backwards"):
            analyze_trace(self._write(records), {0x200000})


class CommandConstructionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name) / "repo"
        self.paths = RuntimePaths.for_test(self.root)
        self.config = RunConfig(
            iterations=16,
            stream_bytes=65536,
            timeout_seconds=240,
            link_gbps=32.0,
            media_ns=100.0,
            request_ns=150.0,
            bi_ns=200.0,
            guest_memory="1G",
        )

    def test_two_commands_use_distinct_files_and_host_ids(self):
        commands = [
            build_qemu_command(self.paths, self.config, node, 31234)
            for node in (0, 1)
        ]
        rendered = [" ".join(command) for command in commands]
        self.assertIn(str(self.paths.endpoint_memory(0)), rendered[0])
        self.assertNotIn(str(self.paths.endpoint_memory(1)), rendered[0])
        self.assertIn(str(self.paths.endpoint_memory(1)), rendered[1])
        self.assertIn("coherence-v2-host-id=0", rendered[0])
        self.assertIn("coherence-v2-host-id=1", rendered[1])
        self.assertIn("coherence-v2-read-exclusive=on", rendered[0])
        self.assertIn("coherence-v2-read-exclusive=off", rendered[1])

    def test_both_commands_require_bi_hdm_db_and_same_server(self):
        for node in (0, 1):
            rendered = " ".join(
                build_qemu_command(self.paths, self.config, node, 31234)
            )
            self.assertIn("cxl-fmw.0.restrictions=0x29", rendered)
            self.assertIn("coherence-v2=on", rendered)
            self.assertIn("hdm-db=on", rendered)
            self.assertIn("x-256b-flit=on", rendered)
            self.assertIn("cxlmemsim-port=31234", rendered)
            self.assertNotIn("virtio-blk", rendered)

    def test_non_bi_command_building_block_removes_fmw_bi_capability(self):
        rendered = " ".join(
            build_qemu_command(
                self.paths, self.config, 1, 31234, bi_enabled=False
            )
        )
        self.assertIn("cxl-fmw.0.restrictions=0x09", rendered)
        self.assertNotIn("cxl-fmw.0.restrictions=0x29", rendered)

    def test_bootargs_identify_role_and_never_request_a_flush(self):
        reader = build_bootargs("reader", self.config)
        writer = build_bootargs("writer", self.config)
        for bootargs in (reader, writer):
            self.assertIn("cxl_core.pmem_as_dax=1", bootargs)
            self.assertIn("cxlbi.iterations=16", bootargs)
            self.assertIn("cxlbi.stream_bytes=65536", bootargs)
            self.assertIn("cxlbi.timeout_ms=240000", bootargs)
            self.assertNotIn("flush", bootargs)
        self.assertIn("cxlbi.role=reader", reader)
        self.assertIn("cxlbi.role=writer", writer)


class GuestEvidenceValidationTests(unittest.TestCase):
    @staticmethod
    def _records(role):
        endpoint = 0 if role == "reader" else 1
        records = [
            {
                "schema_version": 1,
                "event": "mapped",
                "role": role,
                "endpoint_id": endpoint,
                "dax": "dax0.0",
                "dax_size": 268435456,
                "dax_align": 2097152,
                "mapping_offset": 0,
                "mapping_bytes": 4194304,
                "litmus_generation_offset": 256,
                "payload_offset": 320,
                "ping_request_offset": 384,
                "ping_ack_offset": 448,
                "stream_offset": 2097152,
            },
            {
                "schema_version": 1,
                "event": "litmus",
                "role": role,
                "operation": "observe" if role == "reader" else "publish",
                "generation": 1,
                "elapsed_ns": 1000,
                "payload_checksum": 12345,
                "errors": 0,
            },
            {
                "schema_version": 1,
                "event": "pingpong",
                "role": role,
                "iterations": 16,
                "elapsed_ns": 16000,
                "errors": 0,
            },
            {
                "schema_version": 1,
                "event": "stream",
                "role": role,
                "operation": "read_verify" if role == "reader" else "write",
                "direction": 0,
                "bytes": 65536,
                "elapsed_ns": 10000,
                "errors": 0,
            },
            {
                "schema_version": 1,
                "event": "stream",
                "role": role,
                "operation": "write" if role == "reader" else "read_verify",
                "direction": 1,
                "bytes": 65536,
                "elapsed_ns": 11000,
                "errors": 0,
            },
            {
                "schema_version": 1,
                "event": "summary",
                "role": role,
                "status": "pass",
                "iterations": 16,
                "stream_bytes": 65536,
                "errors": 0,
                "explicit_flush_calls": 0,
            },
        ]
        if role == "reader":
            records[2].update(
                min_ns=900,
                mean_ns=1000,
                p50_ns=950,
                p95_ns=1200,
                p99_ns=1300,
                max_ns=1400,
            )
        else:
            records[1]["old_payload_checksum"] = 999
        return records

    def test_accepts_complete_two_role_application_evidence(self):
        result = validate_guest_evidence(
            self._records("reader"), self._records("writer"), 16, 65536
        )
        self.assertEqual(result["payload_offset"], 320)
        self.assertEqual(result["application_errors"], 0)
        self.assertEqual(
            result["measurements"]["classification"],
            "qemu_tcg_tcp_wallclock",
        )
        self.assertAlmostEqual(
            result["measurements"]["stream"]["direction_0_read_verify_MiBps"],
            6250.0,
        )

    def test_rejects_checksum_disagreement(self):
        writer = self._records("writer")
        writer[1]["payload_checksum"] = 88
        with self.assertRaisesRegex(ValueError, "payload checksum"):
            validate_guest_evidence(self._records("reader"), writer, 16, 65536)

    def test_rejects_any_explicit_flush_count(self):
        writer = self._records("writer")
        writer[-1]["explicit_flush_calls"] = 1
        with self.assertRaisesRegex(ValueError, "flush"):
            validate_guest_evidence(self._records("reader"), writer, 16, 65536)


class ServerStatsTests(unittest.TestCase):
    def test_parse_server_stats_requires_zero_error_counters(self):
        output = (
            'noise\nCOHERENCE_V2_STATS_JSON {"registrations":2,"getm":8,'
            '"snp_data_inv":3,"model_acks":3,"dirty_data_completions":3,'
            '"timeouts":0,"protocol_errors":0,"delivery_failures":0,'
            '"server_copy_failures":0,"active_bindings":0}\n'
        )
        stats = parse_server_stats(output)
        self.assertEqual(stats["registrations"], 2)
        self.assertEqual(stats["dirty_data_completions"], 3)

    def test_parse_server_stats_rejects_protocol_error(self):
        output = (
            'COHERENCE_V2_STATS_JSON {"timeouts":0,"protocol_errors":1,'
            '"delivery_failures":0,"server_copy_failures":0,'
            '"active_bindings":0}\n'
        )
        with self.assertRaisesRegex(ValueError, "protocol_errors"):
            parse_server_stats(output)


class CommandLineTests(unittest.TestCase):
    def test_valid_cli_values_become_run_config(self):
        config = parse_args(
            [
                "--iterations",
                "32",
                "--stream-bytes",
                "131072",
                "--timeout",
                "180",
                "--link-gbps",
                "64",
            ]
        )
        self.assertEqual(config.iterations, 32)
        self.assertEqual(config.stream_bytes, 131072)
        self.assertEqual(config.timeout_seconds, 180)
        self.assertEqual(config.link_gbps, 64.0)

    def test_invalid_cli_values_exit_with_usage_error(self):
        cases = (
            ["--iterations", "0"],
            ["--stream-bytes", "65"],
            ["--timeout", "0"],
            ["--link-gbps", "0"],
            ["--media-ns", "-1"],
            ["--guest-memory", "one-gigabyte"],
        )
        for arguments in cases:
            with self.subTest(arguments=arguments), self.assertRaises(SystemExit) as caught:
                parse_args(arguments)
            self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
