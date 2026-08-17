"""Water-balance irrigation volume, computed on the edge.

This is the deployed implementation. It is a hand-written scalar port of the
vectorised ``edge/pi/tools/reference_water_balance.py``, which is not deployed
and exists only so ``tests/test_volume.py`` can prove the two still agree --
change the formula in both, and let that test tell you what the port missed.

A regression model used to produce this number over HTTP. It was trained on
labels this same formula generated, so it could only ever recover this formula
-- less accurately, less auditably and in 557 KB rather than forty lines. See
``docs/design/irrigation_volume.md`` D27.

Scalar and stdlib-only on purpose: the Orange Pi runtime keeps its single
``pyserial`` dependency, and no container or network round-trip stands between
a reading and the number derived from it (D28).

**The returned mL is an estimate, not a measurement.** There is no flow meter.
The pump is a plain DC motor and volume is realised as
``mL = flow_ml_per_s x runtime_ms`` from a single uncalibrated constant (8.0),
so real delivery error is plausibly +-20~40%. That, not this formula's
arithmetic, sets the precision ceiling on every number here. Do not read the
integer as though the units digit means anything (G1, §0).
"""

from __future__ import annotations

import math


# Bumped whenever the arithmetic below changes, so the backend can tell which
# assumptions produced a stored suggestion.
MODEL_VERSION = "water-balance-v1"

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
# the Governor's 200 mL per-dose clamp, so every large-pot suggestion would be
# clamped or rejected and the computation would be useless exactly where it is
# needed.
REFERENCE_VOLUME_ML = 2000.0
MIN_WETTED_FRACTION = 0.35

# How far ahead a single watering is expected to last. Matches the edge
# suppressor's horizon so both sides agree on what "enough water" means.
HORIZON_HOURS = 12.0

# Only half the projected loss is pre-filled. Topping up the full horizon would
# leave the pot saturated right after watering, and over-watering is the
# expensive mistake.
LOOKAHEAD_SHARE = 0.5

# Hard ceiling on a suggestion. The backend's own ceiling is 500 mL; a value
# above it would be rejected there anyway.
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

# What an operator may write in TB_POT_CROPS. "unknown" is in the table above
# as the lookup sentinel, not as something anyone should configure: leaving the
# node out of the map says the same thing without implying it was decided.
SUPPORTED_CROP_CODES = frozenset(CROP_TARGET_MOISTURE_PCT) - {"unknown"}

# Substituted when the caller has no value. These match the contract the
# retired model server applied to the same inputs, so a suggestion computed
# here is comparable with one computed there.
DEFAULT_SOIL_TEMPERATURE_C = 20.0
DEFAULT_PPFD_UMOL_M2_S = 0.0  # night; the dark end is the conservative guess
# Used when the irrigation log has no record for this pot -- it has never been
# watered by this gateway, or the log could not be read. Three days is long enough
# for the redistribution term below to have decayed to nothing, i.e. "assume no
# recent watering is still spreading" -- the assumption that under-states rather
# than over-states how wet the pot already is.
DEFAULT_HOURS_SINCE_LAST_IRRIGATION = 72.0


def _clip(value: float, minimum: float, maximum: float) -> float:
    return min(max(value, minimum), maximum)


def _finite(value: object) -> float | None:
    """Return ``value`` as a float, or ``None`` if it is not a usable number.

    Booleans are rejected the way ``protocol._reading`` rejects them: ``True``
    is an ``int`` to Python and a fault to us.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def evapotranspiration_pct_per_hour(
    soil_moisture_pct: float,
    soil_temperature_c: float,
    air_temperature_c: float,
    relative_humidity_pct: float,
    ppfd_umol_m2_s: float,
) -> float:
    """Soil moisture loss rate in percentage points per hour.

    The edge suppressor (``decision.py``) and this sizing path must share one
    set of physical assumptions -- otherwise the edge could suppress a watering
    it just sized. Do not "improve" one copy alone.
    """

    vapour_deficit = _clip(1.0 - relative_humidity_pct / 100.0, 0.05, 1.0)
    light = 0.30 + ppfd_umol_m2_s / 800.0
    warmth = _clip(1.0 + 0.045 * (air_temperature_c - 20.0), 0.2, 3.0)
    # Root uptake peaks near 22 C and falls off in cold or hot soil.
    roots = _clip(1.0 - abs(soil_temperature_c - 22.0) / 30.0, 0.2, 1.0)
    # A dry soil gives up water more slowly than a wet one.
    availability = _clip(soil_moisture_pct / 40.0, 0.25, 1.2)
    return 3.5 * vapour_deficit * light * warmth * roots * availability


def wetted_fraction(substrate_volume_ml: float) -> float:
    """Share of the substrate a single dose rewets.

    1 at the reference pot size and falling as the square root of volume above
    it, floored so a very large pot still gets a meaningful dose.
    """

    return _clip(
        math.sqrt(REFERENCE_VOLUME_ML / substrate_volume_ml), MIN_WETTED_FRACTION, 1.0
    )


def target_moisture_pct(crop_code: str | None) -> float:
    """Per-crop target soil moisture, defaulting for unrecognised codes."""

    return CROP_TARGET_MOISTURE_PCT.get(str(crop_code), DEFAULT_TARGET_MOISTURE_PCT)


def redistribution_pct(hours_since_last_irrigation: float) -> float:
    """Moisture still spreading downward from a recent watering.

    Without this term a pot watered ten minutes ago is judged to need water
    again, because the probe near the surface has already drained. Same decay
    constant as the edge suppressor.
    """

    return 1.4 * math.exp(-hours_since_last_irrigation / 3.0)


def _water_balance_ml(
    *,
    soil_moisture_pct: float,
    soil_temperature_c: float,
    air_temperature_c: float,
    relative_humidity_pct: float,
    ppfd_umol_m2_s: float,
    hours_since_last_irrigation: float,
    substrate_volume_ml: float,
    crop_code: str | None,
) -> float:
    """The formula itself, in mL, before rounding. All inputs already validated.

    Two parts. The deficit refills the substrate to its crop target; the
    lookahead adds part of what the next 12 hours will evaporate. Both are
    divided by the irrigation efficiency because some of the poured water never
    reaches the roots.

    Kept separate from :func:`suggest_volume_ml` so the parity test can compare
    unrounded floats against the numpy reference; rounding would otherwise hide
    a divergence of up to half a millilitre.
    """

    capacity_ml = (
        substrate_volume_ml * WATER_HOLDING_FRACTION * wetted_fraction(substrate_volume_ml)
    )

    effective_moisture = soil_moisture_pct + redistribution_pct(
        hours_since_last_irrigation
    )
    deficit_pct = max(target_moisture_pct(crop_code) - effective_moisture, 0.0)
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
    return _clip(volume, 0.0, MAX_LABEL_ML)


def suggest_volume_ml(
    *,
    soil_moisture_pct: float | None,
    air_temperature_c: float | None,
    air_humidity_pct: float | None,
    ppfd_umol_m2_s: float | None,
    soil_temperature_c: float | None = None,
    hours_since_last_irrigation: float | None = None,
    substrate_volume_ml: float | None,
    crop_code: str | None = None,
) -> int | None:
    """How much water this pot needs right now, in mL, or ``None``.

    ``None`` means "cannot compute", never "no water needed" -- zero is a real
    answer and is returned as ``0``. The backend has a pot-size fallback table
    for the ``None`` case; a fabricated number here would displace it and hide
    the fact that nothing was actually known (§3.1, §3.2).

    Missing optional inputs are substituted (a dark, cool, long-since-watered
    pot); missing *load-bearing* inputs are not. Soil moisture is the entire
    left-hand side of the balance and pot volume is its scale, so neither can
    be guessed at.

    ``air_humidity_pct`` carries the name the telemetry envelope and the
    backend use; the private formula below keeps the reference's
    ``relative_humidity_pct`` so the two stay diffable line by line.
    """

    moisture = _finite(soil_moisture_pct)
    if moisture is None or not 0.0 <= moisture <= 100.0:
        return None

    volume = _finite(substrate_volume_ml)
    if volume is None or volume <= 0.0:
        return None

    # Air readings are mandatory in protocol v1, so their absence here is a
    # caller bug rather than an absent probe. Refuse rather than substitute:
    # inventing a warm dry day inflates the lookahead term.
    air_temperature = _finite(air_temperature_c)
    humidity = _finite(air_humidity_pct)
    if air_temperature is None or humidity is None:
        return None

    ppfd = _finite(ppfd_umol_m2_s)
    if ppfd is None or ppfd < 0.0:
        ppfd = DEFAULT_PPFD_UMOL_M2_S
    soil_temperature = _finite(soil_temperature_c)
    if soil_temperature is None:
        soil_temperature = DEFAULT_SOIL_TEMPERATURE_C
    hours = _finite(hours_since_last_irrigation)
    if hours is None or hours < 0.0:
        hours = DEFAULT_HOURS_SINCE_LAST_IRRIGATION

    return int(
        round(
            _water_balance_ml(
                soil_moisture_pct=moisture,
                soil_temperature_c=soil_temperature,
                air_temperature_c=air_temperature,
                relative_humidity_pct=humidity,
                ppfd_umol_m2_s=ppfd,
                hours_since_last_irrigation=hours,
                substrate_volume_ml=volume,
                crop_code=crop_code,
            )
        )
    )
