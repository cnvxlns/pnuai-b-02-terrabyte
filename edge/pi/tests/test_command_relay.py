"""Command relay: TTL, translation, dedup, deadman.

No broker and no Arduino. The relay is driven directly so each rule can be
asserted on its own; ``test_command_end_to_end`` then runs the same code with
real threads and the software Arduino.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from threading import Event
import time
import unittest

from terrabyte_edge.command_relay import (
    DEADMAN_FRAME,
    FIRMWARE_REASONS,
    MQTT_REASONS,
    PUMP_ABS_MAX_MS,
    CommandError,
    CommandJournal,
    CommandRelay,
    STOP_PI_LINK_HELD,
    mqtt_reason,
    parse_command,
    serial_command_frame,
)
from terrabyte_edge.loopback import ABS_MAX_RUN_MS
from terrabyte_edge.outbox import KIND_ACK, KIND_CONTROL
from terrabyte_edge.protocol import (
    ProtocolError,
    epoch_to_iso8601,
    parse_iso8601_utc,
    parse_serial_ack,
)
from terrabyte_edge.state import GatewayState


PORT = "/dev/serial/by-path/port-0"
OTHER_PORT = "/dev/serial/by-path/port-1"
NODE = "terrabyte-node-01"
GATEWAY = "orangepi-pro-01"

# Fixed instants, so no test depends on when it runs.
NOW = 1_800_000_000.0
FUTURE = epoch_to_iso8601(NOW + 120)
PAST = epoch_to_iso8601(NOW - 1)


def command(**overrides) -> bytes:
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
        "issued_at": epoch_to_iso8601(NOW),
        "expires_at": FUTURE,
        "origin": "CLOUD",
        "issued_by": "RULE_AI",
    }
    params = overrides.pop("params", None)
    body.update(overrides)
    if params is not None:
        body["params"] = params
    return json.dumps(body).encode()


def light_command(*, on: bool = True, **overrides) -> bytes:
    action = overrides.pop("action", "on" if on else "off")
    params = overrides.pop("params", {"on": on})
    return command(
        actuator="light", action=action, params=params, **overrides
    )


class FakeReader:
    def __init__(self, port: str, *, up: bool = True) -> None:
        self.port = port
        self.up = up
        self.written: list[bytes] = []

    def write_line(self, payload: bytes) -> bool:
        if not self.up:
            return False
        self.written.append(payload)
        return True

    def commands(self) -> list[dict]:
        return [
            json.loads(line)
            for line in self.written
            if json.loads(line).get("t") == "cmd"
        ]


class FakeTransport:
    def __init__(self) -> None:
        self.handler = None

    def subscribe_commands(self, handler) -> None:
        self.handler = handler

    def send_ack(self, ack):  # pragma: no cover - the relay never publishes
        raise AssertionError("the relay must queue acks, never publish them")


class RecordingOutbox:
    def __init__(self) -> None:
        self.acks = []
        self.control = []
        self.seen: set[str] = set()

    def enqueue(self, message, *, kind: str) -> bool:
        assert kind in (KIND_ACK, KIND_CONTROL)
        if message.event_id in self.seen:
            return False
        self.seen.add(message.event_id)
        if kind == KIND_CONTROL:
            self.control.append(message)
        else:
            self.acks.append(message)
        return True

    def phases(self) -> list[str]:
        return [ack.phase for ack in self.acks]

    def reasons(self) -> list[str]:
        return [ack.reason for ack in self.acks]


class RelayFixture:
    """One relay wired to fakes, with a clock the test advances."""

    def __init__(self, testcase: unittest.TestCase, *, ports=(PORT,), node_ports=None):
        self.tempdir = tempfile.TemporaryDirectory()
        testcase.addCleanup(self.tempdir.cleanup)
        self.now = [NOW]
        self.readers = [FakeReader(port) for port in ports]
        self.state = GatewayState(
            gateway_id=GATEWAY,
            claim_code="483920",
            transport="mqtt",
            ports=tuple(ports),
            clock=lambda: self.now[0],
        )
        # The relay resolves a node to a cable from observed traffic, so the
        # nodes have to have been heard from.
        for port, node_id in (node_ports or {ports[0]: NODE}).items():
            self.state.record_frame(port, node_id=node_id, measurements={})
        self.outbox = RecordingOutbox()
        self.transport = FakeTransport()
        self.journal = CommandJournal(
            Path(self.tempdir.name) / "outbox.sqlite3",
            retention_seconds=86_400.0,
            clock=lambda: self.now[0],
        )
        self.journal.initialize()
        self.relay = CommandRelay(
            gateway_id=GATEWAY,
            transport=self.transport,
            outbox=self.outbox,
            state=self.state,
            readers=self.readers,
            journal=self.journal,
            stop_event=Event(),
            deadman_grace_seconds=5.0,
            clock=lambda: self.now[0],
        )

    @property
    def reader(self) -> FakeReader:
        return self.readers[0]

    def advance(self, seconds: float) -> None:
        self.now[0] += seconds


class TimestampTests(unittest.TestCase):
    """The single highest-probability bug in this stream, from both directions.

    ``expires_at`` is compared against epoch seconds. A naive datetime's
    ``.timestamp()`` is interpreted in the *local* zone, so on an Asia/Seoul box
    a UTC deadline read naively lands nine hours early and everything looks
    expired; flip the comparison around and nothing ever expires. Both are
    silent, so both are pinned here.
    """

    def setUp(self) -> None:
        self.original_tz = None
        if hasattr(time, "tzset"):
            import os

            self.original_tz = os.environ.get("TZ")
            os.environ["TZ"] = "Asia/Seoul"
            time.tzset()
            self.addCleanup(self.restore_tz)

    def restore_tz(self) -> None:
        import os

        if self.original_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.original_tz
        time.tzset()

    def test_a_z_suffixed_instant_is_utc(self) -> None:
        parsed = parse_iso8601_utc("2026-08-04T10:02:00Z")
        self.assertEqual(parsed.tzinfo, timezone.utc)
        self.assertEqual(
            parsed.timestamp(),
            datetime(2026, 8, 4, 10, 2, tzinfo=timezone.utc).timestamp(),
        )

    def test_an_explicit_offset_is_honoured_not_treated_as_utc(self) -> None:
        seoul = parse_iso8601_utc("2026-08-04T19:02:00+09:00")
        self.assertEqual(
            seoul.timestamp(),
            datetime(2026, 8, 4, 10, 2, tzinfo=timezone.utc).timestamp(),
        )

    def test_a_naive_timestamp_is_refused_rather_than_guessed(self) -> None:
        with self.assertRaises(ProtocolError):
            parse_iso8601_utc("2026-08-04T10:02:00")

    def test_a_future_command_does_not_expire_under_a_non_utc_local_zone(self) -> None:
        """The "always expires" direction: a nine-hour-early deadline."""

        fixture = RelayFixture(self)
        fixture.relay._process(command(expires_at=FUTURE))
        self.assertEqual(len(fixture.reader.commands()), 1)
        self.assertEqual(fixture.outbox.acks, [])

    def test_a_past_command_still_expires_under_a_non_utc_local_zone(self) -> None:
        """The "never expires" direction: a nine-hour-late deadline."""

        fixture = RelayFixture(self)
        fixture.relay._process(command(expires_at=PAST))
        self.assertEqual(fixture.reader.commands(), [])
        self.assertEqual(fixture.outbox.reasons(), ["EXPIRED"])

    def test_epoch_round_trips_through_the_wire_format(self) -> None:
        self.assertEqual(parse_iso8601_utc(epoch_to_iso8601(NOW)).timestamp(), NOW)


class ReasonMappingTests(unittest.TestCase):
    def test_every_mapped_value_is_in_the_frozen_mqtt_vocabulary(self) -> None:
        for token, reason in FIRMWARE_REASONS.items():
            with self.subTest(token=token):
                self.assertIn(reason, MQTT_REASONS)

    def test_the_firmware_cooldown_becomes_the_interlock_reason(self) -> None:
        """Not Java's DenyReason.COOLDOWN: that is a pre-publish server gate."""

        self.assertEqual(mqtt_reason("rejected", "cooldown"), "INTERLOCK_COOLDOWN")

    def test_busy_maps_to_an_interlock_and_never_to_node_offline(self) -> None:
        """``busy`` has no counterpart among the eight, and the node did answer."""

        self.assertEqual(mqtt_reason("rejected", "busy"), "INTERLOCK_COOLDOWN")
        self.assertNotEqual(mqtt_reason("rejected", "busy"), "NODE_OFFLINE")

    def test_the_safety_clamp_is_reported_as_a_success(self) -> None:
        """G1 stopping a 60 s request at 30 s is the limit working, not a fault."""

        self.assertEqual(mqtt_reason("completed", "max_runtime"), "OK")
        self.assertEqual(mqtt_reason("completed", "volume_reached"), "OK")

    def test_an_unmapped_token_falls_back_per_phase_without_inventing_an_event(
        self,
    ) -> None:
        self.assertEqual(mqtt_reason("rejected", "bad_request"), "INTERLOCK_COOLDOWN")
        self.assertEqual(mqtt_reason("completed", "who_knows"), "OK")
        self.assertEqual(mqtt_reason("aborted", "who_knows"), "WATCHDOG")
        self.assertEqual(mqtt_reason("accepted", None), "OK")


class CommandParsingTests(unittest.TestCase):
    def test_the_long_keys_are_read_including_the_nested_params(self) -> None:
        request = parse_command(command())
        self.assertEqual(request.command_id, "01J8F3QK2M7X9ZB4CDEFGH")
        self.assertEqual(request.pot_id, 42)
        self.assertEqual(request.volume_ml, 120)
        self.assertEqual(request.max_runtime_ms, 18000)

    def test_a_payload_with_no_command_id_is_unanswerable(self) -> None:
        for payload in (b"{}", b"not json", b'{"command_id": ""}', b"[]"):
            with self.subTest(payload=payload):
                with self.assertRaises(CommandError):
                    parse_command(payload)

    def test_an_unusable_expires_at_leaves_the_deadline_unknown(self) -> None:
        request = parse_command(command(expires_at="whenever"))
        self.assertIsNone(request.expires_at_epoch)
        self.assertEqual(request.expires_at_raw, "whenever")

    def test_an_integral_float_is_accepted_but_a_fractional_one_is_not(self) -> None:
        whole = parse_command(command(params={"volume_ml": 120.0, "max_runtime_ms": 18000}))
        self.assertEqual(whole.volume_ml, 120)
        fractional = parse_command(
            command(params={"volume_ml": 120.5, "max_runtime_ms": 18000})
        )
        self.assertIsNone(fractional.volume_ml)

    def test_light_on_is_read_strictly_but_left_for_validation_when_bad(self) -> None:
        self.assertTrue(parse_command(light_command()).on)
        self.assertFalse(parse_command(light_command(on=False)).on)
        self.assertIsNone(
            parse_command(light_command(params={"on": 1})).on
        )


class SerialFrameTests(unittest.TestCase):
    def test_the_keys_are_renamed_to_the_short_serial_spelling(self) -> None:
        frame = json.loads(serial_command_frame(parse_command(command())))
        self.assertEqual(frame["t"], "cmd")
        self.assertEqual(frame["act"], "pump")
        # The rename that is easy to get wrong.
        self.assertEqual(frame["ms"], 18000)
        self.assertEqual(frame["ml"], 120)
        self.assertNotIn("max_runtime_ms", frame)

    def test_an_over_long_runtime_is_not_clamped_here(self) -> None:
        """Clamping would make the firmware report a full dose for a partial one."""

        frame = json.loads(
            serial_command_frame(
                parse_command(command(params={"volume_ml": 120, "max_runtime_ms": 240000}))
            )
        )
        self.assertEqual(frame["ms"], 240000)
        self.assertGreater(frame["ms"], PUMP_ABS_MAX_MS)

    def test_a_light_frame_translates_to_led_and_has_no_pump_fields(self) -> None:
        frame = json.loads(serial_command_frame(parse_command(light_command())))
        self.assertEqual(
            frame,
            {
                "t": "cmd",
                "id": "01J8F3QK2M7X9ZB4CDEFGH",
                "act": "led",
                "on": 1,
            },
        )
        self.assertNotIn("ms", frame)
        self.assertNotIn("ml", frame)

    def test_volume_is_omitted_when_the_command_did_not_size_one(self) -> None:
        frame = json.loads(
            serial_command_frame(parse_command(command(params={"max_runtime_ms": 9000})))
        )
        self.assertNotIn("ml", frame)

    def test_the_widest_legal_frame_fits_the_configured_ceiling(self) -> None:
        """The firmware reads into a fixed buffer on a 2 KB device."""

        widest = serial_command_frame(
            parse_command(
                command(
                    command_id="0" * 26,
                    params={"volume_ml": 9999, "max_runtime_ms": 4294967295},
                )
            )
        )
        self.assertLessEqual(len(widest), 96)

    def test_the_firmware_run_limit_mirror_agrees_with_the_test_double(self) -> None:
        """Two independent mirrors of one C++ constant; drift must be visible."""

        # TelemetryConfig.h/TB_PUMP_ABS_MAX_MS is authoritative; pin both
        # Python mirrors to its value so they cannot drift together unnoticed.
        self.assertEqual(PUMP_ABS_MAX_MS, 210_000)
        self.assertEqual(PUMP_ABS_MAX_MS, ABS_MAX_RUN_MS)


class JournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "state" / "outbox.sqlite3"
        self.now = [NOW]

    def open(self) -> CommandJournal:
        journal = CommandJournal(
            self.path, retention_seconds=100.0, clock=lambda: self.now[0]
        )
        journal.initialize()
        return journal

    def test_a_command_id_can_only_be_claimed_once(self) -> None:
        journal = self.open()
        request = parse_command(command())
        self.assertTrue(journal.claim(request))
        self.assertFalse(journal.claim(request))

    def test_the_claim_survives_a_restart(self) -> None:
        """An in-memory set would forget, and the redelivered command would run."""

        request = parse_command(command())
        self.assertTrue(self.open().claim(request))
        self.assertFalse(self.open().claim(request))

    def test_the_echo_fields_are_recoverable_after_a_restart(self) -> None:
        request = parse_command(command())
        self.open().claim(request)
        context = self.open().context(request.command_id)
        self.assertEqual(context["pot_id"], 42)
        self.assertEqual(context["max_runtime_ms"], 18000)

        light = parse_command(light_command(command_id="light-1"))
        self.open().claim(light)
        light_context = self.open().context(light.command_id)
        self.assertEqual(light_context["actuator"], "light")
        self.assertTrue(light_context["on"])

    def test_pruning_bounds_the_table_without_reopening_the_window(self) -> None:
        journal = self.open()
        journal.claim(parse_command(command()))
        self.now[0] += 50
        self.assertEqual(journal.prune(), 0)
        self.now[0] += 51
        self.assertEqual(journal.prune(), 1)


class RelayDecisionTests(unittest.TestCase):
    def test_a_valid_command_reaches_the_node_with_no_ack_of_its_own(self) -> None:
        """The firmware answers; the relay does not pre-empt it with an ack."""

        fixture = RelayFixture(self)
        fixture.relay._process(command())

        (frame,) = fixture.reader.commands()
        self.assertEqual(frame["id"], "01J8F3QK2M7X9ZB4CDEFGH")
        self.assertEqual(fixture.outbox.acks, [])
        self.assertEqual(fixture.relay.relayed, 1)

    def test_an_expired_command_is_never_written_to_the_serial_link(self) -> None:
        fixture = RelayFixture(self)
        fixture.relay._process(command(expires_at=PAST))

        self.assertEqual(fixture.reader.written, [])
        (ack,) = fixture.outbox.acks
        self.assertEqual(ack.phase, "rejected")
        self.assertEqual(ack.reason, "EXPIRED")
        self.assertEqual(ack.stop_cause, "pi_expired")
        # The echoed fields the backend needs to attribute the refusal.
        self.assertEqual(ack.pot_id, 42)
        self.assertEqual(ack.correlation_id, "3f2b9c0e-7a41-4d88-9c12-5e6f7a8b9c0d")

    def test_a_command_that_expires_while_it_waits_in_the_queue_is_refused(self) -> None:
        """TTL is judged at dequeue, which is as late as the relay can judge it."""

        fixture = RelayFixture(self)
        payload = command()
        fixture.advance(121)
        fixture.relay._process(payload)

        self.assertEqual(fixture.reader.written, [])
        self.assertEqual(fixture.outbox.reasons(), ["EXPIRED"])

    def test_the_delayed_bomb_queue_waters_exactly_zero_times(self) -> None:
        """The mandatory case (§:497). Two hours offline, six queued commands.

        Every one of them is answered so the server learns, and not one of them
        reaches the pump.
        """

        fixture = RelayFixture(self)
        queued = [command(command_id=f"cmd-{index}") for index in range(6)]
        fixture.advance(2 * 3600)
        for payload in queued:
            fixture.relay._process(payload)

        self.assertEqual(fixture.reader.commands(), [])
        self.assertEqual(fixture.relay.relayed, 0)
        self.assertEqual(len(fixture.outbox.acks), 6)
        self.assertEqual(set(fixture.outbox.reasons()), {"EXPIRED"})
        self.assertEqual(set(fixture.outbox.phases()), {"rejected"})

    def test_a_command_with_no_readable_deadline_is_treated_as_expired(self) -> None:
        """Fail-safe: the alternative is running a dose with no deadline at all."""

        fixture = RelayFixture(self)
        fixture.relay._process(command(expires_at="not-a-timestamp"))

        self.assertEqual(fixture.reader.written, [])
        self.assertEqual(fixture.outbox.reasons(), ["EXPIRED"])
        self.assertEqual(fixture.outbox.acks[0].stop_cause, "pi_no_expires_at")

    def test_a_redelivered_command_is_refused_without_running_twice(self) -> None:
        """QoS 1 permits a duplicate at both hops."""

        fixture = RelayFixture(self)
        payload = command()
        fixture.relay._process(payload)
        fixture.relay._process(payload)

        self.assertEqual(len(fixture.reader.commands()), 1)
        self.assertEqual(fixture.outbox.reasons(), ["DUPLICATE"])
        self.assertEqual(fixture.outbox.acks[0].stop_cause, "pi_duplicate")

    def test_a_command_for_another_gateway_is_dropped_unanswered(self) -> None:
        """An ack from us would be a forgery by the backend's own check."""

        fixture = RelayFixture(self)
        fixture.relay._process(command(gateway_id="orangepi-pro-99"))

        self.assertEqual(fixture.reader.written, [])
        self.assertEqual(fixture.outbox.acks, [])
        self.assertEqual(fixture.relay.dropped, 1)

    def test_an_unknown_envelope_version_is_refused_not_guessed_at(self) -> None:
        fixture = RelayFixture(self)
        fixture.relay._process(command(schema_version=3))

        self.assertEqual(fixture.reader.written, [])
        self.assertEqual(fixture.outbox.acks[0].stop_cause, "pi_bad_schema")

    def test_an_unsupported_actuator_or_action_is_refused(self) -> None:
        for overrides in ({"actuator": "heater"}, {"action": "abort"}):
            with self.subTest(**overrides):
                fixture = RelayFixture(self)
                fixture.relay._process(command(**overrides))
                self.assertEqual(fixture.reader.written, [])
                self.assertEqual(fixture.outbox.acks[0].stop_cause, "pi_bad_actuator")

    def test_unusable_parameters_are_refused(self) -> None:
        for params in (
            {},
            {"max_runtime_ms": 0},
            {"max_runtime_ms": -1},
            {"max_runtime_ms": 5_000_000_000},
            {"max_runtime_ms": 9000, "volume_ml": 0},
        ):
            with self.subTest(params=params):
                fixture = RelayFixture(self)
                fixture.relay._process(command(params=params))
                self.assertEqual(fixture.reader.written, [])
                self.assertEqual(fixture.outbox.acks[0].stop_cause, "pi_bad_params")

    def test_light_action_and_on_must_agree(self) -> None:
        for action, params in (
            ("on", {"on": False}),
            ("off", {"on": True}),
            ("on", {"on": 1}),
            ("dose", {"on": True}),
        ):
            with self.subTest(action=action, params=params):
                fixture = RelayFixture(self)
                fixture.relay._process(light_command(action=action, params=params))
                self.assertEqual(fixture.reader.written, [])
                self.assertEqual(fixture.outbox.acks[0].stop_cause, "pi_bad_params")

    def test_light_does_not_require_pump_runtime_or_volume(self) -> None:
        fixture = RelayFixture(self)
        fixture.relay._process(light_command())

        (frame,) = fixture.reader.commands()
        self.assertEqual(frame["act"], "led")
        self.assertNotIn("ms", frame)
        self.assertNotIn("ml", frame)

    def test_a_node_nobody_has_reported_is_offline_not_assumed(self) -> None:
        """Falling back to "the only port we have" would water the wrong pot."""

        fixture = RelayFixture(self, node_ports={PORT: "terrabyte-node-77"})
        fixture.relay._process(command())

        self.assertEqual(fixture.reader.written, [])
        self.assertEqual(fixture.outbox.reasons(), ["NODE_OFFLINE"])
        self.assertEqual(fixture.outbox.acks[0].stop_cause, "pi_unknown_node")

    def test_a_node_id_claimed_by_two_ports_is_refused(self) -> None:
        """Two Arduinos on one TB_NODE_ID: watering both is worse than neither."""

        fixture = RelayFixture(self, ports=(PORT, OTHER_PORT), node_ports={})
        # Bypassing record_frame's duplicate guard, which only fires on the
        # second port; a snapshot can still show one id twice after a swap.
        fixture.state._ports[PORT].node_id = NODE
        fixture.state._ports[OTHER_PORT].node_id = NODE
        fixture.relay._process(command())

        self.assertEqual(fixture.readers[0].written, [])
        self.assertEqual(fixture.readers[1].written, [])
        self.assertEqual(fixture.outbox.acks[0].stop_cause, "pi_ambiguous_node")

    def test_a_command_is_routed_to_the_port_that_reported_its_node(self) -> None:
        fixture = RelayFixture(
            self,
            ports=(PORT, OTHER_PORT),
            node_ports={PORT: "terrabyte-node-02", OTHER_PORT: NODE},
        )
        fixture.relay._process(command())

        self.assertEqual(fixture.readers[0].written, [])
        self.assertEqual(len(fixture.readers[1].commands()), 1)

    def test_a_down_link_is_reported_and_leaves_nothing_in_flight(self) -> None:
        """A command that cannot reach the pump has to be answerable."""

        fixture = RelayFixture(self)
        fixture.reader.up = False
        fixture.relay._process(command())

        self.assertEqual(fixture.outbox.reasons(), ["NODE_OFFLINE"])
        self.assertEqual(fixture.outbox.acks[0].stop_cause, "pi_link_down")
        self.assertEqual(tuple(fixture.relay.in_flight_ids()), ())

    def test_an_over_long_frame_is_refused_rather_than_truncated(self) -> None:
        """Truncating would overrun a fixed buffer on the ATmega."""

        fixture = RelayFixture(self)
        fixture.relay._max_serial_bytes = 40
        fixture.relay._process(command())

        self.assertEqual(fixture.reader.written, [])
        self.assertEqual(fixture.outbox.acks[0].stop_cause, "pi_frame_too_long")


class NetworkThreadTests(unittest.TestCase):
    """``offer`` runs on paho's network thread and must do nothing slow there."""

    def test_offer_only_queues_and_never_touches_the_serial_link(self) -> None:
        fixture = RelayFixture(self)
        fixture.relay.offer(command())

        self.assertEqual(fixture.reader.written, [])
        self.assertEqual(fixture.outbox.acks, [])
        self.assertEqual(fixture.relay._queue.qsize(), 1)

    def test_a_retained_command_is_dropped_and_never_executed(self) -> None:
        """A retained command re-executes stale irrigation on every reconnect."""

        fixture = RelayFixture(self)
        fixture.relay.offer(command(), True)

        self.assertEqual(fixture.relay._queue.qsize(), 0)
        self.assertEqual(fixture.relay.dropped, 1)
        self.assertEqual(fixture.reader.written, [])

    def test_a_full_queue_drops_instead_of_blocking_the_network_thread(self) -> None:
        """Blocking here starves the MQTT keepalive."""

        fixture = RelayFixture(self)
        fixture.relay._queue.maxsize = 2
        for index in range(5):
            fixture.relay.offer(command(command_id=f"cmd-{index}"))

        self.assertEqual(fixture.relay._queue.qsize(), 2)
        self.assertEqual(fixture.relay.dropped, 3)


class AckTranslationTests(unittest.TestCase):
    def relay_a_command(self, fixture: RelayFixture, **overrides) -> None:
        fixture.relay._process(command(**overrides))

    def test_accepted_then_completed_are_two_acks_the_backend_can_see(self) -> None:
        fixture = RelayFixture(self)
        self.relay_a_command(fixture)
        fixture.relay.handle_serial_ack(
            PORT, {"t": "ack", "id": "01J8F3QK2M7X9ZB4CDEFGH", "ph": "accepted"}
        )
        fixture.relay.handle_serial_ack(
            PORT,
            {
                "t": "ack",
                "id": "01J8F3QK2M7X9ZB4CDEFGH",
                "ph": "completed",
                "ms": 17950,
                "stop": "volume_reached",
            },
        )

        self.assertEqual(fixture.outbox.phases(), ["accepted", "completed"])
        completed = fixture.outbox.acks[1]
        self.assertEqual(completed.reason, "OK")
        self.assertEqual(completed.runtime_ms, 17950)
        self.assertEqual(completed.stop_cause, "volume_reached")
        self.assertEqual(completed.node_id, NODE)
        self.assertEqual(completed.pot_id, 42)
        self.assertEqual(
            completed.correlation_id, "3f2b9c0e-7a41-4d88-9c12-5e6f7a8b9c0d"
        )

    def test_the_firmware_token_survives_verbatim_in_stop_cause(self) -> None:
        """Three vocabularies, and this is the field the raw word travels in."""

        fixture = RelayFixture(self)
        self.relay_a_command(fixture)
        fixture.relay.handle_serial_ack(
            PORT,
            {"t": "ack", "id": "01J8F3QK2M7X9ZB4CDEFGH", "ph": "rejected", "r": "busy"},
        )

        (ack,) = fixture.outbox.acks
        self.assertEqual(ack.reason, "INTERLOCK_COOLDOWN")
        self.assertEqual(ack.stop_cause, "busy")

    def test_the_envelope_carries_the_frozen_mqtt_field_names(self) -> None:
        fixture = RelayFixture(self)
        self.relay_a_command(fixture)
        fixture.relay.handle_serial_ack(
            PORT,
            {
                "t": "ack",
                "id": "01J8F3QK2M7X9ZB4CDEFGH",
                "ph": "aborted",
                "ms": 3020,
                "stop": "watchdog",
            },
        )

        body = fixture.outbox.acks[0].ack_payload(gateway_id=GATEWAY)
        self.assertEqual(body["schema_version"], 2)
        self.assertEqual(body["message_type"], "command_ack")
        self.assertEqual(body["gateway_id"], GATEWAY)
        self.assertEqual(body["phase"], "aborted")
        self.assertEqual(body["reason"], "WATCHDOG")
        self.assertEqual(body["actual"]["runtime_ms"], 3020)
        self.assertEqual(body["actual"]["stop_cause"], "watchdog")
        self.assertTrue(body["at"].endswith("Z"))

    def test_a_clamped_run_reports_the_water_that_actually_moved(self) -> None:
        """G1 shortens a 240 s dose. Charging the full volume inflates the budget."""

        fixture = RelayFixture(self)
        self.relay_a_command(
            fixture, params={"volume_ml": 120, "max_runtime_ms": 240000}
        )
        fixture.relay.handle_serial_ack(
            PORT,
            {
                "t": "ack",
                "id": "01J8F3QK2M7X9ZB4CDEFGH",
                "ph": "completed",
                "ms": 210000,
                "stop": "max_runtime",
            },
        )

        (ack,) = fixture.outbox.acks
        self.assertEqual(ack.reason, "OK")
        self.assertEqual(ack.estimated_ml, 105)

    def test_a_measured_volume_from_the_firmware_wins_over_the_estimate(self) -> None:
        fixture = RelayFixture(self)
        self.relay_a_command(fixture)
        fixture.relay.handle_serial_ack(
            PORT,
            {
                "t": "ack",
                "id": "01J8F3QK2M7X9ZB4CDEFGH",
                "ph": "completed",
                "ms": 9000,
                "ml": 61,
            },
        )

        self.assertEqual(fixture.outbox.acks[0].estimated_ml, 61)

    def test_an_ack_after_a_restart_is_still_fully_formed(self) -> None:
        """The journal, not the in-memory table, is what makes this work."""

        fixture = RelayFixture(self)
        self.relay_a_command(fixture)
        fixture.relay._discard_in_flight("01J8F3QK2M7X9ZB4CDEFGH")
        fixture.relay.handle_serial_ack(
            PORT,
            {
                "t": "ack",
                "id": "01J8F3QK2M7X9ZB4CDEFGH",
                "ph": "completed",
                "ms": 18000,
            },
        )

        (ack,) = fixture.outbox.acks
        self.assertEqual(ack.pot_id, 42)
        self.assertEqual(ack.estimated_ml, 120)

    def test_an_ack_for_an_unknown_command_still_reaches_the_backend(self) -> None:
        """It names the node from the cable it arrived on, and nothing else."""

        fixture = RelayFixture(self)
        fixture.relay.handle_serial_ack(
            PORT, {"t": "ack", "id": "who-dis", "ph": "completed", "ms": 1000}
        )

        (ack,) = fixture.outbox.acks
        self.assertEqual(ack.command_id, "who-dis")
        self.assertEqual(ack.node_id, NODE)
        self.assertIsNone(ack.pot_id)

    def test_a_repeated_ack_collapses_onto_one_queue_row(self) -> None:
        fixture = RelayFixture(self)
        self.relay_a_command(fixture)
        frame = {
            "t": "ack",
            "id": "01J8F3QK2M7X9ZB4CDEFGH",
            "ph": "completed",
            "ms": 900,
        }
        fixture.relay.handle_serial_ack(PORT, frame)
        fixture.relay.handle_serial_ack(PORT, frame)

        self.assertEqual(len(fixture.outbox.acks), 1)

    def test_a_malformed_ack_is_counted_against_the_port_not_forwarded(self) -> None:
        fixture = RelayFixture(self)
        for frame in (
            {"t": "ack"},
            {"t": "ack", "id": "c1"},
            {"t": "ack", "id": "c1", "ph": "sideways"},
        ):
            with self.subTest(frame=frame):
                fixture.relay.handle_serial_ack(PORT, frame)
        self.assertEqual(fixture.outbox.acks, [])
        self.assertEqual(fixture.state.snapshot().ports[0].errors, 3)

    def test_an_over_long_firmware_token_is_truncated_to_the_column_width(self) -> None:
        """A 40-character cause would fail the backend INSERT and lose the ack."""

        ack = parse_serial_ack(
            {"t": "ack", "id": "c1", "ph": "aborted", "stop": "x" * 40}
        )
        self.assertEqual(len(ack.stop_cause), 30)

    def test_an_unusable_detail_field_does_not_cost_the_whole_outcome(self) -> None:
        """id and ph make an ack mean something; ms and stop are detail.

        Dropping a ``completed`` over a malformed ``ms`` would leave the backend
        to expire the command and charge its granted volume to the budget anyway.
        """

        ack = parse_serial_ack(
            {"t": "ack", "id": "c1", "ph": "completed", "ms": -5, "stop": 17}
        )
        self.assertEqual(ack.phase, "completed")
        self.assertIsNone(ack.runtime_ms)
        self.assertIsNone(ack.stop_cause)

    def test_light_accepted_is_terminal_and_carries_the_echo_to_the_backend(
        self,
    ) -> None:
        fixture = RelayFixture(self)
        fixture.relay._process(light_command())
        fixture.relay.handle_serial_ack(
            PORT,
            {
                "t": "ack",
                "id": "01J8F3QK2M7X9ZB4CDEFGH",
                "ph": "accepted",
                "on": 1,
            },
        )

        self.assertEqual(tuple(fixture.relay.in_flight_ids()), ())
        self.assertEqual(fixture.relay._pending_lights, {})
        (ack,) = fixture.outbox.acks
        self.assertTrue(ack.on)
        self.assertIsNone(ack.estimated_ml)
        self.assertEqual(
            ack.ack_payload(gateway_id=GATEWAY)["actual"], {"on": True}
        )

    def test_light_latch_uses_the_echoed_state_not_the_requested_state(self) -> None:
        fixture = RelayFixture(self)
        fixture.relay._process(light_command())
        fixture.relay.handle_serial_ack(
            PORT,
            {
                "t": "ack",
                "id": "01J8F3QK2M7X9ZB4CDEFGH",
                "ph": "accepted",
                "on": 0,
            },
        )

        self.assertEqual(fixture.relay._light_latches, {})
        self.assertFalse(fixture.outbox.acks[0].on)

    def test_light_watchdog_abort_clears_the_latch_without_estimating_water(
        self,
    ) -> None:
        fixture = RelayFixture(self)
        fixture.relay._process(light_command())
        fixture.relay.handle_serial_ack(
            PORT,
            {
                "t": "ack",
                "id": "01J8F3QK2M7X9ZB4CDEFGH",
                "ph": "accepted",
                "on": 1,
            },
        )
        fixture.relay.handle_serial_ack(
            PORT,
            {
                "t": "ack",
                "id": "01J8F3QK2M7X9ZB4CDEFGH",
                "ph": "aborted",
                "ms": 60_000,
                "stop": "watchdog",
            },
        )

        self.assertEqual(fixture.relay._light_latches, {})
        aborted = fixture.outbox.acks[1]
        self.assertEqual(aborted.runtime_ms, 60_000)
        self.assertIsNone(aborted.estimated_ml)


class DeadmanTests(unittest.TestCase):
    def test_a_running_dose_gets_a_tick_and_a_finished_one_does_not(self) -> None:
        fixture = RelayFixture(self)
        fixture.relay._process(command())
        fixture.relay.tick_deadman()
        self.assertIn(DEADMAN_FRAME, fixture.reader.written)

        before = len(fixture.reader.written)
        fixture.relay.handle_serial_ack(
            PORT,
            {
                "t": "ack",
                "id": "01J8F3QK2M7X9ZB4CDEFGH",
                "ph": "completed",
                "ms": 18000,
            },
        )
        fixture.relay.tick_deadman()
        self.assertEqual(len(fixture.reader.written), before)

    def test_nothing_is_ticked_when_no_dose_is_running(self) -> None:
        """The tick is deadman evidence, not a generic keepalive."""

        fixture = RelayFixture(self)
        fixture.relay.tick_deadman()
        self.assertEqual(fixture.reader.written, [])

    def test_a_rejected_command_never_starts_the_tick(self) -> None:
        fixture = RelayFixture(self)
        fixture.relay._process(command(expires_at=PAST))
        fixture.relay.tick_deadman()
        self.assertEqual(fixture.reader.written, [])

    def test_ticking_stops_when_the_run_window_closes_without_an_ack(self) -> None:
        """Silence lets G3 stop the pump; ticking forever would hold it open."""

        fixture = RelayFixture(self)
        fixture.relay._process(command())
        self.assertEqual(len(fixture.reader.commands()), 1)
        fixture.advance(18 + 5 + 1)
        fixture.relay.tick_deadman()

        self.assertNotIn(DEADMAN_FRAME, fixture.reader.written)
        self.assertEqual(tuple(fixture.relay.in_flight_ids()), ())

    def test_the_window_is_measured_from_acceptance_not_from_the_write(self) -> None:
        fixture = RelayFixture(self)
        fixture.relay._process(command())
        fixture.advance(18)
        fixture.relay.handle_serial_ack(
            PORT, {"t": "ack", "id": "01J8F3QK2M7X9ZB4CDEFGH", "ph": "accepted"}
        )
        fixture.advance(10)
        fixture.relay.tick_deadman()

        self.assertIn(DEADMAN_FRAME, fixture.reader.written)

    def test_one_tick_per_port_not_per_command(self) -> None:
        fixture = RelayFixture(self)
        fixture.relay._process(command(command_id="cmd-a"))
        fixture.relay._process(command(command_id="cmd-b"))
        fixture.relay.tick_deadman()

        ticks = [line for line in fixture.reader.written if line == DEADMAN_FRAME]
        self.assertEqual(len(ticks), 1)


class LightKeepaliveTests(unittest.TestCase):
    def accept(self, fixture: RelayFixture, *, command_id: str, on: int) -> None:
        fixture.relay.handle_serial_ack(
            PORT,
            {
                "t": "ack",
                "id": command_id,
                "ph": "accepted",
                "on": on,
            },
        )

    def test_pump_deadman_never_ticks_or_sweeps_a_light_latch(self) -> None:
        fixture = RelayFixture(self)
        fixture.relay._process(light_command(command_id="light-on"))
        self.accept(fixture, command_id="light-on", on=1)
        before = len(fixture.reader.written)

        fixture.advance(1_000)
        fixture.relay.tick_deadman()

        self.assertEqual(len(fixture.reader.written), before)
        self.assertIn(PORT, fixture.relay._light_latches)
        fixture.relay.tick_light_keepalive()
        self.assertEqual(fixture.reader.written[-1], DEADMAN_FRAME)

    def test_light_keepalive_writes_only_while_the_echoed_latch_is_on(self) -> None:
        fixture = RelayFixture(self)
        fixture.relay.tick_light_keepalive()
        self.assertEqual(fixture.reader.written, [])

        fixture.relay._process(light_command(command_id="light-on"))
        fixture.relay.tick_light_keepalive()
        self.assertEqual(
            [line for line in fixture.reader.written if line == DEADMAN_FRAME], []
        )
        self.accept(fixture, command_id="light-on", on=1)
        fixture.relay.tick_light_keepalive()
        self.assertEqual(fixture.reader.written[-1], DEADMAN_FRAME)

        fixture.relay._process(
            light_command(command_id="light-off", on=False)
        )
        self.accept(fixture, command_id="light-off", on=0)
        before = len(fixture.reader.written)
        fixture.relay.tick_light_keepalive()
        self.assertEqual(len(fixture.reader.written), before)


if __name__ == "__main__":
    unittest.main()


class LinkGateTests(unittest.TestCase):
    """A gateway that owes the server records must not act on its commands."""

    def setUp(self) -> None:
        self.fixture = RelayFixture(self)

    def test_a_command_reaches_the_node_while_the_link_accepts_them(self) -> None:
        self.fixture.relay._process(command())

        (frame,) = self.fixture.reader.commands()
        self.assertEqual(frame["id"], "01J8F3QK2M7X9ZB4CDEFGH")
        # The firmware answers for itself; the relay does not pre-empt it.
        self.assertEqual(self.fixture.outbox.acks, [])

    def test_a_command_is_rejected_while_the_link_refuses_them(self) -> None:
        self.fixture.relay.set_link_gate(lambda: False)

        self.fixture.relay._process(command())

        # Rejected rather than dropped: the backend holds the command in ISSUED
        # and charges its granted volume to the daily budget until something
        # terminal arrives, so silence here costs the pot water it never got.
        self.assertEqual(self.fixture.outbox.phases(), ["rejected"])
        self.assertEqual(self.fixture.outbox.acks[-1].stop_cause, STOP_PI_LINK_HELD)

    def test_the_gate_does_not_touch_the_serial_port(self) -> None:
        self.fixture.relay.set_link_gate(lambda: False)

        self.fixture.relay._process(command())

        self.assertEqual(self.fixture.reader.written, [])


class LocalDoseTests(unittest.TestCase):
    """Water the gateway decides on itself, delivered through the same path.

    Reusing the relay rather than writing a second serial path is deliberate:
    the framing, the interlock and the dead-man are the most safety-critical
    code here, and a second copy of them would drift from the first.
    """

    NODE_ID = NODE

    def setUp(self) -> None:
        self.fixture = RelayFixture(self)

    def begin(self, volume_ml: float = 60.0) -> str:
        command_id = self.fixture.relay.begin_local_dose(
            self.NODE_ID, volume_ml, max_runtime_ms=61_000
        )
        self.assertIsNotNone(command_id)
        return command_id

    def firmware(self, command_id: str, **overrides) -> None:
        message = {"t": "ack", "id": command_id, "ph": "completed", "ms": 61_000}
        message.update(overrides)
        self.fixture.relay.handle_serial_ack(PORT, message)

    def test_a_local_dose_reaches_the_node(self) -> None:
        self.begin()

        (frame,) = self.fixture.reader.commands()
        self.assertEqual(frame["act"], "pump")
        self.assertEqual(frame["ml"], 60)

    def test_a_local_dose_publishes_no_ack_to_the_server(self) -> None:
        command_id = self.begin()

        self.firmware(command_id)

        # The backend never issued this command_id, so an ack for it would be
        # dropped as an ack for an unknown command — noise at best, and at worst
        # a warning that hides a real one.
        self.assertEqual(self.fixture.outbox.acks, [])

    def test_a_completed_local_dose_becomes_a_control_record(self) -> None:
        command_id = self.begin()

        self.firmware(command_id)

        (control,) = self.fixture.outbox.control
        self.assertEqual(control.node_id, self.NODE_ID)
        self.assertEqual(control.volume_ml, 60.0)

    def test_await_reports_what_the_firmware_actually_delivered(self) -> None:
        command_id = self.begin()

        # A run the firmware cut in half: half the commanded runtime, so half
        # the water. _estimated_ml prorates it.
        self.firmware(command_id, ms=30_500, r="max_runtime")

        delivered = self.fixture.relay.await_local_dose(command_id, timeout=0.0)
        self.assertEqual(delivered, 30.0)
        self.assertEqual(self.fixture.outbox.control[0].volume_ml, 30.0)

    def test_a_rejected_local_dose_delivers_and_records_nothing(self) -> None:
        command_id = self.begin()

        self.fixture.relay.handle_serial_ack(
            PORT, {"t": "ack", "id": command_id, "ph": "rejected", "r": "cooldown"}
        )

        self.assertEqual(self.fixture.relay.await_local_dose(command_id, timeout=0.0), 0.0)
        # Nothing moved, so there is nothing the server needs to know about and
        # nothing to charge the pot's budget for.
        self.assertEqual(self.fixture.outbox.control, [])

    def test_a_firmware_that_never_answers_reports_no_delivery(self) -> None:
        command_id = self.begin()

        self.assertEqual(self.fixture.relay.await_local_dose(command_id, timeout=0.0), 0.0)
        self.assertEqual(self.fixture.outbox.control, [])

    def test_an_unknown_node_cannot_be_dosed(self) -> None:
        self.assertIsNone(
            self.fixture.relay.begin_local_dose(
                "no-such-node", 60.0, max_runtime_ms=61_000
            )
        )
        self.assertEqual(self.fixture.reader.written, [])
