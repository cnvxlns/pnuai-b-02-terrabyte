from pathlib import Path
import json
import math
import unittest

from terrabyte_edge.irrigation import (
    FEATURE_NAMES,
    INPUT_SCHEMA_VERSION,
    FIXED_VOLUME_ML,
    EnvelopeLimits,
    FeatureError,
    IrrigationDecider,
    IrrigationFeatures,
    ModelError,
    RandomForestClassifier,
    Verdict,
    VolumeSource,
    suggest_volume_ml,
)
from terrabyte_edge.irrigation.forest import DEFAULT_MODEL_PATH


DRY = dict(
    soil_moisture_pct=11.0,
    soil_temperature_c=21.0,
    air_temperature_c=28.0,
    relative_humidity_pct=30.0,
    ppfd_umol_m2_s=600.0,
    hours_since_last_irrigation=30.0,
)

WET = dict(DRY, soil_moisture_pct=52.0)


def features(**overrides) -> IrrigationFeatures:
    return IrrigationFeatures(**{**DRY, **overrides})


class _StubModel:
    """Stand-in for a forest, so envelope tests do not depend on the artifact."""

    def __init__(self, irrigate: bool) -> None:
        self.model_version = "stub"
        self.calls = 0
        self._irrigate = irrigate

    def predict(self, features: IrrigationFeatures):
        from terrabyte_edge.irrigation.forest import RandomForestVote

        self.calls += 1
        return RandomForestVote(
            irrigate=self._irrigate,
            probability=0.9 if self._irrigate else 0.1,
            model_version=self.model_version,
        )


class FeatureTests(unittest.TestCase):
    def test_vector_order_matches_the_declared_contract(self) -> None:
        vector = features().to_vector()
        self.assertEqual(len(vector), len(FEATURE_NAMES))
        self.assertEqual(vector[0], 11.0)
        self.assertEqual(vector[FEATURE_NAMES.index("ppfd_umol_m2_s")], 600.0)

    def test_out_of_range_reading_is_rejected(self) -> None:
        with self.assertRaises(FeatureError):
            features(soil_moisture_pct=140.0)

    def test_non_finite_reading_is_rejected(self) -> None:
        with self.assertRaises(FeatureError):
            features(air_temperature_c=float("nan"))

    def test_negative_reading_age_is_rejected(self) -> None:
        with self.assertRaises(FeatureError):
            features(reading_age_seconds=-1.0)


class EnvelopeTests(unittest.TestCase):
    def test_invalid_sensor_blocks_before_the_model_is_consulted(self) -> None:
        model = _StubModel(irrigate=True)
        decision = IrrigationDecider(model).decide(features(soil_sensor_valid=False))
        self.assertFalse(decision.irrigate)
        self.assertEqual(decision.verdict, Verdict.SENSOR_INVALID)
        self.assertEqual(model.calls, 0)

    def test_stale_reading_blocks(self) -> None:
        decision = IrrigationDecider(_StubModel(irrigate=True)).decide(
            features(reading_age_seconds=601.0)
        )
        self.assertEqual(decision.verdict, Verdict.INPUT_STALE)

    def test_wet_soil_blocks(self) -> None:
        decision = IrrigationDecider(_StubModel(irrigate=True)).decide(features(**WET))
        self.assertEqual(decision.verdict, Verdict.SOIL_NOT_DRY)

    def test_cooldown_blocks(self) -> None:
        decision = IrrigationDecider(_StubModel(irrigate=True)).decide(
            features(hours_since_last_irrigation=5.9)
        )
        self.assertEqual(decision.verdict, Verdict.COOLDOWN_ACTIVE)

    def test_daily_budget_blocks_the_dose_that_would_exceed_it(self) -> None:
        decider = IrrigationDecider(_StubModel(irrigate=True))
        allowed = decider.decide(features(), dispensed_today_ml=90.0)
        self.assertTrue(allowed.irrigate)

        blocked = decider.decide(features(), dispensed_today_ml=91.0)
        self.assertEqual(blocked.verdict, Verdict.DAILY_BUDGET_EXHAUSTED)

    def test_missing_model_never_irrigates(self) -> None:
        decision = IrrigationDecider(None).decide(features())
        self.assertFalse(decision.irrigate)
        self.assertEqual(decision.verdict, Verdict.MODEL_UNAVAILABLE)
        self.assertTrue(decision.envelope_allows)

    def test_model_can_withhold_inside_the_envelope(self) -> None:
        decision = IrrigationDecider(_StubModel(irrigate=False)).decide(features())
        self.assertFalse(decision.irrigate)
        self.assertEqual(decision.verdict, Verdict.MODEL_WITHHELD)
        self.assertTrue(decision.envelope_allows)

    def test_approved_decision_uses_the_fixed_volume(self) -> None:
        decision = IrrigationDecider(_StubModel(irrigate=True)).decide(features())
        self.assertTrue(decision.irrigate)
        self.assertEqual(decision.volume_ml, FIXED_VOLUME_ML)
        self.assertEqual(decision.volume_ml, 30.0)
        self.assertEqual(decision.volume_source, VolumeSource.FALLBACK)

    def test_model_cannot_widen_the_autonomous_envelope(self) -> None:
        """`D17`: a model that recommends irrigation at 30% soil moisture is
        still refused, because the deterministic gate is evaluated first."""

        decider = IrrigationDecider(
            _StubModel(irrigate=True), limits=EnvelopeLimits.autonomous()
        )
        decision = decider.decide(features(soil_moisture_pct=30.0))
        self.assertFalse(decision.irrigate)
        self.assertEqual(decision.verdict, Verdict.SOIL_NOT_DRY)

    def test_every_veto_reports_zero_volume(self) -> None:
        decider = IrrigationDecider(_StubModel(irrigate=True))
        for sample, dispensed in (
            (features(soil_sensor_valid=False), 0.0),
            (features(reading_age_seconds=9999.0), 0.0),
            (features(**WET), 0.0),
            (features(hours_since_last_irrigation=0.0), 0.0),
            (features(), 119.0),
        ):
            with self.subTest(verdict=decider.decide(sample, dispensed_today_ml=dispensed).verdict):
                decision = decider.decide(sample, dispensed_today_ml=dispensed)
                self.assertEqual(decision.volume_ml, 0.0)
                self.assertFalse(decision.irrigate)


class ArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.model = RandomForestClassifier.load()

    def test_shipped_artifact_loads(self) -> None:
        self.assertEqual(self.model.model_version, "irrigation-rf-v1")
        self.assertGreater(self.model.tree_count, 0)

    def test_dry_hot_bright_pot_is_recommended_for_irrigation(self) -> None:
        vote = self.model.predict(features())
        self.assertTrue(vote.irrigate)
        self.assertGreater(vote.probability, 0.5)

    def test_wet_cool_dark_pot_is_not_recommended(self) -> None:
        vote = self.model.predict(
            features(
                soil_moisture_pct=55.0,
                air_temperature_c=18.0,
                relative_humidity_pct=80.0,
                ppfd_umol_m2_s=0.0,
            )
        )
        self.assertFalse(vote.irrigate)
        self.assertLess(vote.probability, 0.5)

    def test_probability_is_monotonic_in_dryness(self) -> None:
        wetter = self.model.probability(features(soil_moisture_pct=40.0))
        drier = self.model.probability(features(soil_moisture_pct=8.0))
        self.assertGreater(drier, wetter)

    def test_end_to_end_decision_with_the_real_model(self) -> None:
        decision = IrrigationDecider(self.model).decide(features())
        self.assertTrue(decision.irrigate)
        self.assertEqual(decision.volume_ml, 30.0)
        self.assertIsNotNone(decision.vote)
        self.assertEqual(decision.vote.model_version, "irrigation-rf-v1")


class ArtifactValidationTests(unittest.TestCase):
    def _payload(self) -> dict:
        return json.loads(DEFAULT_MODEL_PATH.read_text(encoding="utf-8"))

    def test_schema_version_mismatch_is_rejected(self) -> None:
        payload = self._payload()
        payload["input_schema_version"] = 99
        with self.assertRaises(ModelError):
            RandomForestClassifier.from_dict(payload)

    def test_feature_rename_is_rejected(self) -> None:
        payload = self._payload()
        payload["feature_names"][0] = "soil_moisture_ratio"
        with self.assertRaises(ModelError):
            RandomForestClassifier.from_dict(payload)

    def test_missing_artifact_raises_model_error(self) -> None:
        with self.assertRaises(ModelError):
            RandomForestClassifier.load(Path("/nonexistent/model.json"))

    def test_malformed_tree_is_rejected(self) -> None:
        payload = self._payload()
        payload["trees"][0]["threshold"] = payload["trees"][0]["threshold"][:-1]
        with self.assertRaises(ModelError):
            RandomForestClassifier.from_dict(payload)

    def test_unknown_aggregation_is_rejected(self) -> None:
        payload = self._payload()
        payload["aggregation"] = "median_vote"
        with self.assertRaises(ModelError):
            RandomForestClassifier.from_dict(payload)

    def test_sum_logit_artifact_scores_as_a_logistic_of_the_margin(self) -> None:
        """The XGBoost export path: leaves are raw margins, not probabilities."""

        stump = {
            "feature": [0, -1, -1],
            "threshold": [20.0, 0.0, 0.0],
            "children_left": [1, -1, -1],
            "children_right": [2, -1, -1],
            "leaf": [0.0, 1.5, -1.5],
        }
        payload = {
            "model_version": "xgb-test",
            "input_schema_version": INPUT_SCHEMA_VERSION,
            "feature_names": list(FEATURE_NAMES),
            "aggregation": "sum_logit",
            "base_score": 0.0,
            "decision_threshold": 0.5,
            "trees": [stump, stump],
        }
        model = RandomForestClassifier.from_dict(payload)

        # Dry soil takes the left branch in both trees: margin = 1.5 + 1.5.
        dry = model.probability(features(soil_moisture_pct=11.0))
        self.assertAlmostEqual(dry, 1.0 / (1.0 + math.exp(-3.0)), places=12)

        # Wet soil takes the right branch: margin = -3.0.
        wet = model.probability(features(soil_moisture_pct=41.0))
        self.assertAlmostEqual(wet, 1.0 / (1.0 + math.exp(3.0)), places=12)

    def test_sum_logit_is_stable_for_large_margins(self) -> None:
        """A saturating margin must not overflow the logistic."""

        stump = {
            "feature": [0, -1, -1],
            "threshold": [20.0, 0.0, 0.0],
            "children_left": [1, -1, -1],
            "children_right": [2, -1, -1],
            "leaf": [0.0, 800.0, -800.0],
        }
        model = RandomForestClassifier.from_dict(
            {
                "model_version": "xgb-test",
                "input_schema_version": INPUT_SCHEMA_VERSION,
                "feature_names": list(FEATURE_NAMES),
                "aggregation": "sum_logit",
                "base_score": 0.0,
                "decision_threshold": 0.5,
                "trees": [stump],
            }
        )
        self.assertEqual(model.probability(features(soil_moisture_pct=11.0)), 1.0)
        self.assertEqual(model.probability(features(soil_moisture_pct=41.0)), 0.0)

    def test_child_pointing_backwards_is_rejected(self) -> None:
        payload = self._payload()
        payload["trees"][0]["children_left"][0] = 0
        with self.assertRaises(ModelError):
            RandomForestClassifier.from_dict(payload)


class InjectedVolumeTests(unittest.TestCase):
    """The seam between "whether" (this module) and "how much" (volume.py).

    The two were written on branches that did not know about each other:
    decision.py declared the dose fixed at 30 mL while volume.py computed a
    variable one. Integration forces the join, and it lands here rather than in
    the constructor because the dose belongs to the reading.
    """

    def test_a_computed_dose_is_used_and_labelled(self) -> None:
        decision = IrrigationDecider(_StubModel(irrigate=True)).decide(
            features(), volume_ml=88.0
        )
        self.assertTrue(decision.irrigate)
        self.assertEqual(decision.volume_ml, 88.0)
        self.assertEqual(decision.volume_source, VolumeSource.COMPUTED)

    def test_an_unsizable_reading_falls_back_rather_than_refusing(self) -> None:
        """A plant dying of an unset TB_POT_SUBSTRATE_ML is the worse failure.

        The fallback is the smallest dose in play, so erring here errs dry.
        """

        decision = IrrigationDecider(_StubModel(irrigate=True)).decide(
            features(), volume_ml=None
        )
        self.assertEqual(decision.volume_ml, FIXED_VOLUME_ML)
        self.assertEqual(decision.volume_source, VolumeSource.FALLBACK)

    def test_zero_is_a_real_answer_and_is_not_a_dose(self) -> None:
        """suggest_volume_ml returns 0 for "needs nothing"; None for "cannot
        tell". Silently treating 0 as the fallback would water a pot the formula
        said was already wet enough."""

        with self.assertRaises(ValueError):
            IrrigationDecider(_StubModel(irrigate=True)).decide(
                features(), volume_ml=0.0
            )

    def test_the_budget_weighs_the_dose_actually_about_to_be_delivered(self) -> None:
        """The whole reason the injection is at decide() and not __init__."""

        decider = IrrigationDecider(_StubModel(irrigate=True))
        self.assertTrue(decider.decide(features(), volume_ml=40.0).irrigate)
        blocked = decider.decide(
            features(), volume_ml=40.0, dispensed_today_ml=90.0
        )
        self.assertEqual(blocked.verdict, Verdict.DAILY_BUDGET_EXHAUSTED)
        # The dose is reported as zero, but its provenance survives the veto.
        self.assertEqual(blocked.volume_ml, 0.0)
        self.assertEqual(blocked.volume_source, VolumeSource.COMPUTED)

    def test_a_dose_larger_than_the_whole_budget_is_its_own_verdict(self) -> None:
        """Otherwise the pot is never watered and the log blames the budget.

        EnvelopeLimits.daily_budget_ml is 120, which the server's own fallback
        table exceeds for a pot over 6 L (160 mL). That deployment must be
        distinguishable from "today's allowance is used up".
        """

        decision = IrrigationDecider(_StubModel(irrigate=True)).decide(
            features(), volume_ml=160.0, dispensed_today_ml=0.0
        )
        self.assertEqual(decision.verdict, Verdict.DOSE_EXCEEDS_DAILY_BUDGET)
        self.assertNotEqual(decision.verdict, Verdict.DAILY_BUDGET_EXHAUSTED)
        self.assertFalse(decision.envelope_allows)

    def test_the_default_budget_is_four_fixed_doses_and_the_interval_caps_four(
        self,
    ) -> None:
        """Why the budget never fired before: it restated min_interval_hours.

        Four 6-hour intervals fit in a day and four 30 mL doses fit in 120 mL, so
        the budget could not bind before the interval did. A computed dose breaks
        that coincidence, which is the whole reason the constant needs revisiting
        against pot volume.
        """

        limits = EnvelopeLimits.supervised()
        self.assertEqual(limits.daily_budget_ml / FIXED_VOLUME_ML, 4.0)
        self.assertEqual(24.0 / limits.min_interval_hours, 4.0)

    def test_the_formula_and_the_decider_join_up(self) -> None:
        """One pass of the real seam, with no stub between them."""

        dose = suggest_volume_ml(
            soil_moisture_pct=11.0,
            air_temperature_c=28.0,
            air_humidity_pct=30.0,
            ppfd_umol_m2_s=600.0,
            soil_temperature_c=21.0,
            hours_since_last_irrigation=30.0,
            substrate_volume_ml=1000.0,
            crop_code="lettuce",
        )
        self.assertIsNotNone(dose)
        decision = IrrigationDecider(
            _StubModel(irrigate=True),
            limits=EnvelopeLimits(daily_budget_ml=1000.0),
        ).decide(features(), volume_ml=float(dose))
        self.assertTrue(decision.irrigate)
        self.assertEqual(decision.volume_ml, float(dose))
        self.assertEqual(decision.volume_source, VolumeSource.COMPUTED)


if __name__ == "__main__":
    unittest.main()
