"""Tests for the scalar water-balance volume formula.

``terrabyte_edge/irrigation/volume.py`` is a hand-written scalar port of the
vectorised ``tools/reference_water_balance.py``. Nothing but this file keeps
the two in step: a divergence introduced while porting (a clip dropped, a term
reordered) produces plausible numbers that are simply wrong, and there is no
flow meter downstream to notice. :class:`PortAgreementTests` is therefore the
load-bearing test here — if you change the formula in either file, expect it to
tell you what the other one missed.
"""

import importlib.util
import math
from pathlib import Path
import random
import unittest

from terrabyte_edge.irrigation.volume import (
    MAX_LABEL_ML,
    MODEL_VERSION,
    SUPPORTED_CROP_CODES,
    _water_balance_ml,
    suggest_volume_ml,
)


REFERENCE_PATH = (
    Path(__file__).resolve().parents[1] / "tools" / "reference_water_balance.py"
)


def _load_reference():
    """Import the numpy reference by path.

    By path rather than by module name so the test does not depend on the
    working directory ``unittest`` happened to be started from. Returns
    ``None`` when numpy is absent — the edge runtime deliberately has no numpy,
    so a developer machine without it is normal.
    """

    try:
        import numpy  # noqa: F401
    except ImportError:
        return None
    if not REFERENCE_PATH.exists():
        return None
    spec = importlib.util.spec_from_file_location(
        "reference_water_balance", REFERENCE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REFERENCE = _load_reference()

# Ranges a real pot on a real bench can produce. Deliberately wider than the
# comfortable middle: the clip boundaries inside the formula are exactly where
# a port goes wrong.
INPUT_RANGES = {
    "soil_moisture_pct": (0.0, 100.0),
    "soil_temperature_c": (-5.0, 45.0),
    "air_temperature_c": (-10.0, 50.0),
    "relative_humidity_pct": (0.0, 100.0),
    "ppfd_umol_m2_s": (0.0, 2000.0),
    "hours_since_last_irrigation": (0.0, 168.0),
    "substrate_volume_ml": (200.0, 12000.0),
}

# "durian" is not a supported crop and None is what an unconfigured node gives;
# both must land on the default target, in both implementations.
CROP_CODES = sorted(SUPPORTED_CROP_CODES) + ["durian", None]


def _random_inputs(rng: random.Random) -> dict[str, object]:
    inputs: dict[str, object] = {
        name: rng.uniform(low, high) for name, (low, high) in INPUT_RANGES.items()
    }
    inputs["crop_code"] = rng.choice(CROP_CODES)
    return inputs


class PortAgreementTests(unittest.TestCase):
    """The port must reproduce the reference, not merely resemble it."""

    @unittest.skipIf(
        REFERENCE is None,
        "numpy (or the reference file) is unavailable; run this suite on an "
        "interpreter with numpy installed to check the port against it",
    )
    def test_port_matches_the_numpy_reference_on_randomised_inputs(self) -> None:
        import numpy as np

        rng = random.Random(20260817)  # fixed: a failure must be reproducible
        for case in range(200):
            inputs = _random_inputs(rng)
            with self.subTest(case=case, **inputs):
                expected = float(
                    REFERENCE.irrigation_volume_ml(
                        np.array([inputs["soil_moisture_pct"]]),
                        np.array([inputs["soil_temperature_c"]]),
                        np.array([inputs["air_temperature_c"]]),
                        np.array([inputs["relative_humidity_pct"]]),
                        np.array([inputs["ppfd_umol_m2_s"]]),
                        np.array([inputs["hours_since_last_irrigation"]]),
                        np.array([inputs["substrate_volume_ml"]]),
                        np.array([inputs["crop_code"]], dtype=object),
                    )[0]
                )

                # The unrounded float first: rounding to whole millilitres
                # would absorb a divergence of up to half the stated tolerance
                # and let a real porting error through.
                self.assertAlmostEqual(_water_balance_ml(**inputs), expected, places=9)

                suggestion = suggest_volume_ml(
                    soil_moisture_pct=inputs["soil_moisture_pct"],
                    air_temperature_c=inputs["air_temperature_c"],
                    air_humidity_pct=inputs["relative_humidity_pct"],
                    ppfd_umol_m2_s=inputs["ppfd_umol_m2_s"],
                    soil_temperature_c=inputs["soil_temperature_c"],
                    hours_since_last_irrigation=inputs[
                        "hours_since_last_irrigation"
                    ],
                    substrate_volume_ml=inputs["substrate_volume_ml"],
                    crop_code=inputs["crop_code"],
                )
                self.assertLessEqual(abs(suggestion - expected), 0.5)

    def test_golden_vectors_hold_without_numpy(self) -> None:
        """A safety net for machines where the test above skips.

        The edge runtime has no numpy and neither does every developer box, so
        the randomised comparison cannot be the only guard. These expectations
        were produced by ``tools/reference_water_balance.py`` itself; regenerate
        them from that file, never from ``volume.py``, or the net checks the
        port against itself.
        """

        for inputs, expected in GOLDEN_VECTORS:
            with self.subTest(**inputs):
                self.assertAlmostEqual(_water_balance_ml(**inputs), expected, places=6)


class ResponseShapeTests(unittest.TestCase):
    """The formula must move in the directions an operator would predict."""

    def suggest(self, **overrides) -> int | None:
        readings = {
            "soil_moisture_pct": 20.0,
            "air_temperature_c": 24.0,
            "air_humidity_pct": 55.0,
            "ppfd_umol_m2_s": 300.0,
            "soil_temperature_c": 21.0,
            "hours_since_last_irrigation": 24.0,
            # Small enough that every comparison below stays clear of the
            # 500 mL ceiling, where two different inputs would produce the same
            # clamped answer and hide the ordering being tested.
            "substrate_volume_ml": 800,
            "crop_code": "lettuce",
        }
        readings.update(overrides)
        return suggest_volume_ml(**readings)

    def test_drier_soil_is_watered_more(self) -> None:
        # Below the crop target only. Above it the deficit term is zero for
        # both sides and all that remains is evaporation, which a wetter pot
        # loses *faster* — so the ordering legitimately inverts up there.
        self.assertGreater(self.suggest(soil_moisture_pct=10.0), self.suggest())
        self.assertGreater(self.suggest(), self.suggest(soil_moisture_pct=30.0))

    def test_a_bigger_pot_is_watered_more(self) -> None:
        volumes = [
            self.suggest(substrate_volume_ml=size)
            for size in (300, 600, 900, 1200, 1800)
        ]
        self.assertEqual(volumes, sorted(volumes))
        self.assertEqual(len(set(volumes)), len(volumes))

    def test_hotter_and_drier_air_is_watered_more(self) -> None:
        self.assertGreater(self.suggest(air_temperature_c=34.0), self.suggest())
        self.assertGreater(self.suggest(air_humidity_pct=20.0), self.suggest())

    def test_a_recently_watered_pot_is_watered_less(self) -> None:
        """Water from ten minutes ago is still spreading past the probe."""

        self.assertLess(self.suggest(hours_since_last_irrigation=0.2), self.suggest())

    def test_crop_targets_order_as_configured(self) -> None:
        wasabi = self.suggest(crop_code="wasabi")  # semi-aquatic, target 45%
        lettuce = self.suggest(crop_code="lettuce")  # 40%
        welsh_onion = self.suggest(crop_code="welsh_onion")  # 33%
        self.assertGreater(wasabi, lettuce)
        self.assertGreater(lettuce, welsh_onion)

    def test_an_unknown_crop_falls_back_to_the_default_target(self) -> None:
        self.assertEqual(self.suggest(crop_code="durian"), self.suggest(crop_code=None))

    def test_a_wet_pot_gets_a_number_not_a_refusal(self) -> None:
        """0 mL is an answer; ``None`` means the question could not be asked."""

        wet = self.suggest(
            soil_moisture_pct=100.0,
            air_humidity_pct=100.0,
            air_temperature_c=-10.0,
            ppfd_umol_m2_s=0.0,
            soil_temperature_c=-5.0,
        )
        self.assertIsNotNone(wet)
        self.assertGreaterEqual(wet, 0)

    def test_output_never_leaves_the_backends_accepted_range(self) -> None:
        extreme = self.suggest(
            soil_moisture_pct=0.0,
            air_temperature_c=50.0,
            air_humidity_pct=0.0,
            ppfd_umol_m2_s=2000.0,
            soil_temperature_c=22.0,
            hours_since_last_irrigation=168.0,
            substrate_volume_ml=100_000,
            crop_code="wasabi",
        )
        self.assertEqual(extreme, int(MAX_LABEL_ML))
        for moisture in (0.0, 25.0, 50.0, 75.0, 100.0):
            for size in (100, 2000, 50_000):
                value = self.suggest(
                    soil_moisture_pct=moisture, substrate_volume_ml=size
                )
                self.assertGreaterEqual(value, 0)
                self.assertLessEqual(value, MAX_LABEL_ML)


class RefusalTests(unittest.TestCase):
    """What the edge must decline to answer.

    ``None`` leaves the backend on its pot-size fallback table. A guessed
    number would displace that fallback while looking just as authoritative.
    """

    def suggest(self, **overrides) -> int | None:
        readings = {
            "soil_moisture_pct": 20.0,
            "air_temperature_c": 24.0,
            "air_humidity_pct": 55.0,
            "ppfd_umol_m2_s": 300.0,
            "substrate_volume_ml": 3000,
        }
        readings.update(overrides)
        return suggest_volume_ml(**readings)

    def test_missing_or_impossible_soil_moisture_is_refused(self) -> None:
        for moisture in (None, -0.1, 100.1, math.nan, math.inf, "40", True):
            with self.subTest(soil_moisture_pct=moisture):
                self.assertIsNone(self.suggest(soil_moisture_pct=moisture))

    def test_an_unconfigured_pot_volume_is_refused(self) -> None:
        for size in (None, 0, -1000, math.nan, "3000"):
            with self.subTest(substrate_volume_ml=size):
                self.assertIsNone(self.suggest(substrate_volume_ml=size))

    def test_missing_air_readings_are_refused_rather_than_invented(self) -> None:
        """Protocol v1 makes these mandatory, so absence is a caller bug.

        Substituting a plausible warm dry day would inflate the lookahead term
        with a number nobody measured.
        """

        self.assertIsNone(self.suggest(air_temperature_c=None))
        self.assertIsNone(self.suggest(air_humidity_pct=None))


class SubstitutionTests(unittest.TestCase):
    """Optional inputs fall back to the values the server contract used."""

    def suggest(self, **overrides) -> int | None:
        readings = {
            "soil_moisture_pct": 22.0,
            "air_temperature_c": 24.0,
            "air_humidity_pct": 55.0,
            "ppfd_umol_m2_s": 300.0,
            "substrate_volume_ml": 3000,
        }
        readings.update(overrides)
        return suggest_volume_ml(**readings)

    def test_absent_soil_probe_is_treated_as_20_c(self) -> None:
        self.assertEqual(self.suggest(), self.suggest(soil_temperature_c=20.0))

    def test_absent_irrigation_history_is_treated_as_72_hours(self) -> None:
        self.assertEqual(
            self.suggest(), self.suggest(hours_since_last_irrigation=72.0)
        )

    def test_absent_light_is_treated_as_night(self) -> None:
        self.assertEqual(
            self.suggest(ppfd_umol_m2_s=None), self.suggest(ppfd_umol_m2_s=0.0)
        )

    def test_a_nonsensical_history_falls_back_instead_of_distorting(self) -> None:
        """A negative "hours since" would inflate the redistribution term."""

        self.assertEqual(
            self.suggest(hours_since_last_irrigation=-5.0), self.suggest()
        )


class VersionTests(unittest.TestCase):
    def test_model_version_is_the_string_the_contract_names(self) -> None:
        self.assertEqual(MODEL_VERSION, "water-balance-v1")

    def test_configurable_crop_codes_exclude_the_lookup_sentinel(self) -> None:
        self.assertNotIn("unknown", SUPPORTED_CROP_CODES)
        self.assertIn("cherry_tomato", SUPPORTED_CROP_CODES)
        self.assertEqual(len(SUPPORTED_CROP_CODES), 8)


# Generated by tools/reference_water_balance.py. See
# PortAgreementTests.test_golden_vectors_hold_without_numpy.
GOLDEN_VECTORS: list[tuple[dict[str, object], float]] = [
    (
        # nominal lettuce
        {
            "soil_moisture_pct": 20.0,
            "soil_temperature_c": 21.0,
            "air_temperature_c": 24.0,
            "relative_humidity_pct": 55.0,
            "ppfd_umol_m2_s": 300.0,
            "hours_since_last_irrigation": 24.0,
            "substrate_volume_ml": 800.0,
            "crop_code": "lettuce",
        },
        100.1119514921828,
    ),
    (
        # humidity floor
        {
            "soil_moisture_pct": 30.0,
            "soil_temperature_c": 22.0,
            "air_temperature_c": 30.0,
            "relative_humidity_pct": 100.0,
            "ppfd_umol_m2_s": 800.0,
            "hours_since_last_irrigation": 48.0,
            "substrate_volume_ml": 2000.0,
            "crop_code": "basil",
        },
        79.24698362594917,
    ),
    (
        # warmth ceiling
        {
            "soil_moisture_pct": 5.0,
            "soil_temperature_c": 22.0,
            "air_temperature_c": 70.0,
            "relative_humidity_pct": 10.0,
            "ppfd_umol_m2_s": 1500.0,
            "hours_since_last_irrigation": 72.0,
            "substrate_volume_ml": 300.0,
            "crop_code": "cherry_tomato",
        },
        101.3780514705043,
    ),
    (
        # cold soil roots floor
        {
            "soil_moisture_pct": 12.0,
            "soil_temperature_c": -5.0,
            "air_temperature_c": 5.0,
            "relative_humidity_pct": 40.0,
            "ppfd_umol_m2_s": 0.0,
            "hours_since_last_irrigation": 72.0,
            "substrate_volume_ml": 1000.0,
            "crop_code": "welsh_onion",
        },
        111.5666999997202,
    ),
    (
        # dry availability floor
        {
            "soil_moisture_pct": 0.0,
            "soil_temperature_c": 20.0,
            "air_temperature_c": 25.0,
            "relative_humidity_pct": 50.0,
            "ppfd_umol_m2_s": 400.0,
            "hours_since_last_irrigation": 72.0,
            "substrate_volume_ml": 500.0,
            "crop_code": "wasabi",
        },
        125.47323529397774,
    ),
    (
        # wet availability ceiling
        {
            "soil_moisture_pct": 100.0,
            "soil_temperature_c": 25.0,
            "air_temperature_c": 35.0,
            "relative_humidity_pct": 20.0,
            "ppfd_umol_m2_s": 2000.0,
            "hours_since_last_irrigation": 72.0,
            "substrate_volume_ml": 700.0,
            "crop_code": "arugula",
        },
        315.3533929411764,
    ),
    (
        # wetted floor big pot
        {
            "soil_moisture_pct": 39.5,
            "soil_temperature_c": 18.0,
            "air_temperature_c": 18.0,
            "relative_humidity_pct": 90.0,
            "ppfd_umol_m2_s": 0.0,
            "hours_since_last_irrigation": 72.0,
            "substrate_volume_ml": 20000.0,
            "crop_code": "peppermint",
        },
        36.71229573333555,
    ),
    (
        # small pot wetted 1.0
        {
            "soil_moisture_pct": 18.0,
            "soil_temperature_c": 19.0,
            "air_temperature_c": 22.0,
            "relative_humidity_pct": 60.0,
            "ppfd_umol_m2_s": 150.0,
            "hours_since_last_irrigation": 12.0,
            "substrate_volume_ml": 400.0,
            "crop_code": "coriander",
        },
        37.656202988235755,
    ),
    (
        # just watered
        {
            "soil_moisture_pct": 10.0,
            "soil_temperature_c": 21.0,
            "air_temperature_c": 26.0,
            "relative_humidity_pct": 45.0,
            "ppfd_umol_m2_s": 500.0,
            "hours_since_last_irrigation": 0.1,
            "substrate_volume_ml": 800.0,
            "crop_code": "lettuce",
        },
        135.2114190924361,
    ),
    (
        # clamped at max
        {
            "soil_moisture_pct": 0.0,
            "soil_temperature_c": 22.0,
            "air_temperature_c": 50.0,
            "relative_humidity_pct": 0.0,
            "ppfd_umol_m2_s": 2000.0,
            "hours_since_last_irrigation": 168.0,
            "substrate_volume_ml": 100000.0,
            "crop_code": "wasabi",
        },
        500.0,
    ),
    (
        # unknown crop
        {
            "soil_moisture_pct": 25.0,
            "soil_temperature_c": 23.0,
            "air_temperature_c": 27.0,
            "relative_humidity_pct": 33.0,
            "ppfd_umol_m2_s": 900.0,
            "hours_since_last_irrigation": 6.0,
            "substrate_volume_ml": 800.0,
            "crop_code": "durian",
        },
        109.01494281322054,
    ),
    (
        # no crop configured
        {
            "soil_moisture_pct": 25.0,
            "soil_temperature_c": 23.0,
            "air_temperature_c": 27.0,
            "relative_humidity_pct": 33.0,
            "ppfd_umol_m2_s": 900.0,
            "hours_since_last_irrigation": 6.0,
            "substrate_volume_ml": 800.0,
            "crop_code": None,
        },
        109.01494281322054,
    ),
    (
        # night
        {
            "soil_moisture_pct": 33.0,
            "soil_temperature_c": 18.0,
            "air_temperature_c": 16.0,
            "relative_humidity_pct": 80.0,
            "ppfd_umol_m2_s": 0.0,
            "hours_since_last_irrigation": 36.0,
            "substrate_volume_ml": 6000.0,
            "crop_code": "basil",
        },
        68.56590995222079,
    ),
]


if __name__ == "__main__":
    unittest.main()
