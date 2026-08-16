"""Command line entrypoint for ``python -m terrabyte_edge``.

Subcommands, with ``run`` as the default so the existing systemd unit's
``ExecStart=... -m terrabyte_edge`` keeps working unchanged:

    run        the telemetry bridge (default)
    dashboard  full-screen status display for an attached monitor
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from pathlib import Path
from threading import Event

from .config import ConfigError, Settings
from .service import BridgeService

DEFAULT_SNAPSHOT_PATH = Path("/run/terrabyte-edge/status.json")


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="terrabyte_edge", description=__doc__)
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("run", help="텔레메트리 브릿지 실행 (기본값)")

    dashboard = sub.add_parser("dashboard", help="모니터용 전체화면 상태 화면")
    dashboard.add_argument(
        "--snapshot-path",
        type=Path,
        default=Path(os.environ.get("TB_STATUS_SNAPSHOT_PATH", DEFAULT_SNAPSHOT_PATH)),
    )
    dashboard.add_argument(
        "--windowed",
        action="store_true",
        help="전체화면 대신 창으로 실행 (개발용)",
    )

    # No subcommand means the bridge, so the deployed unit needs no edit.
    return parser.parse_args(argv if argv else ["run"])


def _run_dashboard(arguments: argparse.Namespace) -> int:
    # Imported here so a missing tkinter cannot stop the bridge from starting.
    from .ui.dashboard import run as run_dashboard

    return run_dashboard(arguments.snapshot_path, fullscreen=not arguments.windowed)


def main(argv: list[str] | None = None) -> int:
    arguments = _parse_args(sys.argv[1:] if argv is None else argv)
    if arguments.command == "dashboard":
        _configure_logging("INFO")
        return _run_dashboard(arguments)

    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    _configure_logging(settings.log_level)
    service = BridgeService(settings)
    terminated = Event()

    def request_stop(signum: int, _frame: object) -> None:
        logging.getLogger(__name__).info("shutdown requested signal=%d", signum)
        service.stop()
        terminated.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    try:
        service.start()
        while not terminated.wait(1.0):
            if service.worker_failed():
                raise RuntimeError("worker thread stopped unexpectedly")
    except Exception:
        logging.getLogger(__name__).exception("service stopped unexpectedly")
        return_code = 1
    else:
        return_code = 0
    finally:
        service.stop()
        service.join()
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
