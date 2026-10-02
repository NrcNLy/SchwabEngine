import { defineConfig, Plugin } from 'vite';
import react from '@vitejs/plugin-react';

function mockTradingApiPlugin(): Plugin {
  const engineStatus = {
    status: 'ONLINE',
    auth_status: 'AUTHORIZED',
    uptime_seconds: 14250,
    active_positions: 3,
    today_pnl: 1420.50,
    external_positions_detected: true,
    timestamp_edt: new Date().toISOString(),
  };

  const positions = {
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

  const ledger = {
    bucket1_settled: 85200.00,
    bucket2_unsettled: 14800.00,
    bucket3_pending: 5000.00,
    max_order_value: 12500.00,
  };

  const orders = [
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
