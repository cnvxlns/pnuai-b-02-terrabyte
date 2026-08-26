"""The only path that moves water with nobody supervising.

Reached when :mod:`cloud_link` has watched the backend go quiet for fifteen
minutes. Everything here is deliberately narrower than what the cloud does,
because the cloud's answer is checked by a Governor that holds the real budget
and this one is checked by nothing:

===================  =========================  =========================
                     Cloud                      Edge autonomous
===================  =========================  =========================
Trigger threshold    crop optimum (e.g. 35 %)   fixed 15 %
Dose                 20–200 mL, AI-sized        fixed 60 mL
Minimum interval     6 h                        12 h
Daily ceiling        600 mL                     120 mL
Basis                rules + model              deterministic thresholds
===================  =========================  =========================

**This is not a second copy of the cloud's rules.** Two rule sets maintained in
parallel drift, and the drift is discovered by a dead plant. The envelope here
is a handful of fixed numbers that exist to keep something alive until the cloud
comes back — not to grow it well.

**The forest suppresses; it cannot trigger.** ``IrrigationDecider`` runs the
deterministic envelope first and only then asks the model, so a model that says
"water" over soil at 30 % changes nothing. Built with ``require_model=False``,
so a missing or schema-mismatched artifact removes a veto rather than becoming
an unexplained refusal — see :mod:`terrabyte_edge.irrigation.decision`.

**Delivery is recorded, intent is not.** ``dispense`` returns the millilitres
the pump actually reported, and that is what lands in ``irrigation_history`` —
not the dose that was asked for. The temptation runs the other way, since an
over-counted budget only ever withholds water, but ``hours_since_last_irrigation``
is read back on the next tick: a fabricated entry withholds from a pot that never
got anything, and an inflated one does it for twelve hours.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import time
from typing import Callable

from .irrigation import IrrigationDecider, IrrigationFeatures, Verdict
from .irrigation.features import FeatureError
from .irrigation_history import SOURCE_EDGE_AUTONOMOUS, IrrigationHistory


LOGGER = logging.getLogger(__name__)

# One emergency dose. Fixed rather than sized, because sizing is the AI's job
# and the AI is on the far side of the link that just went down.
AUTONOMOUS_VOLUME_ML = 60.0


@dataclass(frozen=True)
class Reading:
    """The last thing a node said about itself.

    A flattened telemetry sample rather than the ``TelemetryEvent`` itself, so
    this module has no opinion about the wire format and can be exercised
    without a serial port.
    """

    node_id: str
    observed_at_epoch: float
    soil_moisture_pct: float
    soil_temperature_c: float
    air_temperature_c: float
    relative_humidity_pct: float
    ppfd_umol_m2_s: float
    soil_sensor_valid: bool = True


@dataclass(frozen=True)
class AutonomyOutcome:
    """What one tick decided, for the log and for the status page."""

    node_id: str
    verdict: Verdict
    dispensed: bool
    volume_ml: float
    inference_seconds: float


class EdgeAutonomy:
    """Decides and delivers the emergency dose, one node at a time.

    Holds no thread. ``tick`` is called by whatever timer the service owns,
    which keeps the whole decision path testable against a fake clock.
    """

    def __init__(
        self,
        *,
        link,
        decider: IrrigationDecider,
        history: IrrigationHistory,
        dispense: Callable[[str, float], float],
        clock: Callable[[], float] = time.time,
        volume_ml: float = AUTONOMOUS_VOLUME_ML,
    ) -> None:
        self._link = link
        self._decider = decider
        self._history = history
        self._dispense = dispense
        self._clock = clock
        self._volume_ml = volume_ml
        self._readings: dict[str, Reading] = {}

    def observe(self, reading: Reading) -> None:
        """Remember the newest sample for a node.

        Newest only. A queue would let a backlog of stale samples decide, and
        the envelope's freshness gate is about *now*, not about the last time
        anything was heard.
        """

        self._readings[reading.node_id] = reading

    def tick(self) -> AutonomyOutcome | None:
        """Consider every known node. ``None`` means autonomy is not in play.

        Returns the first outcome that actually dispensed, or the last verdict
        considered, so a caller has something to log either way.
        """

        if not self._link.may_irrigate_autonomously:
            return None

        outcome: AutonomyOutcome | None = None
        for node_id in sorted(self._readings):
            outcome = self._consider(node_id)
            if outcome is not None and outcome.dispensed:
                # One dose per tick. Two pots going dry at once is a real case,
                # but serialising them across ticks keeps only one pump command
                # in flight, which is what the firmware interlock assumes.
                return outcome
        return outcome

    def _consider(self, node_id: str) -> AutonomyOutcome | None:
        reading = self._readings[node_id]
        now = self._clock()

        hours_since = self._history.hours_since_last_irrigation(node_id)
        try:
            features = IrrigationFeatures(
                soil_moisture_pct=reading.soil_moisture_pct,
                soil_temperature_c=reading.soil_temperature_c,
                air_temperature_c=reading.air_temperature_c,
                relative_humidity_pct=reading.relative_humidity_pct,
                ppfd_umol_m2_s=reading.ppfd_umol_m2_s,
                # Never watered is not "watered zero hours ago". The cooldown
                # gate has to read it as "long enough", or a pot that has never
                # been watered can never be watered.
                hours_since_last_irrigation=(
                    self._decider.limits.min_interval_hours
                    if hours_since is None
                    else hours_since
                ),
                soil_sensor_valid=reading.soil_sensor_valid,
                reading_age_seconds=max(0.0, now - reading.observed_at_epoch),
            )
        except FeatureError as error:
            # A reading outside the canonical ranges is a broken sensor, not a
            # thirsty plant. SENSOR_INVALID is the honest verdict.
            LOGGER.warning("autonomy rejected a reading node_id=%s: %s", node_id, error)
            return AutonomyOutcome(
                node_id=node_id,
                verdict=Verdict.SENSOR_INVALID,
                dispensed=False,
                volume_ml=0.0,
                inference_seconds=0.0,
            )

        started = time.monotonic()
        decision = self._decider.decide(
            features, dispensed_today_ml=self._history.dispensed_today_ml(node_id)
        )
        # The design asks for edge inference timing, and this is the only place
        # it can be measured honestly: it covers the envelope and the forest
        # together, which is what a tick actually costs on the board.
        inference_seconds = time.monotonic() - started

        if not decision.irrigate:
            LOGGER.debug(
                "autonomy withheld node_id=%s verdict=%s in %.3fs",
                node_id, decision.verdict.value, inference_seconds,
            )
            return AutonomyOutcome(
                node_id=node_id,
                verdict=decision.verdict,
                dispensed=False,
                volume_ml=0.0,
                inference_seconds=inference_seconds,
            )

        # What the pump reports back, not what was asked for. G1 stops a run at
        # the firmware's absolute limit, so a 60 mL request can end after 24 mL,
        # and recording the request would charge the budget for water that never
        # left the reservoir and push the next dose out by twelve hours.
        delivered_ml = float(self._dispense(node_id, self._volume_ml) or 0.0)
        if delivered_ml <= 0.0:
            LOGGER.error(
                "autonomous irrigation delivered nothing node_id=%s requested_ml=%.1f",
                node_id, self._volume_ml,
            )
            return AutonomyOutcome(
                node_id=node_id,
                verdict=decision.verdict,
                dispensed=False,
                volume_ml=0.0,
                inference_seconds=inference_seconds,
            )

        self._history.record(
            node_id=node_id,
            volume_ml=delivered_ml,
            source=SOURCE_EDGE_AUTONOMOUS,
            at_epoch=now,
        )
        LOGGER.warning(
            "autonomous irrigation node_id=%s requested_ml=%.1f delivered_ml=%.1f "
            "decided in %.3fs",
            node_id, self._volume_ml, delivered_ml, inference_seconds,
        )
        return AutonomyOutcome(
            node_id=node_id,
            verdict=decision.verdict,
            dispensed=True,
            volume_ml=delivered_ml,
            inference_seconds=inference_seconds,
        )
