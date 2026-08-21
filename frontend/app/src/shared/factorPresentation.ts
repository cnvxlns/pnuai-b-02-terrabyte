import type { ScoreFactor } from '../measurement/measurementApi';
import { factorProductMap } from '../data';

export function getIssueFactors(factors: ScoreFactor[]): ScoreFactor[] {
  return factors.filter((factor) => factor.status !== 'OK');
}

const gradeLabels: Record<'GOOD' | 'NORMAL' | 'BAD', string> = {
  GOOD: '스마트팜 전환 적합',
  NORMAL: '일부 환경 보완 필요',
  BAD: '환경 개선 필요',
};

export function getGradeLabel(grade: 'GOOD' | 'NORMAL' | 'BAD' | undefined): string {
  if (!grade) return '계산 중';
  return gradeLabels[grade];
}

const factorRecommendations: Record<ScoreFactor['key'], string> = {
  temperature: '온도가 적정 범위를 벗어나면 환기 또는 히팅 장치를 조절하세요.',
  humidity: '관수와 환기 시간을 조절해 적정 습도를 유지하세요.',
  plantLight: 'PPFD가 부족하면 생장등의 세기와 설치 거리를 조절하세요.',
  soilMoisture: '토양 수분이 권장 범위를 벗어나면 관수량과 배수 상태를 확인하세요.',
  soilTemperature: '토양 온도가 권장 범위를 벗어나면 화분 위치와 보온 상태를 확인하세요.',
};

export function getFactorRecommendation(key: ScoreFactor['key']): string {
  return factorRecommendations[key];
}

export type EnvironmentAlert = {
  id: ScoreFactor['key'];
  severity: '주의' | '확인 필요';
  title: string;
  body: string;
};

/**
 * 알림은 별도 저장소 없이 최신 적합도 점수에서 파생한다. 화면이 3초마다 받는 그 응답을
 * 그대로 쓰므로 알림과 실시간 수치가 어긋날 수 없다.
 *
 * 지속 시간("10분 이상 이탈") 같은 조건은 여기서 판정하지 않는다 — 판정에 필요한 이력이
 * 이 응답에 없고, 없는 근거로 기준을 말하면 그 자체가 또 다른 허구가 된다.
 */
export function deriveEnvironmentAlerts(factors: ScoreFactor[]): EnvironmentAlert[] {
  return getIssueFactors(factors).map((factor) => {
    const direction = factor.status === 'LOW' ? '낮습니다' : '높습니다';
    const bound = factor.status === 'LOW' ? factor.optimalMin : factor.optimalMax;
    return {
      id: factor.key,
      // 점수가 0에 가까울수록 해당 지표가 종합 적합도를 끌어내리는 폭이 크다.
      severity: factor.score < 50 ? '주의' : '확인 필요',
      title: `${factor.label}이(가) 권장 범위보다 ${direction}`,
      body: `현재 ${factor.current}${factor.unit}로 권장 ${factor.status === 'LOW' ? '하한' : '상한'} ${bound}${factor.unit}에서 ${Math.abs(factor.gap)}${factor.unit} 벗어났습니다. ${getFactorRecommendation(factor.key)}`,
    };
  });
}

export function getRecommendedProductIds(factors: ScoreFactor[]): string[] {
  return Array.from(new Set(getIssueFactors(factors).flatMap((factor) => factorProductMap[factor.key] ?? [])));
}
