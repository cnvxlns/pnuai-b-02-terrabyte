import type { SoilRecommendation } from './soilApi';

/** 백엔드 environment_condition 어휘를 화면 문구로. 원시 enum을 그대로 노출하지 않는다. */
export const SOIL_CONDITION_LABELS: Record<SoilRecommendation['targetCondition'], string> = {
  NORMAL: '정상',
  WET_RISK: '과습 주의',
  DRY_RISK: '건조 주의',
};

/**
 * 토양 환경 판정을 한 줄로. 판정되지 않은 상태를 '정상'과 같은 얼굴로 보여주지 않는다.
 *
 * 판정에는 같은 화분의 관수 주기가 최소 두 번 필요하다(백엔드 SoilConditionClassifier).
 * 그만큼의 이력이 쌓이기 전에는 '정상'이라고 말할 근거가 없다.
 */
export function soilConditionText(recommendation: SoilRecommendation | null | undefined): string {
  if (!recommendation) return '판정 대기';
  if (!recommendation.conditionDiagnosed) return '관수 이력 쌓는 중';
  return SOIL_CONDITION_LABELS[recommendation.targetCondition] ?? '판정 대기';
}
