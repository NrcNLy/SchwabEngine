import { defineConfig, Plugin } from 'vite';
import react from '@vitejs/plugin-react';

function mockTradingApiPlugin(): Plugin {
  const engineStatus = {
    status: 'ONLINE',
    auth_status: 'AUTHORIZED',
    uptime_seconds: 14250,
    active_positions: 3,
    today_pnl: 14.20,
    external_positions_detected: true,
    timestamp_edt: new Date().toISOString(),
    circuit_breaker_limit: -30.00,
    circuit_breaker_triggered: false,
    is_connected: true,
    engine_mode: 'SANDBOX_SIMULATION',
  };

  const positions = {
    managed: [
      {
        symbol: 'SOXL',
        quantity: 1,
        entry_price: 165.88,
        current_price: 167.25,
        notional_value: 167.25,
        exposure_pct: 16.72,
        max_exposure_cap: 200.00,
        stop_price: 164.80,
        target_price: 172.20,
        unrealized_pnl: 1.37,
        regime: 'A',
        managed: true,
      },
      {
        symbol: 'TQQQ',
        quantity: 2,
        entry_price: 80.96,
        current_price: 84.10,
        notional_value: 168.20,
        exposure_pct: 16.82,
        max_exposure_cap: 200.00,
        stop_price: 83.10,
        target_price: 86.50,
        unrealized_pnl: 6.28,
        regime: 'A',
        managed: true,
      },
      {
        symbol: 'TNA',
        quantity: 3,
        entry_price: 59.84,
        current_price: 60.10,
        notional_value: 180.30,
        exposure_pct: 18.03,
        max_exposure_cap: 200.00,
        stop_price: 58.80,
        target_price: 62.00,
        unrealized_pnl: 0.78,
        regime: 'C',
        managed: true,
      },
    ],
    unmanaged: [],
  };

  const ledger = {
    bucket1_settled: 720.00,
    bucket2_unsettled: 240.00,
    bucket3_pending: 40.00,
    max_order_value: 200.00,
    total_equity: 1000.00,
  };

  const orders = [
    {
      symbol: 'SOXL',
      side: 'BUY',
      quantity: 5,
      price: 36.40,
      cost: 182.00,
      timestamp: new Date(Date.now() - 3600000).toISOString(),
      status: 'FILLED',
    },
    {
      symbol: 'TQQQ',
      side: 'BUY',
      quantity: 2,
      price: 82.50,
      cost: 165.00,
      timestamp: new Date(Date.now() - 7200000).toISOString(),
      status: 'FILLED',
    },
    {
      symbol: 'TNA',
      side: 'BUY',
      quantity: 4,
      price: 44.10,
      cost: 176.40,
      timestamp: new Date(Date.now() - 10800000).toISOString(),
      status: 'FILLED',
    },
  ];

  return {
    name: 'mock-trading-api',
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const url = req.url?.split('?')[0] || '';
        if (!url.startsWith('/api')) {
          return next();
        }

        res.setHeader('Content-Type', 'application/json');

        if (req.method === 'GET' && url === '/api/status') {
          engineStatus.uptime_seconds += 10;
          engineStatus.timestamp_edt = new Date().toISOString();
          res.end(JSON.stringify(engineStatus));
          return;
        }

        if (req.method === 'GET' && (url === '/api/positions/all' || url === '/api/positions')) {
          res.end(JSON.stringify(positions));
          return;
        }

        if (req.method === 'GET' && url === '/api/ledger') {
          res.end(JSON.stringify(ledger));
          return;
        }

        if (req.method === 'GET' && url === '/api/orders') {
          res.end(JSON.stringify(orders));
          return;
        }

        if (req.method === 'POST' && url === '/api/auth/exchange') {
          engineStatus.auth_status = 'AUTHORIZED';
          res.end(JSON.stringify({ success: true, message: 'OAuth token vaulted successfully' }));
          return;
        }

        if (req.method === 'POST' && url.startsWith('/api/strategy/')) {
          res.end(JSON.stringify({ success: true }));
          return;
        }

        if (req.method === 'POST' && url === '/api/portfolio/scan') {
          res.end(JSON.stringify({ success: true, recommendations_generated: 4 }));
          return;
        }

        res.statusCode = 404;
        res.end(JSON.stringify({ error: 'Endpoint not found' }));
      });
    },
  };
}

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react(), mockTradingApiPlugin()],
  server: {
    host: '0.0.0.0',
    port: 3000,
  },
  preview: {
    host: '0.0.0.0',
    port: 3000,
  },
});
