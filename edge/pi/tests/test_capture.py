import csv
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from terrabyte_edge.capture import (
    CSV_COLUMNS,
    CaptureStats,
    CaptureWriter,
    capture,
    classify,
    gateway_verdict,
    to_row,
)


NODE = "terrabyte-node-001"

TELEMETRY = json.dumps(
    {
        "message_type": "telemetry",
        "protocol_version": 1,
        "node_id": NODE,
        "sequence": 42,
        "uptime_ms": 215000,
        "air_temperature_c": 24.3,
        "relative_humidity_pct": 58.1,
        "ppfd_umol_m2_s": 421.75,
        "illuminance_lux": 18420.83,
        "soil_temperature_c": 19.4,
        "soil_moisture_pct": 63.25,
        "soil_moisture_raw_adc": 412,
    }
).encode() + b"\n"

# What a node emits when the PPFD conversion produced NaN - the signature of a
# light-sensor macro mismatch. Note it still carries the lux reading.
SENSOR_STATUS = json.dumps(
    {
        "message_type": "sensor_status",
        "protocol_version": 1,
        "node_id": NODE,
        "sequence": 43,
        "uptime_ms": 220000,
        "illuminance_lux": 18420.83,
        "validity": {
            "air_temperature_c": True,
            "relative_humidity_pct": True,
            "ppfd_umol_m2_s": False,
        },
        "reason": "sensor_unavailable_or_out_of_range",
    }
).encode() + b"\n"

HELLO = json.dumps(
    {
        "message_type": "hello",
        "protocol_version": 1,
        "node_id": NODE,
        "firmware_version": "0.3.0",
        "ready": True,
    }
).encode() + b"\n"

WITH_ACTUATORS = json.dumps(
    {
        "message_type": "telemetry",
        "protocol_version": 1,
        "node_id": NODE,
        "sequence": 44,
        "uptime_ms": 225000,
        "air_temperature_c": 24.3,
        "relative_humidity_pct": 58.1,
        "ppfd_umol_m2_s": 421.75,
        "actuators": {"pump": 0, "light": 1},
        "pump_lockout_ms": 810000,
    }
).encode() + b"\n"


def _row(raw: bytes, expected_node_id: str | None = None) -> dict:
    frame = classify(raw, now=datetime(2026, 8, 25, 3, 4, 5, tzinfo=timezone.utc))
    return to_row(frame, gateway_verdict(frame, expected_node_id=expected_node_id))


class ClassifyTests(unittest.TestCase):
    def test_telemetry_is_classified_and_stamped(self) -> None:
        frame = classify(TELEMETRY, now=datetime(2026, 8, 25, 3, 4, 5, tzinfo=timezone.utc))
        self.assertEqual(frame.message_type, "telemetry")
        self.assertEqual(frame.host_time_utc, "2026-08-25T03:04:05.000Z")

    def test_non_telemetry_frames_are_kept_not_dropped(self) -> None:
        """hello and sensor_status are how a miscompiled firmware announces itself."""

        for raw, expected in ((SENSOR_STATUS, "sensor_status"), (HELLO, "hello")):
            self.assertEqual(classify(raw).message_type, expected)

    def test_unparseable_line_is_still_a_frame(self) -> None:
        frame = classify(b"not json at all\n")
        self.assertEqual(frame.message_type, "unparseable")
        self.assertFalse(frame.is_json)
        self.assertIsNotNone(frame.decode_error)

    def test_undecodable_bytes_are_still_a_frame(self) -> None:
        frame = classify(b"\xff\xfe\x00 garbage\n")
        self.assertEqual(frame.message_type, "undecodable")

    def test_json_that_is_not_an_object_is_rejected(self) -> None:
        self.assertEqual(classify(b"[1,2,3]\n").message_type, "unparseable")

    def test_unknown_message_type_is_labelled_not_dropped(self) -> None:
        raw = json.dumps({"message_type": "future_thing"}).encode() + b"\n"
        self.assertEqual(classify(raw).message_type, "unknown")


class RowTests(unittest.TestCase):
    def test_every_column_is_present_in_every_row(self) -> None:
        for raw in (TELEMETRY, SENSOR_STATUS, HELLO, b"junk\n"):
            self.assertEqual(set(_row(raw)), set(CSV_COLUMNS))

    def test_telemetry_fields_are_extracted(self) -> None:
        row = _row(TELEMETRY)
        self.assertEqual(row["air_temperature_c"], 24.3)
        self.assertEqual(row["illuminance_lux"], 18420.83)
        self.assertEqual(row["soil_moisture_pct"], 63.25)
        self.assertEqual(row["sequence"], 42)

    def test_raw_adc_survives_alongside_the_percentage(self) -> None:
        """Calibration is still moving; the raw count is what makes a
        re-calibration able to re-derive the percentage instead of discarding
        the capture."""

        self.assertEqual(_row(TELEMETRY)["soil_moisture_raw_adc"], 412)

    def test_lux_is_kept_from_a_sensor_status_frame(self) -> None:
        """Event has no illuminance_lux field, so relying on parse_line here
        would discard the only light reading a mis-calibrated node produces."""

        row = _row(SENSOR_STATUS)
        self.assertEqual(row["illuminance_lux"], 18420.83)
        self.assertEqual(row["reason"], "sensor_unavailable_or_out_of_range")

    def test_validity_flags_are_flattened_to_one_and_zero(self) -> None:
        row = _row(SENSOR_STATUS)
        self.assertEqual(row["valid_air_temperature_c"], 1)
        self.assertEqual(row["valid_ppfd_umol_m2_s"], 0)
        self.assertIsNone(row["valid_soil_moisture_pct"])

    def test_actuator_state_is_extracted_when_the_firmware_sends_it(self) -> None:
        row = _row(WITH_ACTUATORS)
        self.assertEqual(row["pump_on"], 0)
        self.assertEqual(row["light_on"], 1)
        self.assertEqual(row["pump_lockout_ms"], 810000)

    def test_absent_actuators_stay_empty_rather_than_zero(self) -> None:
        """Today's firmware sends no actuators object. Recording 0 would claim
        an actuator exists and is off."""

        row = _row(TELEMETRY)
        self.assertIsNone(row["pump_on"])
        self.assertIsNone(row["light_on"])

    def test_non_finite_numbers_become_empty_not_the_string_nan(self) -> None:
        raw = b'{"message_type":"telemetry","node_id":"n","ppfd_umol_m2_s":NaN}\n'
        self.assertIsNone(_row(raw)["ppfd_umol_m2_s"])


class VerdictTests(unittest.TestCase):
    def test_valid_telemetry_is_accepted(self) -> None:
        self.assertEqual(_row(TELEMETRY)["gateway_verdict"], "accepted")

    def test_sensor_status_is_reported_as_non_telemetry(self) -> None:
        self.assertTrue(_row(SENSOR_STATUS)["gateway_verdict"].startswith("non_telemetry:"))

    def test_frames_from_another_node_are_recorded_not_discarded(self) -> None:
        """Rejecting a node id mismatch would silently lose an entire night."""

        row = _row(TELEMETRY, expected_node_id="some-other-node")
        self.assertTrue(row["gateway_verdict"].startswith("rejected:"))
        self.assertEqual(row["air_temperature_c"], 24.3)

    def test_verdict_without_expected_node_id_reports_field_validation(self) -> None:
        self.assertEqual(_row(TELEMETRY, expected_node_id=None)["gateway_verdict"], "accepted")


class WriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _capture(self, lines, clock=None) -> CaptureStats:
        stats = CaptureStats()
        with CaptureWriter(
            directory=self.directory, prefix="capture", fsync_every=1
        ) as writer:
            capture(
                iter(lines),
                writer=writer,
                stats=stats,
                clock=clock or (lambda: datetime(2026, 8, 25, 3, 4, 5, tzinfo=timezone.utc)),
            )
        return stats

    def test_raw_and_csv_are_both_written(self) -> None:
        self._capture([TELEMETRY, SENSOR_STATUS])
        raw_path = self.directory / "capture-20260825.jsonl"
        csv_path = self.directory / "capture-20260825.csv"
        self.assertTrue(raw_path.exists())
        self.assertTrue(csv_path.exists())

        raw_lines = raw_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(raw_lines), 2)
        first = json.loads(raw_lines[0])
        self.assertEqual(first["host_time_utc"], "2026-08-25T03:04:05.000Z")
        self.assertEqual(json.loads(first["line"])["sequence"], 42)

        with csv_path.open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual([row["message_type"] for row in rows], ["telemetry", "sensor_status"])

    def test_header_is_written_once_and_appends_resume(self) -> None:
        self._capture([TELEMETRY])
        self._capture([TELEMETRY])
        csv_path = self.directory / "capture-20260825.csv"
        text = csv_path.read_text(encoding="utf-8")
        self.assertEqual(text.count("host_time_utc"), 1)
        with csv_path.open(encoding="utf-8") as handle:
            self.assertEqual(len(list(csv.DictReader(handle))), 2)

    def test_files_rotate_on_the_utc_day(self) -> None:
        moments = iter(
            [
                datetime(2026, 8, 25, 23, 59, 59, tzinfo=timezone.utc),
                datetime(2026, 8, 26, 0, 0, 4, tzinfo=timezone.utc),
            ]
        )
        self._capture([TELEMETRY, TELEMETRY], clock=lambda: next(moments))
        self.assertTrue((self.directory / "capture-20260825.csv").exists())
        self.assertTrue((self.directory / "capture-20260826.csv").exists())

    def test_a_bad_line_does_not_stop_the_capture(self) -> None:
        stats = self._capture([b"junk\n", TELEMETRY])
        self.assertEqual(stats.lines, 2)
        self.assertEqual(stats.by_type["unparseable"], 1)
        self.assertEqual(stats.by_type["telemetry"], 1)

    def test_fsync_every_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            CaptureWriter(directory=self.directory, prefix="x", fsync_every=0)


class StatsTests(unittest.TestCase):
    def test_counts_and_coverage(self) -> None:
        stats = CaptureStats()
        for raw in (TELEMETRY, SENSOR_STATUS, HELLO):
            frame = classify(raw)
            verdict = gateway_verdict(frame)
            stats.observe(frame, verdict, to_row(frame, verdict))

        self.assertEqual(stats.lines, 3)
        self.assertEqual(stats.verdict_accepted, 1)
        self.assertEqual(stats.verdict_other, 2)
        self.assertEqual(stats.present["soil_moisture_pct"], 1)
        self.assertEqual(stats.present["illuminance_lux"], 2)
        self.assertIn("telemetry=1", stats.summary())

    def test_sequence_span_is_tracked_for_loss_detection(self) -> None:
        stats = CaptureStats()
        for raw in (TELEMETRY, WITH_ACTUATORS):
            frame = classify(raw)
            verdict = gateway_verdict(frame)
            stats.observe(frame, verdict, to_row(frame, verdict))
        self.assertEqual(stats.first_sequence, 42)
        self.assertEqual(stats.last_sequence, 44)

    def test_empty_capture_summarises_without_dividing_by_zero(self) -> None:
        self.assertEqual(CaptureStats().summary(), "no lines captured")


if __name__ == "__main__":
    unittest.main()
