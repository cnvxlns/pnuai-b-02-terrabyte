"""Unattended telemetry capture for AI training data.

Reads the production firmware's JSON Lines output and archives it. It never
writes to the serial port, so it cannot actuate anything - the capture rig is
sensors-only by construction.

    python tools/telemetry_logger.py --list-ports
    python tools/telemetry_logger.py --port usb:1a86:7523 --preflight 300
    python tools/telemetry_logger.py --port usb:1a86:7523 --output data/raw

Run ``--preflight`` before committing to an overnight run. Two configuration
mismatches are known to produce a night of worthless data, and both are silent:

* ``TelemetryConfig.local.h`` written against the TSL2591 branch defines
  ``TB_TSL2591_*`` while ``develop`` reads ``TB_GY30_*``. The macros are simply
  never seen, PPFD stays NaN, and every sample leaves as ``sensor_status``
  instead of ``telemetry``.
* The soil probe is wired to A0 on the bench while ``develop`` defaults
  ``TB_SOIL_MOISTURE_ADC_PIN`` to A1, so the ADC reads a floating pin.

Preflight reports the message-type split and per-field coverage, which is what
distinguishes both cases from a healthy node.

On Windows the process asks the OS not to sleep while it runs. That is a
convenience, not a safety property: this rig drives nothing.
"""

from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import signal
import sys
from threading import Event
from typing import Any, Iterator

# Allow running as a script from edge/pi without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from terrabyte_edge.capture import (  # noqa: E402
    CaptureStats,
    CaptureWriter,
    Frame,
    MEASUREMENT_KEYS,
    capture,
    classify,
    gateway_verdict,
    to_row,
)
from terrabyte_edge.portspec import (  # noqa: E402
    PortResolutionError,
    list_ports,
    resolve,
    resolving_factory,
)
from terrabyte_edge.serial_reader import SerialLineReader  # noqa: E402


LOGGER = logging.getLogger("telemetry_logger")

DEFAULT_BAUD = 115200
DEFAULT_MAX_LINE_BYTES = 4096


@contextlib.contextmanager
def keep_awake(enabled: bool) -> Iterator[None]:
    """Ask Windows not to sleep while capturing. A no-op elsewhere.

    ES_SYSTEM_REQUIRED keeps the machine awake without ES_DISPLAY_REQUIRED, so
    the screen is still allowed to blank. The flag is cleared on the way out;
    if the process is killed hard, Windows drops it with the thread anyway.
    """

    if not enabled or sys.platform != "win32":
        yield
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        continuous, system_required = 0x80000000, 0x00000001
        if kernel32.SetThreadExecutionState(continuous | system_required) == 0:
            LOGGER.warning("could not inhibit sleep; capture may pause overnight")
        else:
            LOGGER.info("sleep inhibited for the duration of the capture")
        try:
            yield
        finally:
            kernel32.SetThreadExecutionState(continuous)
    except Exception:  # pragma: no cover - platform specific
        LOGGER.warning("sleep inhibition unavailable", exc_info=True)
        yield


def build_reader(args: argparse.Namespace) -> SerialLineReader:
    """Build a reader whose port is re-resolved on every reconnect."""

    return SerialLineReader(
        port=args.port,
        baudrate=args.baud,
        timeout_seconds=args.serial_timeout,
        reconnect_seconds=args.reconnect_seconds,
        max_line_bytes=args.max_line_bytes,
        factory=resolving_factory(args.port),
    )


def preflight_report(stats: CaptureStats) -> tuple[bool, list[str]]:
    """Judge whether an overnight capture from this node would be worth having."""

    notes: list[str] = []
    if stats.lines == 0:
        return False, [
            "No lines were received at all. Check the cable, the baud rate "
            "(the sketch runs at 115200 after boot, not the 57600 bootloader "
            "speed), and that the board is not held in reset."
        ]

    telemetry = stats.by_type.get("telemetry", 0)
    sensor_status = stats.by_type.get("sensor_status", 0)
    configuration_error = stats.by_type.get("configuration_error", 0)
    healthy = True

    if configuration_error:
        healthy = False
        notes.append(
            f"{configuration_error} configuration_error frames: TB_NODE_ID is "
            "unset or invalid, so the firmware refuses to publish telemetry."
        )
    if telemetry == 0 and sensor_status:
        healthy = False
        notes.append(
            f"Every frame is sensor_status ({sensor_status}), none is telemetry. "
            "This is the signature of a light-sensor macro mismatch: a local "
            "header written for TB_TSL2591_* against a build that reads "
            "TB_GY30_*. PPFD stays NaN and the frame is downgraded. Check "
            "valid_ppfd_umol_m2_s in the CSV."
        )
    elif sensor_status > telemetry:
        notes.append(
            f"More sensor_status ({sensor_status}) than telemetry ({telemetry}) "
            "frames: at least one required reading is intermittently invalid."
        )

    for name in MEASUREMENT_KEYS:
        seen = stats.present.get(name, 0)
        if seen == 0:
            level = "absent"
            if name in ("soil_moisture_pct", "soil_moisture_raw_adc"):
                healthy = False
                level = "absent - soil moisture is the single most important input"
            elif name == "soil_temperature_c":
                level = "absent (DS18B20 optional; disabled by default)"
            elif name == "illuminance_lux":
                level = "absent (present only when the light sensor is enabled)"
            notes.append(f"{name}: {level}")

    if stats.first_sequence is not None and stats.last_sequence is not None:
        span = stats.last_sequence - stats.first_sequence + 1
        if span > 0 and stats.lines < span:
            notes.append(
                f"sequence spans {span} but only {stats.lines} lines arrived: "
                f"{span - stats.lines} frames were lost in transit."
            )
    return healthy, notes


def run(args: argparse.Namespace) -> int:
    stop = Event()

    def request_stop(signum: int, _frame: object) -> None:
        LOGGER.info("signal %s received; finishing the current record", signum)
        stop.set()

    for name in ("SIGINT", "SIGTERM"):
        handler = getattr(signal, name, None)
        if handler is not None:
            with contextlib.suppress(ValueError, OSError):
                signal.signal(handler, request_stop)

    try:
        device = resolve(args.port)
    except PortResolutionError as exc:
        LOGGER.error("%s", exc)
        return 2
    LOGGER.info("capturing from %s (%s)", args.port, device)

    stats = CaptureStats()
    deadline = None
    if args.preflight:
        deadline = _monotonic() + args.preflight
    last_report = _monotonic()

    def on_record(frame: Frame, verdict: str, row: dict[str, Any]) -> None:
        nonlocal last_report
        if args.echo:
            LOGGER.info(
                "%s type=%s verdict=%s", frame.host_time_utc, frame.message_type, verdict
            )
        now = _monotonic()
        if now - last_report >= args.report_seconds:
            LOGGER.info("progress %s", stats.summary())
            last_report = now
        if deadline is not None and now >= deadline:
            stop.set()

    reader = build_reader(args)
    writer = CaptureWriter(
        directory=Path(args.output),
        prefix=args.prefix,
        fsync_every=args.fsync_every,
    )
    with keep_awake(not args.allow_sleep), writer:
        try:
            capture(
                reader.lines(stop),
                writer=writer,
                stats=stats,
                expected_node_id=args.expected_node_id,
                on_record=on_record,
            )
        except KeyboardInterrupt:  # pragma: no cover - operator-facing
            LOGGER.info("interrupted")

    LOGGER.info("capture finished: %s", stats.summary())
    summary_path = Path(args.output) / f"{args.prefix}-summary.json"
    with contextlib.suppress(OSError):
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(
            json.dumps(stats.as_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        LOGGER.info("summary written to %s", summary_path)

    if args.preflight:
        healthy, notes = preflight_report(stats)
        print()
        print("=== preflight ===")
        print(stats.summary())
        for note in notes:
            print(f"  - {note}")
        print()
        if healthy:
            print("Looks usable. An overnight capture from this node is worth taking.")
            return 0
        print("NOT usable as-is. Fix the above before leaving it running overnight.")
        return 1
    return 0


def _monotonic() -> float:
    import time

    return time.monotonic()


def show_ports() -> int:
    ports = list_ports()
    if not ports:
        print("No serial ports found.")
        return 1
    print(f"{'DEVICE':<12} {'SPEC':<28} DESCRIPTION")
    for port in ports:
        print(port.describe())
    print()
    print("Pass the SPEC column to --port so the capture survives re-enumeration.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--list-ports", action="store_true", help="list serial ports and exit"
    )
    parser.add_argument(
        "--port",
        help="COM7, /dev/ttyUSB0, or a stable usb:VID:PID[:SERIAL] identity",
    )
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    parser.add_argument(
        "--output", default="data/raw", help="directory for the capture files"
    )
    parser.add_argument(
        "--prefix", default="capture", help="file name prefix before the date"
    )
    parser.add_argument(
        "--expected-node-id",
        default=None,
        help=(
            "only affects the gateway_verdict column; frames from any node are "
            "still recorded"
        ),
    )
    parser.add_argument(
        "--preflight",
        type=float,
        default=None,
        metavar="SECONDS",
        help="capture for this long, then report whether the node looks usable",
    )
    parser.add_argument("--fsync-every", type=int, default=20)
    parser.add_argument("--report-seconds", type=float, default=300.0)
    parser.add_argument("--serial-timeout", type=float, default=1.0)
    parser.add_argument("--reconnect-seconds", type=float, default=2.0)
    parser.add_argument("--max-line-bytes", type=int, default=DEFAULT_MAX_LINE_BYTES)
    parser.add_argument(
        "--allow-sleep",
        action="store_true",
        help="do not inhibit system sleep (Windows only)",
    )
    parser.add_argument("--echo", action="store_true", help="log every frame")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if args.list_ports:
        return show_ports()
    if not args.port:
        build_parser().error("--port is required (see --list-ports)")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
