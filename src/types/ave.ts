/**
 * src/types/ave.ts
 * =================
 * Type definitions for Adaptive Volatility Envelope (AVE) frontend telemetry.
 */

export enum FSMState {
  INIT = 0,
  ARMED = 1,
  ENTRY_PENDING = 2,
  ENTRY_PARTIAL = 3,
  TRACKING = 4,
  TIGHTENING = 5,
  TRIGGERED = 6,
  EXIT_PENDING = 7,
  SETTLED = 8,
  ORPHANED = 9,
}

export type RegimeType = 'FAVORABLE' | 'CHOP' | 'DEFENSIVE';

/**
 * Compact array frame layout:
 * [0] seq: number
 * [1] epoch_ms: number
 * [2] price: number
 * [3] upper: number
 * [4] lower: number
 * [5] lambda: number
 * [6] supremum: number
 * [7] state_code: number
 */
export type AveCompactFrame = [
  seq: number,
  epoch_ms: number,
  price: number,
  upper: number,
  lower: number,
  lambda: number,
  supremum: number,
  state_code: number
];

export interface AveSnapshotMessage {
  type: 'SNAPSHOT';
  symbol: string;
  data: AveCompactFrame[];
}

export interface AveFrameMessage {
  type: 'FRAME';
  symbol: string;
  frame: AveCompactFrame;
}

export interface AveFsmEventMessage {
  type: 'FSM_EVENT';
  symbol: string;
  state: keyof typeof FSMState;
  prev_state?: string;
  reason?: string;
  timestamp: string;
  details?: Record<string, any>;
}

export type AveServerMessage = AveSnapshotMessage | AveFrameMessage | AveFsmEventMessage;

export interface AveStatusMetrics {
  symbol: string;
  state: keyof typeof FSMState;
  stateCode: number;
  price: number;
  upper: number;
  lower: number;
  supremum: number;
  lambdaFactor: number;
  regime: RegimeType;
  lastUpdateEpochMs: number;
  isStale: boolean;
  seq: number;
}
