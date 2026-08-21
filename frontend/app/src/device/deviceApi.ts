import { authenticatedRequest } from '../auth/authApi';

export type DeviceResponse = {
  id: number;
  serialCode: string;
  status: 'ONLINE' | 'OFFLINE';
  cropCode?: string;
  lastSeenAt?: string;
  space?: {
    id: number;
    name: string;
    spaceType: string;
    areaSquareMeters: number;
  };
  pots?: PotResponse[];
};

export type PotResponse = {
  id: number;
  deviceId: number;
  nodeId?: string;
  label: string;
  cropCode?: string;
  cropSelectedAt?: string;
  status: 'ONLINE' | 'OFFLINE';
  lastSeenAt?: string;
};

export type RegisterDeviceInput = {
  serialCode: string;
  spaceId?: number;
  spaceName?: string;
  spaceType?: string;
  areaSquareMeters?: number;
};

export type CreatePotInput = {
  label: string;
  nodeId?: string;
  cropCode?: string;
};

export type UpdatePotInput = {
  label: string;
  cropCode?: string;
};

export async function registerDevice(input: RegisterDeviceInput) {
  return authenticatedRequest<DeviceResponse>('/api/devices', {
    method: 'POST',
    body: JSON.stringify(input),
  });
}

export function getDevice(deviceId: number) {
  return authenticatedRequest<DeviceResponse>(`/api/devices/${deviceId}`);
}

export function createPot(deviceId: number, input: CreatePotInput) {
  return authenticatedRequest<PotResponse>(`/api/devices/${deviceId}/pots`, {
    method: 'POST',
    body: JSON.stringify(input),
  });
}

export function updatePot(potId: number, input: UpdatePotInput) {
  return authenticatedRequest<PotResponse>(`/api/pots/${potId}`, {
    method: 'PATCH',
    body: JSON.stringify(input),
  });
}
