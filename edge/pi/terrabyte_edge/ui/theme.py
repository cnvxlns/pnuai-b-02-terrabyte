"""Colours and fonts shared by the display windows.

Tuned for a 1920x1080 monitor viewed from a metre or two away at a demo table,
not for a desk. Dark background because the box often sits in a dim corner and
a white full-screen panel is unpleasant to stand next to.
"""

from __future__ import annotations

BACKGROUND = "#12161c"
SURFACE = "#1b212b"
SURFACE_ALT = "#232b38"
BORDER = "#2f3947"

TEXT = "#e8edf4"
TEXT_MUTED = "#8b98ab"

OK = "#4ade80"
WARN = "#fbbf24"
ERROR = "#f87171"
IDLE = "#64748b"
ACCENT = "#60a5fa"

LEVEL_COLORS = {"ok": OK, "warn": WARN, "error": ERROR, "idle": IDLE}

# NanumGothicCoding is installed on the Orange Pi image and is monospaced,
# which keeps the metric columns aligned. The fallbacks matter for running the
# display on a developer machine.
MONO_FAMILIES = ("NanumGothicCoding", "D2Coding", "DejaVu Sans Mono", "TkFixedFont")
SANS_FAMILIES = ("NanumGothic", "Noto Sans CJK KR", "DejaVu Sans", "TkDefaultFont")


def pick_family(candidates: tuple[str, ...], available: set[str]) -> str:
    for family in candidates:
        if family in available:
            return family
    return candidates[-1]
