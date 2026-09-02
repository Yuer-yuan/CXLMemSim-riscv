import json
import pathlib
import tempfile
import unittest

from scripts.cxl_bi_app import (
    analytical_envelope,
    analyze_trace,
    parse_guest_records,
    percentiles_ns,
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


if __name__ == "__main__":
    unittest.main()
