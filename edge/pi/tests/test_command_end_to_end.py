"""The command loop through the real service, with real threads.

``backend -> dn/command -> relay -> serial -> ack -> up/ack`` with a software
Arduino at one end and a fake broker at the other. No hardware, no pump, no
network — and still the whole path: the paho-thread hand-off, the dedicated
relay worker, the ingest thread's ack translation, the deadman tick, and the
separate ack drain.

This is the test the unit tests cannot replace. Every rule in
``test_command_relay`` is asserted by calling the relay directly on the test's
own thread, which is exactly the arrangement that hides a keepalive-starving
inline handler. Here the handler really is called from somewhere else.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import time
import unittest

from terrabyte_edge.loopback import LoopbackArduino
from terrabyte_edge.outbox import Outbox
from terrabyte_edge.protocol import epoch_to_iso8601
from terrabyte_edge.publisher import Delivery, DeliveryResult
from terrabyte_edge.serial_reader import SerialLineReader
from terrabyte_edge.service import BridgeService


PORT = "loopback-0"
NODE = "terrabyte-node-01"
GATEWAY = "orangepi-pro-01"
DEADLINE_SECONDS = 5.0


def wait_for(predicate, message: str) -> None:
    deadline = time.monotonic() + DEADLINE_SECONDS
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {message}")


class FakeBroker:
    """Both halves of the MQTT seam: a publisher and a command source."""

    def __init__(self) -> None:
        self.handler = None
        self.telemetry = []
        self.acks = []
        self.closed = False

    # -- Publisher / CommandTransport -----------------------------------

    def send(self, event) -> DeliveryResult:
        self.telemetry.append(event)
        return DeliveryResult(Delivery.DELIVERED, "puback")

    def send_ack(self, ack) -> DeliveryResult:
        self.acks.append(ack)
        return DeliveryResult(Delivery.DELIVERED, "puback")

    def subscribe_commands(self, handler) -> None:
        self.handler = handler

    def close(self) -> None:
        self.closed = True

    # -- the broker side -------------------------------------------------

    def deliver(self, payload: bytes, retained: bool = False) -> None:
        """Hand a command over the way paho does: from another thread."""

        assert self.handler is not None, "the relay never subscribed"
        self.handler(payload, retained)

    def phases(self) -> list[str]:
        return [ack.phase for ack in self.acks]


def command(expires_in_seconds: float = 120.0, **overrides) -> bytes:
    body = {
        "schema_version": 2,
        "message_type": "command",
        "command_id": "01J8F3QK2M7X9ZB4CDEFGH",
        "correlation_id": "3f2b9c0e-7a41-4d88-9c12-5e6f7a8b9c0d",
        "gateway_id": GATEWAY,
        "node_id": NODE,
        "pot_id": 42,
        "actuator": "pump",
        "action": "dose",
        "params": {"volume_ml": 120, "max_runtime_ms": 18000},
        "issued_at": epoch_to_iso8601(time.time()),
        "expires_at": epoch_to_iso8601(time.time() + expires_in_seconds),
        "origin": "CLOUD",
        "issued_by": "RULE_AI",
    }
    body.update(overrides)
    return json.dumps(body).encode()


class EndToEndTests(unittest.TestCase):
    def build(self, *, respond=None) -> tuple[BridgeService, FakeBroker, LoopbackArduino]:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)

        handle = LoopbackArduino(node_id=NODE, respond=respond)
        reader = SerialLineReader(
            port=PORT,
            baudrate=115200,
            timeout_seconds=0.02,
            reconnect_seconds=0.05,
            max_line_bytes=512,
            factory=handle.as_factory(),
        )
        settings = SimpleNamespace(
            device_id=GATEWAY,
            claim_code="483920",
            transport="mqtt",
            database_path=root / "outbox.sqlite3",
            status_snapshot_path=root / "status.json",
            status_snapshot_seconds=0.05,
            upload_batch_size=20,
            upload_interval_seconds=0.02,
            http_timeout_seconds=1.0,
            crop_context_id="ctx-1",
            expected_node_ids=frozenset({NODE}),
            clock_minimum_utc=datetime(2025, 1, 1, tzinfo=timezone.utc),
            pot_substrate_ml={},
            pot_crop_codes={},
            substrate_volume_ml_for=lambda node: None,
            crop_code_for=lambda node: None,
            command_relay_enabled=True,
            command_queue_max=32,
            # Well under the firmware's 3 s so a test does not wait on a tick.
            command_deadman_interval_seconds=0.05,
            command_deadman_grace_seconds=0.5,
            command_max_serial_bytes=120,
            command_journal_retention_seconds=86_400.0,
        )
        outbox = Outbox(
            settings.database_path,
            retry_base_seconds=0.01,
            retry_max_seconds=0.05,
            max_rows=1000,
        )
        broker = FakeBroker()
        service = BridgeService(
            settings, outbox=outbox, publisher=broker, serial_readers=[reader]
        )
        service.start()
        self.addCleanup(self.shut_down, service)
        wait_for(lambda: broker.handler is not None, "the relay to subscribe")
        # The relay routes by the node the cable has actually reported, so the
        # hello frame has to land before a command can be addressed to it.
        wait_for(
            lambda: service.state.snapshot().ports[0].node_id == NODE,
            "the node to announce itself",
        )
        return service, broker, handle

    def shut_down(self, service: BridgeService) -> None:
        service.stop()
        service.join()

    def test_a_command_runs_and_both_outcomes_reach_the_backend(self) -> None:
        service, broker, handle = self.build()

        broker.deliver(command())

        wait_for(lambda: "completed" in broker.phases(), "a completed ack")
        self.assertEqual(broker.phases()[:2], ["accepted", "completed"])

        # The serial side spoke short keys, with the renamed duration.
        (frame,) = [msg for msg in handle.received if msg.get("t") == "cmd"]
        self.assertEqual(frame["id"], "01J8F3QK2M7X9ZB4CDEFGH")
        self.assertEqual(frame["act"], "pump")
        self.assertEqual(frame["ms"], 18000)
        self.assertEqual(frame["ml"], 120)

        # The MQTT side spoke long keys, with the pot the backend has to bill.
        completed = broker.acks[-1].ack_payload(gateway_id=GATEWAY)
        self.assertEqual(completed["message_type"], "command_ack")
        self.assertEqual(completed["command_id"], "01J8F3QK2M7X9ZB4CDEFGH")
        self.assertEqual(completed["pot_id"], 42)
        self.assertEqual(completed["reason"], "OK")
        self.assertEqual(completed["actual"]["stop_cause"], "volume_reached")
        self.assertEqual(completed["actual"]["estimated_ml"], 120)

    def test_two_hours_offline_then_six_queued_commands_water_zero_times(self) -> None:
        """The mandatory case (edge_ai_hardening.md §:497).

        Six commands arrive at once with deadlines two minutes in the past, as
        they would after a reconnect. Not one may reach the pump, and all six
        have to be answered so the server stops waiting on them.
        """

        service, broker, handle = self.build()

        for index in range(6):
            broker.deliver(
                command(expires_in_seconds=-7200, command_id=f"stale-{index}")
            )

        wait_for(lambda: len(broker.acks) == 6, "six rejections")
        self.assertEqual(set(broker.phases()), {"rejected"})
        self.assertEqual({ack.reason for ack in broker.acks}, {"EXPIRED"})
        # The number this whole scenario exists to check.
        self.assertEqual([msg for msg in handle.received if msg.get("t") == "cmd"], [])
        self.assertEqual(service.command_relay.relayed, 0)

    def test_a_retained_command_never_reaches_the_pump(self) -> None:
        service, broker, handle = self.build()

        broker.deliver(command(), True)
        # Nothing to wait for, so give the relay a chance to be wrong.
        time.sleep(0.2)

        self.assertEqual([msg for msg in handle.received if msg.get("t") == "cmd"], [])
        self.assertEqual(broker.acks, [])
        self.assertEqual(service.command_relay.dropped, 1)

    def test_a_dose_with_no_terminal_ack_is_kept_alive_by_the_deadman(self) -> None:
        """The sending side of the firmware's G3 watchdog.

        The double answers ``accepted`` and then goes quiet, as a real board does
        while the pump is actually running. Real firmware would stop after three
        seconds of silence from the host, so the ticks have to be arriving.
        """

        service, broker, handle = self.build(
            respond=lambda message: (
                [] if message.get("t") == "ka"
                else [{"t": "ack", "id": message["id"], "ph": "accepted"}]
            )
        )

        broker.deliver(command())

        wait_for(
            lambda: len([msg for msg in handle.received if msg.get("t") == "ka"]) >= 3,
            "three deadman ticks",
        )
        self.assertEqual(broker.phases(), ["accepted"])
        self.assertIn("01J8F3QK2M7X9ZB4CDEFGH", tuple(service.command_relay.in_flight_ids()))

    def test_telemetry_keeps_flowing_while_commands_are_handled(self) -> None:
        """The two paths share the ingest thread and the queue, not a bottleneck."""

        service, broker, handle = self.build()

        handle.queue_telemetry()
        broker.deliver(command())

        wait_for(lambda: "completed" in broker.phases(), "a completed ack")
        wait_for(lambda: len(broker.telemetry) >= 1, "a delivered reading")
        self.assertEqual(broker.telemetry[0].node_id, NODE)

    def test_an_ack_that_cannot_be_published_yet_is_not_lost(self) -> None:
        """The outbox is why a pump result survives a broker outage.

        A merely-logged ack becomes a phantom budget deduction: the backend
        expires the command and charges the granted volume to the daily budget
        regardless.
        """

        service, broker, handle = self.build()
        broker.send_ack = lambda ack: DeliveryResult(Delivery.RETRY, "not_connected")

        broker.deliver(command())
        wait_for(
            lambda: service.outbox.counts(kind="ack")[0] >= 2,
            "both acks to be queued durably",
        )

        published: list = []
        broker.send_ack = lambda ack: (
            published.append(ack) or DeliveryResult(Delivery.DELIVERED, "puback")
        )
        wait_for(lambda: len(published) >= 2, "the queue to drain once reconnected")
        self.assertEqual([ack.phase for ack in published][:2], ["accepted", "completed"])


if __name__ == "__main__":
    unittest.main()
