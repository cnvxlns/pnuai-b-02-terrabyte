"""Long-running serial ingestion and outbox upload service."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import logging
import sqlite3
from threading import Event, Thread
from typing import Any, Callable, Sequence

from .backend import HttpPublisher
from .command_relay import CommandJournal, CommandRelay
from .config import Settings
from .irrigation.decision import (
    SERVER_DOSE_MAX_ML,
    EnvelopeLimits,
    IrrigationDecider,
    IrrigationDecision,
)
from .irrigation.features import (
    MAX_HOURS_SINCE_LAST_IRRIGATION,
    FeatureError,
    IrrigationFeatures,
)
from .irrigation.forest import ModelError, RandomForestClassifier
from .irrigation.volume import (
    DEFAULT_HOURS_SINCE_LAST_IRRIGATION,
    DEFAULT_SOIL_TEMPERATURE_C,
    MODEL_VERSION,
    suggest_volume_ml,
)
from .irrigation_history import IrrigationHistory
from .mqtt_publisher import MqttPublisher
from .outbox import KIND_ACK, KIND_TELEMETRY, Outbox, OutboxFullError
from .protocol import (
    # Aliased because ``Event`` already means threading's here, and renaming
    # that would touch every worker loop.
    Event as TelemetryEvent,
    IrrigationSuggestion,
    NonTelemetryMessage,
    ProtocolError,
    UnknownNodeError,
    parse_line,
)
from .publisher import CommandTransport, Delivery, DeliveryResult, Publisher
from .serial_reader import SerialLineReader
from .state import GatewayState, write_snapshot


LOGGER = logging.getLogger(__name__)

# (port, raw line, decoded envelope). The raw line is kept in the signature
# because a family's validator may want to decide from the bytes alone.
SerialRoute = Callable[[str, bytes, dict], None]

# What the on-screen event log calls each queue kind. The screen is Korean and
# read by whoever is standing at the box, so the internal kind name must not leak
# into it.
_KIND_LABELS = {KIND_TELEMETRY: "측정값", KIND_ACK: "관수 결과"}


def load_irrigation_model() -> RandomForestClassifier | None:
    """Load the shipped forest artifact, or ``None`` if it cannot be loaded.

    ``None`` is not a degraded-but-working state: the decider reports
    ``MODEL_UNAVAILABLE`` and refuses to irrigate for as long as it lasts. Failing
    to *start* over a missing artifact would be worse — telemetry is the gateway's
    first job and it does not need a model — so the failure is logged loudly here
    and carried as a refusal there.
    """

    try:
        model = RandomForestClassifier.load()
    except ModelError as exc:
        LOGGER.error("irrigation model unavailable; will not irrigate reason=%s", exc)
        return None
    LOGGER.info(
        "irrigation model loaded version=%s trees=%d",
        model.model_version,
        model.tree_count,
    )
    return model


@dataclass(frozen=True)
class _PotHistory:
    """What the irrigation log says about one pot, read once per reading.

    Both gates and the dose formula need it, and reading it once keeps them
    consistent: two separate queries a second apart could straddle a dispense and
    size a dose against history the gate did not see.
    """

    hours_since_last_irrigation: float | None
    dispensed_today_ml: float


def _epoch_from_utc_iso(timestamp: str) -> float | None:
    """Parse an ISO-8601 ``...Z`` timestamp to epoch seconds, or ``None``.

    Tz-aware on purpose, and this is not pedantry: a naive ``datetime`` compared
    against ``time.time()`` is off by the local UTC offset, and the sign of that
    offset decides whether every reading looks fresh or every reading looks stale.
    Both failures are silent. ``fromisoformat`` on Python 3.10 does not accept the
    ``Z`` suffix, so it is rewritten rather than trusted.
    """

    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


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
        command_relay: CommandRelay | None = None,
        history: IrrigationHistory | None = None,
        model_loader: Callable[[], RandomForestClassifier | None] = load_irrigation_model,
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
        # Same database file as the outbox, its own table. Public because the
        # writers are elsewhere: the command relay records what an ack reports
        # delivered, and the autonomy state machine records its own emergency
        # doses. Nothing in this class writes to it — a decision is not a
        # delivery.
        self.history = history or IrrigationHistory(settings.database_path)
        self._model_loader = model_loader
        self._model: RandomForestClassifier | None = None
        self._model_resolved = False
        # One decider per node: the envelope's daily budget is derived from that
        # pot's volume, so four pots on one gateway do not share a budget.
        self._deciders: dict[str, IrrigationDecider] = {}
        self._last_decision: dict[str, IrrigationDecision] = {}
        self._threads: list[Thread] = []
        # Ingest threads are tracked separately from the uploader: one dead
        # port must not take the gateway down while three pots keep reporting.
        self._critical_threads: list[Thread] = []
        # Envelope discriminator -> handler. See _ingest_line for why this is a
        # table. Bound methods, so it is built per instance rather than on the
        # class.
        self._serial_routes: dict[str, SerialRoute] = {
            "message_type": self._ingest_telemetry,
            "t": self._ingest_short_key_frame,
        }
        self.command_relay = (
            command_relay if command_relay is not None else self._build_command_relay()
        )

    def _build_command_relay(self) -> CommandRelay | None:
        """The relay, or None when this deployment cannot carry commands.

        Two ways it stays absent. ``TB_COMMAND_RELAY_ENABLED=false`` is the
        operator's kill switch. The transport check is structural: HTTP has no
        command downlink in the contract at all — no topic to subscribe to and no
        ack endpoint — so under ``TB_TRANSPORT=http`` there is nothing to relay.
        Building a relay that could never receive anything would give the
        misleading impression that commands were being waited for.
        """

        if not getattr(self.settings, "command_relay_enabled", False):
            LOGGER.info("command relay disabled by configuration")
            return None
        if not isinstance(self.publisher, CommandTransport):
            LOGGER.info(
                "transport %s carries no command downlink; relay not started",
                getattr(self.settings, "transport", "?"),
            )
            return None
        return CommandRelay(
            gateway_id=self.settings.device_id,
            transport=self.publisher,
            outbox=self.outbox,
            state=self.state,
            readers=self.serial_readers,
            journal=CommandJournal(
                self.settings.database_path,
                retention_seconds=self.settings.command_journal_retention_seconds,
            ),
            stop_event=self.stop_event,
            queue_max=self.settings.command_queue_max,
            deadman_interval_seconds=self.settings.command_deadman_interval_seconds,
            deadman_grace_seconds=self.settings.command_deadman_grace_seconds,
            max_serial_bytes=self.settings.command_max_serial_bytes,
        )

    def _critical_workers(self) -> list[tuple[str, Callable[[], None]]]:
        """Workers whose death means the gateway is not doing its job.

        ``(name, target)`` pairs rather than constructed Threads so adding a
        worker — the command relay, the deadman tick — is one line here and
        nothing in start(). Every entry is fatal by construction: if a worker
        belongs in this list it must not be one whose exit the gateway can
        survive (that is what the ingest threads are, and they are built
        separately below).
        """

        workers: list[tuple[str, Callable[[], None]]] = [
            ("backend-upload", self._upload_loop),
            # The snapshot writer is its own thread rather than a tick inside
            # the uploader. The uploader blocks for up to the publish timeout on
            # a dead broker, which is exactly the moment somebody walks over and
            # plugs a monitor in; a frozen display then would hide the one fact
            # they need.
            ("status-snapshot", self._snapshot_loop),
        ]
        if self.command_relay is not None:
            # Three more, and all three are fatal by construction. A dead relay
            # means commands are being accepted by the broker and silently never
            # executed; a dead deadman means a running pump loses its keepalive;
            # a dead ack uploader means every dose becomes a phantom budget
            # deduction. None of those is survivable the way one dead port is.
            workers.extend(self.command_relay.workers())
            workers.append(("ack-upload", self._ack_upload_loop))
        return workers

    def start(self) -> None:
        self.outbox.initialize()
        # Separate table in the same file, so this is one extra CREATE TABLE IF
        # NOT EXISTS against a database the outbox has just opened.
        self.history.initialize()
        pending, dead = self.outbox.counts()
        LOGGER.info("outbox ready pending=%d dead=%d", pending, dead)
        self.state.record_outbox(pending=pending, dead=dead)

        self._critical_threads = [
            Thread(target=target, name=name, daemon=True)
            for name, target in self._critical_workers()
        ]

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

    def _suggest_irrigation(
        self,
        event: TelemetryEvent,
        *,
        hours_since_last_irrigation: float | None,
    ) -> IrrigationSuggestion | None:
        """Size a dose for this reading, or ``None`` if it cannot be sized.

        This lives in the service rather than in ``parse_line`` on purpose.
        ``parse_line`` answers one question — "is this line a valid telemetry
        message?" — using only the bytes it was handed. Sizing needs deployment
        configuration (which pot, which crop), which is the service's to know;
        threading it through the parser as a callback would put config in the
        one function that must stay decidable from the wire alone.

        ``hours_since_last_irrigation`` comes from the irrigation log, and
        ``None`` — never watered, or the log could not be read — leaves the
        formula's own "long since watered" default in place. That default makes
        the redistribution term vanish, which under-states how wet the pot
        already is, so an unreadable log biases the dose upward and the caller
        must not treat this path as authoritative.
        """

        substrate_volume_ml = self.settings.substrate_volume_ml_for(event.node_id)
        crop_code = self.settings.crop_code_for(event.node_id)
        volume_ml = suggest_volume_ml(
            soil_moisture_pct=event.soil_moisture_pct,
            air_temperature_c=event.air_temperature_c,
            air_humidity_pct=event.relative_humidity_pct,
            ppfd_umol_m2_s=event.ppfd_umol_m2_s,
            soil_temperature_c=event.soil_temperature_c,
            hours_since_last_irrigation=hours_since_last_irrigation,
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

    def _irrigation_pass(
        self, event: TelemetryEvent
    ) -> IrrigationSuggestion | None:
        """Size a dose and judge whether to deliver it. Returns the suggestion.

        One entry point for both halves, because they share an input that must
        not be read twice: how long ago this pot was watered. A dose sized against
        one answer and gated against another taken a moment later could straddle a
        dispense.

        Soil moisture gates the whole pass. The formula is the left-hand side of a
        water balance and the decision needs a moisture the sensor actually
        reported, so without one there is nothing to size and nothing to judge —
        and no reason to touch the irrigation log at all.

        Never raises. The gateway's first job is delivering observations, and a
        fault in the irrigation path must cost a verdict, not a measurement.
        Nothing downstream of here actuates anything yet, so a skipped pass costs
        a log line.
        """

        if event.soil_moisture_pct is None:
            return None
        try:
            history = self._pot_history(event.node_id)
            suggestion = self._suggest_irrigation(
                event,
                hours_since_last_irrigation=(
                    None if history is None else history.hours_since_last_irrigation
                ),
            )
            if history is not None:
                self._evaluate_irrigation(
                    event, suggestion=suggestion, history=history
                )
            return suggestion
        except Exception:  # noqa: BLE001 — telemetry must survive anything here
            LOGGER.exception("irrigation pass failed node_id=%s", event.node_id)
            return None

    def record_irrigation(
        self,
        node_id: str,
        volume_ml: float,
        *,
        source: str,
        command_id: str | None = None,
        at_epoch: float | None = None,
    ) -> bool:
        """Record water that was actually delivered to a pot.

        The seam for the command relay (an ack that reports a completed or
        partial dispense) and, later, for the autonomy state machine. Only a
        downstream that moved water may call this: everything in this class
        treats the log as read-only, because a decision is not a delivery.
        """

        recorded = self.history.record(
            node_id=node_id,
            volume_ml=volume_ml,
            source=source,
            command_id=command_id,
            at_epoch=at_epoch,
        )
        if recorded:
            LOGGER.info(
                "irrigation recorded node_id=%s volume_ml=%.1f source=%s command_id=%s",
                node_id,
                volume_ml,
                source,
                command_id,
            )
        else:
            LOGGER.info(
                "irrigation already recorded, not counted twice command_id=%s",
                command_id,
            )
        return recorded

    def latest_decision(self, node_id: str) -> IrrigationDecision | None:
        """The most recent irrigation verdict for a pot, for whoever acts on it."""

        return self._last_decision.get(node_id)

    def _irrigation_model(self) -> RandomForestClassifier | None:
        """The forest, loaded once on first use.

        Lazy rather than loaded in ``__init__`` so constructing a service does no
        file IO, and cached including the ``None`` case: a missing artifact is a
        deployment fact that will not fix itself between readings, and retrying
        the read once a second would only fill the log.
        """

        if not self._model_resolved:
            self._model = self._model_loader()
            self._model_resolved = True
        return self._model

    def _decider_for(self, node_id: str) -> IrrigationDecider:
        substrate_volume_ml = self.settings.substrate_volume_ml_for(node_id)
        decider = self._deciders.get(node_id)
        if decider is None:
            decider = IrrigationDecider(
                self._irrigation_model(),
                # Derived from this pot, not a shared constant: the daily budget
                # is a function of how much substrate there is to wet.
                limits=EnvelopeLimits.supervised(
                    substrate_volume_ml=substrate_volume_ml
                ),
            )
            LOGGER.info(
                "irrigation envelope for node_id=%s substrate_ml=%s daily_budget_ml=%.0f",
                node_id,
                substrate_volume_ml,
                decider.limits.daily_budget_ml,
            )
            self._deciders[node_id] = decider
        return decider

    def _pot_history(self, node_id: str) -> _PotHistory | None:
        """Read the irrigation log for one pot. ``None`` means it could not be.

        Distinguished from "never watered" deliberately. Both look like an absent
        record, but an unreadable log must not be read as an empty one: an empty
        log passes the minimum-interval gate and contributes nothing to the daily
        budget, so a SQLite error would open both gates at once. No history, no
        decision.
        """

        try:
            return _PotHistory(
                hours_since_last_irrigation=self.history.hours_since_last_irrigation(
                    node_id
                ),
                dispensed_today_ml=self.history.dispensed_today_ml(node_id),
            )
        except sqlite3.Error as exc:
            LOGGER.error(
                "irrigation history unreadable; no decision node_id=%s reason=%s",
                node_id,
                exc,
            )
            return None

    def _features_for(
        self, event: TelemetryEvent, history: _PotHistory
    ) -> IrrigationFeatures | None:
        """Build the model's input for this reading, or ``None`` if it cannot be.

        Returning ``None`` refuses the decision outright rather than substituting
        anything for soil moisture. The envelope would veto an invalid sensor
        anyway, but reaching that veto means passing a fabricated moisture value
        through the constructor first, and a 0.0 standing in for "no probe" is the
        one reading that must never be invented.
        """

        if event.soil_moisture_pct is None:
            return None
        captured_epoch = _epoch_from_utc_iso(event.captured_at_utc)
        if captured_epoch is None:
            LOGGER.warning(
                "unparseable capture time, no irrigation decision node_id=%s value=%s",
                event.node_id,
                event.captured_at_utc,
            )
            return None
        hours = history.hours_since_last_irrigation
        if hours is None:
            # Never watered by this gateway. The formula's own "assume nothing
            # recent is still spreading" figure, so the two paths answer the same
            # unknown the same way.
            hours = DEFAULT_HOURS_SINCE_LAST_IRRIGATION
        try:
            return IrrigationFeatures(
                soil_moisture_pct=event.soil_moisture_pct,
                # Substituted exactly as the sizing path substitutes it, so the
                # suppressor and the dose cannot disagree about the same pot.
                soil_temperature_c=(
                    DEFAULT_SOIL_TEMPERATURE_C
                    if event.soil_temperature_c is None
                    else event.soil_temperature_c
                ),
                air_temperature_c=event.air_temperature_c,
                relative_humidity_pct=event.relative_humidity_pct,
                ppfd_umol_m2_s=event.ppfd_umol_m2_s,
                hours_since_last_irrigation=min(
                    hours, MAX_HOURS_SINCE_LAST_IRRIGATION
                ),
                # The same fact the envelope publishes as
                # ``quality.soil_sensor_valid``, so the edge decision and the
                # server's own sensor gate cannot disagree about one reading.
                # Protocol v1 has no field for a probe declaring itself broken —
                # validity *is* presence — so an unusable probe reaches here as an
                # absent reading and gets no decision at all, which is stricter
                # than the SENSOR_INVALID veto.
                soil_sensor_valid=event.has_soil_reading,
                reading_age_seconds=max(0.0, self.history.clock() - captured_epoch),
            )
        except FeatureError as exc:
            # The wire contract is wider than the model's training range in one
            # place: protocol v1 accepts soil temperature up to 80 C and the
            # features stop at 70. A DS18B20 that has lost power reports its
            # 85 C reset value, so this is a real reading, not a hypothetical —
            # and a reading outside what the trees have seen gets no decision
            # rather than a clamped one.
            LOGGER.warning(
                "reading outside the model's range, no decision node_id=%s reason=%s",
                event.node_id,
                exc,
            )
            return None

    def _gated_dose_ml(
        self, node_id: str, suggestion: IrrigationSuggestion | None
    ) -> float | None:
        """The dose to weigh against the envelope, or ``None`` to fall back.

        The suggestion is deliberately unclamped: it goes to the backend as the
        water balance computed it, so the server can log a disagreement with its
        own limits instead of receiving a number already trimmed to agree
        (docs/design/irrigation_volume.md §3.2). The *gate* is the other way
        round. It must weigh the dose that could actually be delivered, and the
        Governor clamps every grant to :data:`SERVER_DOSE_MAX_ML`, so a 390 mL
        suggestion describes water that will never leave the pump. Weighing it
        would refuse an ordinary dry pot with a misconfiguration verdict.

        The clamp is logged rather than silent: a pot whose readings routinely ask
        for more than the system can deliver is a real finding about pot size or
        about the formula, and the whole point of not clamping the suggestion is
        that such faults stay visible.
        """

        if suggestion is None:
            return None
        dose = float(suggestion.volume_ml)
        if dose > SERVER_DOSE_MAX_ML:
            LOGGER.info(
                "dose exceeds what the server would grant; gating on the ceiling "
                "node_id=%s suggested_ml=%.0f ceiling_ml=%.0f",
                node_id,
                dose,
                SERVER_DOSE_MAX_ML,
            )
            return SERVER_DOSE_MAX_ML
        return dose

    def _evaluate_irrigation(
        self,
        event: TelemetryEvent,
        *,
        suggestion: IrrigationSuggestion | None,
        history: _PotHistory,
    ) -> IrrigationDecision | None:
        """Decide whether this pot should be watered now, and record the verdict.

        **Nothing is actuated here.** There is no path from this gateway to a
        pump yet; the relay that will carry one is a separate stream, and the
        autonomy state machine that may act on this verdict without the cloud is
        later still. What this does is close the loop the decider was written for
        and never wired into: the envelope is evaluated against real history, the
        forest is consulted only when the envelope allows it, and the outcome is
        available to whoever will act.

        Order is the safety argument and it lives in ``decide``: the deterministic
        envelope runs first and independently, so the model can withhold water
        but can never widen what the rules already allow (`D17`).
        """

        features = self._features_for(event, history)
        if features is None:
            return None
        if suggestion is not None and suggestion.volume_ml <= 0:
            # 0 mL is a real answer from the formula — "this pot needs nothing" —
            # and it is not a dose. Passing None instead would fall back to
            # 30 mL and water a pot the formula just said was wet enough.
            LOGGER.debug(
                "formula asked for no water, no decision node_id=%s", event.node_id
            )
            return None
        decision = self._decider_for(event.node_id).decide(
            features,
            volume_ml=self._gated_dose_ml(event.node_id, suggestion),
            dispensed_today_ml=history.dispensed_today_ml,
        )
        self._log_decision(event.node_id, decision)
        self._last_decision[event.node_id] = decision
        return decision

    def _log_decision(self, node_id: str, decision: IrrigationDecision) -> None:
        """Log verdicts without flooding: telemetry arrives about once a second.

        A change of verdict is the interesting event ("was refusing, now allows"),
        so that is what goes to INFO. The steady state goes to DEBUG.
        """

        previous = self._last_decision.get(node_id)
        changed = previous is None or previous.verdict is not decision.verdict
        message = (
            "irrigation verdict node_id=%s verdict=%s irrigate=%s volume_ml=%.1f "
            "source=%s envelope_allows=%s"
        )
        arguments = (
            node_id,
            decision.verdict.value,
            decision.irrigate,
            decision.volume_ml,
            decision.volume_source.value,
            decision.envelope_allows,
        )
        if decision.irrigate or changed:
            LOGGER.info(message, *arguments)
        else:
            LOGGER.debug(message, *arguments)

    def _ingest_loop(self, reader: SerialLineReader) -> None:
        try:
            for line in reader.lines(self.stop_event):
                self._ingest_line(reader.port, line)
        finally:
            self.state.record_link(reader.port, up=False)

    def _ingest_line(self, port: str, line: bytes) -> None:
        """Route one serial line to the handler for its envelope.

        The link is asymmetric and that is the trap. Telemetry arrives as
        ``{"message_type": "telemetry", ...}`` with long keys; command acks
        arrive as ``{"t": "ack", ...}`` with short ones, because the ATmega328P
        has 2 KB of SRAM and cannot afford the long spelling
        (docs/design/edge_ai_hardening.md §serial contract). A reader that looks
        at only one of the two discriminators drops the other family while
        reporting a validation error about the family it does know — which reads
        like a firmware bug rather than a missing route.

        A table rather than an if-chain so a new family is one entry. Both keys
        present resolves to telemetry: insertion order puts it first, and it is
        the family with the strict validator.
        """

        try:
            message = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            LOGGER.warning("discarding non-JSON serial line port=%s", port)
            self.state.record_error(port)
            return
        if not isinstance(message, dict):
            LOGGER.warning("discarding non-object serial line port=%s", port)
            self.state.record_error(port)
            return

        for key, handler in self._serial_routes.items():
            if key in message:
                handler(port, line, message)
                return
        LOGGER.warning(
            "discarding serial line with no known envelope key port=%s keys=%s",
            port,
            ",".join(sorted(message)[:5]),
        )
        self.state.record_error(port)

    def _ingest_short_key_frame(
        self, port: str, line: bytes, message: dict[str, object]
    ) -> None:
        """The short-key family (``{"t": ...}``) — command acks, so far.

        Handled on the ingest thread but only as far as a local SQLite write: the
        translated ack is queued and the ack-upload worker publishes it. A broker
        round-trip here would sit in the same loop that reads telemetry, so a
        stalled broker would stop the readings too.
        """

        if message.get("t") != "ack":
            LOGGER.warning(
                "discarding short-key serial frame with no handler port=%s t=%s",
                port,
                message.get("t"),
            )
            self.state.record_error(port)
            return
        if self.command_relay is None:
            # An ack with no relay means the firmware is answering commands this
            # build never sent — a stale queued frame, or a second host on the
            # same cable. Worth saying out loud rather than dropping silently.
            LOGGER.warning(
                "received a command ack with no relay configured port=%s id=%s",
                port,
                message.get("id"),
            )
            return
        self.command_relay.handle_serial_ack(port, message)

    def _ingest_telemetry(
        self, port: str, line: bytes, message: dict[str, object]
    ) -> None:
        # ``line`` rather than ``message``: parse_line answers "is this line a
        # valid telemetry message?" from the bytes alone, and keeping that true
        # is worth one redundant decode per second.
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
        # The display is fed before sizing: ``record_frame`` reads only
        # ``measurements()``, which a suggestion cannot change, so the screen
        # never waits on the irrigation path.
        self.state.record_frame(
            port, node_id=event.node_id, measurements=event.measurements()
        )
        # Sized and judged before the row is queued, so the suggestion is durable
        # with the reading that produced it. Sizing at upload time instead would
        # let a config change between capture and delivery attach a volume to a
        # reading taken from a different pot.
        event = replace(event, irrigation_suggestion=self._irrigation_pass(event))
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

    def _ack_upload_loop(self) -> None:
        """The second drain, for command outcomes.

        A separate thread from the telemetry uploader, not a second call inside
        it. The head-of-line block in ``_upload_once`` is per kind by design, but
        the *thread* would still be shared: a telemetry batch that is mid-backoff
        against a dead broker holds this thread for the whole publish timeout, and
        an ack delayed that long is one the backend has already expired — after
        which it charges the granted volume to the daily budget anyway. Separate
        threads are what make the per-kind block actually mean anything.
        """

        assert self.command_relay is not None  # only started when a relay exists
        while not self.stop_event.is_set():
            # Resolved per iteration rather than bound once: this thread outlives
            # any single publisher state, and a captured bound method would keep
            # calling a transport the publisher has since replaced.
            uploaded = self._upload_once(KIND_ACK, send=self.publisher.send_ack)
            if uploaded == 0:
                self.stop_event.wait(self.settings.upload_interval_seconds)

    def _upload_once(
        self,
        kind: str = KIND_TELEMETRY,
        *,
        send: Callable[[Any], DeliveryResult] | None = None,
    ) -> int:
        """Drain one batch of one kind.

        Scoped to a kind rather than to the whole table because the ``break``
        below is the head-of-line block that keeps observations in capture
        order, and that block must not reach across kinds.

        ``send`` is the publish path for this kind — acks go to a different topic
        with a different payload shape. Parameterised rather than duplicated so
        the retry, dead-letter and ordering rules below exist once: an ack path
        with its own copy of this loop would be the place those rules drift.
        """

        publish = send or self.publisher.send
        items = self.outbox.due(self.settings.upload_batch_size, kind=kind)
        processed = 0
        for item in items:
            if self.stop_event.is_set():
                break
            result = publish(item.event)
            processed += 1
            event_id = item.event.event_id
            if result.outcome is Delivery.DELIVERED:
                self.outbox.mark_delivered(event_id)
                self.state.record_delivery()
                LOGGER.info("%s delivered event_id=%s", kind, event_id)
            elif result.outcome is Delivery.RETRY:
                delay = self.outbox.mark_retry(
                    event_id,
                    item.attempts,
                    result.reason,
                    result.retry_after_seconds,
                )
                self.state.record_transport(connected=False, error=result.reason)
                LOGGER.warning(
                    "%s retry event_id=%s reason=%s delay_seconds=%.1f",
                    kind,
                    event_id,
                    result.reason,
                    delay,
                )
                # Preserve capture order and avoid multiplying requests while
                # the backend or its provisioning state is unavailable.
                break
            else:
                self.outbox.mark_dead(event_id, result.reason)
                self.state.add_event(
                    "error", f"{_KIND_LABELS.get(kind, kind)} 폐기: {result.reason}"
                )
                LOGGER.error(
                    "%s quarantined event_id=%s reason=%s",
                    kind,
                    event_id,
                    result.reason,
                )
        return processed
