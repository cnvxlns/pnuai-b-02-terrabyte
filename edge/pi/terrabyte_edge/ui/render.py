"""Pure snapshot-to-view formatting for the status board.

Renderer-agnostic on purpose: ui/web.py and ui/text.py both consume the
DashboardView this produces, so the formatting rules exist once.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Literal
import time

STALE_AFTER_SECONDS = 8.0
QUEUE_WARN, QUEUE_ERROR = 50, 500
Level = Literal["ok", "warn", "error", "idle"]
CLAIM_CODE_LABEL = "기기 등록 코드"
CLAIM_CODE_HELP = "앱에서 이 코드를 입력해 기기를 등록하세요"
METRIC_LABELS = (
    ("air_temperature_c", "기온", "{:.1f}℃"),
    ("air_humidity_pct", "습도", "{:.0f}%"),
    ("plant_light_ppfd_umol_m2_s", "광량", "{:.1f}"),
    ("soil_temperature_c", "지온", "{:.1f}℃"),
    ("soil_moisture_pct", "수분", "{:.0f}%"),
)


@dataclass(frozen=True)
class Banner:
    text: str
    detail: str
    level: Level


@dataclass(frozen=True)
class NodeRow:
    label: str
    node_id: str
    link_text: str
    link_level: Level
    values: tuple[str, ...]
    last_seen: str
    fault: str | None = None


@dataclass(frozen=True)
class DashboardView:
    gateway_id: str
    claim_code: str
    banner: Banner | None
    server_text: str
    server_level: Level
    queue_text: str
    queue_level: Level
    uptime_text: str
    rows: tuple[NodeRow, ...]
    events: tuple[tuple[str, str], ...]
    footer: str


def format_age(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = max(0.0, seconds)
    if seconds < 60: return f"{int(seconds)}초 전"
    if seconds < 3600: return f"{int(seconds // 60)}분 전"
    if seconds < 86400: return f"{int(seconds // 3600)}시간 {int(seconds % 3600 // 60)}분 전"
    return f"{int(seconds // 86400)}일 전"


def format_uptime(seconds: float) -> str:
    if seconds < 60: return f"{int(seconds)}초"
    if seconds < 3600: return f"{int(seconds // 60)}분"
    hours = int(seconds // 3600)
    if hours < 24: return f"{hours}시간 {int(seconds % 3600 // 60)}분"
    return f"{hours // 24}일 {hours % 24}시간"


def format_claim_code(claim_code: str) -> str:
    """Group a code for display without changing the value held by the view."""

    # The placeholder is also six characters, so it gets the same visual break
    # instead of becoming one long rule that could be mistaken for an empty box.
    shown = claim_code or "——————"
    return f"{shown[:3]} {shown[3:]}" if len(shown) == 6 else shown


def _empty_row(index: int) -> NodeRow:
    return NodeRow(f"화분 {index}", "—", "포트 없음", "idle", tuple("—" for _ in METRIC_LABELS), "—")


def build_view(snapshot: dict | None, *, now_epoch: float, slots: int = 4) -> DashboardView:
    if snapshot is None:
        return DashboardView("—", "——————", Banner("브리지 서비스 응답 없음", "상태 파일을 읽을 수 없습니다.", "error"), "확인 불가", "error", "—", "idle", "—", tuple(_empty_row(i) for i in range(1, slots + 1)), (), "브리지 서비스를 확인하세요: systemctl status terrabyte-edge")
    age = now_epoch - float(snapshot.get("generated_at_epoch", 0))
    banner = Banner("브리지 서비스 응답 없음", f"마지막 갱신 {format_age(age)}. 측정값이 현재 상태가 아닙니다.", "error") if age > STALE_AFTER_SECONDS else None
    transport = snapshot.get("transport", {}) or {}
    connected, delivered = bool(transport.get("connected")), transport.get("last_delivery_epoch")
    if connected: server_text, server_level = "연결됨", "ok"
    elif delivered is None: server_text, server_level = "연결 시도 중", "warn"
    else: server_text, server_level = f"끊김 · {transport.get('last_error') or '원인 미상'}", "error"
    outbox = snapshot.get("outbox", {}) or {}
    pending, dead = int(outbox.get("pending", 0)), int(outbox.get("dead", 0))
    queue_text = f"{pending}건" + (f" · 폐기 {dead}건" if dead else "")
    queue_level = "error" if dead or pending >= QUEUE_ERROR else "warn" if pending >= QUEUE_WARN else "ok"
    rows = []
    ports = list(snapshot.get("ports", []) or [])
    for index in range(slots):
        if index >= len(ports):
            rows.append(_empty_row(index + 1)); continue
        port = ports[index]
        link = port.get("link", "never_seen")
        link_text, link_level = (("정상", "ok") if link == "up" else ("끊김", "warn") if link == "down" else ("연결 대기", "idle"))
        measurements = port.get("measurements") or {}
        values = tuple("—" if measurements.get(key) is None else template.format(float(measurements[key])) for key, _, template in METRIC_LABELS)
        last = port.get("last_frame_epoch")
        rows.append(NodeRow(f"화분 {index + 1}", str(port.get("node_id") or "—"), link_text, link_level, values, "—" if last is None else format_age(now_epoch - float(last))))
    events = tuple((time.strftime("%H:%M:%S", time.localtime(float(e.get("at_epoch", now_epoch)))), str(e.get("text", ""))) for e in reversed(list(snapshot.get("events", []) or [])))[:6]
    started = float(snapshot.get("started_at_epoch", now_epoch))
    return DashboardView(str(snapshot.get("gateway_id", "—")), str(snapshot.get("claim_code") or "").strip() or "——————", banner, server_text, server_level, queue_text, queue_level, format_uptime(max(0, now_epoch - started)), tuple(rows), events, f"마지막 전송 {format_age(None if delivered is None else now_epoch - float(delivered))} · 화면 갱신 {format_age(age)}")
