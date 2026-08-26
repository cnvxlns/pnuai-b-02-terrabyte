"""Emergency irrigation with the cloud unreachable."""

from pathlib import Path
import tempfile
import unittest

from terrabyte_edge.autonomy import AUTONOMOUS_VOLUME_ML, EdgeAutonomy, Reading
from terrabyte_edge.cloud_link import CloudLink, CloudLinkState
from terrabyte_edge.irrigation import EnvelopeLimits, IrrigationDecider, Verdict
from terrabyte_edge.irrigation_history import (
    SOURCE_CLOUD_COMMAND,
    SOURCE_EDGE_AUTONOMOUS,
    IrrigationHistory,
)


NOW = 1_800_000_000.0
HOUR = 3600.0
NODE = "node-1"

DRY = dict(
    soil_moisture_pct=11.0,
    soil_temperature_c=21.0,
    air_temperature_c=28.0,
    relative_humidity_pct=30.0,
    ppfd_umol_m2_s=600.0,
    soil_sensor_valid=True,
)


class StubLink:
    """Stands in for CloudLink so these tests are about the dose, not the clock."""

    def __init__(self, autonomous: bool = True) -> None:
        self.may_irrigate_autonomously = autonomous


class RecordingPump:
    """Captures dispense calls and reports the millilitres the test wants.

    Mirrors the real contract: what comes back is what the firmware said it
    delivered, which is not always what was asked for.
    """

    def __init__(self, succeed: bool = True) -> None:
        self.succeed = succeed
        self.calls: list[tuple[str, float]] = []

    def __call__(self, node_id: str, volume_ml: float) -> float:
        self.calls.append((node_id, volume_ml))
        return volume_ml if self.succeed else 0.0


class AutonomyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.now = NOW
        self.history = IrrigationHistory(
            Path(self._directory.name) / "edge.sqlite3", clock=lambda: self.now
        )
        self.history.initialize()
        self.link = StubLink()
        self.pump = RecordingPump()

    def build(self, **overrides) -> EdgeAutonomy:
        decider = IrrigationDecider(
            None,
            limits=EnvelopeLimits.autonomous(),
            volume_ml=AUTONOMOUS_VOLUME_ML,
            require_model=False,
        )
        return EdgeAutonomy(
            link=overrides.get("link", self.link),
            decider=overrides.get("decider", decider),
            history=self.history,
            dispense=overrides.get("dispense", self.pump),
            clock=lambda: self.now,
        )

    def observe(self, autonomy: EdgeAutonomy, **overrides) -> None:
        autonomy.observe(Reading(node_id=NODE, observed_at_epoch=self.now, **{**DRY, **overrides}))

    # -- the gate ----------------------------------------------------------

    def test_does_nothing_while_the_cloud_is_reachable(self) -> None:
        self.link.may_irrigate_autonomously = False
        autonomy = self.build()
        self.observe(autonomy)

        self.assertIsNone(autonomy.tick())
        self.assertEqual(self.pump.calls, [])

    def test_does_nothing_before_any_reading_arrives(self) -> None:
        autonomy = self.build()

        self.assertIsNone(autonomy.tick())
        self.assertEqual(self.pump.calls, [])

    # -- the emergency dose ------------------------------------------------

    def test_dry_soil_gets_exactly_the_emergency_dose(self) -> None:
        autonomy = self.build()
        self.observe(autonomy)

        outcome = autonomy.tick()

        self.assertIsNotNone(outcome)
        self.assertEqual(outcome.verdict, Verdict.IRRIGATE)
        self.assertEqual(self.pump.calls, [(NODE, AUTONOMOUS_VOLUME_ML)])

    def test_a_delivered_dose_is_recorded_as_edge_autonomous(self) -> None:
        autonomy = self.build()
        self.observe(autonomy)

        autonomy.tick()

        recent = self.history.recent(NODE, since_epoch=0.0, limit=5)
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0].source, SOURCE_EDGE_AUTONOMOUS)
        self.assertEqual(recent[0].volume_ml, AUTONOMOUS_VOLUME_ML)

    def test_wet_soil_is_left_alone(self) -> None:
        autonomy = self.build()
        self.observe(autonomy, soil_moisture_pct=30.0)

        outcome = autonomy.tick()

        # 30 % is dry by the cloud's 45 % rule and wet by the emergency 15 % one.
        # Autonomy deliberately runs the narrower rule.
        self.assertEqual(outcome.verdict, Verdict.SOIL_NOT_DRY)
        self.assertEqual(self.pump.calls, [])

    def test_an_invalid_soil_sensor_blocks_the_dose(self) -> None:
        autonomy = self.build()
        self.observe(autonomy, soil_sensor_valid=False)

        outcome = autonomy.tick()

        self.assertEqual(outcome.verdict, Verdict.SENSOR_INVALID)
        self.assertEqual(self.pump.calls, [])

    def test_a_stale_reading_blocks_the_dose(self) -> None:
        autonomy = self.build()
        self.observe(autonomy)

        self.now += 1_200.0

        outcome = autonomy.tick()

        # Twenty minutes old. Watering on it is watering on what the pot looked
        # like before the last dose could have changed anything.
        self.assertEqual(outcome.verdict, Verdict.INPUT_STALE)
        self.assertEqual(self.pump.calls, [])

    # -- the envelope, from real history -----------------------------------

    def test_a_second_dose_inside_twelve_hours_is_refused(self) -> None:
        autonomy = self.build()
        self.observe(autonomy)
        autonomy.tick()

        self.now += 11 * HOUR
        self.observe(autonomy)
        outcome = autonomy.tick()

        self.assertEqual(outcome.verdict, Verdict.COOLDOWN_ACTIVE)
        self.assertEqual(len(self.pump.calls), 1)

    def test_the_next_dose_is_allowed_after_twelve_hours(self) -> None:
        autonomy = self.build()
        self.observe(autonomy)
        autonomy.tick()

        self.now += 13 * HOUR
        self.observe(autonomy)
        outcome = autonomy.tick()

        self.assertEqual(outcome.verdict, Verdict.IRRIGATE)
        self.assertEqual(len(self.pump.calls), 2)

    def test_water_the_cloud_delivered_still_counts_against_the_budget(self) -> None:
        # The case the daily ceiling actually exists for. Two autonomous doses
        # cannot reach it on their own — the twelve-hour interval spaces them
        # past the twenty-four hour window — so the ceiling only ever binds when
        # something else put water in first. A generous cloud dose just before
        # the backend died is exactly that.
        self.history.record(
            node_id=NODE,
            volume_ml=100.0,
            source=SOURCE_CLOUD_COMMAND,
            at_epoch=self.now - 13 * HOUR,
        )
        autonomy = self.build()
        self.observe(autonomy)

        outcome = autonomy.tick()

        self.assertEqual(outcome.verdict, Verdict.DAILY_BUDGET_EXHAUSTED)
        self.assertEqual(self.pump.calls, [])

    # -- delivery, not intent ----------------------------------------------

    def test_a_failed_dispense_writes_no_history(self) -> None:
        autonomy = self.build(dispense=RecordingPump(succeed=False))
        self.observe(autonomy)

        outcome = autonomy.tick()

        self.assertFalse(outcome.dispensed)
        # Recording it would push hours_since_last_irrigation out on the
        # strength of water that never moved, and the next tick would withhold.
        self.assertEqual(self.history.recent(NODE, since_epoch=0.0, limit=5), [])

    # -- the real link -----------------------------------------------------

    def test_a_safe_hold_stops_autonomy_even_when_the_cloud_is_gone(self) -> None:
        link = CloudLink(clock=lambda: self.now)
        link.hold("clock unsynced")
        autonomy = self.build(link=link)
        self.observe(autonomy)

        self.assertIsNone(autonomy.tick())
        self.assertEqual(link.evaluate(), CloudLinkState.SAFE_HOLD)
        self.assertEqual(self.pump.calls, [])


class PartialDeliveryTests(AutonomyTests):
    """A dose the firmware cut short is still water, but not the whole dose."""

    def test_a_clamped_dose_is_recorded_at_what_actually_ran(self) -> None:
        # G1 stops a run at the firmware's absolute limit, so a 60 mL request
        # can end after 24 mL. Recording 60 would charge the daily budget for
        # water that never left the reservoir and delay the next dose.
        autonomy = self.build(dispense=lambda node_id, volume_ml: 24.0)
        self.observe(autonomy)

        outcome = autonomy.tick()

        self.assertTrue(outcome.dispensed)
        self.assertEqual(outcome.volume_ml, 24.0)
        recent = self.history.recent(NODE, since_epoch=0.0, limit=5)
        self.assertEqual(recent[0].volume_ml, 24.0)

    def test_a_dose_that_delivered_nothing_writes_no_history(self) -> None:
        autonomy = self.build(dispense=lambda node_id, volume_ml: 0.0)
        self.observe(autonomy)

        outcome = autonomy.tick()

        self.assertFalse(outcome.dispensed)
        self.assertEqual(self.history.recent(NODE, since_epoch=0.0, limit=5), [])
