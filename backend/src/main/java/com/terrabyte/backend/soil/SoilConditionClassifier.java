package com.terrabyte.backend.soil;

import java.time.Duration;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Objects;
import java.util.OptionalDouble;
import java.util.function.Function;

import com.terrabyte.backend.measurement.TelemetrySample;
import org.springframework.stereotype.Component;

/**
 * 최근 측정 이력에서 토양 환경 유형(NORMAL/WET_RISK/DRY_RISK)을 판정한다.
 *
 * <p>판정 규칙의 원본은 {@code resources/soil/indoor_potting_substrate_recommendations.json}
 * 의 {@code agent_decision_policy} 다. 그 문서가 못박은 제약이 이 구현의 형태를 결정한다.
 *
 * <ul>
 *   <li>"한 번의 토양수분센서 값만으로 과습 또는 건조를 확정하지 않는다" — 그래서 단일
 *       측정값으로는 절대 판정하지 않고 {@code diagnosed=false} 로 돌려준다.</li>
 *   <li>"모든 화분에 공통인 고정 토양수분 퍼센트를 사용하지 않는다" — 그래서 "몇 % 이하면
 *       건조" 같은 절대 임계값이 이 파일에 하나도 없다. 판정은 전부 <em>같은 화분의 이전
 *       건조 구간 대비 비율</em>이다.</li>
 *   <li>"동일 화분과 동일 센서에서 관수 전후 변화 추세를 우선한다" — 그래서 비교 단위가
 *       개별 측정값이 아니라 관수와 관수 사이의 '건조 구간'이다.</li>
 * </ul>
 *
 * <p>알려진 한계: 같은 문서가 요구하는 "실제 배수 상태"는 센서가 없어 관측할 수 없다.
 * 그래서 이 분류기는 배수 신호 없이 수분 추세와 보조 환경 신호만으로 판정하며, 응답의
 * {@code assumption_notice} 가 그 사실을 사용자에게 전달한다.
 */
@Component
public class SoilConditionClassifier {

    static final String NORMAL = "NORMAL";
    static final String DRY_RISK = "DRY_RISK";
    static final String WET_RISK = "WET_RISK";

    /**
     * 상승을 '관수'로 볼 기준. 같은 화분의 일반적인 측정 간 변화폭(중앙값)의 몇 배인가로
     * 정한다 — 고정 퍼센트를 쓰지 말라는 제약 때문에 절대값이 아니라 배수로 둔다.
     */
    private static final double IRRIGATION_RISE_FACTOR = 3.0;

    /**
     * 현재 구간이 이전 구간보다 이만큼 빨리 마르면 '빨라졌다'로 본다. 절대 퍼센트가 아니라
     * 같은 화분의 이전 건조 속도 대비 배수다.
     */
    private static final double FASTER_THAN_BEFORE = 1.5;

    /** 같은 기준의 반대쪽. 이전 건조 속도의 이 비율 이하로 느려지면 '느려졌다'로 본다. */
    private static final double SLOWER_THAN_BEFORE = 0.6;

    /** 기울기를 낼 수 있는 최소 점 개수. 두 점이면 한 구간의 감소율이 나온다. */
    private static final int MIN_SEGMENT_POINTS = 2;

    /** 현재 구간과 견줄 이전 구간이 최소 하나는 있어야 '비교'가 성립한다. */
    private static final int MIN_SEGMENTS_TO_COMPARE = 2;

    public SoilConditionAssessment classify(List<TelemetrySample> samples) {
        List<TelemetrySample> readings = soilReadings(samples);
        List<DryingSegment> segments = measurableSegments(readings);
        if (segments.size() < MIN_SEGMENTS_TO_COMPARE) {
            return new SoilConditionAssessment(NORMAL, false);
        }
        DryingSegment current = segments.get(segments.size() - 1);
        List<DryingSegment> earlier = segments.subList(0, segments.size() - 1);
        double baseline = median(earlier.stream().map(DryingSegment::ratePerHour).toList());
        if (baseline <= 0) {
            // 이전 주기가 마르지 않았다면(정체·상승) '몇 배 빠른가'라는 질문 자체가 성립하지
            // 않는다. 비율을 억지로 내면 어떤 감소든 무한히 빠른 것이 되어 늘 DRY_RISK 가
            // 나온다. 판정하지 않았다고 말하는 편이 정직하다.
            return new SoilConditionAssessment(NORMAL, false);
        }

        if (current.ratePerHour() >= baseline * FASTER_THAN_BEFORE && turnedHotDryAndBright(current, earlier)) {
            return new SoilConditionAssessment(DRY_RISK, true);
        }
        if (current.ratePerHour() <= baseline * SLOWER_THAN_BEFORE && turnedHumidAndDim(current, earlier)) {
            return new SoilConditionAssessment(WET_RISK, true);
        }
        return new SoilConditionAssessment(NORMAL, true);
    }

    /**
     * "수분 감소가 느리고 높은 습도·낮은 조도가 함께 나타남".
     *
     * <p>규칙 원문은 여기에 "느린 배수"도 요구하지만 배수 센서가 없어 관측할 수 없다.
     * 없는 신호를 참으로 가정하지 않고, 관측 가능한 두 신호만으로 판단한 뒤 그 한계를
     * 응답의 {@code pre_checks} 로 사용자에게 넘긴다 — hard rule 이 과습 판정 시 배수구
     * 막힘·받침 물을 먼저 확인하라고 요구하는 것도 같은 이유다.
     */
    private boolean turnedHumidAndDim(DryingSegment current, List<DryingSegment> earlier) {
        List<TelemetrySample> before = samplesOf(earlier);
        return rose(current.samples(), before, TelemetrySample::airHumidityPct)
                && fell(current.samples(), before, TelemetrySample::plantLightPpfdUmolM2S);
    }

    /**
     * "높은 온도·낮은 습도·높은 조도가 함께 나타남" — 규칙이 요구하는 세 신호를 모두 본다.
     *
     * <p>기준은 절대값이 아니라 <em>같은 화분의 이전 건조 구간</em>이다. 셋 중 하나라도
     * 뒷받침하지 않으면 건조로 확정하지 않는다: 규칙이 "함께"를 요구하고, hard rule 이
     * 보조 신호만으로 확정하는 것을 금지한다.
     */
    private boolean turnedHotDryAndBright(DryingSegment current, List<DryingSegment> earlier) {
        List<TelemetrySample> before = samplesOf(earlier);
        return rose(current.samples(), before, TelemetrySample::airTemperatureC)
                && fell(current.samples(), before, TelemetrySample::airHumidityPct)
                && rose(current.samples(), before, TelemetrySample::plantLightPpfdUmolM2S);
    }

    private List<TelemetrySample> samplesOf(List<DryingSegment> segments) {
        return segments.stream().flatMap(segment -> segment.samples().stream()).toList();
    }

    /** 현재 구간 평균이 이전보다 높은가. */
    private boolean rose(
            List<TelemetrySample> now, List<TelemetrySample> before, Function<TelemetrySample, Number> field) {
        Double after = mean(now, field);
        Double earlier = mean(before, field);
        return after != null && earlier != null && after > earlier;
    }

    /** 현재 구간 평균이 이전보다 낮은가. */
    private boolean fell(
            List<TelemetrySample> now, List<TelemetrySample> before, Function<TelemetrySample, Number> field) {
        Double after = mean(now, field);
        Double earlier = mean(before, field);
        return after != null && earlier != null && after < earlier;
    }

    /**
     * 값이 하나도 없으면 null. 0 으로 대신하지 않는다 — PPFD 는 읽기 시점에 유도되는 값이라
     * 비어 있을 수 있고, 없는 조도를 0 으로 채우면 "어두워졌다"는 관측이 되어 버린다.
     */
    private Double mean(List<TelemetrySample> samples, Function<TelemetrySample, Number> field) {
        OptionalDouble average = samples.stream()
                .map(field)
                .filter(Objects::nonNull)
                .mapToDouble(Number::doubleValue)
                .average();
        return average.isPresent() ? average.getAsDouble() : null;
    }

    /** 한 건조 구간과 그 구간의 시간당 감소율(%/h). */
    private record DryingSegment(List<TelemetrySample> samples, double ratePerHour) {
    }

    /** 감소율을 낼 수 있는 구간만. 오래된 구간이 앞이다. */
    private List<DryingSegment> measurableSegments(List<TelemetrySample> readings) {
        List<DryingSegment> segments = new ArrayList<>();
        for (List<TelemetrySample> samples : dryingSegments(readings)) {
            Double rate = decayRatePerHour(samples);
            if (rate != null) {
                segments.add(new DryingSegment(samples, rate));
            }
        }
        return segments;
    }

    /**
     * 토양 센서가 유효하다고 자기보고한 측정만, 시간순으로.
     *
     * <p>무효 표본을 걸러내지 않으면 프로브가 빠진 노드의 0.0 이 '급격한 건조'로 읽힌다.
     */
    private List<TelemetrySample> soilReadings(List<TelemetrySample> samples) {
        return samples.stream()
                .filter(TelemetrySample::soilSensorValid)
                .sorted(Comparator.comparing(TelemetrySample::observedAt))
                .toList();
    }

    /** 관수(뚜렷한 상승)를 경계로 측정열을 건조 구간들로 자른다. */
    private List<List<TelemetrySample>> dryingSegments(List<TelemetrySample> readings) {
        List<List<TelemetrySample>> segments = new ArrayList<>();
        if (readings.size() < MIN_SEGMENT_POINTS) {
            return segments;
        }
        double riseThreshold = irrigationRiseThreshold(readings);
        List<TelemetrySample> current = new ArrayList<>();
        current.add(readings.get(0));
        for (int index = 1; index < readings.size(); index++) {
            double delta = readings.get(index).soilMoisturePct()
                    - readings.get(index - 1).soilMoisturePct();
            if (delta >= riseThreshold) {
                segments.add(current);
                current = new ArrayList<>();
            }
            current.add(readings.get(index));
        }
        segments.add(current);
        return segments;
    }

    /**
     * 관수로 볼 상승폭. 이 화분 이 센서의 측정 간 변화폭 중앙값을 기준으로 삼는다.
     *
     * <p>변화가 전혀 없는 열(중앙값 0)에서는 어떤 상승도 관수로 보지 않는다 —
     * 노이즈를 관수로 세면 없는 주기가 생겨 비교 자체가 거짓이 된다.
     */
    private double irrigationRiseThreshold(List<TelemetrySample> readings) {
        List<Double> steps = new ArrayList<>();
        for (int index = 1; index < readings.size(); index++) {
            steps.add(Math.abs(readings.get(index).soilMoisturePct()
                    - readings.get(index - 1).soilMoisturePct()));
        }
        double typicalStep = median(steps);
        return typicalStep <= 0 ? Double.MAX_VALUE : typicalStep * IRRIGATION_RISE_FACTOR;
    }

    /** 구간 처음과 끝의 수분 차이를 경과 시간으로 나눈다. 낼 수 없으면 null. */
    private Double decayRatePerHour(List<TelemetrySample> segment) {
        if (segment.size() < MIN_SEGMENT_POINTS) {
            return null;
        }
        TelemetrySample first = segment.get(0);
        TelemetrySample last = segment.get(segment.size() - 1);
        double hours = Duration.between(first.observedAt(), last.observedAt()).toSeconds() / 3600.0;
        if (hours <= 0) {
            return null;
        }
        return (first.soilMoisturePct() - last.soilMoisturePct()) / hours;
    }

    private double median(List<Double> values) {
        if (values.isEmpty()) {
            return 0;
        }
        List<Double> sorted = values.stream().sorted().toList();
        int middle = sorted.size() / 2;
        return sorted.size() % 2 == 1
                ? sorted.get(middle)
                : (sorted.get(middle - 1) + sorted.get(middle)) / 2.0;
    }
}
