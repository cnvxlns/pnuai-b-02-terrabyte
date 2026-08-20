package com.terrabyte.backend.score;

import java.time.Instant;
import java.util.Optional;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.tuple;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

import com.terrabyte.backend.device.DeviceStatus;
import com.terrabyte.backend.measurement.MeasurementStore;
import com.terrabyte.backend.measurement.TelemetrySample;
import com.terrabyte.backend.pot.Pot;
import com.terrabyte.backend.pot.PotRepository;
import org.junit.jupiter.api.Test;

class EnvironmentScoreServiceTests {

    private static final long USER_ID = 3;
    private static final long POT_ID = 7;
    private static final long DEVICE_ID = 11;
    private static final Instant OBSERVED_AT = Instant.parse("2026-08-20T00:00:00Z");

    private final PotRepository potRepository = mock(PotRepository.class);
    private final MeasurementStore measurementStore = mock(MeasurementStore.class);
    private final CropScoreProfileRepository profileRepository = mock(CropScoreProfileRepository.class);
    private final EnvironmentScoreService service = new EnvironmentScoreService(
            potRepository,
            measurementStore,
            profileRepository,
            new SuitabilityScoreCalculator());

    @Test
    void improvesLowHumidityAndPlantLightToTheirNearestOptimalBoundaries() {
        givenLatestSample(27, 40, 130);

        EnvironmentScorePotentialResponse response = service.potential(USER_ID, POT_ID);

        assertThat(response.potential()).isGreaterThan(response.current());
        assertThat(response.improvedFactors())
                .extracting(
                        EnvironmentScorePotentialResponse.ImprovedFactor::key,
                        EnvironmentScorePotentialResponse.ImprovedFactor::label,
                        EnvironmentScorePotentialResponse.ImprovedFactor::from,
                        EnvironmentScorePotentialResponse.ImprovedFactor::to)
                .containsExactly(
                        tuple("humidity", "습도", 40.0, 50.0),
                        tuple("plantLight", "광량", 130.0, 260.0));
    }

    @Test
    void keepsCurrentScoreWhenControllableFactorsAreAlreadyOptimal() {
        givenLatestSample(27, 60, 300);

        EnvironmentScorePotentialResponse response = service.potential(USER_ID, POT_ID);

        assertThat(response.potential()).isEqualTo(response.current());
        assertThat(response.improvedFactors()).isEmpty();
    }

    private void givenLatestSample(double temperatureC, double humidityPct, double ppfd) {
        Pot pot = new Pot(
                POT_ID,
                DEVICE_ID,
                "pot-01",
                "상추 화분",
                "lettuce",
                OBSERVED_AT.minusSeconds(60),
                DeviceStatus.ONLINE,
                OBSERVED_AT,
                OBSERVED_AT.minusSeconds(120));
        TelemetrySample sample = new TelemetrySample(
                POT_ID,
                DEVICE_ID,
                "pot-01",
                "lettuce",
                "orangepi-pro-01",
                "event-1",
                OBSERVED_AT,
                1,
                0,
                0,
                temperatureC,
                humidityPct,
                ppfd,
                null,
                false,
                true,
                true);
        CropScoreProfile profile = new CropScoreProfile(
                "lettuce", "상추",
                15, 24, 30, 36,
                30, 50, 70, 90,
                0, 260, 500, 750);
        when(potRepository.findOwned(POT_ID, USER_ID)).thenReturn(Optional.of(pot));
        when(measurementStore.findLatest(POT_ID)).thenReturn(Optional.of(sample));
        when(profileRepository.findActiveByCropCode("lettuce")).thenReturn(Optional.of(profile));
    }
}
