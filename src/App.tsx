import { useState, useEffect, useCallback } from 'react';
import { 
  TrendingUp, 
  TrendingDown, 
  Layers, 
  ShieldAlert, 
  Wallet, 
  CheckCircle2, 
  Clock 
} from 'lucide-react';
import { EngineStatus, PositionsResponse, LedgerSnapshot, TradeOrder } from './types';
import { fetchStatus, fetchAllPositions, fetchLedger, fetchOrders } from './services/api';
import { DashboardLayout } from './components/DashboardLayout';
import { SchwabControls } from './components/SchwabControls';
import { EtfPositionTracker } from './components/EtfPositionTracker';
import { LedgerCard } from './components/LedgerCard';

export function App() {
  const [activeTab, setActiveTab] = useState<'overview' | 'positions' | 'controls' | 'ledger'>('overview');
  const [isRefreshing, setIsRefreshing] = useState(false);

  const [status, setStatus] = useState<EngineStatus>({
    status: 'ONLINE',
    auth_status: 'AUTHORIZED',
    uptime_seconds: 14250,
    active_positions: 3,
    today_pnl: 1420.50,
    external_positions_detected: true,
    timestamp_edt: new Date().toISOString(),
  });

  const [positions, setPositions] = useState<PositionsResponse>({
    managed: [],
    unmanaged: [],
  });

  const [ledger, setLedger] = useState<LedgerSnapshot>({
    bucket1_settled: 85200.0,
    bucket2_unsettled: 14800.0,
    bucket3_pending: 5000.0,
    max_order_value: 12500.0,
  });

  const [orders, setOrders] = useState<TradeOrder[]>([]);

  const loadData = useCallback(async () => {
    setIsRefreshing(true);
    try {
      const [s, p, l, o] = await Promise.all([
        fetchStatus(),
        fetchAllPositions(),
        fetchLedger(),
        fetchOrders(),
      ]);
      setStatus(s);
      setPositions(p);
      setLedger(l);
      setOrders(o);
    } catch (err) {
      console.error('Failed to update dashboard data', err);
    } finally {
      setIsRefreshing(false);
    }
  }, []);

  useEffect(() => {
    loadData();
    const interval = setInterval(loadData, 10000); // 10s auto-refresh
    return () => clearInterval(interval);
  }, [loadData]);


  return (
    <DashboardLayout
      status={status}
      onRefresh={loadData}
      isRefreshing={isRefreshing}
      activeTab={activeTab}
      setActiveTab={setActiveTab}
    >
      <div className="space-y-6">
        {/* Metric Cards Banner */}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          {/* Today's PnL */}
          <div className="bg-[#111827] border border-gray-800 rounded-xl p-4 shadow">
            <div className="flex items-center justify-between text-gray-400 text-xs mb-1">
              <span>Today's Realized P&L</span>
              <div className={`p-1.5 rounded-md ${status.today_pnl >= 0 ? 'bg-emerald-500/10 text-emerald-400' : 'bg-rose-500/10 text-rose-400'}`}>
                {status.today_pnl >= 0 ? <TrendingUp className="w-4 h-4" /> : <TrendingDown className="w-4 h-4" />}
              </div>
            </div>
            <div className={`text-2xl font-bold font-mono ${status.today_pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
              {status.today_pnl >= 0 ? '+' : ''}${status.today_pnl.toLocaleString('en-US', { minimumFractionDigits: 2 })}
            </div>
            <span className="text-[10px] text-gray-500 font-mono">Net closed trade performance</span>
          </div>

          {/* Managed Positions */}
          <div className="bg-[#111827] border border-gray-800 rounded-xl p-4 shadow">
            <div className="flex items-center justify-between text-gray-400 text-xs mb-1">
              <span>Engine Active ETF Positions</span>
              <div className="p-1.5 rounded-md bg-cyan-500/10 text-cyan-400">
                <Layers className="w-4 h-4" />
              </div>
            </div>
            <div className="text-2xl font-bold font-mono text-cyan-300">
              {positions.managed.length}
            </div>
            <span className="text-[10px] text-gray-500 font-mono">
              Dual-tier stop & target trailing active
            </span>
          </div>

          {/* Reconciliation & External Holdings */}
          <div className="bg-[#111827] border border-gray-800 rounded-xl p-4 shadow">
            <div className="flex items-center justify-between text-gray-400 text-xs mb-1">
              <span>Reconciled Schwab Holdings</span>
              <div className="p-1.5 rounded-md bg-amber-500/10 text-amber-400">
                <ShieldAlert className="w-4 h-4" />
              </div>
            </div>
            <div className="text-2xl font-bold font-mono text-amber-300">
              {positions.unmanaged.length}
            </div>
            <span className="text-[10px] text-gray-500 font-mono">
              {status.external_positions_detected ? 'External holdings synchronized' : 'None detected'}
            </span>
          </div>

          {/* Settled Cash */}
          <div className="bg-[#111827] border border-gray-800 rounded-xl p-4 shadow">
            <div className="flex items-center justify-between text-gray-400 text-xs mb-1">
              <span>Available Settled Buying Power</span>
              <div className="p-1.5 rounded-md bg-emerald-500/10 text-emerald-400">
                <Wallet className="w-4 h-4" />
              </div>
            </div>
            <div className="text-2xl font-bold font-mono text-emerald-300">
              ${ledger.bucket1_settled.toLocaleString('en-US', { minimumFractionDigits: 2 })}
            </div>
            <span className="text-[10px] text-gray-500 font-mono">
              GFV-safe Bucket 1 capital
            </span>
          </div>
        </div>

        {/* Tab 1: Overview */}
        {activeTab === 'overview' && (
          <div className="space-y-6">
            <SchwabControls status={status} onRefresh={loadData} />
            <EtfPositionTracker positions={positions} onRefresh={loadData} />
            <LedgerCard ledger={ledger} />

            {/* Recent Execution Log */}
            <div className="bg-[#111827] border border-gray-800 rounded-xl p-5 shadow-lg shadow-black/40">
              <div className="flex items-center justify-between pb-3 border-b border-gray-800 mb-4">
                <h3 className="text-sm font-semibold text-gray-100 flex items-center">
                  <Clock className="w-4 h-4 mr-2 text-cyan-400" />
                  Recent Execution Audit Log
                </h3>
                <span className="text-xs text-gray-400 font-mono">{orders.length} events today</span>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-xs text-left">
                  <thead className="bg-[#0b1120] text-gray-400 uppercase tracking-wider font-mono">
                    <tr>
                      <th className="py-2.5 px-3">Timestamp</th>
                      <th className="py-2.5 px-3">Symbol</th>
                      <th className="py-2.5 px-3">Action</th>
                      <th className="py-2.5 px-3 text-right">Shares</th>
                      <th className="py-2.5 px-3 text-right">Execution Price</th>
                      <th className="py-2.5 px-3 text-right">Notional</th>
                      <th className="py-2.5 px-3 text-center">Status</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-800 font-mono">
                    {orders.length === 0 ? (
                      <tr>
                        <td colSpan={7} className="py-6 text-center text-gray-500 font-sans">
                          No order fills recorded today.
                        </td>
                      </tr>
                    ) : (
                      orders.map((o, idx) => (
                        <tr key={idx} className="hover:bg-gray-800/30">
                          <td className="py-2.5 px-3 text-gray-400">
                            {new Date(o.timestamp).toLocaleTimeString()}
                          </td>
                          <td className="py-2.5 px-3 font-bold text-gray-200">{o.symbol}</td>
                          <td className="py-2.5 px-3">
                            <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${
                              o.side === 'BUY' ? 'bg-emerald-950 text-emerald-300 border border-emerald-800' : 'bg-rose-950 text-rose-300 border border-rose-800'
                            }`}>
                              {o.side}
                            </span>
                          </td>
                          <td className="py-2.5 px-3 text-right">{o.quantity}</td>
                          <td className="py-2.5 px-3 text-right">${o.price.toFixed(2)}</td>
                          <td className="py-2.5 px-3 text-right font-semibold">${o.cost.toLocaleString('en-US', { minimumFractionDigits: 2 })}</td>
                          <td className="py-2.5 px-3 text-center">
                            <span className="inline-flex items-center text-[10px] text-emerald-400">
                              <CheckCircle2 className="w-3 h-3 mr-1" /> {o.status}
                            </span>
                          </td>
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        )}

        {/* Tab 2: Positions & Guard */}
        {activeTab === 'positions' && (
          <div className="space-y-6">
            <EtfPositionTracker positions={positions} onRefresh={loadData} />
            <div className="p-4 bg-gray-900/60 border border-gray-800 rounded-xl text-xs text-gray-400 space-y-2">
              <h4 className="font-semibold text-gray-200">Reconciliation & Dual-Tier Stop Architecture</h4>
              <p>
                The engine automatically distinguishes between positions managed by engine strategy routines (SPY, QQQ, IWM) and unmanaged manual positions held in the underlying Schwab account.
              </p>
              <ul className="list-disc pl-5 space-y-1 text-gray-300">
                <li>Hard initial stops are computed at entry based on NATR (Normalized Average True Range).</li>
                <li>Dynamic trailing stops trail high-water marks and ratchet upward as prices reach intermediate targets.</li>
                <li>External positions are protected with Schwab-side stops when unmanaged risk is detected.</li>
              </ul>
            </div>
          </div>
        )}

        {/* Tab 3: Controls & AI */}
        {activeTab === 'controls' && (
          <div className="space-y-6">
            <SchwabControls status={status} onRefresh={loadData} />
          </div>
        )}

        {/* Tab 4: Ledger & GFV Prevention */}
        {activeTab === 'ledger' && (
          <div className="space-y-6">
            <LedgerCard ledger={ledger} />
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              <div className="p-4 bg-[#111827] border border-gray-800 rounded-xl">
                <h4 className="font-semibold text-xs text-emerald-400 mb-1">Bucket 1: Settled Funds</h4>
                <p className="text-xs text-gray-400">
                  Fully cleared cash available for any day-trade or swing-trade without risking Good Faith Violations.
                </p>
              </div>
              <div className="p-4 bg-[#111827] border border-gray-800 rounded-xl">
                <h4 className="font-semibold text-xs text-cyan-400 mb-1">Bucket 2: Unsettled T+1</h4>
                <p className="text-xs text-gray-400">
                  Proceeds from positions sold today. Under SEC T+1 rules, these clear the next trading morning.
                </p>
              </div>
              <div className="p-4 bg-[#111827] border border-gray-800 rounded-xl">
                <h4 className="font-semibold text-xs text-amber-400 mb-1">Bucket 3: Pending Transfers</h4>
                <p className="text-xs text-gray-400">
                  ACH or wire deposits currently in transit. Kept segregated from active risk sizing until final settlement.
                </p>
              </div>
            </div>
          </div>
        )}
      </div>
    </DashboardLayout>
  );
}

export default App;
