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
  engine_mode: 'LIVE_DAEMON' | 'SANDBOX_SIMULATION' | 'LIVE_TRADING';
  bayesian_kelly_confidence?: number;
  almgren_chriss_slices_executed?: number;
  almgren_chriss_slices_total?: number;
  lifecycle_phase?: 'PRE_MARKET' | 'CORE_SESSION' | 'SWEEP' | 'REFLECTION' | 'OFFLINE';
  vm_stats?: {
    cpu_pct: number;
    mem_pct: number;
    api_ping_ms: number;
    uptime_string: string;
  };
  llm_insight?: {
    last_prompt: string;
    last_response: string;
    latency_ms: number;
    model: string;
  };
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
  status_flag?: string;
}

export interface PositionsResponse {
  managed: Position[];
  unmanaged: Position[];
}

export interface LedgerSnapshot {
  total_nlv?: number;
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

export type BureauType = 'EXPERIAN' | 'TRANSUNION' | 'EQUIFAX';

export interface Tradeline {
  account_name: string;
  masked_account_number: string;
  credit_limit: number;
  current_balance: number;
  monthly_payment?: number;
  date_opened?: string;
  last_reported?: string;
  is_promotional: boolean;
  promotional_expiration?: string;
}

export interface CreditReportSnapshot {
  bureau: BureauType;
  report_date: string;
  total_revolving_limit: number;
  total_revolving_balance: number;
  aggregate_utilization_pct: number;
  hard_inquiries_count: number;
  tradelines: Tradeline[];
  detected_discrepancies: string[];
}

export interface PromotionalDebt {
  id: string;
  institution: string;
  total_balance: number;
  promotional_apr: number;
  expiration_date: string;
  minimum_monthly_payment: number;
  days_remaining: number;
  is_manual: boolean;
  notes?: string;
}

export interface CollateralInvariantState {
  timestamp: string;
  settled_cash: number;
  unsettled_cash: number;
  external_liquid_backstop: number;
  total_liquid_backstop: number;
  active_promotional_debt: number;
  net_collateral_buffer: number;
  is_solvent: boolean;
  risk_multiplier: number;
}

export interface MacroLiquidityStateResponse {
  _updated_at: string;
  file_source: string;
  sha256: string;
  credit_report?: CreditReportSnapshot;
  collateral_state: CollateralInvariantState;
  promotional_debts: PromotionalDebt[];
  external_liquid_backstop: number;
}
