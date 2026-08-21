import { authenticatedRequest } from '../auth/authApi';

export type CropRecommendation = {
  cropCode: string;
  cropName: string;
  total: number;
  reason: string;
  caution: string;
};

export function getCropRecommendations(potId: number) {
  return authenticatedRequest<CropRecommendation[]>(`/api/pots/${potId}/crop-recommendations`);
}

/**
 * 실제로 옮긴 지표만 담긴다. 모든 지표가 이미 최적 범위면 빈 배열이 온다.
 */
export type ImprovedFactor = {
  key: 'humidity' | 'plantLight';
  label: string;
  from: number;
  to: number;
};

export type EnvironmentScorePotential = {
  potId: number;
  current: number;
  potential: number;
  delta: number;
  improvedFactors: ImprovedFactor[];
};

export function getScorePotential(potId: number) {
  return authenticatedRequest<EnvironmentScorePotential>(`/api/pots/${potId}/score/potential`);
}
