"""Irrigation decision: random forest proposal under a deterministic envelope.

This module decides *whether* to irrigate now. It does not compute how much:
``irrigation/volume.py`` does that, and its answer is passed into
:meth:`IrrigationDecider.decide` per reading. :data:`FIXED_VOLUME_ML` remains as
the fallback for a reading the formula could not size — it needs soil moisture
and a configured pot volume, and either can be absent.

Injected at ``decide`` time rather than at construction because the volume
belongs to the reading, not to the decider: the decider is long-lived and the
dose changes every cycle. It also keeps the envelope honest — the daily-budget
rule has to weigh the dose that is actually about to be delivered, not a
placeholder.

Two layers, in this order:

1. **Safety envelope** — deterministic, auditable rules (sensor validity,
   reading freshness, wet-soil lockout, minimum interval, daily budget). Any
   veto here short-circuits and the model is never consulted.
2. **Random forest** — consulted only inside the envelope. Because the envelope
   is evaluated first and independently, the model can withhold irrigation but
   can never widen what the deterministic rules already allow (`D17`).

`D15`/`D17` in ``docs/todolist.md`` reserve the "whether" decision for the
deterministic layer and cast the model as suppression-only. Under that reading
the forest here is the *last* gate rather than the first, which is why
:meth:`IrrigationDecider.decide` reports the envelope verdict separately: a
caller can ignore ``vote`` entirely and still have a working rule-based path.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .features import IrrigationFeatures
from .forest import RandomForestClassifier, RandomForestVote


FIXED_VOLUME_ML = 30.0


class VolumeSource(str, Enum):
    """Where the dose in a decision came from.

    Recorded because "30 mL because that is the fallback" and "30 mL because the
    water balance asked for 30 mL" are different facts about a pot, and a
    deployment silently running on the fallback — an unset ``TB_POT_SUBSTRATE_ML``
    is enough — looks identical to a working one otherwise.
    """

    COMPUTED = "COMPUTED"
    FALLBACK = "FALLBACK"
    NONE = "NONE"


class Verdict(str, Enum):
    """Why a decision came out the way it did."""

    IRRIGATE = "IRRIGATE"
    MODEL_WITHHELD = "MODEL_WITHHELD"
    SENSOR_INVALID = "SENSOR_INVALID"
    INPUT_STALE = "INPUT_STALE"
    SOIL_NOT_DRY = "SOIL_NOT_DRY"
    COOLDOWN_ACTIVE = "COOLDOWN_ACTIVE"
    DAILY_BUDGET_EXHAUSTED = "DAILY_BUDGET_EXHAUSTED"
    # Distinct from DAILY_BUDGET_EXHAUSTED, which means "enough for today has
    # already gone out". This one means a *single* dose does not fit in the whole
    # day's allowance, so the pot can never be watered at all. It is a
    # misconfiguration, and without its own verdict it would report as an
    # ordinary budget veto against a pot that has received nothing.
    DOSE_EXCEEDS_DAILY_BUDGET = "DOSE_EXCEEDS_DAILY_BUDGET"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"


@dataclass(frozen=True)
class EnvelopeLimits:
    """Deterministic bounds. Defaults track the server-side Governor (P1-8).

    ``min_interval_hours`` is deliberately not shorter than the backend cooldown
    so the edge can never out-pace the authority it is standing in for.

    ``daily_budget_ml`` needs reading with care, because a variable dose changed
    what it means. At the fixed 30 mL it was written for, 120 mL is exactly four
    doses — and ``min_interval_hours=6`` already caps the day at four. The budget
    could therefore never bind before the interval did: it was a restatement of
    the interval, not an independent limit, and it never fired.

    A computed dose makes it bind first, and it binds on a number with no
    physical basis. The server's own per-pot fallback table asks for 120 mL for a
    3–6 L pot and 160 mL for anything larger
    (docs/design/irrigation_volume.md §3.2), and the server Governor's daily
    budget is 600 mL. So this 120 permits one dose a day for a mid-size pot and
    **zero for a large one** — the pot is never watered and the reason reads
    "budget exhausted" against a pot that has received nothing. That case now has
    its own verdict rather than hiding inside the ordinary one.

    Left at 120 here rather than quietly raised: the safe ceiling depends on pot
    volume, which this dataclass does not know (``Settings.pot_substrate_ml``
    does), and inventing a bigger constant would replace a limit that is wrong
    and detectable with one that is wrong and plausible. Whoever wires the
    decider is expected to pass a budget derived from the pot, and to treat
    DOSE_EXCEEDS_DAILY_BUDGET as a configuration error rather than a normal veto.
    """

    max_reading_age_seconds: float = 600.0
    dry_gate_pct: float = 45.0
    min_interval_hours: float = 6.0
    daily_budget_ml: float = 120.0

    @classmethod
    def supervised(cls) -> "EnvelopeLimits":
        """Normal operation, with the cloud Governor reachable."""

        return cls()

    @classmethod
    def autonomous(cls) -> "EnvelopeLimits":
        """Cloud-down operation. Mirrors the `D16` emergency rule.

        Far narrower than :meth:`supervised`: only genuinely dry soil qualifies,
        and the daily budget assumes nobody is watching.
        """

        return cls(
            max_reading_age_seconds=600.0,
            dry_gate_pct=15.0,
            min_interval_hours=12.0,
            daily_budget_ml=120.0,
        )

    def __post_init__(self) -> None:
        if self.max_reading_age_seconds <= 0.0:
            raise ValueError("max_reading_age_seconds must be positive")
        if not 0.0 < self.dry_gate_pct <= 100.0:
            raise ValueError("dry_gate_pct must be within (0, 100]")
        if self.min_interval_hours < 0.0:
            raise ValueError("min_interval_hours must not be negative")
        if self.daily_budget_ml < 0.0:
            raise ValueError("daily_budget_ml must not be negative")


@dataclass(frozen=True)
class IrrigationDecision:
    """Outcome of one decision cycle.

    ``volume_ml`` is 0.0 on every non-irrigating outcome, and ``volume_source``
    still says where the dose *would* have come from. A veto against a computed
    dose and a veto against the fallback are different diagnoses.
    """

    irrigate: bool
    volume_ml: float
    verdict: Verdict
    envelope_allows: bool
    vote: RandomForestVote | None = None
    volume_source: VolumeSource = VolumeSource.NONE

    @property
    def reason(self) -> str:
        return self.verdict.value


class IrrigationDecider:
    """Applies the safety envelope, then the forest, to produce a decision."""

    def __init__(
        self,
        model: RandomForestClassifier | None,
        *,
        limits: EnvelopeLimits | None = None,
        fallback_volume_ml: float = FIXED_VOLUME_ML,
    ) -> None:
        if fallback_volume_ml <= 0.0:
            raise ValueError("fallback_volume_ml must be positive")
        self._model = model
        self._limits = limits or EnvelopeLimits()
        self._fallback_volume_ml = fallback_volume_ml

    @property
    def limits(self) -> EnvelopeLimits:
        return self._limits

    @property
    def fallback_volume_ml(self) -> float:
        return self._fallback_volume_ml

    def _resolve_volume(self, volume_ml: float | None) -> tuple[float, VolumeSource]:
        """Pick the dose for this cycle and say where it came from.

        ``None`` — the formula could not size this reading — falls back rather
        than refusing. The alternative would be to stop watering whenever soil
        moisture or the pot volume is unavailable, and a plant dying of an unset
        environment variable is a worse failure than a dose that is merely
        conservative. The fallback is the smallest dose in play, so erring here
        errs dry.
        """

        if volume_ml is None:
            return self._fallback_volume_ml, VolumeSource.FALLBACK
        if volume_ml <= 0.0:
            # 0 is a real answer from the formula: the pot needs nothing. That is
            # not a dose, so there is nothing to authorise.
            raise ValueError("volume_ml must be positive; pass None to fall back")
        return float(volume_ml), VolumeSource.COMPUTED

    def _envelope_veto(
        self,
        features: IrrigationFeatures,
        dispensed_today_ml: float,
        volume_ml: float,
    ) -> Verdict | None:
        """Return the first deterministic veto, or ``None`` if all rules pass."""

        limits = self._limits
        if not features.soil_sensor_valid:
            return Verdict.SENSOR_INVALID
        if features.reading_age_seconds > limits.max_reading_age_seconds:
            return Verdict.INPUT_STALE
        if features.soil_moisture_pct >= limits.dry_gate_pct:
            return Verdict.SOIL_NOT_DRY
        if features.hours_since_last_irrigation < limits.min_interval_hours:
            return Verdict.COOLDOWN_ACTIVE
        # Checked before the running total, and separately, because the two are
        # different problems. One dose larger than the entire daily allowance
        # means this pot can never be watered — a configuration error that would
        # otherwise report as an ordinary budget veto against a pot that has had
        # nothing all day.
        if volume_ml > limits.daily_budget_ml:
            return Verdict.DOSE_EXCEEDS_DAILY_BUDGET
        if dispensed_today_ml + volume_ml > limits.daily_budget_ml:
            return Verdict.DAILY_BUDGET_EXHAUSTED
        return None

    def decide(
        self,
        features: IrrigationFeatures,
        *,
        volume_ml: float | None = None,
        dispensed_today_ml: float = 0.0,
    ) -> IrrigationDecision:
        """Decide whether to irrigate, and with how much.

        ``volume_ml`` is this reading's dose, normally from
        :func:`irrigation.volume.suggest_volume_ml`. ``None`` means the formula
        could not size it and :data:`FIXED_VOLUME_ML` stands in.

        ``dispensed_today_ml`` is the volume already delivered to this pot in the
        current 24-hour window, from every source.
        """

        if dispensed_today_ml < 0.0:
            raise ValueError("dispensed_today_ml must not be negative")

        dose, source = self._resolve_volume(volume_ml)

        veto = self._envelope_veto(features, dispensed_today_ml, dose)
        if veto is not None:
            return IrrigationDecision(
                irrigate=False,
                volume_ml=0.0,
                verdict=veto,
                envelope_allows=False,
                volume_source=source,
            )

        if self._model is None:
            return IrrigationDecision(
                irrigate=False,
                volume_ml=0.0,
                verdict=Verdict.MODEL_UNAVAILABLE,
                envelope_allows=True,
                volume_source=source,
            )

        vote = self._model.predict(features)
        if not vote.irrigate:
            return IrrigationDecision(
                irrigate=False,
                volume_ml=0.0,
                verdict=Verdict.MODEL_WITHHELD,
                envelope_allows=True,
                vote=vote,
                volume_source=source,
            )
        return IrrigationDecision(
            irrigate=True,
            volume_ml=dose,
            verdict=Verdict.IRRIGATE,
            envelope_allows=True,
            vote=vote,
            volume_source=source,
        )
