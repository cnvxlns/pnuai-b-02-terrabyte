import { authenticatedRequest } from '../auth/authApi';
import type { PotResponse } from '../device/deviceApi';

export function getPots() {
  return authenticatedRequest<PotResponse[]>('/api/pots');
}

export function getPot(potId: number) {
  return authenticatedRequest<PotResponse>(`/api/pots/${potId}`);
}

/** Hands this pot to the rule engine, or takes it back. */
export function setAutoControl(potId: number, enabled: boolean) {
  return authenticatedRequest<PotResponse>(`/api/pots/${potId}/auto-control`, {
    method: 'PATCH',
    body: JSON.stringify({ enabled }),
  });
}
