import { authenticatedRequest } from '../auth/authApi';

/** Mirrors the backend's CommandState. Every value except REJECTED may have moved water. */
export type CommandState =
  | 'ISSUED'
  | 'ACCEPTED'
  | 'COMPLETED'
  | 'REJECTED'
  | 'ABORTED'
  | 'EXPIRED';

export type CommandOrigin = 'CLOUD' | 'EDGE_FALLBACK';

export type CommandHistoryEntry = {
  commandId: string;
  actuator: 'pump' | 'light';
  action: string;
  state: CommandState;
  origin: CommandOrigin;
  grantedMl: number | null;
  actualMl: number | null;
  stopCause: string | null;
  issuedAt: string;
  completedAt: string | null;
};

/**
 * The last thing each actuator was told to do — not live hardware state.
 *
 * `null` means never commanded, which is different from off: the server does not
 * track the firmware's actuator block, so claiming "off" for a lamp nothing has
 * looked at would be a claim about hardware rather than about our records.
 */
export type ActuatorStatus = {
  pump: CommandHistoryEntry | null;
  light: CommandHistoryEntry | null;
};

export function getCommandHistory(potId: number, limit = 20) {
  return authenticatedRequest<CommandHistoryEntry[]>(
    `/api/pots/${potId}/commands?limit=${limit}`,
  );
}

export function getActuatorStatus(potId: number) {
  return authenticatedRequest<ActuatorStatus>(`/api/pots/${potId}/actuators`);
}

/** Korean for one command state, for a screen rather than a log. */
export function describeCommandState(state: CommandState): string {
  switch (state) {
    case 'ISSUED':
      return '전달 중';
    case 'ACCEPTED':
      return '실행 중';
    case 'COMPLETED':
      return '완료';
    case 'REJECTED':
      return '거절됨';
    case 'ABORTED':
      return '중단됨';
    case 'EXPIRED':
      return '시간 초과';
  }
}

/**
 * Whether a command is still expected to change.
 *
 * Drives the spinner rather than a timer: a command the device has not answered
 * for is exactly the case a user is staring at the screen for.
 */
export function isCommandPending(state: CommandState): boolean {
  return state === 'ISSUED' || state === 'ACCEPTED';
}
