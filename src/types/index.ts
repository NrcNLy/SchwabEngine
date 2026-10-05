export type EnvName = 'active' | 'sandbox';

export type SystemState = 'OK' | 'DEGRADED' | 'DOWN' | 'HALTED' | 'SIMULATED';

export type DataSource = 'LIVE_SCHWAB' | 'SANDBOX_SIM' | 'UNAVAILABLE';

export type LifecyclePhase = 'PRE_MARKET' | 'CORE_SESSION' | 'SWEEP' | 'REFLECTION' | 'OFFLINE';

export interface VmStats {
  cpu_pct: number;
  mem_pct: number;
  api_ping_ms: number | null;
  uptime_string: string;
}

export interface EngineStatus {
  status: 'ONLINE' | 'WAITING_AUTH' | 'HALTED' | 'OFFLINE';
  system_state: SystemState;
  system_reasons: string[];
  auth_status: 'AUTHORIZED' | 'NEEDS_AUTH' | 'EXPIRED' | 'NOT_REQUIRED';
  auth_expires_in_s: number | null;
  env: EnvName;
  data_source: DataSource;
  engine_mode: 'LIVE_TRADING' | 'SANDBOX_SIMULATION';
  is_connected: boolean;
  uptime_seconds: number;
  active_positions: number;
  nlv: number | null;
  net_change_usd: number | null;
  net_change_pct: number | null;
  today_pnl: number;
  today_realized_pnl: number;
  unrealized_pnl: number;
  circuit_breaker_limit: number;
  circuit_breaker_triggered: boolean;
  external_positions_detected: boolean;
  timestamp_edt: string;
  lifecycle_phase?: LifecyclePhase;
  session_phase?: string;
  entry_permitted?: boolean;
  phase_sizing_multiplier?: number;
  vm_stats?: VmStats;
  microstructure?: MicrostructureTelemetry;
}

export interface Position {
  symbol: string;
  quantity: number;
  entry_price: number;
  current_price: number;
  notional_value: number;
  exposure_pct: number;
  max_exposure_cap: number;
  hard_stop_price?: number | null;
  stop_price?: number | null;
  target_price?: number | null;
  unrealized_pnl: number;
  regime?: 'A' | 'B' | 'C' | 'UNKNOWN' | string;
  managed: boolean;
  simulated?: boolean;
  avg_cost?: number;
  status_flag?: string;
}

export interface PositionsResponse {
  env: EnvName;
  data_source: DataSource;
  managed: Position[];
  unmanaged: Position[];
}

export interface SweepAdvice {
  action: 'NONE' | 'SWEEP_IN' | 'REDEEM_FOR_NEXT_SESSION';
  amount: number;
  place_by: string | null;
  settles_on: string | null;
  mode: 'ADVISORY' | string;
  routed: boolean;
  rationale: string;
}

export interface BuyingPowerBreakdown {
  as_of: string;
  nlv: number;
  settled_cash: number;
  hard_reserve: number;
  hard_reserve_unsettled_cash: number;
  hard_reserve_swvxx_in_flight: number;
  swvxx_balance: number;
  pending_ach: number;
  external_backstop: number;
  soft_reserve_target: number;
  soft_reserve_target_effective: number;
  soft_reserve_current: number;
  soft_reserve_shortfall: number;
  inflow_offset: number;
  next_inflow_date: string | null;
  next_inflow_settles: string | null;
  replenish_eta: string | null;
  base_float: number;
  soft_draw_available: number;
  high_probability: boolean;
  tactical_float: number;
  single_ticker_cap: number;
  max_order_notional: number;
  sweep: SweepAdvice | null;
}

export interface LedgerSnapshot {
  env: EnvName;
  data_source: DataSource;
  available: boolean;
  synced: boolean;
  synced_at: string | null;
  total_nlv: number;
  nlv: number;
  total_equity: number;
  bucket1_settled: number;
  bucket2_unsettled: number;
  bucket3_pending: number;
  swvxx_balance: number;
  max_single_exposure: number;
  max_order_value: number;
  max_risk_per_trade: number;
  daily_drawdown_limit: number;
  quarter_kelly_size: number;
  safe_daytrade_buying_power: number;
  gfv_risk_flag: boolean;
  buying_power: BuyingPowerBreakdown | null;
}

export interface BuyingPowerResponse {
  env: EnvName;
  available: boolean;
  data_source: DataSource;
  buying_power: BuyingPowerBreakdown | null;
}

export type Weekday = 'MONDAY' | 'TUESDAY' | 'WEDNESDAY' | 'THURSDAY' | 'FRIDAY';

export interface LiquidityPolicy {
  inflow: {
    enabled: boolean;
    amount: number;
    weekday: Weekday;
    lag_business_days: number;
    confidence: number;
    horizon_business_days: number;
    offset_cap_fraction: number;
  };
  soft_reserve: {
    pct_nlv: number;
    floor_usd: number;
    max_draw_fraction: number;
    min_cash_buffer: number;
  };
  sweep: {
    symbol: string;
    mode: 'ADVISORY' | 'OFF' | string;
    min_idle_usd: number;
    trading_float_pct_nlv: number;
    redemption_lead_business_days: number;
  };
  risk_gate: {
    single_ticker_cap_pct: number;
    high_prob_posterior_min: number;
    high_prob_regime: string;
  };
}

export interface RegimeSummary {
  headline: string;
  bias: 'bullish' | 'bearish' | 'neutral' | string;
  bias_label: string;
  regime: string;
  regime_label: string;
  regime_description: string;
  volatility_label: string;
  volatility_multiplier: number | null;
  as_of: string | null;
  age_hours: number | null;
  stale: boolean;
  engine_regimes: Array<{ symbol: string; regime: string; label?: string }>;
  advisory_note: string;
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
  risk_notional?: number | null;
  simulated?: boolean;
}

export interface MacroBlackout {
  is_blackout: boolean;
  reason?: string;
  next_event?: string;
  event_time?: string;
}

export type DocumentClass =
  | 'CREDIT_REPORT'
  | 'BANK_STATEMENT'
  | 'CREDIT_CARD_STATEMENT'
  | 'PAYSTUB'
  | 'STUDENT_LOAN_STATEMENT'
  | 'TAX_DOCUMENT'
  | 'MISCELLANEOUS_FINANCIAL';

export interface DocumentLineItem {
  account_name: string;
  masked_account_number?: string;
  credit_limit?: number;
  current_balance?: number;
  monthly_payment?: number;
  date_opened?: string;
  last_reported?: string;
  is_promotional: boolean;
  promotional_expiration?: string;
  notes?: string;
}

export interface UnifiedDocumentSnapshot {
  document_class: DocumentClass;
  institution_or_bureau: string;
  report_date: string;
  total_revolving_limit?: number;
  total_revolving_balance?: number;
  aggregate_utilization_pct?: number;
  hard_inquiries_count?: number;
  line_items: DocumentLineItem[];
  detected_discrepancies: string[];
}

export interface LiquidityTarget {
  target_id: string;
  label: string;
  target_amount: number;
  target_date: string;
  is_active: boolean;
  created_at?: string;
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
}

export interface MacroLiquidityStateResponse {
  success: boolean;
  state: CollateralInvariantState;
  promotional_debts: PromotionalDebt[];
  liquidity_targets: LiquidityTarget[];
  snapshot: UnifiedDocumentSnapshot | null;
  external_liquid_backstop: number;
  file_source: string | null;
  updated_at: string | null;
  sha256: string | null;
  _is_simulated?: boolean;
}

export type DocumentJobState = 'QUEUED' | 'PROCESSING' | 'DONE' | 'DUPLICATE' | 'FAILED';

export interface DocumentJobStatus {
  saved_as: string;
  status: DocumentJobState;
  document_class?: string;
  error?: string;
}

export interface ActionResult {
  success: boolean;
  message: string;
}

export interface MicrostructureSymbolView {
  mlofi: number | null;
  mlofi_1s: number | null;
  mlofi_ready: boolean;
  vpin: number | null;
  vpin_percentile: number | null;
  vpin_ready: boolean;
  vpin_toxic: boolean;
  lead_lag_bias: number | null;
  lead_available: boolean;
  depth_levels: number;
  depth_source: string;
  gate: string | null;
  gate_code: string | null;
}

export interface MicrostructureTelemetry {
  enabled: boolean;
  mode?: 'shadow' | 'enforce';
  ready?: boolean;
  symbols?: Record<string, MicrostructureSymbolView>;
}
