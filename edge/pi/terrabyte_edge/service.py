"""Long-running serial ingestion and outbox upload service."""

from __future__ import annotations

from dataclasses import replace
import logging
from threading import Event, Thread

from .backend import HttpPublisher
from .config import Settings
from .irrigation.volume import MODEL_VERSION, suggest_volume_ml
from .mqtt_publisher import MqttPublisher
from .outbox import Outbox, OutboxFullError
from .protocol import (
    # Aliased because ``Event`` already means threading's here, and renaming
    # that would touch every worker loop.
    Event as TelemetryEvent,
    IrrigationSuggestion,
    NonTelemetryMessage,
    ProtocolError,
    parse_line,
)
from .publisher import Delivery, Publisher
from .serial_reader import SerialLineReader


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
        serial_reader: SerialLineReader | None = None,
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
        self.serial_reader = serial_reader or SerialLineReader(
            port=settings.serial_port,
            baudrate=settings.serial_baud,
            timeout_seconds=settings.serial_timeout_seconds,
            reconnect_seconds=settings.serial_reconnect_seconds,
            max_line_bytes=settings.serial_max_line_bytes,
        )
        self._threads: list[Thread] = []

    def start(self) -> None:
        self.outbox.initialize()
        pending, dead = self.outbox.counts()
        LOGGER.info("outbox ready pending=%d dead=%d", pending, dead)
        self._threads = [
            Thread(target=self._ingest_loop, name="serial-ingest", daemon=True),
            Thread(target=self._upload_loop, name="backend-upload", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def join(self) -> None:
        for thread in self._threads:
            thread.join(self.settings.http_timeout_seconds + 2.0)
            if thread.is_alive():
                LOGGER.warning("worker did not stop in time name=%s", thread.name)
        self.publisher.close()

    def worker_failed(self) -> bool:
        return bool(self._threads) and any(
            not thread.is_alive() for thread in self._threads
        )

    def _ingest_loop(self) -> None:
        for line in self.serial_reader.lines(self.stop_event):
            self._ingest_line(line)

    def _suggest_irrigation(
        self, event: TelemetryEvent
    ) -> IrrigationSuggestion | None:
        """Size a dose for this reading, or ``None`` if it cannot be sized.

        This lives in the service rather than in ``parse_line`` on purpose.
        ``parse_line`` answers one question — "is this line a valid telemetry
        message?" — using only the bytes it was handed. Sizing needs deployment
        configuration (which pot, which crop), which is the service's to know;
        threading it through the parser as a callback would put config in the
        one function that must stay decidable from the wire alone.
        """

        substrate_volume_ml = self.settings.substrate_volume_ml_for(event.node_id)
        crop_code = self.settings.crop_code_for(event.node_id)
        volume_ml = suggest_volume_ml(
            soil_moisture_pct=event.soil_moisture_pct,
            air_temperature_c=event.air_temperature_c,
            air_humidity_pct=event.relative_humidity_pct,
            ppfd_umol_m2_s=event.ppfd_umol_m2_s,
            soil_temperature_c=event.soil_temperature_c,
            # The edge keeps no irrigation history — there is no path to run
            # the pump from here yet (G5) — so the formula's "long since
            # watered" default stands in, and the redistribution term
            # contributes nothing.
            hours_since_last_irrigation=None,
            substrate_volume_ml=substrate_volume_ml,
            crop_code=crop_code,
        )
        if volume_ml is None:
            return None
        return IrrigationSuggestion(
            volume_ml=volume_ml,
            model_version=MODEL_VERSION,
            # Echoed back so the backend can catch its pot record drifting from
            # what is physically plugged in here.
            assumed_crop_code=crop_code,
            assumed_substrate_volume_ml=substrate_volume_ml,
        )

    def _ingest_line(self, line: bytes) -> None:
        try:
            event = parse_line(
                line,
                context_id=self.settings.crop_context_id,
                expected_node_id=self.settings.expected_node_id,
                clock_minimum_utc=self.settings.clock_minimum_utc,
            )
        except NonTelemetryMessage as exc:
            LOGGER.info("arduino status message type=%s", exc)
            return
        except ProtocolError as exc:
            LOGGER.warning("discarding invalid telemetry reason=%s", exc)
            return
        # Computed before the row is queued so the suggestion is durable with
        # the reading that produced it. Sizing at upload time instead would let
        # a config change between capture and delivery attach a volume to a
        # reading taken from a different pot.
        event = replace(event, irrigation_suggestion=self._suggest_irrigation(event))
        try:
            enqueued = self.outbox.enqueue(event)
        except OutboxFullError as exc:
            LOGGER.critical(
                "telemetry dropped because durable outbox is full reason=%s", exc
            )
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
                LOGGER.info("telemetry delivered event_id=%s", event_id)
            elif result.outcome is Delivery.RETRY:
                delay = self.outbox.mark_retry(
                    event_id,
                    item.attempts,
                    result.reason,
                    result.retry_after_seconds,
                )
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
                LOGGER.error(
                    "telemetry quarantined event_id=%s reason=%s",
                    event_id,
                    result.reason,
                )
        return processed
