import { authenticatedRequest } from '../auth/authApi';

export type LatestMeasurements = {
  deviceId: number;
  hardwareDeviceId: string;
  observedAt: string;
  sequence: number;
  measurements: {
    soilMoisturePct: number;
    soilMoistureRawAdc: number;
    airTemperatureC: number;
    airHumidityPct: number;
    plantLightPpfdUmolM2S: number | null;
  };
  quality: {
    soilSensorValid: boolean;
    airSensorValid: boolean;
    lightSensorValid: boolean;
  };
};

export type ScoreFactor = {
  key: 'temperature' | 'humidity' | 'plantLight';
  label: string;
  unit: string;
  current: number;
  optimalMin: number;
  optimalMax: number;
  status: 'LOW' | 'OK' | 'HIGH';
  gap: number;
  score: number;
};

export type EnvironmentScore = {
  deviceId: number;
  cropCode: string;
  cropName: string;
  total: number;
  grade: 'GOOD' | 'NORMAL' | 'BAD';
  measuredAt: string;
  formula: string;
  factors: ScoreFactor[];
};

export function getLatestMeasurements(deviceId: number) {
  return authenticatedRequest<LatestMeasurements>(`/api/devices/${deviceId}/measurements/latest`);
}

export function getEnvironmentScore(deviceId: number) {
  return authenticatedRequest<EnvironmentScore>(`/api/devices/${deviceId}/score`);
}
