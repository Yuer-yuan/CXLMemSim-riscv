import importlib.util
import json
import pathlib
import socket
import sys
import tempfile
import threading
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
RUNNER = SCRIPTS / "legofs_type3_3node.py"


def load_runner():
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location("legofs_type3_3node", RUNNER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(SCRIPTS))


class LegofsTwoClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runner = load_runner()

    def test_three_endpoints_have_unique_coherence_host_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.runner.base.RuntimePaths.for_test(temporary)
            commands = [
                self.runner.base.build_qemu_command(paths, node, 19000, 23000)
                for node in range(3)
            ]
        for node, command in enumerate(commands):
            joined = " ".join(command)
            self.assertIn(f"coherence-v2-host-id={node}", joined)
            self.assertIn(f"persistent-memdev=t3ssd-node{node}", joined)
        self.assertEqual(sum("hostfwd=" in " ".join(cmd) for cmd in commands), 1)

    def test_registration_validator_rejects_duplicate_host_id(self):
        valid = [
            {
                "event": "registration",
                "status": "OK",
                "src_host": host,
                "session_id": host + 11,
            }
            for host in range(3)
        ]
        self.assertEqual(
            [record["src_host"] for record in self.runner.validate_three_registrations(valid)],
            [0, 1, 2],
        )
        valid[2]["src_host"] = 1
        with self.assertRaisesRegex(ValueError, "duplicate coherence host ID"):
            self.runner.validate_three_registrations(valid)

    def test_case_validator_requires_both_clients_and_exact_bytes(self):
        outputs = []
        for client in range(2):
            lines = []
            for case in self.runner.CASES:
                lines.append(f"LEG_OFS_CASE_PASS case={case} client={client}")
                if case == "client-crash" and client == 0:
                    lines.append("BADFS_2C1S_CRASH_ARMED client=0")
                    continue
                record = {
                    "schema_version": "badfs.2c1s.case.v1",
                    "case": case,
                    "client": client,
                    "status": "passed",
                    "bytes": 4096,
                    "validation": "exact-byte-comparison",
                }
                lines.append("badfs_2c1s_result " + json.dumps(record))
            outputs.append("\n".join(lines))
        records = self.runner.validate_case_outputs(outputs, 4096)
        self.assertEqual(len(records), 9)
        with self.assertRaises(ValueError):
            self.runner.validate_case_outputs((outputs[0], ""), 4096)

    def test_guest_has_two_client_cases_and_ungraceful_exit(self):
        source = (ROOT / "guest/legofs_node_init.c").read_text(encoding="utf-8")
        for case in self.runner.CASES:
            self.assertIn(f'"{case}"', source)
        bench = (ROOT.parent.parent / "badfs-bench/src/main.rs").read_text(
            encoding="utf-8"
        )
        self.assertIn("libc::_exit(CRASH_EXIT_CODE)", bench)
        self.assertIn('"validation": "exact-byte-comparison"', bench)
        self.assertIn("legofs.barrier_port=", source)
        self.assertIn("BADFS_BENCH_BARRIER_ADDR=10.0.2.2:", source)
        self.assertNotIn("2c1s-marker", bench)
        derive_region = source.split("static void derive_region_id", 1)[1].split(
            "static void discover_dax", 1
        )[0]
        self.assertNotIn("status.inode", derive_region)
        self.assertIn("rotate_left(2, 43)", derive_region)

    def test_host_barrier_releases_two_distinct_clients(self):
        reservation = self.runner.base.PortReservation()
        barrier = self.runner.BarrierServer(reservation, expected=("unit",))
        barrier.start()
        responses = {}

        def arrive(client):
            with socket.create_connection(("127.0.0.1", barrier.port), timeout=2) as stream:
                stream.sendall(f"unit {client}\n".encode())
                stream.shutdown(socket.SHUT_WR)
                responses[client] = stream.recv(3)

        threads = [threading.Thread(target=arrive, args=(client,)) for client in (0, 1)]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=3)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(responses, {0: b"GO\n", 1: b"GO\n"})
            self.assertEqual(barrier.assert_complete()["completed"], ["unit"])
        finally:
            barrier.close()


if __name__ == "__main__":
    unittest.main()
