export interface EngineStatus {
  status: 'ONLINE' | 'WAITING_AUTH' | 'HALTED' | 'OFFLINE';
  auth_status: 'AUTHORIZED' | 'NEEDS_AUTH' | 'EXPIRED';
  uptime_seconds: number;
  active_positions: number;
  today_pnl: number;
  external_positions_detected: boolean;
  timestamp_edt: string;
}

export interface Position {
  symbol: string;
  quantity: number;
  entry_price: number;
  current_price: number;
  stop_price?: number;
  target_price?: number;
  unrealized_pnl: number;
  regime?: 'A' | 'B' | 'C' | 'UNKNOWN';
  managed: boolean;
  stop_order_id?: string;
  avg_cost?: number;
}

export interface PositionsResponse {
  managed: Position[];
  unmanaged: Position[];
}

export interface LedgerSnapshot {
  bucket1_settled: number;
  bucket2_unsettled: number;
  bucket3_pending: number;
  max_order_value: number;
}

export interface StrategyConfig {
  ci_threshold_regime_a: number;
  ci_threshold_regime_c: number;
  rvol_min_regime_a: number;
  rsi_oversold: number;
  llm_mode: 'pro' | 'flash' | 'disabled';
}

export interface TradeOrder {
  symbol: string;
  side: 'BUY' | 'SELL';
  quantity: number;
  price: number;
  cost: number;
  timestamp: string;
  status: string;
}

export interface MacroBlackout {
  is_blackout: boolean;
  reason?: string;
  next_event?: string;
  event_time?: string;
}
