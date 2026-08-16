package com.terrabyte.backend.score;

import java.util.List;
import java.util.Locale;

import com.terrabyte.backend.api.ApiException;
import com.terrabyte.backend.pot.Pot;
import com.terrabyte.backend.pot.PotRepository;
import com.terrabyte.backend.measurement.MeasurementStore;
import com.terrabyte.backend.measurement.TelemetrySample;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;

@Service
public class EnvironmentScoreService {

    private static final String EQUAL_FORMULA = "100 × (T/100 × H/100 × L/100)^(1/3)";

    private final PotRepository potRepository;
    private final MeasurementStore measurementStore;
    private final CropScoreProfileRepository profileRepository;
    private final SuitabilityScoreCalculator calculator;

    public EnvironmentScoreService(
            PotRepository potRepository,
            MeasurementStore measurementStore,
            CropScoreProfileRepository profileRepository,
            SuitabilityScoreCalculator calculator) {
        this.potRepository = potRepository;
        this.measurementStore = measurementStore;
        this.profileRepository = profileRepository;
        this.calculator = calculator;
    }

    public EnvironmentScoreResponse latest(long userId, long potId) {
        Pot pot = potRepository.findOwned(potId, userId)
                .orElseThrow(() -> notFound("POT_NOT_FOUND", "화분을 찾을 수 없습니다."));
        TelemetrySample sample = measurementStore.findLatest(pot.id())
                .orElseThrow(() -> notFound("MEASUREMENT_NOT_FOUND", "아직 수신된 측정 데이터가 없습니다."));
        if (pot.cropCode() == null) {
            throw notFound("CROP_NOT_SELECTED", "환경 적합도를 계산할 작물을 먼저 선택해 주세요.");
        }
        if (sample.plantLightPpfdUmolM2S() == null || !sample.lightSensorValid()) {
            throw new ApiException(
                    HttpStatus.UNPROCESSABLE_ENTITY,
                    "SCORE_INPUT_INCOMPLETE",
                    "광량 측정값이 없어 환경 적합도를 계산할 수 없습니다.");
        }
        if (!sample.airSensorValid()) {
            throw new ApiException(
                    HttpStatus.UNPROCESSABLE_ENTITY,
                    "INVALID_SCORE_INPUT",
                    "온도·습도 센서값이 유효해야 점수를 계산할 수 있습니다.");
        }
        CropScoreProfile profile = profileRepository.findActiveByCropCode(pot.cropCode())
                .orElseThrow(() -> notFound("CROP_PROFILE_NOT_FOUND", "작물 점수 기준을 찾을 수 없습니다."));

        EnvironmentScoreResponse.Factor temperature = factor(
                "temperature", "온도", "℃", sample.airTemperatureC(),
                profile.temperatureZeroLow(), profile.temperatureOptimalLow(),
                profile.temperatureOptimalHigh(), profile.temperatureZeroHigh());
        EnvironmentScoreResponse.Factor humidity = factor(
                "humidity", "습도", "%", sample.airHumidityPct(),
                profile.humidityZeroLow(), profile.humidityOptimalLow(),
                profile.humidityOptimalHigh(), profile.humidityZeroHigh());
        EnvironmentScoreResponse.Factor light = factor(
                "plantLight", "광량", "μmol/m²/s", sample.plantLightPpfdUmolM2S(),
                profile.ppfdZeroLow(), profile.ppfdOptimalLow(),
                profile.ppfdOptimalHigh(), profile.ppfdZeroHigh());
        double total = calculator.overall(
                temperature.score(),
                humidity.score(),
                light.score(),
                profile.temperatureExponent(),
                profile.humidityExponent(),
                profile.plantLightExponent());

        return new EnvironmentScoreResponse(
                pot.id(),
                profile.cropCode(),
                profile.cropName(),
                total,
                grade(total),
                sample.observedAt(),
                formula(profile),
                List.of(temperature, humidity, light));
    }

    private String formula(CropScoreProfile profile) {
        if ("equal_geometric_v1".equals(profile.aggregationFamily())) {
            return EQUAL_FORMULA;
        }
        double exponentSum = profile.temperatureExponent()
                + profile.humidityExponent()
                + profile.plantLightExponent();
        return String.format(
                Locale.ROOT,
                "100 × (T/100)^%.6f × (H/100)^%.6f × (L/100)^%.6f",
                profile.temperatureExponent() / exponentSum,
                profile.humidityExponent() / exponentSum,
                profile.plantLightExponent() / exponentSum);
    }

    private EnvironmentScoreResponse.Factor factor(
            String key,
            String label,
            String unit,
            double current,
            double zeroLow,
            double optimalLow,
            double optimalHigh,
            double zeroHigh) {
        String status = current < optimalLow ? "LOW" : current > optimalHigh ? "HIGH" : "OK";
        double gap = status.equals("LOW")
                ? optimalLow - current
                : status.equals("HIGH") ? current - optimalHigh : 0;
        return new EnvironmentScoreResponse.Factor(
                key,
                label,
                unit,
                current,
                optimalLow,
                optimalHigh,
                status,
                Math.round(gap * 10.0) / 10.0,
                calculator.factor(current, zeroLow, optimalLow, optimalHigh, zeroHigh));
    }

    private String grade(double total) {
        if (total >= 80) return "GOOD";
        if (total >= 60) return "NORMAL";
        return "BAD";
    }

    private ApiException notFound(String code, String message) {
        return new ApiException(HttpStatus.NOT_FOUND, code, message);
    }
}
