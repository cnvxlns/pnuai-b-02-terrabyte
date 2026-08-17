from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import time
from datetime import datetime, timezone
import unittest

from terrabyte_edge.irrigation import (
    FIXED_VOLUME_ML,
    IrrigationFeatures,
    Verdict,
    VolumeSource,
)
from terrabyte_edge.irrigation.decision import SERVER_DOSE_MAX_ML
from terrabyte_edge.irrigation.forest import RandomForestVote
from terrabyte_edge.irrigation_history import (
    SOURCE_CLOUD_COMMAND,
    IrrigationHistory,
)
from terrabyte_edge.outbox import KIND_TELEMETRY, OutboxItem
from terrabyte_edge.protocol import Event
from terrabyte_edge.publisher import Delivery, DeliveryResult
from terrabyte_edge.service import BridgeService


# ``_ingest_line`` takes the port the line arrived on so a fault can be shown
# against the right cable. Which port is irrelevant to these tests, but it has
# to be a real string: GatewayState keys its per-port records by it.
PORT = "/dev/serial/by-id/usb-test"


def event(event_id: str) -> Event:
    return Event(
        event_id=event_id,
        context_id="ctx-1",
        captured_at_utc="2026-07-21T04:05:06Z",
        node_id="node-1",
        sequence=1,
        uptime_ms=100,
        air_temperature_c=20.0,
        relative_humidity_pct=50.0,
        ppfd_umol_m2_s=300.0,
    )


class FakeOutbox:
    def __init__(self) -> None:
        self.items = [
            OutboxItem(event("oldest"), 0),
            OutboxItem(event("newer"), 0),
        ]
        self.retried: list[str] = []
        self.delivered: list[str] = []
        self.due_kinds: list[str] = []

    def due(self, _limit: int, *, kind: str = KIND_TELEMETRY) -> list[OutboxItem]:
        self.due_kinds.append(kind)
        return self.items

    def mark_retry(
        self,
        event_id: str,
        _attempts: int,
        _error: str,
        _retry_after: float | None,
    ) -> float:
        self.retried.append(event_id)
        return 2.0

    def mark_delivered(self, event_id: str) -> None:
        self.delivered.append(event_id)

    def mark_dead(self, _event_id: str, _error: str) -> None:
        raise AssertionError("unexpected dead-letter")


class FakePublisher:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False

    def send(self, item: Event) -> DeliveryResult:
        self.sent.append(item.event_id)
        return DeliveryResult(Delivery.RETRY, "offline")

    def close(self) -> None:
        self.closed = True


class ServiceTests(unittest.TestCase):
    def test_retry_stops_newer_delivery_to_preserve_order(self) -> None:
        outbox = FakeOutbox()
        publisher = FakePublisher()
        settings = SimpleNamespace(
            upload_batch_size=20,
            http_timeout_seconds=1.0,
            device_id="orangepi-test",
            claim_code="483920",
            transport="mqtt",
            # Never opened: these two tests drive the uploader, not the ingest
            # path, so no irrigation decision is attempted.
            database_path=Path("/tmp/unused-outbox.sqlite3"),
        )
        service = BridgeService(
            settings,
            outbox=outbox,
            publisher=publisher,
            serial_readers=[],
        )

        self.assertEqual(service._upload_once(), 1)
        self.assertEqual(publisher.sent, ["oldest"])
        self.assertEqual(outbox.retried, ["oldest"])
        self.assertEqual(outbox.delivered, [])
        # The block above is scoped to telemetry. An ack must not be stuck
        # behind a backed-off observation, so this worker never asks for a
        # mixed batch.
        self.assertEqual(outbox.due_kinds, [KIND_TELEMETRY])

    def test_join_closes_the_publisher(self) -> None:
        outbox = FakeOutbox()
        publisher = FakePublisher()
        settings = SimpleNamespace(
            upload_batch_size=20,
            http_timeout_seconds=1.0,
            device_id="orangepi-test",
            claim_code="483920",
            transport="mqtt",
            # Never opened: these two tests drive the uploader, not the ingest
            # path, so no irrigation decision is attempted.
            database_path=Path("/tmp/unused-outbox.sqlite3"),
        )
        service = BridgeService(
            settings,
            outbox=outbox,
            publisher=publisher,
            serial_readers=[],
        )

        service.join()
        self.assertTrue(publisher.closed)


class _StubModel:
    """Stand-in for the forest that counts how often it was consulted.

    The counter is the point: `D17` says the model may only suppress irrigation,
    and the way that is enforced is that the deterministic envelope runs first and
    short-circuits. "The model was never asked" is the observable form of that
    guarantee, and nothing else in a decision distinguishes it.
    """

    def __init__(self, irrigate: bool = True) -> None:
        self.model_version = "stub"
        self.calls = 0
        self._irrigate = irrigate

    def predict(self, features: IrrigationFeatures) -> RandomForestVote:
        self.calls += 1
        return RandomForestVote(
            irrigate=self._irrigate,
            probability=0.9 if self._irrigate else 0.1,
            model_version=self.model_version,
        )


class IngestIrrigationTestCase(unittest.TestCase):
    """Shared harness for the two halves of the irrigation path on ingest."""

    NODE = "node-1"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "outbox.sqlite3"

    def build(self, *, volumes=None, crops=None, model=None):
        settings = SimpleNamespace(
            upload_batch_size=20,
            http_timeout_seconds=1.0,
            crop_context_id="ctx-1",
            expected_node_ids=frozenset({self.NODE}),
            clock_minimum_utc=datetime(2025, 1, 1, tzinfo=timezone.utc),
            device_id="orangepi-test",
            claim_code="483920",
            transport="mqtt",
            database_path=self.database_path,
            pot_substrate_ml=volumes or {},
            pot_crop_codes=crops or {},
            substrate_volume_ml_for=lambda node: (volumes or {}).get(node),
            crop_code_for=lambda node: (crops or {}).get(node),
        )
        history = IrrigationHistory(self.database_path)
        history.initialize()
        outbox = RecordingOutbox()
        service = BridgeService(
            settings,
            outbox=outbox,
            publisher=SimpleNamespace(close=lambda: None),
            serial_readers=[],
            history=history,
            model_loader=lambda: model,
        )
        return service, outbox

    def line(self, moisture=20.0, **overrides):
        message = {
            "message_type": "telemetry",
            "protocol_version": 1,
            "node_id": self.NODE,
            "sequence": 1,
            "uptime_ms": 1000,
            "air_temperature_c": 24.0,
            "relative_humidity_pct": 55.0,
            "ppfd_umol_m2_s": 300.0,
            "soil_moisture_pct": moisture,
        }
        message.update(overrides)
        return (json.dumps(message) + "\n").encode()


class IrrigationSuggestionWiringTests(IngestIrrigationTestCase):
    """The suggestion has to be attached before the event is queued.

    If it were computed at upload time instead, a reading that sat in the outbox
    through a network outage would be sized against configuration that may have
    changed since — the dose would no longer match the reading that justified it.
    """

    def test_configured_pot_gets_a_suggestion(self):
        service, outbox = self.build(
            volumes={"node-1": 3000}, crops={"node-1": "lettuce"}
        )
        service._ingest_line(PORT, self.line())

        suggestion = outbox.events[0].irrigation_suggestion
        self.assertIsNotNone(suggestion)
        self.assertGreater(suggestion.volume_ml, 0)
        self.assertEqual(suggestion.assumed_crop_code, "lettuce")
        self.assertEqual(suggestion.assumed_substrate_volume_ml, 3000)

    def test_no_pot_volume_means_no_suggestion(self):
        """Better an absent block than a number sized for a pot we guessed at —
        the backend's fallback table exists for exactly this case."""

        service, outbox = self.build(crops={"node-1": "lettuce"})
        service._ingest_line(PORT, self.line())

        self.assertIsNone(outbox.events[0].irrigation_suggestion)

    def test_the_suggestion_travels_in_the_envelope(self):
        service, outbox = self.build(
            volumes={"node-1": 3000}, crops={"node-1": "lettuce"}
        )
        service._ingest_line(PORT, self.line())

        node = outbox.events[0].envelope_v2(gateway_id="gw-1")["nodes"][0]
        self.assertIn("irrigation_suggestion", node)
        self.assertEqual(node["irrigation_suggestion"]["model_version"], "water-balance-v1")

    def test_a_recent_irrigation_lowers_the_dose(self):
        """The history the sizing path could not see before.

        The formula's redistribution term says a pot watered an hour ago still has
        water spreading downward past the probe. With ``hours_since_last_irrigation``
        hardcoded to None that term was always zero, so every dose was sized as if
        the pot had been dry for three days.
        """

        service, outbox = self.build(
            volumes={"node-1": 3000}, crops={"node-1": "lettuce"}
        )
        service._ingest_line(PORT, self.line())
        without_history = outbox.events[0].irrigation_suggestion.volume_ml

        service.record_irrigation(
            self.NODE,
            50.0,
            source=SOURCE_CLOUD_COMMAND,
            command_id="cmd-1",
            at_epoch=time.time() - 3600.0,
        )
        service._ingest_line(PORT, self.line())
        with_history = outbox.events[1].irrigation_suggestion.volume_ml

        self.assertLess(with_history, without_history)


class IrrigationDecisionWiringTests(IngestIrrigationTestCase):
    """The "should we water now" half, which had no caller at all.

    ``IrrigationDecider`` and the forest were implemented and tested and then
    invoked from nowhere outside their own package, because the envelope needs an
    irrigation history and the service had none. These tests are what proves the
    path is live.
    """

    def decide(self, **kwargs):
        service, _ = self.build(**kwargs)
        return service

    def test_a_veto_short_circuits_before_the_model_is_consulted(self):
        """`D17` in its observable form: wet soil, and the forest is never asked.

        If the order were reversed the model could widen what the deterministic
        rules allow, and no assertion about the verdict alone would notice.
        """

        model = _StubModel(irrigate=True)
        service = self.decide(
            volumes={"node-1": 3000}, crops={"node-1": "lettuce"}, model=model
        )
        service._ingest_line(PORT, self.line(moisture=70.0))

        decision = service.latest_decision(self.NODE)
        self.assertEqual(decision.verdict, Verdict.SOIL_NOT_DRY)
        self.assertFalse(decision.irrigate)
        self.assertFalse(decision.envelope_allows)
        self.assertEqual(model.calls, 0)

    def test_the_forest_decides_only_inside_the_envelope(self):
        model = _StubModel(irrigate=True)
        service = self.decide(
            volumes={"node-1": 3000}, crops={"node-1": "lettuce"}, model=model
        )
        service._ingest_line(PORT, self.line(moisture=32.0))

        decision = service.latest_decision(self.NODE)
        self.assertEqual(model.calls, 1)
        self.assertTrue(decision.envelope_allows)
        self.assertTrue(decision.irrigate)
        self.assertEqual(decision.verdict, Verdict.IRRIGATE)
        # The dose came from the water balance, not from the 30 mL fallback, and
        # 177 mL is under the server's per-dose ceiling so it passes through
        # untouched.
        self.assertEqual(decision.volume_source, VolumeSource.COMPUTED)
        self.assertEqual(
            decision.volume_ml,
            float(self.last_suggestion(service).volume_ml),
        )

    def last_suggestion(self, service):
        return service.outbox.events[-1].irrigation_suggestion

    def test_the_gate_weighs_a_dose_the_server_could_actually_grant(self):
        """A bone-dry 3 L pot asks for 390 mL; the Governor would grant 200.

        The suggestion still leaves for the backend unclamped — that is how a
        formula disagreeing with the system's limits stays visible — but weighing
        390 mL against a 320 mL budget would refuse an ordinary dry pot with
        DOSE_EXCEEDS_DAILY_BUDGET, a verdict that means "misconfigured".
        """

        model = _StubModel(irrigate=True)
        service = self.decide(
            volumes={"node-1": 3000}, crops={"node-1": "lettuce"}, model=model
        )
        service._ingest_line(PORT, self.line(moisture=12.0))

        self.assertEqual(self.last_suggestion(service).volume_ml, 390)
        decision = service.latest_decision(self.NODE)
        self.assertTrue(decision.irrigate)
        self.assertEqual(decision.volume_ml, SERVER_DOSE_MAX_ML)
        self.assertEqual(decision.volume_source, VolumeSource.COMPUTED)

    def test_the_model_can_withhold_inside_the_envelope(self):
        model = _StubModel(irrigate=False)
        service = self.decide(
            volumes={"node-1": 3000}, crops={"node-1": "lettuce"}, model=model
        )
        service._ingest_line(PORT, self.line(moisture=32.0))

        decision = service.latest_decision(self.NODE)
        self.assertEqual(model.calls, 1)
        self.assertTrue(decision.envelope_allows)
        self.assertFalse(decision.irrigate)
        self.assertEqual(decision.verdict, Verdict.MODEL_WITHHELD)

    def test_a_missing_artifact_fails_towards_not_watering(self):
        """A gateway with no model must refuse, not fall through to the rules.

        The envelope alone says "allowed", and taking that as permission would
        make a deployment that failed to ship its artifact water on rules that
        were only ever meant to bound a model.
        """

        service = self.decide(
            volumes={"node-1": 3000}, crops={"node-1": "lettuce"}, model=None
        )
        service._ingest_line(PORT, self.line(moisture=12.0))

        decision = service.latest_decision(self.NODE)
        self.assertEqual(decision.verdict, Verdict.MODEL_UNAVAILABLE)
        self.assertFalse(decision.irrigate)
        self.assertTrue(decision.envelope_allows)

    def test_the_interval_gate_reads_real_history(self):
        """The gate that could not exist: hours_since_last_irrigation was None."""

        model = _StubModel(irrigate=True)
        service = self.decide(
            volumes={"node-1": 3000}, crops={"node-1": "lettuce"}, model=model
        )
        service.record_irrigation(
            self.NODE,
            60.0,
            source=SOURCE_CLOUD_COMMAND,
            command_id="cmd-1",
            at_epoch=time.time() - 3600.0,
        )
        service._ingest_line(PORT, self.line(moisture=12.0))

        decision = service.latest_decision(self.NODE)
        self.assertEqual(decision.verdict, Verdict.COOLDOWN_ACTIVE)
        self.assertEqual(model.calls, 0)

    def test_the_budget_gate_reads_real_history(self):
        """A 3 L pot's budget is 320 mL, and 130 already went out today.

        Dated seven hours back so the interval gate is clear and the refusal can
        only be the budget. This is the gate that had never once fired: at the old
        fixed dose it was arithmetically identical to the interval.
        """

        model = _StubModel(irrigate=True)
        service = self.decide(
            volumes={"node-1": 3000}, crops={"node-1": "lettuce"}, model=model
        )
        service.record_irrigation(
            self.NODE,
            130.0,
            source=SOURCE_CLOUD_COMMAND,
            command_id="cmd-0",
            at_epoch=time.time() - 7.0 * 3600.0,
        )
        service._ingest_line(PORT, self.line(moisture=12.0))

        decision = service.latest_decision(self.NODE)
        self.assertEqual(decision.verdict, Verdict.DAILY_BUDGET_EXHAUSTED)
        self.assertEqual(model.calls, 0)
        self.assertEqual(decision.volume_ml, 0.0)

    def test_an_unconfigured_pot_still_gets_a_decision_on_the_fallback_dose(self):
        """No pot volume means no computed dose, not no decision.

        The suggestion is omitted so the backend can use its own fallback table,
        while the decider falls back to 30 mL — a plant dying of an unset
        TB_POT_SUBSTRATE_ML is the worse failure.
        """

        model = _StubModel(irrigate=True)
        service = self.decide(crops={"node-1": "lettuce"}, model=model)
        service._ingest_line(PORT, self.line(moisture=12.0))

        self.assertIsNone(service.outbox.events[0].irrigation_suggestion)
        decision = service.latest_decision(self.NODE)
        self.assertTrue(decision.irrigate)
        self.assertEqual(decision.volume_ml, FIXED_VOLUME_ML)
        self.assertEqual(decision.volume_source, VolumeSource.FALLBACK)

    def test_a_reading_without_soil_moisture_gets_no_decision(self):
        """Nothing to size and nothing to judge, so the log is not even opened."""

        model = _StubModel(irrigate=True)
        service = self.decide(
            volumes={"node-1": 3000}, crops={"node-1": "lettuce"}, model=model
        )
        line = self.line()
        message = json.loads(line)
        del message["soil_moisture_pct"]
        service._ingest_line(PORT, (json.dumps(message) + "\n").encode())

        self.assertIsNone(service.latest_decision(self.NODE))
        self.assertEqual(model.calls, 0)
        # The reading itself still reaches the outbox: no soil probe is a
        # legitimate configuration, not a fault.
        self.assertEqual(len(service.outbox.events), 1)

    def test_a_decision_is_never_written_to_the_history(self):
        """Only a delivery is history. An IRRIGATE verdict moved no water.

        Recording intent would err "safe" once — an over-counted budget withholds
        water — and then corrupt every later cycle, because a model told a
        bone-dry pot was watered an hour ago keeps withholding.
        """

        service = self.decide(
            volumes={"node-1": 3000},
            crops={"node-1": "lettuce"},
            model=_StubModel(irrigate=True),
        )
        service._ingest_line(PORT, self.line(moisture=12.0))

        self.assertTrue(service.latest_decision(self.NODE).irrigate)
        self.assertIsNone(service.history.hours_since_last_irrigation(self.NODE))
        self.assertEqual(service.history.dispensed_today_ml(self.NODE), 0.0)

    def test_an_unreadable_history_refuses_to_decide(self):
        """An empty log and an unreadable one must not look alike.

        An empty log passes the interval gate and contributes nothing to the
        budget, so treating a SQLite failure as "no records" would open both gates
        at once. The reading still reaches the outbox.
        """

        model = _StubModel(irrigate=True)
        service = self.decide(
            volumes={"node-1": 3000}, crops={"node-1": "lettuce"}, model=model
        )
        service.history.path = Path(self._tmp.name) / "missing" / "outbox.sqlite3"
        service._ingest_line(PORT, self.line(moisture=12.0))

        self.assertIsNone(service.latest_decision(self.NODE))
        self.assertEqual(model.calls, 0)
        self.assertEqual(len(service.outbox.events), 1)

    def test_a_pot_that_needs_nothing_is_not_watered_on_the_fallback(self):
        """suggest_volume_ml returns 0 for "needs nothing".

        Injecting None instead — the "cannot size it" signal — would fall back to
        30 mL and water a pot the formula had just declared wet enough. A cold dark
        humid night over an already-wet onion pot: soil at 40% is above the crop's
        33% target and below the 45% dry gate, so the envelope would otherwise
        allow it.
        """

        model = _StubModel(irrigate=True)
        service = self.decide(
            volumes={"node-1": 1000}, crops={"node-1": "welsh_onion"}, model=model
        )
        service._ingest_line(
            PORT,
            self.line(
                moisture=40.0,
                air_temperature_c=5.0,
                relative_humidity_pct=98.0,
                ppfd_umol_m2_s=0.0,
                soil_temperature_c=5.0,
            ),
        )

        self.assertEqual(service.outbox.events[0].irrigation_suggestion.volume_ml, 0)
        self.assertIsNone(service.latest_decision(self.NODE))
        self.assertEqual(model.calls, 0)


class RecordingOutbox:
    def __init__(self):
        self.events = []

    def enqueue(self, event):
        self.events.append(event)
        return True


if __name__ == "__main__":
    unittest.main()
