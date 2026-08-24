"""Unattended raw-telemetry capture for AI training data.

This is collection tooling, not part of the control path. It reads the
*production* firmware's JSON Lines output and writes it to disk; it never
writes to the serial port, so it cannot actuate anything. That is deliberate:
the bench alternative (``dataset_logger`` firmware plus ``capture_dataset.py``)
carries no actuator interlocks at all, and it needs an operator to type
``w120`` after every watering to produce a label. See
``docs/design/ml_irrigation_contract.md`` section 4.4 for why a human-typed
label cannot reach the 200-event threshold that real training data needs.

Two files are written per capture day:

* ``<prefix>-YYYYMMDD.jsonl`` - every received line, verbatim. This is the
  archive of record. If the extraction below turns out to be wrong, the raw
  file still holds everything and the CSV can be regenerated offline.
* ``<prefix>-YYYYMMDD.csv`` - one flattened row per line, for analysis.

The CSV is built from this module's own field extraction rather than from
``protocol.parse_line``, for three reasons that each cost data if ignored:

1. ``parse_line`` raises ``NonTelemetryMessage`` for ``hello``,
   ``sensor_status`` and ``configuration_error``. Those are exactly the frames
   that diagnose a miscompiled firmware, so a collector must keep them.
2. ``Event`` has no ``illuminance_lux`` field, but ``sensor_status`` carries
   the lux reading even when the PPFD conversion failed. Dropping it would
   throw away the only light data a mis-calibrated node produces.
3. ``parse_line`` rejects a frame whose ``node_id`` does not match the
   provisioned one. For a capture rig that is a liability rather than a safety
   property - it would silently discard an entire night.

``parse_line`` is still called, but only to fill the ``gateway_verdict``
column: "would the gateway have accepted this line?". That keeps the collector
honest about divergence from the production parser without letting it lose
rows.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
from threading import Event
from typing import Any, Callable, Iterator, TextIO


LOGGER = logging.getLogger(__name__)

# Message types the firmware emits. Anything else is recorded with
# message_type="unknown" rather than dropped.
KNOWN_MESSAGE_TYPES = ("telemetry", "sensor_status", "hello", "configuration_error")

# Fixed validity keys, mirroring SensorAdapter.h's SensorValidity bits. A fixed
# set keeps the CSV schema stable across firmware builds that enable different
# probes; a key a given build never sends simply stays empty.
VALIDITY_KEYS = (
    "air_temperature_c",
    "relative_humidity_pct",
    "ppfd_umol_m2_s",
    "illuminance_lux",
    "soil_temperature_c",
    "soil_moisture_pct",
)

# soil_moisture_raw_adc is not redundant with the percentage: the dry/wet ADC
# endpoints are still being calibrated, and keeping the raw count is what lets
# a re-calibration re-derive the percentage instead of discarding the capture.
MEASUREMENT_KEYS = (
    "air_temperature_c",
    "relative_humidity_pct",
    "ppfd_umol_m2_s",
    "illuminance_lux",
    "soil_temperature_c",
    "soil_moisture_pct",
    "soil_moisture_raw_adc",
)

CSV_COLUMNS = (
    "host_time_utc",
    "message_type",
    "node_id",
    "protocol_version",
    "firmware_version",
    "sequence",
    "uptime_ms",
    *MEASUREMENT_KEYS,
    # Actuator state. Absent on today's firmware; present once the actuator
    # branch lands, and needed to correlate light output against measured PPFD.
    "pump_on",
    "light_on",
    "pump_lockout_ms",
    # Diagnostics.
    "reason",
    *(f"valid_{name}" for name in VALIDITY_KEYS),
    "gateway_verdict",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _scalar(value: Any) -> Any:
    """Return a CSV-safe scalar, or None when the value is unusable.

    NaN and infinity become None rather than the strings "nan"/"inf". A reader
    that sees "nan" in a float column usually coerces it to a number and then
    treats it as a measurement.
    """

    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (int, float, str)):
        return value
    return None


def _flag(value: Any) -> Any:
    """Normalise a firmware boolean to 1/0, leaving anything else alone."""

    if isinstance(value, bool):
        return 1 if value else 0
    return _scalar(value)


@dataclass(frozen=True)
class Frame:
    """One received line, classified but not judged."""

    host_time_utc: str
    raw: bytes
    message: dict[str, Any] | None
    message_type: str
    decode_error: str | None = None

    @property
    def is_json(self) -> bool:
        return self.message is not None


def classify(raw: bytes, *, now: datetime | None = None) -> Frame:
    """Decode one line into a Frame. Never raises - a bad line is still data."""

    host_time = _stamp(now or _utc_now())
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        return Frame(host_time, raw, None, "undecodable", str(exc))
    try:
        message = json.loads(text)
    except json.JSONDecodeError as exc:
        return Frame(host_time, raw, None, "unparseable", str(exc))
    if not isinstance(message, dict):
        return Frame(host_time, raw, None, "unparseable", "top level is not an object")

    declared = message.get("message_type")
    message_type = declared if declared in KNOWN_MESSAGE_TYPES else "unknown"
    return Frame(host_time, raw, message, str(message_type))


def gateway_verdict(frame: Frame, *, expected_node_id: str | None = None) -> str:
    """What the production gateway would have made of this line.

    Diagnostic only - the row is written either way. When no node id is
    configured the frame's own id is used, so the verdict reports field
    validation rather than a provisioning mismatch the operator did not ask
    about.
    """

    if not frame.is_json:
        return frame.message_type

    from terrabyte_edge.protocol import NonTelemetryMessage, ProtocolError, parse_line

    message = frame.message
    assert message is not None
    node_id = expected_node_id
    if node_id is None:
        candidate = message.get("node_id")
        node_id = candidate if isinstance(candidate, str) else ""
    try:
        parse_line(
            frame.raw,
            context_id="capture",
            expected_node_id=node_id,
            clock_minimum_utc=datetime(1970, 1, 1, tzinfo=timezone.utc),
        )
    except NonTelemetryMessage as exc:
        return f"non_telemetry:{exc}"
    except ProtocolError as exc:
        return f"rejected:{exc}"
    return "accepted"


def to_row(frame: Frame, verdict: str) -> dict[str, Any]:
    """Flatten one frame into the fixed CSV schema."""

    row: dict[str, Any] = {name: None for name in CSV_COLUMNS}
    row["host_time_utc"] = frame.host_time_utc
    row["message_type"] = frame.message_type
    row["gateway_verdict"] = verdict
    if not frame.is_json:
        row["reason"] = frame.decode_error
        return row

    message = frame.message
    assert message is not None
    for name in (
        "node_id",
        "protocol_version",
        "firmware_version",
        "reason",
        "sequence",
        "uptime_ms",
        "pump_lockout_ms",
        *MEASUREMENT_KEYS,
    ):
        row[name] = _scalar(message.get(name))

    actuators = message.get("actuators")
    if isinstance(actuators, dict):
        row["pump_on"] = _flag(actuators.get("pump"))
        row["light_on"] = _flag(actuators.get("light"))

    validity = message.get("validity")
    if isinstance(validity, dict):
        for name in VALIDITY_KEYS:
            row[f"valid_{name}"] = _flag(validity.get(name))
    return row


@dataclass
class CaptureStats:
    """Counters for the morning summary.

    Logged periodically rather than only at exit: an unattended run can be
    ended by a power cut or a hard kill, and a summary that exists only at
    shutdown is a summary nobody reads.
    """

    started_at_utc: str = field(default_factory=lambda: _stamp(_utc_now()))
    lines: int = 0
    by_type: dict[str, int] = field(default_factory=dict)
    verdict_accepted: int = 0
    verdict_other: int = 0
    present: dict[str, int] = field(default_factory=dict)
    reconnects: int = 0
    first_sequence: int | None = None
    last_sequence: int | None = None

    def observe(self, frame: Frame, verdict: str, row: dict[str, Any]) -> None:
        self.lines += 1
        self.by_type[frame.message_type] = self.by_type.get(frame.message_type, 0) + 1
        if verdict == "accepted":
            self.verdict_accepted += 1
        else:
            self.verdict_other += 1
        for name in MEASUREMENT_KEYS:
            if row.get(name) is not None:
                self.present[name] = self.present.get(name, 0) + 1
        sequence = row.get("sequence")
        if isinstance(sequence, int) and not isinstance(sequence, bool):
            if self.first_sequence is None:
                self.first_sequence = sequence
            self.last_sequence = sequence

    def summary(self) -> str:
        if self.lines == 0:
            return "no lines captured"
        types = " ".join(f"{key}={value}" for key, value in sorted(self.by_type.items()))
        coverage = " ".join(
            f"{name}={self.present.get(name, 0) * 100 // self.lines}%"
            for name in MEASUREMENT_KEYS
        )
        return (
            f"lines={self.lines} accepted={self.verdict_accepted} "
            f"other={self.verdict_other} reconnects={self.reconnects} "
            f"types[{types}] coverage[{coverage}]"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at_utc": self.started_at_utc,
            "ended_at_utc": _stamp(_utc_now()),
            "lines": self.lines,
            "by_message_type": dict(sorted(self.by_type.items())),
            "gateway_accepted": self.verdict_accepted,
            "gateway_other": self.verdict_other,
            "field_present_counts": dict(sorted(self.present.items())),
            "reconnects": self.reconnects,
            "first_sequence": self.first_sequence,
            "last_sequence": self.last_sequence,
        }


class CaptureWriter:
    """Day-rotated raw JSONL plus flattened CSV, with bounded data loss.

    Rotation is keyed on the UTC date of each record rather than on a timer, so
    a restart mid-day reopens the same pair of files in append mode instead of
    starting a third one.

    ``fsync_every`` bounds how much a power cut can take: the OS buffer is
    flushed on every record (cheap) but ``fsync`` is called every N records
    (expensive on an SD card). At the 5 s telemetry cadence the default of 20
    means at most about 100 seconds of capture is at risk.
    """

    def __init__(
        self,
        *,
        directory: Path,
        prefix: str,
        fsync_every: int = 20,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if fsync_every < 1:
            raise ValueError("fsync_every must be at least 1")
        self.directory = Path(directory)
        self.prefix = prefix
        self.fsync_every = fsync_every
        self.clock = clock
        self._day: str | None = None
        self._raw: TextIO | None = None
        self._csv: TextIO | None = None
        self._writer: csv.DictWriter | None = None
        self._since_sync = 0

    def paths_for(self, day: str) -> tuple[Path, Path]:
        return (
            self.directory / f"{self.prefix}-{day}.jsonl",
            self.directory / f"{self.prefix}-{day}.csv",
        )

    def _rotate(self, day: str) -> None:
        self.close()
        self.directory.mkdir(parents=True, exist_ok=True)
        raw_path, csv_path = self.paths_for(day)
        # Encoding is explicit on both handles: the default on Windows is the
        # ANSI code page, which would mangle any non-ASCII the firmware emits
        # and make the archive non-reproducible on another machine.
        self._raw = raw_path.open("a", encoding="utf-8", newline="")
        needs_header = not csv_path.exists() or csv_path.stat().st_size == 0
        self._csv = csv_path.open("a", encoding="utf-8", newline="")
        self._writer = csv.DictWriter(
            self._csv, fieldnames=list(CSV_COLUMNS), extrasaction="ignore"
        )
        if needs_header:
            self._writer.writeheader()
        self._day = day
        self._since_sync = 0
        LOGGER.info("capture files opened raw=%s csv=%s", raw_path, csv_path)

    def write(self, frame: Frame, row: dict[str, Any]) -> None:
        day = frame.host_time_utc[:10].replace("-", "")
        if day != self._day:
            self._rotate(day)
        assert self._raw is not None and self._csv is not None
        assert self._writer is not None

        # The raw line is stored verbatim inside an envelope that adds only the
        # host timestamp. The firmware has no RTC, so uptime_ms alone cannot be
        # placed on a calendar.
        self._raw.write(
            json.dumps(
                {
                    "host_time_utc": frame.host_time_utc,
                    "line": frame.raw.decode("utf-8", errors="replace").rstrip("\r\n"),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        )
        self._writer.writerow(row)
        self._raw.flush()
        self._csv.flush()
        self._since_sync += 1
        if self._since_sync >= self.fsync_every:
            self.sync()

    def sync(self) -> None:
        for handle in (self._raw, self._csv):
            if handle is None:
                continue
            try:
                os.fsync(handle.fileno())
            except OSError:
                # A filesystem that cannot fsync must not end the capture.
                LOGGER.debug("fsync failed", exc_info=True)
        self._since_sync = 0

    def close(self) -> None:
        if self._raw is None and self._csv is None:
            return
        self.sync()
        for handle in (self._raw, self._csv):
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    LOGGER.debug("close failed", exc_info=True)
        self._raw = None
        self._csv = None
        self._writer = None
        self._day = None

    def __enter__(self) -> "CaptureWriter":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def capture(
    lines: Iterator[bytes],
    *,
    writer: CaptureWriter,
    stats: CaptureStats,
    expected_node_id: str | None = None,
    clock: Callable[[], datetime] = _utc_now,
    on_record: Callable[[Frame, str, dict[str, Any]], None] | None = None,
) -> CaptureStats:
    """Drain ``lines`` into ``writer``, updating ``stats``.

    Separated from the serial plumbing so the whole pipeline can be tested
    against a list of byte strings.
    """

    for raw in lines:
        frame = classify(raw, now=clock())
        verdict = gateway_verdict(frame, expected_node_id=expected_node_id)
        row = to_row(frame, verdict)
        writer.write(frame, row)
        stats.observe(frame, verdict, row)
        if on_record is not None:
            on_record(frame, verdict, row)
    return stats


def stop_after(source: Iterator[bytes], stop: Event) -> Iterator[bytes]:
    """Yield from ``source`` until ``stop`` is set."""

    for item in source:
        if stop.is_set():
            return
        yield item
