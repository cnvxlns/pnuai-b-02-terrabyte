"""Reference implementation of the water-balance volume formula.

This is NOT deployed and NOT imported by the edge agent. It exists for one
reason: `terrabyte_edge/irrigation/volume.py` is a hand-written scalar port
of this file, and a silent divergence during that port would misize every
dose with nothing to catch it. `tests/test_volume.py` feeds randomised
inputs to both and asserts they agree.

Keep the two in step. If you change the formula, change it here and let the
test tell you what the port missed.

The vectorised numpy form is retained because it is also what generated the
synthetic labels during the earlier ML experiment — see
docs/design/irrigation_volume.md for why that model was not deployed.
"""

from __future__ import annotations

import numpy as np

# Fraction of substrate volume that is water at field capacity. A peat/perlite
# potting mix holds roughly this much. Assumption, not a measurement (G4).
WATER_HOLDING_FRACTION = 0.45

# Share of poured water that reaches the root zone; the rest drains or runs off
# the surface. Assumption (G4).
IRRIGATION_EFFICIENCY = 0.85

# A single dose wets the root zone, not the whole pot. The root zone does not
# grow proportionally with pot volume, so a big pot gets a proportionally
# smaller share rewetted per dose and the rest of the deficit is corrected over
# later cycles. Without this the physics asks for 340 mL on a 12 L pot -- above
# the Governor's 200 mL per-dose clamp, so every large-pot prediction would be
# clamped or rejected and the model would be useless exactly where it is needed.
REFERENCE_VOLUME_ML = 2000.0
MIN_WETTED_FRACTION = 0.35

# How far ahead a single watering is expected to last. Matches the edge model's
# horizon so both sides agree on what "enough water" means.
HORIZON_HOURS = 12.0

# Only half the projected loss is pre-filled. Topping up the full horizon would
# leave the pot saturated right after watering, and over-watering is the
# expensive mistake.
LOOKAHEAD_SHARE = 0.5

# Hard ceiling on a generated label. The backend's own ceiling is 500 mL; a
# label above it would teach the model to emit values the backend always rejects.
MAX_LABEL_ML = 500.0

# Default target soil moisture, and per-crop deviations from it. Deviations are
# small because `crop_score_profile` carries no soil-moisture target to derive
# them from (G3) -- these are placeholders that must be revisited with real data.
DEFAULT_TARGET_MOISTURE_PCT = 35.0
CROP_TARGET_MOISTURE_PCT: dict[str, float] = {
    "cherry_tomato": 38.0,
    "lettuce": 40.0,
    "basil": 36.0,
    "peppermint": 40.0,
    "welsh_onion": 33.0,
    "arugula": 37.0,
    "wasabi": 45.0,  # semi-aquatic, wants the wettest substrate
    "coriander": 34.0,
    "unknown": DEFAULT_TARGET_MOISTURE_PCT,
}


def evapotranspiration_pct_per_hour(
    soil_moisture_pct: np.ndarray,
    soil_temperature_c: np.ndarray,
    air_temperature_c: np.ndarray,
    relative_humidity_pct: np.ndarray,
    ppfd_umol_m2_s: np.ndarray,
) -> np.ndarray:
    """Soil moisture loss rate in percentage points per hour.

    Ported verbatim from the edge trainer. Do not "improve" one copy alone.
    """

    vapour_deficit = np.clip(1.0 - relative_humidity_pct / 100.0, 0.05, 1.0)
    light = 0.30 + ppfd_umol_m2_s / 800.0
    warmth = np.clip(1.0 + 0.045 * (air_temperature_c - 20.0), 0.2, 3.0)
    # Root uptake peaks near 22 C and falls off in cold or hot soil.
    roots = np.clip(1.0 - np.abs(soil_temperature_c - 22.0) / 30.0, 0.2, 1.0)
    # A dry soil gives up water more slowly than a wet one.
    availability = np.clip(soil_moisture_pct / 40.0, 0.25, 1.2)
    return 3.5 * vapour_deficit * light * warmth * roots * availability


def wetted_fraction(substrate_volume_ml: np.ndarray) -> np.ndarray:
    """Share of the substrate a single dose rewets.

    1 at the reference pot size and falling as the square root of volume above
    it, floored so a very large pot still gets a meaningful dose.
    """

    return np.clip(
        np.sqrt(REFERENCE_VOLUME_ML / substrate_volume_ml), MIN_WETTED_FRACTION, 1.0
    )


def target_moisture_pct(crop_codes: np.ndarray) -> np.ndarray:
    """Per-crop target soil moisture, defaulting for unrecognised codes."""

    return np.array(
        [
            CROP_TARGET_MOISTURE_PCT.get(str(code), DEFAULT_TARGET_MOISTURE_PCT)
            for code in crop_codes
        ],
        dtype=np.float64,
    )


def redistribution_pct(hours_since_last_irrigation: np.ndarray) -> np.ndarray:
    """Moisture still spreading downward from a recent watering.

    Without this term the generator labels a pot watered ten minutes ago as
    needing water again, because the probe near the surface has already drained.
    Same decay constant as the edge model.
    """

    return 1.4 * np.exp(-hours_since_last_irrigation / 3.0)


def irrigation_volume_ml(
    soil_moisture_pct: np.ndarray,
    soil_temperature_c: np.ndarray,
    air_temperature_c: np.ndarray,
    relative_humidity_pct: np.ndarray,
    ppfd_umol_m2_s: np.ndarray,
    hours_since_last_irrigation: np.ndarray,
    substrate_volume_ml: np.ndarray,
    crop_codes: np.ndarray,
    noise: np.ndarray | None = None,
) -> np.ndarray:
    """Label: how much water this pot needs, in mL.

    Two parts. The deficit refills the substrate to its crop target; the
    lookahead adds part of what the next 12 hours will evaporate. Both are
    divided by the irrigation efficiency because some of the poured water never
    reaches the roots.

    ``noise`` is a multiplicative factor (mean 1). Pass ``None`` only when you
    want the noiseless formula -- training on it teaches the model to memorise a
    deterministic boundary and makes every metric meaningless.
    """

    capacity_ml = (
        substrate_volume_ml * WATER_HOLDING_FRACTION * wetted_fraction(substrate_volume_ml)
    )

    effective_moisture = soil_moisture_pct + redistribution_pct(
        hours_since_last_irrigation
    )
    deficit_pct = np.clip(target_moisture_pct(crop_codes) - effective_moisture, 0.0, None)
    deficit_ml = deficit_pct / 100.0 * capacity_ml

    loss_rate = evapotranspiration_pct_per_hour(
        soil_moisture_pct,
        soil_temperature_c,
        air_temperature_c,
        relative_humidity_pct,
        ppfd_umol_m2_s,
    )
    lookahead_ml = loss_rate * HORIZON_HOURS / 100.0 * capacity_ml * LOOKAHEAD_SHARE

    volume = (deficit_ml + lookahead_ml) / IRRIGATION_EFFICIENCY
    if noise is not None:
        volume = volume * noise
    return np.clip(volume, 0.0, MAX_LABEL_ML)
