"""Render a DashboardView as plain text.

The view a headless gateway can always show. There is no display server, no
toolkit and no browser in the path, so this works over SSH on anything that can
run the bridge at all - which is the situation an operator is usually in when
they most need to know why a node is quiet.

Widths are computed from the content rather than hard-coded, because the labels
are Korean: a fixed column count would be wrong the moment a node id or a fault
string is longer than expected, and a table that shifts by one character per row
is harder to read than no table.
"""

from __future__ import annotations

from .render import (
    CLAIM_CODE_HELP,
    CLAIM_CODE_LABEL,
    METRIC_LABELS,
    DashboardView,
    format_claim_code,
)


HEADERS = ("화분", "노드", "링크", *(label for _, label, _ in METRIC_LABELS), "마지막 수신")


def _display_width(text: str) -> int:
    """Columns a terminal will spend on this string.

    Hangul and the degree-sign-adjacent symbols used in the metric formats are
    double-width in a monospace terminal. Counting characters instead of columns
    is what makes a Korean table look ragged.
    """

    width = 0
    for character in text:
        code = ord(character)
        wide = (
            0x1100 <= code <= 0x115F
            or 0x2E80 <= code <= 0xA4CF
            or 0xAC00 <= code <= 0xD7A3
            or 0xF900 <= code <= 0xFAFF
            or 0xFE30 <= code <= 0xFE6F
            or 0xFF00 <= code <= 0xFF60
            or 0xFFE0 <= code <= 0xFFE6
        )
        width += 2 if wide else 1
    return width


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def render(view: DashboardView) -> str:
    """The whole board as one string, newline separated, no trailing newline."""

    lines: list[str] = []
    if view.banner is not None:
        lines.append(f"[{view.banner.level.upper()}] {view.banner.text} — {view.banner.detail}")
        lines.append("")

    lines.append(f"게이트웨이  {view.gateway_id}")
    lines.append(
        f"서버 {view.server_text}   대기열 {view.queue_text}   가동 {view.uptime_text}"
    )
    lines.append("")
    lines.append(CLAIM_CODE_LABEL)
    lines.append(f"    {format_claim_code(view.claim_code)}")
    lines.append(CLAIM_CODE_HELP)
    lines.append("")

    table = [HEADERS, *((row.label, row.node_id, row.link_text, *row.values, row.last_seen)
                        for row in view.rows)]
    widths = [max(_display_width(cell) for cell in column) for column in zip(*table)]
    for index, cells in enumerate(table):
        lines.append("  ".join(_pad(cell, width) for cell, width in zip(cells, widths)).rstrip())
        if index == 0:
            lines.append("─" * (sum(widths) + 2 * (len(widths) - 1)))

    faults = [(row.label, row.fault) for row in view.rows if row.fault]
    if faults:
        lines.append("")
        for label, fault in faults:
            lines.append(f"! {label}  {fault}")

    if view.events:
        lines.append("")
        for at, text in view.events:
            lines.append(f"{at}  {text}")

    lines.append("")
    lines.append(view.footer)
    return "\n".join(lines)
