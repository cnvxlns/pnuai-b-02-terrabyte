"""Four Arduinos, driven by fake serial ports.

No hardware is involved: ``SerialLineReader`` already takes an injectable
factory, so a fake reader can replay canned JSONL exactly the way a board
would. This is the path that decides whether four pots reach the outbox with
the right attribution.
"""

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from types import SimpleNamespace

from terrabyte_edge.service import BridgeService
from terrabyte_edge.state import GatewayState

MINIMUM_CLOCK = datetime(2025, 1, 1, tzinfo=timezone.utc)


def telemetry(node_id: str, sequence: int = 1, temperature: float = 27.1) -> bytes:
    return (
        json.dumps(
            {
                "message_type": "telemetry",
                "protocol_version": 1,
                "node_id": node_id,
                "sequence": sequence,
                "uptime_ms": 1000,
                "air_temperature_c": temperature,
                "relative_humidity_pct": 58.0,
                "ppfd_umol_m2_s": 230.5,
            }
        )
        + "\n"
    ).encode()


def hello(node_id: str) -> bytes:
    return (
        json.dumps(
            {"message_type": "hello", "protocol_version": 1, "node_id": node_id}
        )
        + "\n"
    ).encode()


class FakeReader:
    """Replays a fixed list of lines, then stops."""

    def __init__(self, port: str, lines_to_emit: list[bytes]) -> None:
        self.port = port
        self._lines = lines_to_emit

    def lines(self, stop: Event):
        for line in self._lines:
            if stop.is_set():
                return
            yield line


class FakeOutbox:
    def __init__(self) -> None:
        self.events = []

    def initialize(self) -> None:
        pass

    def counts(self):
        return (len(self.events), 0)

    def enqueue(self, event) -> bool:
        self.events.append(event)
        return True


def settings(ports, nodes, snapshot_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        serial_ports=tuple(ports),
        expected_node_ids=frozenset(nodes),
        crop_context_id="ctx-1",
        clock_minimum_utc=MINIMUM_CLOCK,
        device_id="orangepi-pro-01",
        claim_code="483920",
        transport="mqtt",
        status_snapshot_path=snapshot_path,
        status_snapshot_seconds=0.05,
        upload_batch_size=20,
        http_timeout_seconds=1.0,
        upload_interval_seconds=0.05,
    )


class MultiNodeIngestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ports = [f"/dev/ttyUSB{index}" for index in range(4)]
        self.nodes = [f"terrabyte-node-0{index + 1}" for index in range(4)]
        self.outbox = FakeOutbox()

    def build(self, readers) -> BridgeService:
        config = settings(self.ports, self.nodes, Path("/tmp/unused-status.json"))
        state = GatewayState(
            gateway_id=config.device_id,
            claim_code=config.claim_code,
            transport=config.transport,
            ports=tuple(self.ports),
            clock=lambda: 1000.0,
        )
        return BridgeService(
            config,
            outbox=self.outbox,
            publisher=SimpleNamespace(close=lambda: None),
            serial_readers=readers,
            state=state,
        )

    def test_four_ports_all_reach_the_outbox_with_correct_attribution(self) -> None:
        service = self.build([])
        for port, node in zip(self.ports, self.nodes):
            service._ingest_line(port, telemetry(node))

        self.assertEqual(len(self.outbox.events), 4)
        self.assertEqual(
            sorted(event.node_id for event in self.outbox.events), sorted(self.nodes)
        )

        ports = {p.path: p for p in service.state.snapshot().ports}
        for port, node in zip(self.ports, self.nodes):
            self.assertEqual(ports[port].node_id, node)
            self.assertEqual(ports[port].link, "up")

    def test_one_envelope_per_node(self) -> None:
        """nodes[] stays length 1. The Arduinos are unsynchronised, so batching
        four readings under one envelope-level observed_at would misattribute
        time."""

        service = self.build([])
        service._ingest_line(self.ports[0], telemetry(self.nodes[0]))
        envelope = self.outbox.events[0].envelope_v2(gateway_id="orangepi-pro-01")
        self.assertEqual(len(envelope["nodes"]), 1)
        self.assertEqual(envelope["nodes"][0]["node_id"], self.nodes[0])

    def test_unlisted_node_is_dropped_and_named(self) -> None:
        service = self.build([])
        service._ingest_line(self.ports[0], telemetry("terrabyte-node-99"))

        self.assertEqual(self.outbox.events, [])
        port = next(
            p for p in service.state.snapshot().ports if p.path == self.ports[0]
        )
        self.assertEqual(port.fault, "unknown_node")
        self.assertIn("terrabyte-node-99", port.fault_detail)

    def test_same_firmware_on_two_boards_is_caught(self) -> None:
        """The likeliest field mistake: four Arduinos flashed identically."""

        service = self.build([])
        service._ingest_line(self.ports[0], telemetry(self.nodes[0]))
        service._ingest_line(self.ports[1], telemetry(self.nodes[0]))

        ports = {p.path: p for p in service.state.snapshot().ports}
        self.assertIsNone(ports[self.ports[0]].fault)
        self.assertEqual(ports[self.ports[1]].fault, "duplicate_node")
        # Only the first board's reading is kept.
        self.assertEqual(len(self.outbox.events), 2)

    def test_hello_names_a_port_before_its_first_reading(self) -> None:
        service = self.build([])
        service._ingest_line(self.ports[2], hello(self.nodes[2]))

        port = next(
            p for p in service.state.snapshot().ports if p.path == self.ports[2]
        )
        self.assertEqual(port.node_id, self.nodes[2])
        self.assertEqual(port.frames, 0)

    def test_reader_threads_feed_one_outbox(self) -> None:
        readers = [
            FakeReader(port, [hello(node), telemetry(node, sequence=index + 1)])
            for index, (port, node) in enumerate(zip(self.ports, self.nodes))
        ]
        service = self.build(readers)
        for reader in readers:
            service._ingest_loop(reader)

        self.assertEqual(len(self.outbox.events), 4)
        # Each reader marks its own port down when its stream ends.
        self.assertTrue(
            all(port.link == "down" for port in service.state.snapshot().ports)
        )

    def test_a_dead_ingest_thread_is_not_fatal(self) -> None:
        """Losing one Arduino must not restart the process and take the other
        three pots down with it."""

        service = self.build([])
        service._critical_threads = [SimpleNamespace(is_alive=lambda: True)]
        service._threads = [
            SimpleNamespace(is_alive=lambda: False),
            *service._critical_threads,
        ]
        self.assertFalse(service.worker_failed())

    def test_a_dead_uploader_is_fatal(self) -> None:
        service = self.build([])
        service._critical_threads = [SimpleNamespace(is_alive=lambda: False)]
        self.assertTrue(service.worker_failed())


if __name__ == "__main__":
    unittest.main()
