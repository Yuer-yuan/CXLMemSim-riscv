import importlib.util
import json
import pathlib
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "legofs_type3_2node.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("legofs_type3_2node_evidence", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LegofsEvidenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runner = load_runner()

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
                "snoop_id": 91, "line_address": 0x4080, "monotonic_ns": 20,
                "payload_len": 0, "status": "OK", "ack_strength": "NONE",
                "dirty_data": False,
            },
            {
                "schema_version": 1, "event": "snoop_ack",
                "opcode": "SNOOP_ACK", "src_host": 1, "dst_host": 0xFFFF,
                "snoop_id": 91, "line_address": 0x4080, "monotonic_ns": 21,
                "payload_len": 64, "status": "OK", "ack_strength": "MODEL",
                "dirty_data": True,
            },
            {
                "schema_version": 1, "event": "dirty_completion",
                "opcode": "SNP_DATA_INV", "src_host": 0xFFFF, "dst_host": 1,
                "snoop_id": 91, "line_address": 0x4080, "monotonic_ns": 22,
                "payload_len": 0, "status": "OK", "ack_strength": "NONE",
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

    def test_benchmark_and_fallback_gate(self):
        bytes_count = 65536
        checksum = self.runner.expected_benchmark_checksum(bytes_count, 4096, 1)
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
        }
        output = (
            f"badfs_bench file_size={bytes_count} block_size=4096 iterations=1 "
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
