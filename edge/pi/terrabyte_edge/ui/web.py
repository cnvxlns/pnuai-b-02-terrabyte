"""Serve the gateway status board over HTTP.

This replaces a Tk window, and the reason is portability rather than taste. A
GUI toolkit is the least portable thing a gateway can depend on: the Tk that
Apple ships is 8.5, which paints a blank white window on current macOS, and the
Tk available on a given Linux image varies by distribution and by whether anyone
installed the -tk package. A browser, by contrast, exists on every machine an
operator might use - including the phone in their pocket, which is the device
they actually have with them when they are standing next to the pot.

Containerising the gateway does not solve this and on macOS makes it worse:
Docker Desktop runs a Linux VM that cannot pass a host USB serial device
through, so ``--device=/dev/cu.usbserial-10`` names a path that does not exist
inside the container. Removing the toolkit dependency is the portable answer;
containerising is not.

Only the standard library is used. The bridge's dependency list is pyserial and
paho-mqtt, and a status page is not a good enough reason to add a third.

The server is read-only and unauthenticated, so it binds to loopback unless the
operator says otherwise. See ``serve``.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import html
import json
import logging
from pathlib import Path
import time
from typing import Callable

from ..state import read_snapshot
from . import theme
from .render import (
    CLAIM_CODE_HELP,
    CLAIM_CODE_LABEL,
    METRIC_LABELS,
    DashboardView,
    build_view,
    format_claim_code,
)


LOGGER = logging.getLogger(__name__)

REFRESH_SECONDS = 2

# The Tk theme's palette, carried over so the board looks the same as it did on
# the Orange Pi's monitor.
PALETTE = {
    "background": theme.BACKGROUND,
    "surface": theme.SURFACE,
    "surface_alt": theme.SURFACE_ALT,
    "text": theme.TEXT,
    "muted": theme.TEXT_MUTED,
    "accent": theme.ACCENT,
    "ok": theme.OK,
    "warn": theme.WARN,
    "error": theme.ERROR,
    "idle": theme.IDLE,
}

_STYLE = """
*{box-sizing:border-box}
body{margin:0;padding:28px;background:%(background)s;color:%(text)s;
 font-family:-apple-system,"Noto Sans KR","Malgun Gothic",sans-serif;font-size:17px}
.banner{color:#1b1b1b;font-weight:700;padding:12px 16px;
 border-radius:8px;margin-bottom:14px}
header{display:flex;justify-content:space-between;align-items:baseline;gap:16px;
 background:%(surface)s;padding:14px 20px;border-radius:8px}
h1{margin:0;font-size:21px;color:%(accent)s}
.claim{margin-top:14px;padding:14px 20px;text-align:center;
 background:%(surface_alt)s;border-radius:8px}
.claim-label{font-size:18px;font-weight:700}
.claim-code{color:%(accent)s;font-size:clamp(42px,8vw,72px);
 font-weight:700;line-height:1.15;letter-spacing:.08em}
.claim-help{color:%(muted)s;font-size:15px}
table{width:100%%;border-collapse:collapse;margin:18px 0}
th{text-align:left;color:%(muted)s;font-weight:500;padding:6px 8px;font-size:15px}
td{padding:12px 8px}
tbody tr:nth-child(odd){background:%(surface)s}
tbody tr:nth-child(even){background:%(surface_alt)s}
tbody tr td:first-child{border-radius:6px 0 0 6px}
tbody tr td:last-child{border-radius:0 6px 6px 0}
.num{font-variant-numeric:tabular-nums}
.fault{color:%(error)s;font-size:15px}
.events{color:%(muted)s;font-size:15px;line-height:1.7;margin:0;padding:0;list-style:none}
footer{color:%(muted)s;font-size:15px;margin-top:18px}
.ok{color:%(ok)s}.warn{color:%(warn)s}.error{color:%(error)s}.idle{color:%(idle)s}
""" % PALETTE


def render_html(view: DashboardView) -> str:
    """The board as a self-refreshing page.

    A meta refresh rather than JavaScript polling: it survives a browser with
    scripting disabled, it recovers on its own when the gateway restarts mid
    request, and there is no state worth preserving across a reload.
    """

    esc = html.escape
    parts = [
        "<!doctype html><html lang=\"ko\"><head><meta charset=\"utf-8\">",
        f"<meta http-equiv=\"refresh\" content=\"{REFRESH_SECONDS}\">",
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">",
        f"<title>TerraByte 게이트웨이 {esc(view.gateway_id)}</title>",
        "<style>", _STYLE, "</style></head><body>",
    ]

    if view.banner is not None:
        # The colour is per-render data, so it rides on the element rather than
        # being substituted into the shared stylesheet.
        parts.append(
            f"<div class=\"banner\" style=\"background:{PALETTE[view.banner.level]}\">"
            f"⚠ {esc(view.banner.text)} — {esc(view.banner.detail)}</div>"
        )

    parts.append(
        "<header><h1>게이트웨이 {gateway}</h1><div class=\"num\">서버 "
        "<span class=\"{level}\">{server}</span>&nbsp;&nbsp; 대기열 {queue}"
        "&nbsp;&nbsp; 가동 {uptime}</div></header>".format(
            gateway=esc(view.gateway_id),
            level=view.server_level,
            server=esc(view.server_text),
            queue=esc(view.queue_text),
            uptime=esc(view.uptime_text),
        )
    )
    parts.append(
        "<section class=\"claim\"><div class=\"claim-label\">{label}</div>"
        "<div class=\"claim-code num\">{code}</div>"
        "<div class=\"claim-help\">{help}</div></section>".format(
            label=esc(CLAIM_CODE_LABEL),
            code=esc(format_claim_code(view.claim_code)),
            help=esc(CLAIM_CODE_HELP),
        )
    )

    headers = "".join(
        f"<th>{esc(name)}</th>"
        for name in ("화분", "노드", "링크", *(label for _, label, _ in METRIC_LABELS), "마지막 수신")
    )
    rows = []
    for row in view.rows:
        values = "".join(f"<td class=\"num\">{esc(value)}</td>" for value in row.values)
        fault = f"<div class=\"fault\">{esc(row.fault)}</div>" if row.fault else ""
        rows.append(
            f"<tr><td>{esc(row.label)}{fault}</td><td>{esc(row.node_id)}</td>"
            f"<td class=\"{row.link_level}\">{esc(row.link_text)}</td>"
            f"{values}<td class=\"num\">{esc(row.last_seen)}</td></tr>"
        )
    parts.append(f"<table><thead><tr>{headers}</tr></thead><tbody>{''.join(rows)}</tbody></table>")

    if view.events:
        items = "".join(f"<li>{esc(at)} &nbsp; {esc(text)}</li>" for at, text in view.events)
        parts.append(f"<ul class=\"events\">{items}</ul>")

    parts.append(f"<footer>{esc(view.footer)}</footer></body></html>")
    return "".join(parts)


class StatusHandler(BaseHTTPRequestHandler):
    """Three routes and nothing else. A status board is not an API."""

    server_version = "terrabyte-edge"
    sys_version = ""

    # Set by serve().
    view_factory: Callable[[], DashboardView]

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's contract
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/":
            self._respond(200, "text/html; charset=utf-8", render_html(self.view_factory()))
        elif path == "/status.json":
            # The same view as JSON, for a probe or a second screen. Deliberately
            # the rendered view rather than the raw snapshot: the formatting
            # rules live in one place and a consumer of this cannot drift from
            # what the page shows.
            self._respond(200, "application/json; charset=utf-8", _view_json(self.view_factory()))
        elif path == "/healthz":
            self._respond(200, "text/plain; charset=utf-8", "ok")
        else:
            self._respond(404, "text/plain; charset=utf-8", "not found")

    def _respond(self, status: int, content_type: str, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        # The page refreshes itself; a cached copy would freeze the board at
        # whatever the gateway looked like the first time someone opened it.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args: object) -> None:
        # One line per request at INFO would be a line every two seconds per
        # viewer, which buries the bridge's own logging.
        LOGGER.debug("status request %s", fmt % args)


def _view_json(view: DashboardView) -> str:
    return json.dumps(
        {
            "gateway_id": view.gateway_id,
            "server": {"text": view.server_text, "level": view.server_level},
            "queue": {"text": view.queue_text, "level": view.queue_level},
            "uptime": view.uptime_text,
            "banner": None if view.banner is None else {
                "text": view.banner.text,
                "detail": view.banner.detail,
                "level": view.banner.level,
            },
            "rows": [
                {
                    "label": row.label,
                    "node_id": row.node_id,
                    "link": {"text": row.link_text, "level": row.link_level},
                    "values": dict(zip((key for key, _, _ in METRIC_LABELS), row.values)),
                    "last_seen": row.last_seen,
                    "fault": row.fault,
                }
                for row in view.rows
            ],
            "events": [{"at": at, "text": text} for at, text in view.events],
            "footer": view.footer,
        },
        ensure_ascii=False,
    )


def build_server(
    *,
    snapshot_path: Path,
    host: str = "127.0.0.1",
    port: int = 8090,
    slots: int = 4,
    clock: Callable[[], float] = time.time,
) -> ThreadingHTTPServer:
    """A configured server that has not started serving yet.

    Split from :func:`serve` so a test can bind an ephemeral port, make one
    request and shut down without a thread or a sleep.
    """

    def view_factory() -> DashboardView:
        return build_view(read_snapshot(snapshot_path), now_epoch=clock(), slots=slots)

    handler = type("BoundStatusHandler", (StatusHandler,), {"view_factory": staticmethod(view_factory)})
    return ThreadingHTTPServer((host, port), handler)


def serve(
    snapshot_path: Path, *, host: str = "127.0.0.1", port: int = 8090, slots: int = 4
) -> int:
    """Serve until interrupted.

    Loopback by default. The board is read-only, but it still names the pots and
    says when the gateway last heard from them, and it has no authentication -
    so exposing it is a decision the operator makes explicitly by passing a host.
    """

    server = build_server(snapshot_path=snapshot_path, host=host, port=port, slots=slots)
    shown = "localhost" if host in {"127.0.0.1", "::1"} else host
    LOGGER.info("status board on http://%s:%d (snapshot %s)", shown, port, snapshot_path)
    print(f"상태판: http://{shown}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0
