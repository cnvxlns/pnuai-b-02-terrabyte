import { authenticatedRequest } from '../auth/authApi';

export type SoilMaterial = {
  name: string;
  parts: number;
  role: string;
};

export type SoilRecommendation = {
  deviceId: number;
  cropCode: string;
  cropName: string;
  targetCondition: 'NORMAL' | 'WET_RISK' | 'DRY_RISK';
  /**
   * targetCondition 이 실제 측정 이력에서 판정된 것인지, 판정할 이력이 없어 기본값으로
   * 되돌아간 것인지. false 를 '정상'으로 보여주면 없는 진단을 지어내는 것이 된다.
   */
  conditionDiagnosed: boolean;
  profileId: string;
  materials: SoilMaterial[];
  mixRatio: string;
  mixRatioText: string;
  reason: string;
  environmentSignals: string[];
  preChecks: string[];
  cautions: string[];
  assumptionNotice: string[];
};

export function getSoilRecommendation(potId: number) {
  return authenticatedRequest<SoilRecommendation>(`/api/pots/${potId}/soil-recommendation`);
}
