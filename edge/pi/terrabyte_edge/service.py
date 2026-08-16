"""Long-running serial ingestion and outbox upload service."""

from __future__ import annotations

import logging
from threading import Event, Thread
from typing import Sequence

from .backend import HttpPublisher
from .config import Settings
from .mqtt_publisher import MqttPublisher
from .outbox import Outbox, OutboxFullError
from .protocol import (
    NonTelemetryMessage,
    ProtocolError,
    UnknownNodeError,
    parse_line,
)
from .publisher import Delivery, Publisher
from .serial_reader import SerialLineReader
from .state import GatewayState, write_snapshot


LOGGER = logging.getLogger(__name__)


def _build_publisher(settings: Settings) -> Publisher:
    if settings.transport == "mqtt":
        return MqttPublisher(
            host=settings.mqtt_host,
            port=settings.mqtt_port,
            gateway_id=settings.device_id,
            topic_prefix=settings.mqtt_topic_prefix,
            username=settings.mqtt_username,
            password=settings.mqtt_password,
            tls=settings.mqtt_tls,
            tls_ca_cert=settings.mqtt_tls_ca_cert,
            keepalive_seconds=settings.mqtt_keepalive_seconds,
            publish_timeout_seconds=settings.mqtt_publish_timeout_seconds,
        )
    return HttpPublisher(
        telemetry_url=settings.telemetry_url,
        device_id=settings.device_id,
        token=settings.device_token,
        timeout_seconds=settings.http_timeout_seconds,
    )


class BridgeService:
    def __init__(
        self,
        settings: Settings,
        *,
        outbox: Outbox | None = None,
        publisher: Publisher | None = None,
        serial_readers: Sequence[SerialLineReader] | None = None,
        state: GatewayState | None = None,
    ) -> None:
        self.settings = settings
        self.stop_event = Event()
        self.outbox = outbox or Outbox(
            settings.database_path,
            retry_base_seconds=settings.retry_base_seconds,
            retry_max_seconds=settings.retry_max_seconds,
            max_rows=settings.outbox_max_rows,
        )
        self.publisher = publisher or _build_publisher(settings)
        self.serial_readers = list(serial_readers) if serial_readers is not None else [
            SerialLineReader(
                port=port,
                baudrate=settings.serial_baud,
                timeout_seconds=settings.serial_timeout_seconds,
                reconnect_seconds=settings.serial_reconnect_seconds,
                max_line_bytes=settings.serial_max_line_bytes,
            )
            for port in settings.serial_ports
        ]
        self.state = state or GatewayState(
            gateway_id=settings.device_id,
            claim_code=settings.claim_code,
            transport=settings.transport,
            ports=tuple(reader.port for reader in self.serial_readers),
        )
        self._threads: list[Thread] = []
        # Ingest threads are tracked separately from the uploader: one dead
        # port must not take the gateway down while three pots keep reporting.
        self._critical_threads: list[Thread] = []

    def start(self) -> None:
        self.outbox.initialize()
        pending, dead = self.outbox.counts()
        LOGGER.info("outbox ready pending=%d dead=%d", pending, dead)
        self.state.record_outbox(pending=pending, dead=dead)

        uploader = Thread(target=self._upload_loop, name="backend-upload", daemon=True)
        # The snapshot writer is its own thread rather than a tick inside the
        # uploader. The uploader blocks for up to the publish timeout on a dead
        # broker, which is exactly the moment somebody walks over and plugs a
        # monitor in; a frozen display then would hide the one fact they need.
        snapshotter = Thread(target=self._snapshot_loop, name="status-snapshot", daemon=True)
        self._critical_threads = [uploader, snapshotter]

        ingest_threads = [
            Thread(
                target=self._ingest_loop,
                args=(reader,),
                name=f"serial-ingest-{index}",
                daemon=True,
            )
            for index, reader in enumerate(self.serial_readers)
        ]
        self._threads = ingest_threads + self._critical_threads
        for thread in self._threads:
            thread.start()
        LOGGER.info(
            "bridge started ports=%d nodes=%s",
            len(self.serial_readers),
            ",".join(sorted(self.settings.expected_node_ids)),
        )

    def stop(self) -> None:
        self.stop_event.set()

    def join(self) -> None:
        for thread in self._threads:
            thread.join(self.settings.http_timeout_seconds + 2.0)
            if thread.is_alive():
                LOGGER.warning("worker did not stop in time name=%s", thread.name)
        self.publisher.close()

    def worker_failed(self) -> bool:
        """Only the uploader and the snapshot writer are fatal.

        An ingest thread that exits has lost one Arduino. Restarting the whole
        process for that would drop the other three pots and empty nothing from
        the outbox — the port is marked down and the display says so instead.
        """

        return bool(self._critical_threads) and any(
            not thread.is_alive() for thread in self._critical_threads
        )

    def _snapshot_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                pending, dead = self.outbox.counts()
                self.state.record_outbox(pending=pending, dead=dead)
                write_snapshot(self.settings.status_snapshot_path, self.state.snapshot())
            except OSError as exc:
                # A snapshot is a convenience for whoever is looking at the
                # screen; failing to write one must never stop telemetry.
                LOGGER.warning("status snapshot write failed reason=%s", exc)
            self.stop_event.wait(self.settings.status_snapshot_seconds)

    def _ingest_loop(self, reader: SerialLineReader) -> None:
        try:
            for line in reader.lines(self.stop_event):
                self._ingest_line(reader.port, line)
        finally:
            self.state.record_link(reader.port, up=False)

    def _ingest_line(self, port: str, line: bytes) -> None:
        try:
            event = parse_line(
                line,
                context_id=self.settings.crop_context_id,
                allowed_node_ids=self.settings.expected_node_ids,
                clock_minimum_utc=self.settings.clock_minimum_utc,
            )
        except UnknownNodeError as exc:
            LOGGER.warning(
                "discarding telemetry from unlisted node port=%s node_id=%s",
                port,
                exc.node_id,
            )
            self.state.record_unknown_node(port, exc.node_id)
            self.state.add_event("warn", f"알 수 없는 노드 {exc.node_id}")
            return
        except NonTelemetryMessage as exc:
            LOGGER.info(
                "arduino status message port=%s type=%s", port, exc.message_type
            )
            if exc.node_id:
                self.state.record_announcement(port, exc.node_id)
            return
        except ProtocolError as exc:
            LOGGER.warning(
                "discarding invalid telemetry port=%s reason=%s", port, exc
            )
            self.state.record_error(port)
            return
        self.state.record_frame(
            port, node_id=event.node_id, measurements=event.measurements()
        )
        try:
            enqueued = self.outbox.enqueue(event)
        except OutboxFullError as exc:
            LOGGER.critical(
                "telemetry dropped because durable outbox is full reason=%s", exc
            )
            self.state.add_event("error", "저장 큐가 가득 차 측정값을 버렸습니다")
            return
        if enqueued:
            LOGGER.info(
                "telemetry persisted event_id=%s node_id=%s sequence=%d",
                event.event_id,
                event.node_id,
                event.sequence,
            )

    def _upload_loop(self) -> None:
        while not self.stop_event.is_set():
            uploaded = self._upload_once()
            if uploaded == 0:
                self.stop_event.wait(self.settings.upload_interval_seconds)

    def _upload_once(self) -> int:
        items = self.outbox.due(self.settings.upload_batch_size)
        processed = 0
        for item in items:
            if self.stop_event.is_set():
                break
            result = self.publisher.send(item.event)
            processed += 1
            event_id = item.event.event_id
            if result.outcome is Delivery.DELIVERED:
                self.outbox.mark_delivered(event_id)
                self.state.record_delivery()
                LOGGER.info("telemetry delivered event_id=%s", event_id)
            elif result.outcome is Delivery.RETRY:
                delay = self.outbox.mark_retry(
                    event_id,
                    item.attempts,
                    result.reason,
                    result.retry_after_seconds,
                )
                self.state.record_transport(connected=False, error=result.reason)
                LOGGER.warning(
                    "telemetry retry event_id=%s reason=%s delay_seconds=%.1f",
                    event_id,
                    result.reason,
                    delay,
                )
                # Preserve capture order and avoid multiplying requests while
                # the backend or its provisioning state is unavailable.
                break
            else:
                self.outbox.mark_dead(event_id, result.reason)
                self.state.add_event("error", f"측정값 폐기: {result.reason}")
                LOGGER.error(
                    "telemetry quarantined event_id=%s reason=%s",
                    event_id,
                    result.reason,
                )
        return processed
