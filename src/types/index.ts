export interface EngineStatus {
  status: 'ONLINE' | 'WAITING_AUTH' | 'HALTED' | 'OFFLINE';
  auth_status: 'AUTHORIZED' | 'NEEDS_AUTH' | 'EXPIRED';
  uptime_seconds: number;
  active_positions: number;
  today_pnl: number;
  circuit_breaker_limit: number;
  circuit_breaker_triggered: boolean;
  external_positions_detected: boolean;
  timestamp_edt: string;
  is_connected: boolean;
  engine_mode: 'LIVE_DAEMON' | 'SANDBOX_SIMULATION';
}

export interface Position {
  symbol: string;
  quantity: number;
  entry_price: number;
  current_price: number;
  notional_value: number;
  exposure_pct: number;
  max_exposure_cap: number;
  hard_stop_price?: number;
  trailing_stop_price?: number;
  stop_price?: number; // legacy fallback
  target_price?: number;
  unrealized_pnl: number;
  regime?: 'A' | 'B' | 'C' | 'UNKNOWN';
  managed: boolean;
  rotation_pair?: string;
  wash_sale_armed?: boolean;
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
  total_equity: number;
  max_single_exposure: number;
  max_order_value: number;
  max_risk_per_trade: number;
  daily_drawdown_limit: number;
  quarter_kelly_size: number;
  safe_daytrade_buying_power: number;
  gfv_risk_flag: boolean;
}

export interface StrategyConfig {
  ci_threshold_regime_a: number;
  ci_threshold_regime_c: number;
  rvol_min_regime_a: number;
  rsi_oversold: number;
  llm_mode: 'gemini-2.5-pro' | 'gemini-2.5-flash' | 'disabled';
}

export interface TradeOrder {
  symbol: string;
  side: 'BUY' | 'SELL';
  quantity: number;
  price: number;
  cost: number;
  timestamp: string;
  status: string;
  regime?: string;
  risk_notional?: number;
}

export interface MacroBlackout {
  is_blackout: boolean;
  reason?: string;
  next_event?: string;
  event_time?: string;
}

