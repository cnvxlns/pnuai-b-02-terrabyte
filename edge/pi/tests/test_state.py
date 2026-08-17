import json
import tempfile
import threading
import unittest
from pathlib import Path

from terrabyte_edge.state import GatewayState, read_snapshot, write_snapshot


def state(ports=("/dev/ttyUSB0", "/dev/ttyUSB1"), clock=None) -> GatewayState:
    return GatewayState(
        gateway_id="orangepi-pro-01",
        claim_code="483920",
        transport="mqtt",
        ports=ports,
        clock=clock or (lambda: 1000.0),
    )


class GatewayStateTests(unittest.TestCase):
    def test_ports_start_as_never_seen(self) -> None:
        snapshot = state().snapshot()
        self.assertEqual(len(snapshot.ports), 2)
        self.assertTrue(all(port.link == "never_seen" for port in snapshot.ports))
        self.assertTrue(all(port.node_id is None for port in snapshot.ports))

    def test_frame_records_node_and_measurements(self) -> None:
        subject = state()
        subject.record_frame(
            "/dev/ttyUSB0",
            node_id="terrabyte-node-01",
            measurements={"air_temperature_c": 27.1},
        )
        port = next(p for p in subject.snapshot().ports if p.path == "/dev/ttyUSB0")
        self.assertEqual(port.node_id, "terrabyte-node-01")
        self.assertEqual(port.link, "up")
        self.assertEqual(port.frames, 1)
        self.assertEqual(port.measurements, {"air_temperature_c": 27.1})

    def test_snapshot_does_not_observe_later_mutations(self) -> None:
        """A caller holding a snapshot must not see the state move under it."""

        subject = state()
        subject.record_frame(
            "/dev/ttyUSB0", node_id="node-1", measurements={"air_temperature_c": 20.0}
        )
        snapshot = subject.snapshot()
        subject.record_frame(
            "/dev/ttyUSB0", node_id="node-1", measurements={"air_temperature_c": 99.0}
        )
        port = next(p for p in snapshot.ports if p.path == "/dev/ttyUSB0")
        self.assertEqual(port.measurements["air_temperature_c"], 20.0)
        self.assertEqual(port.frames, 1)

    def test_same_node_on_two_ports_is_a_duplicate_fault(self) -> None:
        """Two Arduinos flashed with the same TB_NODE_ID is the likeliest
        wiring mistake, and interleaved readings would look plausible."""

        subject = state()
        subject.record_frame(
            "/dev/ttyUSB0", node_id="node-1", measurements={"air_temperature_c": 20.0}
        )
        subject.record_frame(
            "/dev/ttyUSB1", node_id="node-1", measurements={"air_temperature_c": 30.0}
        )
        ports = {p.path: p for p in subject.snapshot().ports}
        self.assertIsNone(ports["/dev/ttyUSB0"].fault)
        self.assertEqual(ports["/dev/ttyUSB1"].fault, "duplicate_node")
        # The duplicate's reading must not be adopted.
        self.assertEqual(ports["/dev/ttyUSB1"].measurements, {})

    def test_unknown_node_is_recorded_against_its_port(self) -> None:
        subject = state()
        subject.record_unknown_node("/dev/ttyUSB0", "terrabyte-node-09")
        port = next(p for p in subject.snapshot().ports if p.path == "/dev/ttyUSB0")
        self.assertEqual(port.fault, "unknown_node")
        self.assertIn("terrabyte-node-09", port.fault_detail)

    def test_hello_frame_names_the_port_before_any_reading(self) -> None:
        subject = state()
        subject.record_announcement("/dev/ttyUSB0", "terrabyte-node-01")
        port = next(p for p in subject.snapshot().ports if p.path == "/dev/ttyUSB0")
        self.assertEqual(port.node_id, "terrabyte-node-01")
        self.assertEqual(port.frames, 0)

    def test_events_are_bounded(self) -> None:
        subject = state()
        for index in range(50):
            subject.add_event("info", f"event-{index}")
        events = subject.snapshot().events
        self.assertEqual(len(events), 20)
        self.assertEqual(events[-1][2], "event-49")

    def test_concurrent_writers_lose_no_frames(self) -> None:
        subject = state(ports=("/dev/ttyUSB0",))

        def write() -> None:
            for _ in range(200):
                subject.record_frame(
                    "/dev/ttyUSB0",
                    node_id="node-1",
                    measurements={"air_temperature_c": 20.0},
                )

        threads = [threading.Thread(target=write) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        port = next(p for p in subject.snapshot().ports if p.path == "/dev/ttyUSB0")
        self.assertEqual(port.frames, 800)


class SnapshotFileTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            write_snapshot(path, state().snapshot())
            payload = read_snapshot(path)
        self.assertEqual(payload["gateway_id"], "orangepi-pro-01")
        self.assertEqual(payload["claim_code"], "483920")
        self.assertEqual(len(payload["ports"]), 2)

    def test_missing_file_reads_as_none(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(read_snapshot(Path(directory) / "absent.json"))

    def test_wrong_schema_reads_as_none(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            path.write_text(json.dumps({"schema": 999}), encoding="utf-8")
            self.assertIsNone(read_snapshot(path))

    def test_reader_never_sees_a_torn_file(self) -> None:
        """The display polls this file while the bridge rewrites it. Without an
        atomic replace it would intermittently parse a half-written JSON."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            subject = state()
            write_snapshot(path, subject.snapshot())

            failures: list[str] = []
            stop = threading.Event()

            def read_forever() -> None:
                while not stop.is_set():
                    try:
                        with path.open(encoding="utf-8") as handle:
                            json.load(handle)
                    except json.JSONDecodeError as exc:
                        failures.append(str(exc))
                    except FileNotFoundError:
                        failures.append("file vanished")

            reader = threading.Thread(target=read_forever)
            reader.start()
            try:
                for index in range(200):
                    subject.add_event("info", f"tick-{index}")
                    write_snapshot(path, subject.snapshot())
            finally:
                stop.set()
                reader.join()

            self.assertEqual(failures, [])

    def test_no_temp_files_are_left_behind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            for _ in range(5):
                write_snapshot(path, state().snapshot())
            self.assertEqual([p.name for p in Path(directory).iterdir()], ["status.json"])


if __name__ == "__main__":
    unittest.main()
