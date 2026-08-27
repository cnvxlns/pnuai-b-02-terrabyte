package com.terrabyte.backend.soil;

import java.time.Duration;
import java.time.Instant;

import com.terrabyte.backend.api.ApiException;
import com.terrabyte.backend.measurement.MeasurementStore;
import com.terrabyte.backend.pot.Pot;
import com.terrabyte.backend.pot.PotRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;

@Service
public class SoilRecommendationService {

    private static final Logger LOGGER = LoggerFactory.getLogger(SoilRecommendationService.class);

    /**
     * 판정에 쓰는 조회 구간. 규칙이 "이전 관수 주기와 비교"를 요구하므로 최소 두 번의 관수가
     * 들어올 만큼 길어야 한다. 엣지의 최소 관수 간격이 6시간이라 7일이면 넉넉하다.
     */
    private static final Duration DIAGNOSIS_WINDOW = Duration.ofDays(7);

    private final PotRepository potRepository;
    private final SoilProfileCatalog catalog;
    private final MeasurementStore measurementStore;
    private final SoilConditionClassifier classifier;

    public SoilRecommendationService(
            PotRepository potRepository,
            SoilProfileCatalog catalog,
            MeasurementStore measurementStore,
            SoilConditionClassifier classifier) {
        this.potRepository = potRepository;
        this.catalog = catalog;
        this.measurementStore = measurementStore;
        this.classifier = classifier;
    }

    public SoilRecommendationResponse latest(long userId, long potId) {
        Pot pot = potRepository.findOwned(potId, userId)
                .orElseThrow(() -> notFound("POT_NOT_FOUND", "화분을 찾을 수 없습니다."));
        if (pot.cropCode() == null) {
            throw notFound("CROP_NOT_SELECTED", "토양 배지를 추천할 작물을 먼저 선택해 주세요.");
        }
        SoilConditionAssessment assessment = assess(pot);

        // 판정된 조건의 배합이 없으면 판정 자체를 접고 기본 배합으로 돌아간다. 판정은 했는데
        // 배합은 기본인 상태를 conditionDiagnosed=true 로 내보내면 프론트가 "건조 주의"라고
        // 말하면서 평상시 배합을 권하는, 서로 어긋나는 화면이 된다.
        SoilProfile diagnosed = assessment.diagnosed()
                ? catalog.findProfile(pot.cropCode(), assessment.condition()).orElse(null)
                : null;
        SoilProfile profile = diagnosed != null
                ? diagnosed
                : catalog.findNormalProfile(pot.cropCode())
                        .orElseThrow(() -> notFound(
                                "SOIL_PROFILE_NOT_FOUND", "선택한 작물의 토양 배지 추천 데이터를 찾을 수 없습니다."));

        return SoilRecommendationResponse.from(
                pot.id(), pot.cropCode(), profile, diagnosed != null, catalog.assumptionNotice());
    }

    @Deprecated
    public SoilRecommendationResponse latestForDevice(long userId, long deviceId) {
        Pot pot = potRepository.representative(deviceId, userId)
                .orElseThrow(() -> notFound("DEVICE_NOT_FOUND", "기기를 찾을 수 없습니다."));
        return latest(userId, pot.id());
    }

    /**
     * 최근 이력으로 토양 환경 유형을 판정한다. 읽지 못하면 판정하지 않은 것으로 둔다.
     *
     * <p>배합 추천은 작물만 알면 성립하고, 시계열 저장소는 이 기능이 생기기 전까지 이 경로의
     * 의존성이 아니었다. 저장소가 죽었다고 추천까지 500 으로 죽이면 원래 되던 기능이
     * 새 기능 때문에 무너진다. 판정만 포기하고 기본 배합으로 답한다.
     */
    private SoilConditionAssessment assess(Pot pot) {
        try {
            return classifier.classify(
                    measurementStore.findSamples(pot.id(), Instant.now().minus(DIAGNOSIS_WINDOW)));
        } catch (RuntimeException exception) {
            LOGGER.warn("토양 환경 판정을 건너뛴다 pot_id={} reason={}", pot.id(), exception.toString());
            return new SoilConditionAssessment(SoilConditionClassifier.NORMAL, false);
        }
    }

    private ApiException notFound(String code, String message) {
        return new ApiException(HttpStatus.NOT_FOUND, code, message);
    }
}
