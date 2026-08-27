package com.terrabyte.backend.soil;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Optional;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.terrabyte.backend.device.DeviceStatus;
import com.terrabyte.backend.measurement.MeasurementMetric;
import com.terrabyte.backend.measurement.MeasurementPoint;
import com.terrabyte.backend.measurement.MeasurementStore;
import com.terrabyte.backend.measurement.TelemetrySample;
import com.terrabyte.backend.pot.Pot;
import com.terrabyte.backend.pot.PotRepository;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

class SoilRecommendationServiceTests {

    private static final long USER_ID = 7L;
    private static final long POT_ID = 3L;
    private static final Instant START = Instant.parse("2026-08-01T00:00:00Z");

    private final PotRepository potRepository = mock(PotRepository.class);
    private final FakeMeasurementStore measurementStore = new FakeMeasurementStore();
    private final SoilRecommendationService service = new SoilRecommendationService(
            potRepository,
            new SoilProfileCatalog(new ObjectMapper()),
            measurementStore,
            new SoilConditionClassifier());

    @BeforeEach
    void potIsOwnedAndGrowsBasil() {
        when(potRepository.findOwned(anyLong(), anyLong())).thenReturn(Optional.of(new Pot(
                POT_ID, 1L, "node-001", "바질 화분", "basil", START,
                DeviceStatus.ONLINE, START, START, 2000, true)));
    }

    @Test
    void recommendsTheDryRiskMixWhenDryingSpedUpAndTheAirTurnedHotDryAndBright() {
        measurementStore.samples = List.of(
                sample(0, 30, 22, 55, 200),
                sample(1, 29, 22, 55, 200),
                sample(2, 28, 22, 55, 200),
                sample(3, 27, 22, 55, 200),
                sample(4, 45, 30, 30, 600),
                sample(5, 42, 30, 30, 600),
                sample(6, 39, 30, 30, 600),
                sample(7, 36, 30, 30, 600));

        SoilRecommendationResponse response = service.latest(USER_ID, POT_ID);

        assertThat(response.targetCondition()).isEqualTo("DRY_RISK");
        assertThat(response.conditionDiagnosed()).isTrue();
    }

    @Test
    void fallsBackToTheDefaultMixAndSaysSoWhenHistoryIsTooThinToJudge() {
        measurementStore.samples = List.of(sample(0, 30, 22, 55, 200));

        SoilRecommendationResponse response = service.latest(USER_ID, POT_ID);

        assertThat(response.targetCondition()).isEqualTo("NORMAL");
        assertThat(response.conditionDiagnosed()).isFalse();
    }

    @Test
    void stillRecommendsTheDefaultMixWhenTheHistoryStoreIsUnreachable() {
        measurementStore.failOnRead = true;

        SoilRecommendationResponse response = service.latest(USER_ID, POT_ID);

        // 배합 추천은 이력 없이도 성립한다. 시계열 저장소가 죽었다고 500 을 내면 안 된다.
        assertThat(response.targetCondition()).isEqualTo("NORMAL");
        assertThat(response.conditionDiagnosed()).isFalse();
    }

    // --- helpers ------------------------------------------------------------

    private static TelemetrySample sample(
            double hoursFromStart,
            double soilMoisturePct,
            double airTemperatureC,
            double airHumidityPct,
            double ppfd) {
        return new TelemetrySample(
                POT_ID, 1L, "node-001", "basil", "gateway-1", "event-" + hoursFromStart,
                START.plus(Duration.ofSeconds((long) (hoursFromStart * 3600))), 1L,
                soilMoisturePct, 0L, airTemperatureC, airHumidityPct, null, ppfd, 21.0,
                true, true, true, null);
    }

    private static final class FakeMeasurementStore implements MeasurementStore {

        private List<TelemetrySample> samples = new ArrayList<>();
        private boolean failOnRead;

        @Override
        public void write(TelemetrySample sample) {
            throw new UnsupportedOperationException();
        }

        @Override
        public Optional<TelemetrySample> findLatest(long potId) {
            return samples.isEmpty()
                    ? Optional.empty()
                    : Optional.of(samples.get(samples.size() - 1));
        }

        @Override
        public List<TelemetrySample> findSamples(long potId, Instant start) {
            if (failOnRead) {
                throw new IllegalStateException("시계열 저장소에 연결할 수 없습니다.");
            }
            return samples;
        }

        @Override
        public List<MeasurementPoint> findPoints(long potId, MeasurementMetric metric, Instant start) {
            throw new UnsupportedOperationException();
        }
    }
}
