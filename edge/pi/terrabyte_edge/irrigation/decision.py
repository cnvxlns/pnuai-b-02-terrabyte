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
import math

from .features import IrrigationFeatures
from .forest import RandomForestClassifier, RandomForestVote


FIXED_VOLUME_ML = 30.0

# The cloud Governor's own daily ceiling per pot
# (``IrrigationProperties.dailyBudgetMl``). The edge budget is capped here rather
# than allowed to exceed it: the edge stands in for the server's authority and
# must never permit more water than the server would have granted (`D16`/`D17`).
SERVER_DAILY_BUDGET_ML = 600.0

# The largest single dose the system can deliver: the Governor clamps every grant
# to it (``IrrigationProperties.doseMaxMl``, gate 7 — "not a refusal: the request
# is honoured, smaller"). It bounds a dose in two ways here.
#
# It is the floor of the derived budget, because a daily budget below one
# deliverable dose is not a budget, it is a ban — the pot can never be watered
# and the refusal reads as "budget". And it is the ceiling on the dose weighed
# against that budget, because the water balance is not clamped and asks for
# 390 mL for a bone-dry 3 L pot: a number the server would never grant, and
# weighing it would refuse an ordinary dry pot with a misconfiguration verdict.
SERVER_DOSE_MAX_ML = 200.0

# The server's per-pot fallback dose, by substrate volume
# (docs/design/irrigation_volume.md §3.2). ``(upper bound in mL, dose in mL)``,
# first match wins; anything larger gets :data:`LARGE_POT_DOSE_ML`.
#
# Borrowed rather than reinvented because it is the only *documented* answer to
# "how much water does a pot this size want in one go", and a second table would
# drift from it. It is a reference dose, not the dose: the water balance sizes
# the actual one from the reading. This table only sets the scale of a day.
REFERENCE_DOSE_ML: tuple[tuple[float, float], ...] = (
    (1000.0, 40.0),
    (3000.0, 80.0),
    (6000.0, 120.0),
)
LARGE_POT_DOSE_ML = 160.0


def reference_dose_ml(substrate_volume_ml: float | None) -> float:
    """The documented dose for a pot this size, in mL.

    ``None`` — no configured pot volume — returns :data:`FIXED_VOLUME_ML`, which
    is what the decider will actually deliver in that case: with no volume the
    water balance cannot size a dose and the fallback stands in. Deriving the
    budget from the same number keeps the two consistent, and keeps the
    unconfigured deployment at exactly the budget it has today.
    """

    if substrate_volume_ml is None:
        return FIXED_VOLUME_ML
    volume = float(substrate_volume_ml)
    if volume <= 0.0:
        return FIXED_VOLUME_ML
    for upper_bound, dose in REFERENCE_DOSE_ML:
        if volume <= upper_bound:
            return dose
    return LARGE_POT_DOSE_ML


def daily_budget_ml_for(
    substrate_volume_ml: float | None, *, min_interval_hours: float
) -> float:
    """Derive a daily budget from pot volume, in mL.

    ``reference dose x doses the interval gate already allows``, floored at one
    deliverable dose and capped at the server's own daily budget:

        min(max(reference_dose x doses_per_day, 200), 600)

    Every number in it comes from somewhere. The *rule* is the one that produced
    the old constant: 120 mL was 30 mL times the four doses a six-hour interval
    permits in a day. Only its dose input was wrong — it assumed a fixed dose that
    no longer exists, so the budget could only ever restate the interval and never
    fired. Substituting the documented per-pot dose (§3.2's fallback table) keeps
    the rule and fixes the input, which is a smaller and more defensible change
    than inventing a new ceiling.

    Both bounds are the server's own constants, and each answers a failure the
    middle term has on its own:

    * **Floor** (:data:`SERVER_DOSE_MAX_ML`). A 1 L pot derives 160 mL, and its
      own water balance asks for 159 mL at 12% moisture — one bad reading away
      from a budget that cannot buy a single dose. A budget below one deliverable
      dose bans watering rather than bounding it.
    * **Cap** (:data:`SERVER_DAILY_BUDGET_ML`). Nothing here may raise the edge
      above what the cloud would have granted (`D16`/`D17`), so a very large pot
      lands on the server's number and not on a bigger one derived locally.

    What the derivation buys: the gate now measures *volume*. A pot whose readings
    ask for 200 mL gets one dose a day where a pot asking for 60 mL gets several,
    and neither is decided by counting. That is the property the old constant
    could not have, because it was arithmetically identical to the interval.

    Note what it costs: :attr:`Verdict.DOSE_EXCEEDS_DAILY_BUDGET` becomes
    unreachable for any budget derived here, since the floor is the same ceiling
    the dose is clamped to. That is the intent — it is a misconfiguration verdict,
    and a derived budget is not a misconfiguration. It stays reachable for a
    hand-built :class:`EnvelopeLimits`, which is where the mistake would be.
    """

    if min_interval_hours <= 0.0:
        # No interval limit means no doses-per-day to multiply by. Fall back to
        # the server ceiling rather than to something unbounded.
        return SERVER_DAILY_BUDGET_ML
    doses_per_day = max(1.0, math.floor(24.0 / min_interval_hours))
    derived = reference_dose_ml(substrate_volume_ml) * doses_per_day
    return min(max(derived, SERVER_DOSE_MAX_ML), SERVER_DAILY_BUDGET_ML)


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

    ``daily_budget_ml`` is **derived from pot volume**, not configured. Pass the
    pot to :meth:`supervised` and let :func:`daily_budget_ml_for` size it. The
    120 mL default below is not that derivation's answer for any pot: it is the
    narrowest envelope in the file, kept for a hand-built limit set.

    The history is worth keeping, because the default reads like a decision and
    was not one. At the fixed 30 mL this module was written for, 120 mL is exactly
    four doses — and ``min_interval_hours=6`` already caps the day at four. The
    budget therefore could not bind before the interval did: it restated the
    interval and never once fired. A computed dose breaks that coincidence, and
    the first pot to prove it was a 3 L lettuce planting whose real suggestion is
    about 200 mL — over the whole day's allowance on the first dose. Against 120
    a large pot would never be watered at all, and the log would read "budget
    exhausted" against a pot that had received nothing; that case has its own
    verdict (:attr:`Verdict.DOSE_EXCEEDS_DAILY_BUDGET`) so it cannot hide inside
    the ordinary one.

    ``min_interval_hours`` is deliberately not shorter than the backend cooldown,
    for the same reason the budget is capped at the server's: the edge can never
    out-pace the authority it is standing in for.
    """

    max_reading_age_seconds: float = 600.0
    dry_gate_pct: float = 45.0
    min_interval_hours: float = 6.0
    # Four fallback doses, which is what this was when the dose was fixed at
    # 30 mL. Left here rather than raised to the derived figure so a limit set
    # built by hand stays the narrowest one available, and so the historical
    # coincidence with min_interval_hours remains visible in the file that caused
    # it. Deployments get their budget from supervised(), not from this.
    daily_budget_ml: float = 120.0

    @classmethod
    def supervised(
        cls, *, substrate_volume_ml: float | None = None
    ) -> "EnvelopeLimits":
        """Normal operation, with the cloud Governor reachable.

        ``substrate_volume_ml`` is the pot this envelope guards
        (``Settings.substrate_volume_ml_for``). Omitting it is not a neutral
        choice — it produces the smallest budget on offer, sized for the fallback
        dose — but it is the honest one when the deployment has not said how big
        the pot is.
        """

        min_interval_hours = 6.0
        return cls(
            min_interval_hours=min_interval_hours,
            daily_budget_ml=daily_budget_ml_for(
                substrate_volume_ml, min_interval_hours=min_interval_hours
            ),
        )

    @classmethod
    def autonomous(cls) -> "EnvelopeLimits":
        """Cloud-down operation. Mirrors the `D16` emergency rule.

        Far narrower than :meth:`supervised`: only genuinely dry soil qualifies,
        and the daily budget assumes nobody is watching.

        **The 120 mL here is not the accident the supervised default was.** `D16`
        fixes the emergency dose at 60 mL and the interval at 12 hours, so 120 is
        two doses — a designed cap, hardcoded on purpose so no configuration can
        forge it (docs/design/edge_ai_hardening.md §개선 4). It deliberately does
        not scale with pot volume: deriving it would *widen* the emergency
        envelope exactly when nobody is watching, and the point of this profile is
        that it is narrower than supervised operation, not proportional to it.

        Which means this profile must be paired with the fixed 60 mL emergency
        dose, not with a water-balance dose. A computed dose is routinely larger
        than the whole 120 mL — a dry 3 L pot asks for about 390 mL — and would
        trip :attr:`Verdict.DOSE_EXCEEDS_DAILY_BUDGET`, refusing water in exactly
        the situation this profile exists for. Whoever builds the autonomy state
        machine owns that pairing.
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
