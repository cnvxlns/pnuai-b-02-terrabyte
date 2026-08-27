package com.terrabyte.backend.soil;

import static org.assertj.core.api.Assertions.assertThat;

import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;

import com.terrabyte.backend.measurement.TelemetrySample;
import org.junit.jupiter.api.Test;

class SoilConditionClassifierTests {

    private static final Instant START = Instant.parse("2026-08-01T00:00:00Z");

    private final SoilConditionClassifier classifier = new SoilConditionClassifier();

    @Test
    void doesNotDiagnoseFromASingleReading() {
        SoilConditionAssessment assessment = classifier.classify(List.of(sample(0, 40.0)));

        assertThat(assessment.diagnosed()).isFalse();
        assertThat(assessment.condition()).isEqualTo("NORMAL");
    }

    @Test
    void diagnosesNormalWhenThisCycleDriesAtTheSameRateAsThePrevious() {
        // 두 번의 건조 구간, 둘 다 시간당 1%씩 감소. 사이에 관수(30 -> 45)가 있다.
        List<TelemetrySample> samples = series(new double[][] {
                {0, 30}, {1, 29}, {2, 28}, {3, 27},
                {4, 45}, {5, 44}, {6, 43}, {7, 42},
        });

        SoilConditionAssessment assessment = classifier.classify(samples);

        assertThat(assessment.diagnosed()).isTrue();
        assertThat(assessment.condition()).isEqualTo("NORMAL");
    }

    @Test
    void diagnosesDryRiskWhenDryingSpedUpAndTheAirTurnedHotDryAndBright() {
        List<TelemetrySample> samples = new ArrayList<>();
        // 이전 건조 구간: 1%/h, 평범한 공기
        samples.add(sample(0, 30, 22, 55, 200));
        samples.add(sample(1, 29, 22, 55, 200));
        samples.add(sample(2, 28, 22, 55, 200));
        samples.add(sample(3, 27, 22, 55, 200));
        // 관수 후 현재 구간: 3%/h 로 빨라졌고, 공기는 더 덥고 건조하고 밝다
        samples.add(sample(4, 45, 30, 30, 600));
        samples.add(sample(5, 42, 30, 30, 600));
        samples.add(sample(6, 39, 30, 30, 600));
        samples.add(sample(7, 36, 30, 30, 600));

        SoilConditionAssessment assessment = classifier.classify(samples);

        assertThat(assessment.diagnosed()).isTrue();
        assertThat(assessment.condition()).isEqualTo("DRY_RISK");
    }

    @Test
    void diagnosesWetRiskWhenDryingSlowedDownAndTheAirTurnedHumidAndDim() {
        List<TelemetrySample> samples = new ArrayList<>();
        // 이전 건조 구간: 2%/h
        samples.add(sample(0, 40, 22, 50, 400));
        samples.add(sample(1, 38, 22, 50, 400));
        samples.add(sample(2, 36, 22, 50, 400));
        samples.add(sample(3, 34, 22, 50, 400));
        // 관수 후 현재 구간: 0.5%/h 로 느려졌고, 공기는 더 습하고 어둡다
        samples.add(sample(4, 50, 22, 80, 100));
        samples.add(sample(5, 49.5, 22, 80, 100));
        samples.add(sample(6, 49, 22, 80, 100));
        samples.add(sample(7, 48.5, 22, 80, 100));

        SoilConditionAssessment assessment = classifier.classify(samples);

        assertThat(assessment.diagnosed()).isTrue();
        assertThat(assessment.condition()).isEqualTo("WET_RISK");
    }

    @Test
    void doesNotCallItDryWhenOnlyTheMoistureTrendMovedAndTheAirDidNot() {
        List<TelemetrySample> samples = new ArrayList<>();
        // 위 DRY 사례와 같은 3배 가속인데, 공기는 내내 그대로다.
        samples.add(sample(0, 30, 22, 55, 200));
        samples.add(sample(1, 29, 22, 55, 200));
        samples.add(sample(2, 28, 22, 55, 200));
        samples.add(sample(3, 27, 22, 55, 200));
        samples.add(sample(4, 45, 22, 55, 200));
        samples.add(sample(5, 42, 22, 55, 200));
        samples.add(sample(6, 39, 22, 55, 200));
        samples.add(sample(7, 36, 22, 55, 200));

        SoilConditionAssessment assessment = classifier.classify(samples);

        assertThat(assessment.condition()).isEqualTo("NORMAL");
    }

    @Test
    void doesNotCompareAgainstAPreviousCycleThatNeverDried() {
        List<TelemetrySample> samples = new ArrayList<>();
        // 이전 구간이 오히려 젖어들었다(감소율 음수). 비율 비교의 기준이 될 수 없다.
        samples.add(sample(0, 30, 22, 55, 200));
        samples.add(sample(1, 31, 22, 55, 200));
        samples.add(sample(2, 32, 22, 55, 200));
        samples.add(sample(3, 33, 22, 55, 200));
        samples.add(sample(4, 60, 30, 30, 600));
        samples.add(sample(5, 57, 30, 30, 600));
        samples.add(sample(6, 54, 30, 30, 600));
        samples.add(sample(7, 51, 30, 30, 600));

        SoilConditionAssessment assessment = classifier.classify(samples);

        // 기준선을 세울 수 없으면 '정상으로 판정했다'고 말해서는 안 된다.
        assertThat(assessment.diagnosed()).isFalse();
        assertThat(assessment.condition()).isEqualTo("NORMAL");
    }

    @Test
    void ignoresReadingsWhoseSoilSensorReportedItselfInvalid() {
        List<TelemetrySample> samples = new ArrayList<>();
        samples.add(invalidSoil(0, 0));
        samples.add(invalidSoil(1, 0));
        samples.add(sample(2, 30));
        samples.add(sample(3, 29));

        SoilConditionAssessment assessment = classifier.classify(samples);

        // 유효 측정은 한 구간뿐이라 비교 대상이 없다.
        assertThat(assessment.diagnosed()).isFalse();
    }

    @Test
    void survivesReadingsThatCarryNoLightValue() {
        List<TelemetrySample> samples = new ArrayList<>();
        // PPFD 는 읽기 시점에 유도되는 값이라 저장된 표본에서 비어 있을 수 있다.
        samples.add(withoutLight(sample(0, 30, 22, 55, 200)));
        samples.add(withoutLight(sample(1, 29, 22, 55, 200)));
        samples.add(withoutLight(sample(2, 28, 22, 55, 200)));
        samples.add(withoutLight(sample(3, 27, 22, 55, 200)));
        samples.add(withoutLight(sample(4, 45, 30, 30, 600)));
        samples.add(withoutLight(sample(5, 42, 30, 30, 600)));
        samples.add(withoutLight(sample(6, 39, 30, 30, 600)));
        samples.add(withoutLight(sample(7, 36, 30, 30, 600)));

        SoilConditionAssessment assessment = classifier.classify(samples);

        // 조도 신호가 없으면 "높은 조도가 함께 나타남"을 확인할 수 없다.
        assertThat(assessment.condition()).isEqualTo("NORMAL");
    }

    // --- helpers ------------------------------------------------------------

    private static TelemetrySample sample(double hoursFromStart, double soilMoisturePct) {
        return sample(hoursFromStart, soilMoisturePct, 22.0, 55.0, 200.0);
    }

    private static TelemetrySample sample(
            double hoursFromStart,
            double soilMoisturePct,
            double airTemperatureC,
            double airHumidityPct,
            double ppfd) {
        return new TelemetrySample(
                1L,
                1L,
                "node-001",
                "basil",
                "gateway-1",
                "event-" + hoursFromStart,
                START.plus(Duration.ofSeconds((long) (hoursFromStart * 3600))),
                1L,
                soilMoisturePct,
                0L,
                airTemperatureC,
                airHumidityPct,
                null,
                ppfd,
                21.0,
                true,
                true,
                true,
                null);
    }

    /** 토양 프로브가 없는 노드가 보내는 모양: soilSensorValid=false, 수분은 0.0. */
    private static TelemetrySample invalidSoil(double hoursFromStart, double soilMoisturePct) {
        TelemetrySample valid = sample(hoursFromStart, soilMoisturePct);
        return new TelemetrySample(
                valid.potId(), valid.deviceId(), valid.nodeId(), valid.cropCode(),
                valid.hardwareDeviceId(), valid.eventId(), valid.observedAt(), valid.sequence(),
                valid.soilMoisturePct(), valid.soilMoistureRawAdc(), valid.airTemperatureC(),
                valid.airHumidityPct(), valid.illuminanceLux(), valid.plantLightPpfdUmolM2S(),
                valid.soilTemperatureC(), false, valid.airSensorValid(), valid.lightSensorValid(),
                valid.irrigationSuggestion());
    }

    private static TelemetrySample withoutLight(TelemetrySample source) {
        return new TelemetrySample(
                source.potId(), source.deviceId(), source.nodeId(), source.cropCode(),
                source.hardwareDeviceId(), source.eventId(), source.observedAt(), source.sequence(),
                source.soilMoisturePct(), source.soilMoistureRawAdc(), source.airTemperatureC(),
                source.airHumidityPct(), source.illuminanceLux(), null,
                source.soilTemperatureC(), source.soilSensorValid(), source.airSensorValid(),
                source.lightSensorValid(), source.irrigationSuggestion());
    }

    private static List<TelemetrySample> series(double[][] hourAndMoisture) {
        List<TelemetrySample> samples = new ArrayList<>();
        for (double[] point : hourAndMoisture) {
            samples.add(sample(point[0], point[1]));
        }
        return samples;
    }
}
