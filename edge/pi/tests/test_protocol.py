from datetime import datetime, timezone
import json
import math
import unittest
import uuid

from terrabyte_edge.protocol import (
    Event,
    NonTelemetryMessage,
    ProtocolError,
    parse_line,
)


VALID = {
    "message_type": "telemetry",
    "protocol_version": 1,
    "node_id": "terrabyte-node-01",
    "sequence": 42,
    "uptime_ms": 123456,
    "air_temperature_c": 24.5,
    "relative_humidity_pct": 61.2,
    "ppfd_umol_m2_s": 382.0,
}

MINIMUM_CLOCK = datetime(2025, 1, 1, tzinfo=timezone.utc)


def line(message: dict[str, object]) -> bytes:
    return (json.dumps(message) + "\n").encode()


def parse(raw: bytes, **overrides):
    options = {
        "context_id": "ctx-1",
        "allowed_node_ids": frozenset({"terrabyte-node-01"}),
        "clock_minimum_utc": MINIMUM_CLOCK,
    }
    options.update(overrides)
    return parse_line(raw, **options)


class ProtocolTests(unittest.TestCase):
    def test_valid_telemetry_is_timestamped_and_mapped_to_backend(self) -> None:
        event = parse(
            line(VALID),
            clock=lambda: datetime(2026, 7, 21, 4, 5, 6, 123, tzinfo=timezone.utc),
            event_id_factory=lambda: uuid.UUID("12345678-1234-5678-1234-567812345678"),
        )

        self.assertEqual(event.event_id, "12345678-1234-5678-1234-567812345678")
        self.assertEqual(event.context_id, "ctx-1")
        self.assertEqual(event.captured_at_utc, "2026-07-21T04:05:06Z")
        self.assertEqual(
            event.envelope_v2(gateway_id="orangepi-pro-01"),
            {
                "schema_version": 2,
                "event_type": "telemetry.sample",
                "gateway_id": "orangepi-pro-01",
                "event_id": "12345678-1234-5678-1234-567812345678",
                "observed_at": "2026-07-21T04:05:06Z",
                "nodes": [
                    {
                        "node_id": "terrabyte-node-01",
                        "sequence": 42,
                        "measurements": {
                            "air_temperature_c": 24.5,
                            "air_humidity_pct": 61.2,
                            "plant_light_ppfd_umol_m2_s": 382.0,
                        },
                        "quality": {
                            "air_sensor_valid": True,
                            "light_sensor_valid": True,
                            "soil_sensor_valid": False,
                        },
                    }
                ],
            },
        )

    def test_non_telemetry_status_is_not_treated_as_bad_sensor_data(self) -> None:
        for message_type in ("hello", "sensor_status", "configuration_error"):
            with self.subTest(message_type=message_type):
                with self.assertRaises(NonTelemetryMessage):
                    parse(
                        line(
                            {
                                "message_type": message_type,
                                "protocol_version": 1,
                            }
                        )
                    )

    def test_node_id_uses_same_safe_character_set_as_firmware(self) -> None:
        parse(
            line(dict(VALID, node_id="node_A-1.2:usb")),
            allowed_node_ids=frozenset({"node_A-1.2:usb"}),
        )
        with self.assertRaisesRegex(ProtocolError, "node_id"):
            parse(
                line(dict(VALID, node_id="node with spaces")),
                allowed_node_ids=frozenset({"node with spaces"}),
            )

    def test_all_three_axes_are_required(self) -> None:
        invalid = dict(VALID)
        del invalid["ppfd_umol_m2_s"]
        with self.assertRaisesRegex(ProtocolError, "ppfd_umol_m2_s"):
            parse(line(invalid))

    def test_nan_and_out_of_range_values_are_rejected(self) -> None:
        for field, value in (
            ("air_temperature_c", math.nan),
            ("relative_humidity_pct", 100.01),
            ("ppfd_umol_m2_s", -0.01),
        ):
            with self.subTest(field=field):
                invalid = dict(VALID)
                invalid[field] = value
                with self.assertRaises(ProtocolError):
                    parse(line(invalid))

    def test_boolean_sequence_and_wrong_protocol_are_rejected(self) -> None:
        invalid = dict(VALID, sequence=True)
        with self.assertRaises(ProtocolError):
            parse(line(invalid))

    def test_sequence_and_uptime_follow_arduino_uint32_range(self) -> None:
        parse(line(dict(VALID, sequence=0xFFFFFFFF, uptime_ms=0xFFFFFFFF)))
        with self.assertRaises(ProtocolError):
            parse(line(dict(VALID, sequence=0x100000000)))
        invalid = dict(VALID, protocol_version=2)
        with self.assertRaises(ProtocolError):
            parse(line(invalid))

    def test_invalid_utf8_and_json_are_rejected(self) -> None:
        for raw in (b"\xff\n", b"not-json\n", b"[]\n"):
            with self.subTest(raw=raw):
                with self.assertRaises(ProtocolError):
                    parse(raw)

    def test_wrong_node_and_unsynchronized_clock_are_rejected(self) -> None:
        with self.assertRaisesRegex(ProtocolError, "allowlist"):
            parse(line(dict(VALID, node_id="another-node")))
        with self.assertRaisesRegex(ProtocolError, "system clock"):
            parse(
                line(VALID),
                clock=lambda: datetime(1970, 1, 1, tzinfo=timezone.utc),
            )


if __name__ == "__main__":
    unittest.main()


class SoilMetricTests(unittest.TestCase):
    """Optional soil probes, which the firmware only emits when compiled in."""

    def soil_message(self, **overrides):
        message = dict(VALID)
        message.update(
            {"soil_temperature_c": 18.5, "soil_moisture_pct": 50.5}
        )
        message.update(overrides)
        return message

    def test_soil_readings_reach_the_envelope_and_mark_the_probe_valid(self) -> None:
        event = parse(line(self.soil_message()))
        node = event.envelope_v2(gateway_id="gw")["nodes"][0]

        self.assertEqual(node["measurements"]["soil_temperature_c"], 18.5)
        self.assertEqual(node["measurements"]["soil_moisture_pct"], 50.5)
        self.assertTrue(node["quality"]["soil_sensor_valid"])

    def test_absent_soil_readings_are_omitted_not_zeroed(self) -> None:
        """A fabricated 0.0 would reach irrigation as a confident 'bone dry'."""

        event = parse(line(VALID))
        node = event.envelope_v2(gateway_id="gw")["nodes"][0]

        self.assertNotIn("soil_temperature_c", node["measurements"])
        self.assertNotIn("soil_moisture_pct", node["measurements"])
        self.assertFalse(node["quality"]["soil_sensor_valid"])

    def test_an_out_of_range_soil_reading_is_rejected_rather_than_clamped(self) -> None:
        with self.assertRaises(ProtocolError):
            parse(line(self.soil_message(soil_moisture_pct=140.0)))
        with self.assertRaises(ProtocolError):
            parse(line(self.soil_message(soil_temperature_c=-99.0)))

    def test_soil_readings_survive_an_outbox_round_trip(self) -> None:
        event = parse(line(self.soil_message()))
        restored = Event.from_record(json.loads(json.dumps(event.to_record())))
        self.assertEqual(restored, event)

    def test_a_record_queued_before_soil_support_still_loads(self) -> None:
        """Outbox rows are JSON blobs, so older rows simply lack these keys."""

        event = parse(line(VALID))
        legacy = {
            key: value
            for key, value in event.to_record().items()
            if not key.startswith("soil_")
        }
        restored = Event.from_record(legacy)
        self.assertIsNone(restored.soil_temperature_c)
        self.assertIsNone(restored.soil_moisture_pct)
