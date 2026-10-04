import {
  ActionResult,
  BuyingPowerResponse,
  DocumentJobStatus,
  EngineStatus,
  EnvName,
  LedgerSnapshot,
  LiquidityPolicy,
  LiquidityTarget,
  MacroLiquidityStateResponse,
  PositionsResponse,
  PromotionalDebt,
  RegimeSummary,
  TradeOrder,
} from '../types';

/**
 * Base URL of the engine API. In dev the Vite proxy forwards `/api` to the
 * engine; in production the engine serves this bundle and the API from the
 * same origin. There is NO mock/fallback data: when the engine is unreachable
 * every call rejects with an ApiError and the UI shows an explicit offline state.
 */
const ENGINE_BASE = import.meta.env.VITE_ENGINE_BASE_URL || '/api';

const DEFAULT_TIMEOUT_MS = 6000;

export class ApiError extends Error {
  readonly status: number | null;

  constructor(message: string, status: number | null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE';
  body?: unknown;
  formData?: FormData;
  timeoutMs?: number;
}

function detailMessage(payload: unknown, fallback: string): string {
  if (payload && typeof payload === 'object' && 'detail' in payload) {
    const detail = (payload as { detail: unknown }).detail;
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) {
      return detail
        .map((d) => (d && typeof d === 'object' && 'msg' in d ? String((d as { msg: unknown }).msg) : String(d)))
        .join('; ');
    }
  }
  return fallback;
}

async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), opts.timeoutMs ?? DEFAULT_TIMEOUT_MS);
  const headers: Record<string, string> = {};
  let body: BodyInit | undefined;
  if (opts.formData) {
    body = opts.formData;
  } else if (opts.body !== undefined) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(opts.body);
  }

  let res: Response;
  try {
    res = await fetch(`${ENGINE_BASE}${path}`, {
      method: opts.method ?? 'GET',
      headers,
      body,
      signal: controller.signal,
    });
  } catch (err: unknown) {
    const aborted = err instanceof DOMException && err.name === 'AbortError';
    throw new ApiError(aborted ? 'Engine did not respond in time.' : 'Engine unreachable.', null);
  } finally {
    clearTimeout(timer);
  }

  let payload: unknown = null;
  const text = await res.text();
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = null;
    }
  }
  if (!res.ok) {
    throw new ApiError(detailMessage(payload, `Engine returned HTTP ${res.status}.`), res.status);
  }
  return payload as T;
}

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof Error ? err.message : fallback;
}

/** Wraps an action whose success/failure must be reported truthfully to the operator. */
async function action<T extends { success?: boolean; message?: string }>(
  fn: () => Promise<T>,
  failure: string,
): Promise<T & ActionResult> {
  try {
    const out = await fn();
    return { ...out, success: out.success !== false, message: out.message ?? '' } as T & ActionResult;
  } catch (err: unknown) {
    return { success: false, message: errorMessage(err, failure) } as T & ActionResult;
  }
}

const envQuery = (env: EnvName) => `?env=${encodeURIComponent(env)}`;

// ---------------------------------------------------------------- telemetry

/** `env` omitted → the engine picks its default (Active when live, Sandbox in dry-run). */
export const fetchStatus = (env?: EnvName) => request<EngineStatus>(`/status${env ? envQuery(env) : ''}`);

export const fetchAllPositions = (env: EnvName) =>
  request<PositionsResponse>(`/positions/all${envQuery(env)}`);

export const fetchLedger = (env: EnvName) => request<LedgerSnapshot>(`/ledger${envQuery(env)}`);

export const fetchOrders = (env: EnvName) => request<TradeOrder[]>(`/orders${envQuery(env)}`);

export const fetchBuyingPower = (env: EnvName) =>
  request<BuyingPowerResponse>(`/v1/liquidity/buying-power${envQuery(env)}`);

export const fetchRegimeSummary = () => request<RegimeSummary>('/v1/regime/summary');

export const fetchMacroLiquidityState = () =>
  request<MacroLiquidityStateResponse>('/v1/liquidity/state');

// ------------------------------------------------------------ liquidity policy

export const fetchLiquidityPolicy = () => request<LiquidityPolicy>('/v1/liquidity/policy');

export async function updateLiquidityPolicy(policy: LiquidityPolicy): Promise<ActionResult & { policy?: LiquidityPolicy }> {
  try {
    const saved = await request<LiquidityPolicy>('/v1/liquidity/policy', { method: 'PUT', body: policy });
    return { success: true, message: 'Liquidity policy saved.', policy: saved };
  } catch (err: unknown) {
    return { success: false, message: errorMessage(err, 'Could not save the liquidity policy.') };
  }
}

// ----------------------------------------------------------------------- auth

export const exchangeOAuthCode = (code: string) =>
  action(async () => {
    await request<{ status: string }>('/auth/exchange', { method: 'POST', body: { code } });
    return { success: true, message: 'Authorization code exchanged; tokens vaulted.' };
  }, 'OAuth exchange failed.');

export const refreshOAuthToken = () =>
  action(
    () =>
      request<{ success: boolean; message: string; expires_in_seconds?: number }>('/auth/refresh', {
        method: 'POST',
        timeoutMs: 20000,
      }),
    'Token refresh failed.',
  );

// ------------------------------------------------------------------ emergency

export const setEmergencyHalt = (halted: boolean) =>
  action(
    () =>
      request<{ success: boolean; halted: boolean; message: string }>('/emergency/halt', {
        method: 'POST',
        body: { halted },
      }),
    'Halt request failed. The engine may still be routing orders.',
  );

export const triggerLiquidationSweep = () =>
  action(
    () =>
      request<{ success: boolean; liquidated_count: number; message: string }>('/emergency/liquidate', {
        method: 'POST',
        timeoutMs: 60000,
      }),
    'Liquidation request failed. Check Schwab directly.',
  );

// ------------------------------------------------------------------ documents

export interface UploadReceipt {
  success: boolean;
  message: string;
  saved_as?: string;
}

export async function uploadDocument(file: File): Promise<UploadReceipt> {
  const formData = new FormData();
  formData.append('file', file);
  try {
    const out = await request<{ saved_as: string }>('/v1/documents/upload', {
      method: 'POST',
      formData,
      timeoutMs: 60000,
    });
    return { success: true, message: 'Upload accepted.', saved_as: out.saved_as };
  } catch (err: unknown) {
    return { success: false, message: errorMessage(err, 'Upload failed.') };
  }
}

export const fetchDocumentStatus = (savedAs: string) =>
  request<DocumentJobStatus>(`/v1/documents/status/${encodeURIComponent(savedAs)}`);

export const saveLiquidityTarget = (target: LiquidityTarget) =>
  action(
    () => request<{ success: boolean; message?: string }>('/v1/liquidity/targets', { method: 'POST', body: target }),
    'Could not save the goal.',
  );

export const deleteLiquidityTarget = (targetId: string) =>
  action(
    () =>
      request<{ success: boolean; message?: string }>(`/v1/liquidity/targets/${encodeURIComponent(targetId)}`, {
        method: 'DELETE',
      }),
    'Could not delete the goal.',
  );

export const savePromotionalDebt = (
  debt: Pick<PromotionalDebt, 'id' | 'institution' | 'total_balance' | 'promotional_apr' | 'expiration_date' | 'minimum_monthly_payment'> & {
    notes?: string;
  },
) =>
  action(
    () => request<{ success: boolean; message?: string }>('/v1/liquidity/promotional-debt', { method: 'POST', body: debt }),
    'Could not save the promotional debt.',
  );
