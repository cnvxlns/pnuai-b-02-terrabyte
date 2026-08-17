"""Feature vector consumed by the edge irrigation classifier."""

from __future__ import annotations

from dataclasses import dataclass
import math


# 2: tree leaves carry a single scalar plus a named aggregation rule, so forests
#    trained by scikit-learn and by XGBoost share one runtime format.
INPUT_SCHEMA_VERSION = 2

FEATURE_NAMES = (
    "soil_moisture_pct",
    "soil_temperature_c",
    "air_temperature_c",
    "relative_humidity_pct",
    "ppfd_umol_m2_s",
    "hours_since_last_irrigation",
)

# Public because the caller has to clamp to it. The irrigation history reports a
# true elapsed time and a pot left alone for two months legitimately exceeds any
# range the model was trained on; the feature vector must be clamped to what the
# trees have seen rather than raising, since "even longer than the longest gap in
# training" and "the longest gap in training" are the same decision.
MAX_HOURS_SINCE_LAST_IRRIGATION = 720.0

_RANGES = {
    "soil_moisture_pct": (0.0, 100.0),
    "soil_temperature_c": (-20.0, 70.0),
    "air_temperature_c": (-50.0, 80.0),
    "relative_humidity_pct": (0.0, 100.0),
    "ppfd_umol_m2_s": (0.0, 5000.0),
    "hours_since_last_irrigation": (0.0, MAX_HOURS_SINCE_LAST_IRRIGATION),
}


class FeatureError(ValueError):
    """Raised when a feature is missing, non-finite or outside its range."""


@dataclass(frozen=True)
class IrrigationFeatures:
    """One irrigation decision input.

    ``soil_sensor_valid`` mirrors the Arduino self-report. The classifier is
    never asked to compensate for a sensor it cannot trust; an invalid reading
    is rejected by the safety envelope before inference runs.
    """

    soil_moisture_pct: float
    soil_temperature_c: float
    air_temperature_c: float
    relative_humidity_pct: float
    ppfd_umol_m2_s: float
    hours_since_last_irrigation: float
    soil_sensor_valid: bool = True
    reading_age_seconds: float = 0.0

    def __post_init__(self) -> None:
        for name in FEATURE_NAMES:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise FeatureError(f"{name} must be numeric")
            minimum, maximum = _RANGES[name]
            if not math.isfinite(value) or not minimum <= value <= maximum:
                raise FeatureError(f"{name} is outside its canonical range")
        if self.reading_age_seconds < 0.0:
            raise FeatureError("reading_age_seconds must not be negative")

    def to_vector(self) -> list[float]:
        """Return features in the exact order the model was trained on."""

        return [float(getattr(self, name)) for name in FEATURE_NAMES]
