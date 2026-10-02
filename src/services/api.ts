import { EngineStatus, PositionsResponse, LedgerSnapshot, StrategyConfig, TradeOrder } from '../types';

const API_BASE = import.meta.env.VITE_ENGINE_BASE_URL || '/api';

// Fallback mock data when running in standalone preview or AI Studio Build Mode
const MOCK_STATUS: EngineStatus = {
  status: 'ONLINE',
  auth_status: 'AUTHORIZED',
  uptime_seconds: 14250,
  active_positions: 3,
  today_pnl: 1420.50,
  external_positions_detected: true,
  timestamp_edt: new Date().toISOString(),
};

const MOCK_POSITIONS: PositionsResponse = {
  managed: [
    {
      symbol: 'SPY',
      quantity: 50,
      entry_price: 572.40,
      current_price: 575.80,
      stop_price: 570.10,
      target_price: 579.50,
      unrealized_pnl: 170.00,
      regime: 'A',
      managed: true,
    },
    {
      symbol: 'QQQ',
      quantity: 40,
      entry_price: 489.15,
      current_price: 494.30,
      stop_price: 486.20,
      target_price: 502.00,
      unrealized_pnl: 206.00,
      regime: 'A',
      managed: true,
    },
    {
      symbol: 'IWM',
      quantity: 120,
      entry_price: 218.60,
      current_price: 221.45,
      stop_price: 216.50,
      target_price: 225.00,
      unrealized_pnl: 342.00,
      regime: 'C',
      managed: true,
    },
  ],
  unmanaged: [
    {
      symbol: 'VTI',
      quantity: 200,
      avg_cost: 260.00,
      entry_price: 260.00,
      current_price: 275.20,
      unrealized_pnl: 3040.00,
      managed: false,
      stop_order_id: 'SW-STOP-9921',
    },
    {
      symbol: 'TLT',
      quantity: 100,
      avg_cost: 92.50,
      entry_price: 92.50,
      current_price: 91.80,
      unrealized_pnl: -70.00,
      managed: false,
    },
  ],
};

const MOCK_LEDGER: LedgerSnapshot = {
  bucket1_settled: 85200.00,
  bucket2_unsettled: 14800.00,
  bucket3_pending: 5000.00,
  max_order_value: 12500.00,
};

const MOCK_ORDERS: TradeOrder[] = [
  {
    symbol: 'SPY',
    side: 'BUY',
    quantity: 50,
    price: 572.40,
    cost: 28620.00,
    timestamp: new Date(Date.now() - 3600000).toISOString(),
    status: 'FILLED',
  },
  {
    symbol: 'QQQ',
    side: 'BUY',
    quantity: 40,
    price: 489.15,
    cost: 19566.00,
    timestamp: new Date(Date.now() - 7200000).toISOString(),
    status: 'FILLED',
  },
];

export async function fetchStatus(): Promise<EngineStatus> {
  try {
    const res = await fetch(`${API_BASE}/status`);
    if (!res.ok) throw new Error('API offline');
    return await res.json();
  } catch {
    return MOCK_STATUS;
  }
}

export async function fetchAllPositions(): Promise<PositionsResponse> {
  try {
    const res = await fetch(`${API_BASE}/positions/all`);
    if (!res.ok) throw new Error('API offline');
    return await res.json();
  } catch {
    return MOCK_POSITIONS;
  }
}

export async function fetchLedger(): Promise<LedgerSnapshot> {
  try {
    const res = await fetch(`${API_BASE}/ledger`);
    if (!res.ok) throw new Error('API offline');
    return await res.json();
  } catch {
    return MOCK_LEDGER;
  }
}

export async function fetchOrders(): Promise<TradeOrder[]> {
  try {
    const res = await fetch(`${API_BASE}/orders`);
    if (!res.ok) throw new Error('API offline');
    return await res.json();
  } catch {
    return MOCK_ORDERS;
  }
}

export async function exchangeOAuthCode(code: string): Promise<{ success: boolean; message: string }> {
  try {
    const res = await fetch(`${API_BASE}/auth/exchange`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code }),
    });
    return await res.json();
  } catch (err: any) {
    return { success: false, message: err.message || 'OAuth exchange failed' };
  }
}

export async function toggleStrategy(name: string, enabled: boolean): Promise<any> {
  try {
    const res = await fetch(`${API_BASE}/strategy/${name}/toggle`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    });
    return await res.json();
  } catch {
    return { success: true, strategy: name, enabled };
  }
}

export async function updateStrategyConfig(config: Partial<StrategyConfig>): Promise<any> {
  try {
    const res = await fetch(`${API_BASE}/strategy/config`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config),
    });
    return await res.json();
  } catch {
    return { success: true, updated: config };
  }
}

export async function triggerPortfolioScan(): Promise<any> {
  try {
    const res = await fetch(`${API_BASE}/portfolio/scan`, {
      method: 'POST',
    });
    return await res.json();
  } catch {
    return { success: true, message: 'Portfolio scan simulated' };
  }
}
