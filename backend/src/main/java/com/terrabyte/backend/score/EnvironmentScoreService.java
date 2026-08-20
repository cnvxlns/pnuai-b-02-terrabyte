package com.terrabyte.backend.score;

import java.util.List;
import java.util.Locale;
import java.util.Comparator;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Collections;

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
    private static final double SOIL_MOISTURE_ZERO_LOW = 0;
    private static final double SOIL_MOISTURE_OPTIMAL_LOW = 30;
    private static final double SOIL_MOISTURE_OPTIMAL_HIGH = 45;
    private static final double SOIL_MOISTURE_ZERO_HIGH = 100;
    private static final double SOIL_TEMPERATURE_ZERO_LOW = 5;
    private static final double SOIL_TEMPERATURE_OPTIMAL_LOW = 18;
    private static final double SOIL_TEMPERATURE_OPTIMAL_HIGH = 25;
    private static final double SOIL_TEMPERATURE_ZERO_HIGH = 40;

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
        if (!sample.airSensorValid() || !sample.lightSensorValid()) {
            throw new ApiException(
                    HttpStatus.UNPROCESSABLE_ENTITY,
                    "INVALID_SCORE_INPUT",
                    "온도·습도·광량 센서값이 모두 유효해야 점수를 계산할 수 있습니다.");
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
        double total = totalFor(
                profile,
                sample.airTemperatureC(),
                sample.airHumidityPct(),
                sample.plantLightPpfdUmolM2S());
        List<EnvironmentScoreResponse.Factor> factors = new ArrayList<>(List.of(temperature, humidity, light));
        if (sample.soilSensorValid()) {
            factors.add(factor(
                    "soilMoisture", "토양 수분", "%", sample.soilMoisturePct(),
                    SOIL_MOISTURE_ZERO_LOW, SOIL_MOISTURE_OPTIMAL_LOW,
                    SOIL_MOISTURE_OPTIMAL_HIGH, SOIL_MOISTURE_ZERO_HIGH));
            if (sample.soilTemperatureC() != null) {
                factors.add(factor(
                        "soilTemperature", "토양 온도", "℃", sample.soilTemperatureC(),
                        SOIL_TEMPERATURE_ZERO_LOW, SOIL_TEMPERATURE_OPTIMAL_LOW,
                        SOIL_TEMPERATURE_OPTIMAL_HIGH, SOIL_TEMPERATURE_ZERO_HIGH));
            }
        }

        return new EnvironmentScoreResponse(
                pot.id(),
                profile.cropCode(),
                profile.cropName(),
                total,
                grade(total),
                sample.observedAt(),
                formula(profile),
                factors);
    }

    /**
     * 모든 지표를 최적값으로 옮기면 항상 100점이 되어 의미가 없으므로, 사용자가 실제로 조절할 수 있는
     * 습도와 광량만 보정되었다고 가정한다. 최적 범위의 중간값이 아닌 가장 가까운 경계로 옮겨 최소한의
     * 충분한 조치를 모델링하며, 온도는 측정값을 그대로 유지한다.
     */
    public EnvironmentScorePotentialResponse potential(long userId, long potId) {
        Pot pot = potRepository.findOwned(potId, userId)
                .orElseThrow(() -> notFound("POT_NOT_FOUND", "화분을 찾을 수 없습니다."));
        TelemetrySample sample = measurementStore.findLatest(pot.id())
                .orElseThrow(() -> notFound("MEASUREMENT_NOT_FOUND", "아직 수신된 측정 데이터가 없습니다."));
        if (pot.cropCode() == null) {
            throw notFound("CROP_NOT_SELECTED", "환경 적합도를 계산할 작물을 먼저 선택해 주세요.");
        }
        if (!sample.airSensorValid() || !sample.lightSensorValid()) {
            throw new ApiException(
                    HttpStatus.UNPROCESSABLE_ENTITY,
                    "INVALID_SCORE_INPUT",
                    "온도·습도·광량 센서값이 모두 유효해야 점수를 계산할 수 있습니다.");
        }
        CropScoreProfile profile = profileRepository.findActiveByCropCode(pot.cropCode())
                .orElseThrow(() -> notFound("CROP_PROFILE_NOT_FOUND", "작물 점수 기준을 찾을 수 없습니다."));

        double humidity = nearestOptimalBoundary(
                sample.airHumidityPct(),
                profile.humidityOptimalLow(),
                profile.humidityOptimalHigh());
        double plantLight = nearestOptimalBoundary(
                sample.plantLightPpfdUmolM2S(),
                profile.ppfdOptimalLow(),
                profile.ppfdOptimalHigh());
        List<EnvironmentScorePotentialResponse.ImprovedFactor> improvedFactors = new ArrayList<>();
        if (Double.compare(humidity, sample.airHumidityPct()) != 0) {
            improvedFactors.add(new EnvironmentScorePotentialResponse.ImprovedFactor(
                    "humidity", "습도", sample.airHumidityPct(), humidity));
        }
        if (Double.compare(plantLight, sample.plantLightPpfdUmolM2S()) != 0) {
            improvedFactors.add(new EnvironmentScorePotentialResponse.ImprovedFactor(
                    "plantLight", "광량", sample.plantLightPpfdUmolM2S(), plantLight));
        }

        double current = totalFor(
                profile,
                sample.airTemperatureC(),
                sample.airHumidityPct(),
                sample.plantLightPpfdUmolM2S());
        double potential = totalFor(profile, sample.airTemperatureC(), humidity, plantLight);
        double delta = Math.round((potential - current) * 10.0) / 10.0;
        return new EnvironmentScorePotentialResponse(
                pot.id(), current, potential, delta, improvedFactors);
    }

    public List<CropRecommendationResponse> cropRecommendations(long userId, long potId) {
        Pot pot = potRepository.findOwned(potId, userId)
                .orElseThrow(() -> notFound("POT_NOT_FOUND", "화분을 찾을 수 없습니다."));
        TelemetrySample sample = measurementStore.findLatest(pot.id())
                .orElseThrow(() -> notFound("MEASUREMENT_NOT_FOUND", "아직 수신된 측정 데이터가 없습니다."));
        if (!sample.airSensorValid() || !sample.lightSensorValid()) {
            throw new ApiException(
                    HttpStatus.UNPROCESSABLE_ENTITY,
                    "INVALID_SCORE_INPUT",
                    "온도·습도·광량 센서값이 모두 유효해야 추천할 수 있습니다.");
        }

        return profileRepository.findAllActive().stream()
                .map(profile -> recommendation(profile, sample))
                .sorted(Comparator.comparingDouble(CropRecommendationResponse::total).reversed())
                .limit(3)
                .toList();
    }

    public List<DiagnosticHistoryRecord> diagnosticHistory(long userId, long potId) {
        Pot pot = potRepository.findOwned(potId, userId)
                .orElseThrow(() -> notFound("POT_NOT_FOUND", "화분을 찾을 수 없습니다."));
        if (pot.cropCode() == null) {
            throw notFound("CROP_NOT_SELECTED", "환경 적합도를 계산할 작물을 먼저 선택해 주세요.");
        }
        CropScoreProfile profile = profileRepository.findActiveByCropCode(pot.cropCode())
                .orElseThrow(() -> notFound("CROP_PROFILE_NOT_FOUND", "작물 점수 기준을 찾을 수 없습니다."));
        List<TelemetrySample> samples = measurementStore.findSamples(pot.id(), Instant.now().minusSeconds(30L * 24 * 60 * 60));
        if (samples.isEmpty()) {
            throw notFound("MEASUREMENT_NOT_FOUND", "아직 수신된 측정 데이터가 없습니다.");
        }

        int step = Math.max(1, (int) Math.ceil(samples.size() / 14.0));
        List<DiagnosticHistoryRecord> records = new ArrayList<>();
        for (int index = 0; index < samples.size(); index += step) {
            TelemetrySample sample = samples.get(index);
            if (sample.airSensorValid() && sample.lightSensorValid()) {
                records.add(historyRecord(profile, sample));
            }
        }
        TelemetrySample latestSample = samples.get(samples.size() - 1);
        if (latestSample.airSensorValid() && latestSample.lightSensorValid()
                && (records.isEmpty() || !records.get(records.size() - 1).observedAt().equals(latestSample.observedAt()))) {
            records.add(historyRecord(profile, latestSample));
        }
        Collections.reverse(records);
        return records;
    }

    private DiagnosticHistoryRecord historyRecord(CropScoreProfile profile, TelemetrySample sample) {
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
        double total = totalFor(
                profile,
                sample.airTemperatureC(),
                sample.airHumidityPct(),
                sample.plantLightPpfdUmolM2S());
        String issues = List.of(temperature, humidity, light).stream()
                .filter(factor -> !factor.status().equals("OK"))
                .map(EnvironmentScoreResponse.Factor::label)
                .reduce((left, right) -> left + "·" + right)
                .orElse("주요 환경 지표 안정");
        return new DiagnosticHistoryRecord(
                sample.observedAt(),
                total,
                "측정 환경 적합도 재계산",
                issues);
    }

    private CropRecommendationResponse recommendation(CropScoreProfile profile, TelemetrySample sample) {
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
        double total = totalFor(
                profile,
                sample.airTemperatureC(),
                sample.airHumidityPct(),
                sample.plantLightPpfdUmolM2S());
        EnvironmentScoreResponse.Factor limitingFactor = List.of(temperature, humidity, light).stream()
                .min(Comparator.comparingDouble(EnvironmentScoreResponse.Factor::score))
                .orElse(temperature);
        String caution = limitingFactor.status().equals("OK")
                ? "현재 주요 환경 지표가 권장 범위 안에 있습니다."
                : limitingFactor.label() + "을(를) 권장 범위에 맞추면 적합도를 높일 수 있습니다.";

        return new CropRecommendationResponse(
                profile.cropCode(),
                profile.cropName(),
                total,
                "현재 측정된 온도·습도·광량을 기준으로 계산한 예상 적합도입니다.",
                caution);
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

    private double totalFor(
            CropScoreProfile profile,
            double temperatureC,
            double humidityPct,
            double ppfd) {
        double temperatureScore = calculator.factor(
                temperatureC,
                profile.temperatureZeroLow(),
                profile.temperatureOptimalLow(),
                profile.temperatureOptimalHigh(),
                profile.temperatureZeroHigh());
        double humidityScore = calculator.factor(
                humidityPct,
                profile.humidityZeroLow(),
                profile.humidityOptimalLow(),
                profile.humidityOptimalHigh(),
                profile.humidityZeroHigh());
        double plantLightScore = calculator.factor(
                ppfd,
                profile.ppfdZeroLow(),
                profile.ppfdOptimalLow(),
                profile.ppfdOptimalHigh(),
                profile.ppfdZeroHigh());
        return calculator.overall(
                temperatureScore,
                humidityScore,
                plantLightScore,
                profile.temperatureExponent(),
                profile.humidityExponent(),
                profile.plantLightExponent());
    }

    private double nearestOptimalBoundary(double value, double optimalLow, double optimalHigh) {
        if (value < optimalLow) return optimalLow;
        if (value > optimalHigh) return optimalHigh;
        return value;
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
