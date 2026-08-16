from datetime import datetime, timezone
import json
import math
import unittest
import uuid

from terrabyte_edge.protocol import NonTelemetryMessage, ProtocolError, parse_line


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
        "expected_node_id": "terrabyte-node-01",
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
            event.backend_body(),
            {
                "capturedAtUtc": "2026-07-21T04:05:06Z",
                "airTemperatureC": 24.5,
                "relativeHumidityPct": 61.2,
                "ppfdUmolM2S": 382.0,
                "inputContract": "perfect_calibrated_v1",
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
            expected_node_id="node_A-1.2:usb",
        )
        with self.assertRaisesRegex(ProtocolError, "node_id"):
            parse(
                line(dict(VALID, node_id="node with spaces")),
                expected_node_id="node with spaces",
            )

    def test_ppfd_field_is_required(self) -> None:
        invalid = dict(VALID)
        del invalid["ppfd_umol_m2_s"]
        with self.assertRaisesRegex(ProtocolError, "ppfd_umol_m2_s"):
            parse(line(invalid))

    def test_explicit_null_ppfd_is_preserved_for_backend(self) -> None:
        event = parse(line(dict(VALID, ppfd_umol_m2_s=None)))

        self.assertIsNone(event.ppfd_umol_m2_s)
        self.assertIsNone(event.backend_body()["ppfdUmolM2S"])

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
        with self.assertRaisesRegex(ProtocolError, "provisioned Arduino"):
            parse(line(dict(VALID, node_id="another-node")))
        with self.assertRaisesRegex(ProtocolError, "system clock"):
            parse(
                line(VALID),
                clock=lambda: datetime(1970, 1, 1, tzinfo=timezone.utc),
            )


if __name__ == "__main__":
    unittest.main()
