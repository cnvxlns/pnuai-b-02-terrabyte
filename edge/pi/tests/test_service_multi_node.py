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
        # Never opened. BridgeService builds an IrrigationHistory from it, but
        # constructing one touches no disk, and the readings below carry no soil
        # moisture — so the whole irrigation pass is skipped and nothing queries
        # the log. Same convention as the unused snapshot path above.
        database_path=Path("/tmp/unused-outbox.sqlite3"),
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
        # No pot is configured here: this file is about routing four ports to
        # four nodes, and an unconfigured pot yields no irrigation suggestion,
        # so the ingest path stays exercised without dragging dose sizing into
        # these assertions. test_service.py owns the sizing wiring.
        pot_substrate_ml={},
        pot_crop_codes={},
        substrate_volume_ml_for=lambda node_id: None,
        crop_code_for=lambda node_id: None,
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

    def test_every_critical_worker_is_named_and_callable(self) -> None:
        """start() builds threads from this table, so a bad entry fails at boot.

        Naming matters beyond tidiness: join() logs the name of a worker that
        would not stop, and an unnamed thread makes that log useless.
        """

        service = self.build([])
        workers = service._critical_workers()
        self.assertEqual(
            [name for name, _ in workers], ["backend-upload", "status-snapshot"]
        )
        for name, target in workers:
            with self.subTest(worker=name):
                self.assertTrue(callable(target))


class SerialRoutingTests(unittest.TestCase):
    """The link carries two envelopes: long-key telemetry and short-key acks.

    Routing on the discriminator rather than assuming telemetry means a frame
    from the other family reports what it actually is instead of failing
    telemetry validation and looking like a firmware fault.
    """

    def setUp(self) -> None:
        self.port = "/dev/ttyUSB0"
        self.outbox = FakeOutbox()
        config = settings([self.port], ["terrabyte-node-01"], Path("/tmp/unused.json"))
        self.service = BridgeService(
            config,
            outbox=self.outbox,
            publisher=SimpleNamespace(close=lambda: None),
            serial_readers=[],
            state=GatewayState(
                gateway_id=config.device_id,
                claim_code=config.claim_code,
                transport=config.transport,
                ports=(self.port,),
                clock=lambda: 1000.0,
            ),
        )

    def port_snapshot(self):
        return next(
            p for p in self.service.state.snapshot().ports if p.path == self.port
        )

    def test_telemetry_still_routes_to_the_validator(self) -> None:
        self.service._ingest_line(self.port, telemetry("terrabyte-node-01"))
        self.assertEqual(len(self.outbox.events), 1)

    def test_a_short_key_frame_reaches_its_own_route(self) -> None:
        seen = []
        self.service._serial_routes["t"] = lambda port, line, message: seen.append(
            (port, message["t"])
        )
        self.service._ingest_line(self.port, b'{"t":"ack","id":"c-1","ph":"ok"}\n')

        self.assertEqual(seen, [(self.port, "ack")])
        # Not counted as a protocol error: it is a valid frame of another family.
        self.assertEqual(self.port_snapshot().errors, 0)

    def test_adding_a_family_is_one_table_entry(self) -> None:
        """The property that makes the ack path an addition and not a rewrite."""

        self.service._serial_routes["v3"] = lambda port, line, message: None
        self.service._ingest_line(self.port, b'{"v3":1}\n')
        self.assertEqual(self.port_snapshot().errors, 0)

    def test_an_unroutable_frame_is_counted_as_an_error(self) -> None:
        self.service._ingest_line(self.port, b'{"nonsense":1}\n')
        self.assertEqual(self.port_snapshot().errors, 1)

    def test_non_json_and_non_object_lines_are_counted_as_errors(self) -> None:
        self.service._ingest_line(self.port, b"not json at all\n")
        self.service._ingest_line(self.port, b"[1,2,3]\n")
        self.assertEqual(self.port_snapshot().errors, 2)
        self.assertEqual(self.outbox.events, [])


if __name__ == "__main__":
    unittest.main()
