BACKGROUND = "#12161c"
SURFACE = "#1b212b"
SURFACE_ALT = "#232b38"
TEXT = "#e8edf4"
TEXT_MUTED = "#8b98ab"
OK = "#4ade80"
WARN = "#fbbf24"
ERROR = "#f87171"
IDLE = "#64748b"
ACCENT = "#60a5fa"
LEVEL_COLORS = {"ok": OK, "warn": WARN, "error": ERROR, "idle": IDLE}
MONO_FAMILIES = ("NanumGothicCoding", "D2Coding", "DejaVu Sans Mono", "TkFixedFont")
SANS_FAMILIES = ("NanumGothic", "Noto Sans CJK KR", "DejaVu Sans", "TkDefaultFont")
BODY_FONT_SIZE = 16
TITLE_FONT_SIZE = 20
# This value is read from across a room, not scanned alongside the table.
CLAIM_CODE_FONT_SIZE = 48


def pick_family(candidates: tuple[str, ...], available: set[str]) -> str:
    return next((family for family in candidates if family in available), candidates[-1])
