"""Irrigation decision: random forest proposal under a deterministic envelope.

Volume is fixed at :data:`FIXED_VOLUME_ML`. This module decides *whether* to
irrigate now, not how much.

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


class Verdict(str, Enum):
    """Why a decision came out the way it did."""

    IRRIGATE = "IRRIGATE"
    MODEL_WITHHELD = "MODEL_WITHHELD"
    SENSOR_INVALID = "SENSOR_INVALID"
    INPUT_STALE = "INPUT_STALE"
    SOIL_NOT_DRY = "SOIL_NOT_DRY"
    COOLDOWN_ACTIVE = "COOLDOWN_ACTIVE"
    DAILY_BUDGET_EXHAUSTED = "DAILY_BUDGET_EXHAUSTED"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"


@dataclass(frozen=True)
class EnvelopeLimits:
    """Deterministic bounds. Defaults track the server-side Governor (P1-8).

    ``min_interval_hours`` is deliberately not shorter than the backend cooldown
    so the edge can never out-pace the authority it is standing in for.
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
    """Outcome of one decision cycle."""

    irrigate: bool
    volume_ml: float
    verdict: Verdict
    envelope_allows: bool
    vote: RandomForestVote | None = None

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
        volume_ml: float = FIXED_VOLUME_ML,
        require_model: bool = True,
    ) -> None:
        if volume_ml <= 0.0:
            raise ValueError("volume_ml must be positive")
        self._model = model
        self._limits = limits or EnvelopeLimits()
        self._volume_ml = volume_ml
        self._require_model = require_model

    @property
    def limits(self) -> EnvelopeLimits:
        return self._limits

    def _envelope_veto(
        self, features: IrrigationFeatures, dispensed_today_ml: float
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
        if dispensed_today_ml + self._volume_ml > limits.daily_budget_ml:
            return Verdict.DAILY_BUDGET_EXHAUSTED
        return None

    def decide(
        self,
        features: IrrigationFeatures,
        *,
        dispensed_today_ml: float = 0.0,
    ) -> IrrigationDecision:
        """Decide whether to irrigate ``volume_ml`` right now.

        ``dispensed_today_ml`` is the volume already delivered to this pot in the
        current 24-hour window, from every source.
        """

        if dispensed_today_ml < 0.0:
            raise ValueError("dispensed_today_ml must not be negative")

        veto = self._envelope_veto(features, dispensed_today_ml)
        if veto is not None:
            return IrrigationDecision(
                irrigate=False,
                volume_ml=0.0,
                verdict=veto,
                envelope_allows=False,
            )

        if self._model is None:
            if self._require_model:
                return IrrigationDecision(
                    irrigate=False,
                    volume_ml=0.0,
                    verdict=Verdict.MODEL_UNAVAILABLE,
                    envelope_allows=True,
                )
            # Autonomous operation. The forest is a suppressor, never a trigger,
            # so its absence removes a veto rather than a reason — the envelope
            # above has already established that this pot is genuinely dry, past
            # its interval and inside its budget. Withholding here would make a
            # missing model file look identical to a healthy plant, and the
            # cloud that would otherwise notice is by definition unreachable.
            return IrrigationDecision(
                irrigate=True,
                volume_ml=self._volume_ml,
                verdict=Verdict.IRRIGATE,
                envelope_allows=True,
            )

        vote = self._model.predict(features)
        if not vote.irrigate:
            return IrrigationDecision(
                irrigate=False,
                volume_ml=0.0,
                verdict=Verdict.MODEL_WITHHELD,
                envelope_allows=True,
                vote=vote,
            )
        return IrrigationDecision(
            irrigate=True,
            volume_ml=self._volume_ml,
            verdict=Verdict.IRRIGATE,
            envelope_allows=True,
            vote=vote,
        )
