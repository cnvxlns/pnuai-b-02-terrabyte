"""Snapshot JSON to a view model, with no toolkit involved.

Every layout and formatting decision lives here as a pure function so the whole
display can be tested without a display: no Tk, no X server, no window. The
Tkinter layer below this only maps the view model onto widgets and never
decides what to show.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# How stale a snapshot may get before the display stops presenting it as live.
# The bridge writes once a second, so this tolerates several missed writes
# before accusing it of being down.
STALE_AFTER_SECONDS = 8.0

# Queue depth thresholds. The outbox is doing its job when it holds a few
# items; it is telling you something when it keeps growing.
QUEUE_WARN = 50
QUEUE_ERROR = 500

Level = Literal["ok", "warn", "error", "idle"]

METRIC_LABELS: tuple[tuple[str, str, str], ...] = (
    ("air_temperature_c", "기온", "{:.1f}℃"),
    ("air_humidity_pct", "습도", "{:.0f}%"),
    ("plant_light_ppfd_umol_m2_s", "광량", "{:.0f}"),
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
    slot: int
    label: str
    node_id: str
    link_text: str
    link_level: Level
    values: tuple[str, ...]
    last_seen: str
    fault: str | None


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
    if seconds < 0:
        # Clock went backwards (NTP step). Saying "0초 전" beats a negative.
        seconds = 0.0
    if seconds < 60:
        return f"{int(seconds)}초 전"
    if seconds < 3600:
        return f"{int(seconds // 60)}분 전"
    if seconds < 86400:
        return f"{int(seconds // 3600)}시간 {int(seconds % 3600 // 60)}분 전"
    return f"{int(seconds // 86400)}일 전"


def format_uptime(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}초"
    if seconds < 3600:
        return f"{int(seconds // 60)}분"
    hours = int(seconds // 3600)
    minutes = int(seconds % 3600 // 60)
    if hours < 24:
        return f"{hours}시간 {minutes}분"
    return f"{hours // 24}일 {hours % 24}시간"


def format_clock(epoch: float) -> str:
    import time as _time

    return _time.strftime("%H:%M:%S", _time.localtime(epoch))


def _missing_view(reason: str, detail: str, slots: int) -> DashboardView:
    return DashboardView(
        gateway_id="—",
        claim_code="——————",
        banner=Banner(reason, detail, "error"),
        server_text="확인 불가",
        server_level="error",
        queue_text="—",
        queue_level="idle",
        uptime_text="—",
        rows=tuple(
            NodeRow(
                slot=index + 1,
                label=f"화분 {index + 1}",
                node_id="—",
                link_text="확인 불가",
                link_level="idle",
                values=tuple("—" for _ in METRIC_LABELS),
                last_seen="—",
                fault=None,
            )
            for index in range(slots)
        ),
        events=(),
        footer="브리지 서비스를 확인하세요:  systemctl status terrabyte-edge",
    )


def build_view(
    snapshot: dict | None,
    *,
    now_epoch: float,
    slots: int = 4,
) -> DashboardView:
    """Turn a published snapshot into everything the screen needs.

    ``snapshot`` is None when the bridge has never run or has stopped, which is
    a normal state and renders as a diagnostic rather than an error dialog.
    """

    if snapshot is None:
        return _missing_view(
            "브리지 서비스 응답 없음",
            "상태 파일이 없습니다. 서비스가 정지되었을 수 있습니다.",
            slots,
        )

    age = now_epoch - float(snapshot.get("generated_at_epoch", 0.0))
    banner: Banner | None = None
    if age > STALE_AFTER_SECONDS:
        banner = Banner(
            "브리지 서비스 응답 없음",
            f"마지막 갱신 {format_age(age)}. 측정값이 현재 상태가 아닙니다.",
            "error",
        )

    transport = snapshot.get("transport", {}) or {}
    connected = bool(transport.get("connected"))
    last_delivery = transport.get("last_delivery_epoch")
    if connected:
        server_text = "연결됨"
        server_level: Level = "ok"
    elif last_delivery is None:
        server_text = "연결 시도 중"
        server_level = "warn"
    else:
        reason = transport.get("last_error") or "원인 미상"
        server_text = f"끊김 · {reason}"
        server_level = "error"

    outbox = snapshot.get("outbox", {}) or {}
    pending = int(outbox.get("pending", 0))
    dead = int(outbox.get("dead", 0))
    queue_text = f"{pending}건"
    if dead:
        queue_text += f" · 폐기 {dead}건"
    if dead or pending >= QUEUE_ERROR:
        queue_level: Level = "error"
    elif pending >= QUEUE_WARN:
        queue_level = "warn"
    else:
        queue_level = "ok"

    ports = list(snapshot.get("ports", []) or [])
    rows: list[NodeRow] = []
    for index in range(slots):
        port = ports[index] if index < len(ports) else None
        rows.append(_build_row(index + 1, port, now_epoch))

    events = tuple(
        (
            format_clock(float(event.get("at_epoch", now_epoch))),
            str(event.get("text", "")),
        )
        for event in reversed(list(snapshot.get("events", []) or []))
    )

    started = float(snapshot.get("started_at_epoch", now_epoch))
    delivery_age = (
        None if last_delivery is None else now_epoch - float(last_delivery)
    )

    return DashboardView(
        gateway_id=str(snapshot.get("gateway_id", "—")),
        claim_code=str(snapshot.get("claim_code") or "").strip() or "——————",
        banner=banner,
        server_text=server_text,
        server_level=server_level,
        queue_text=queue_text,
        queue_level=queue_level,
        uptime_text=format_uptime(max(0.0, now_epoch - started)),
        rows=tuple(rows),
        events=events[:6],
        footer=f"마지막 전송 {format_age(delivery_age)} · 화면 갱신 {format_age(age)}",
    )


def _build_row(slot: int, port: dict | None, now_epoch: float) -> NodeRow:
    label = f"화분 {slot}"
    if port is None:
        return NodeRow(
            slot=slot,
            label=label,
            node_id="—",
            link_text="포트 없음",
            link_level="idle",
            values=tuple("—" for _ in METRIC_LABELS),
            last_seen="—",
            fault=None,
        )

    node_id = str(port.get("node_id") or "—")
    link = str(port.get("link", "never_seen"))
    fault = port.get("fault")
    fault_detail = port.get("fault_detail")

    if fault:
        # A fault outranks the link state: a port that is electrically up but
        # sending a duplicate id is worse than one that is simply absent,
        # because its readings look plausible.
        link_text = "중복 노드" if fault == "duplicate_node" else "미등록 노드"
        link_level: Level = "error"
    elif link == "up":
        link_text = "정상"
        link_level = "ok"
    elif link == "down":
        link_text = "끊김"
        link_level = "warn"
    else:
        link_text = "연결 대기"
        link_level = "idle"

    measurements = port.get("measurements") or {}
    values: list[str] = []
    for key, _label, template in METRIC_LABELS:
        raw = measurements.get(key)
        # A missing probe stays a dash. Substituting 0 here would read as a
        # real measurement and, for soil moisture, as "bone dry".
        values.append("—" if raw is None else template.format(float(raw)))

    last_frame = port.get("last_frame_epoch")
    last_seen = (
        "—" if last_frame is None else format_age(now_epoch - float(last_frame))
    )

    return NodeRow(
        slot=slot,
        label=label,
        node_id=node_id,
        link_text=link_text,
        link_level=link_level,
        values=tuple(values),
        last_seen=last_seen,
        fault=str(fault_detail) if fault_detail else None,
    )
