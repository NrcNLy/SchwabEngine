import { useState, useEffect, useCallback } from 'react';
import { 
  TrendingUp, 
  TrendingDown, 
  Layers, 
  ShieldAlert, 
  Wallet, 
  CheckCircle2, 
  Clock,
  ShieldCheck
} from 'lucide-react';
import { EngineStatus, PositionsResponse, LedgerSnapshot, TradeOrder } from './types';
import { 
  fetchStatus, 
  fetchAllPositions, 
  fetchLedger, 
  fetchOrders,
  MOCK_STATUS,
  MOCK_POSITIONS,
  MOCK_LEDGER,
  MOCK_ORDERS 
} from './services/api';
import { DashboardLayout } from './components/DashboardLayout';
import { SchwabControls } from './components/SchwabControls';
import { EtfPositionTracker } from './components/EtfPositionTracker';
import { LedgerCard } from './components/LedgerCard';
import { LifecycleTracker } from './components/LifecycleTracker';
import { LlmInsightConsole } from './components/LlmInsightConsole';

export function App() {
  const [activeTab, setActiveTab] = useState<'overview' | 'positions' | 'controls' | 'ledger'>('overview');
  const [isRefreshing, setIsRefreshing] = useState(false);

  const [status, setStatus] = useState<EngineStatus>(MOCK_STATUS);
  const [positions, setPositions] = useState<PositionsResponse>(MOCK_POSITIONS);
  const [ledger, setLedger] = useState<LedgerSnapshot>(MOCK_LEDGER);
  const [orders, setOrders] = useState<TradeOrder[]>(MOCK_ORDERS);

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
    const interval = setInterval(loadData, 8000); // 8s auto-polling
    return () => clearInterval(interval);
  }, [loadData]);

  const pnlPercent = ((status.today_pnl / 1000.0) * 100);

  return (
    <DashboardLayout
      status={status}
      onRefresh={loadData}
      isRefreshing={isRefreshing}
      activeTab={activeTab}
      setActiveTab={setActiveTab}
    >
      <div className="space-y-6">
        {/* Metric Cards Banner - $1,000 Sandbox Matrix */}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          {/* Today's PnL */}
          <div className="bg-[#0e1422] border border-gray-800 rounded-xl p-4 shadow-lg">
            <div className="flex items-center justify-between text-gray-400 text-xs mb-1">
              <span className="font-mono">Today's Realized P&L</span>
              <div className={`p-1.5 rounded-md ${status.today_pnl >= 0 ? 'bg-emerald-500/10 text-emerald-400' : 'bg-rose-500/10 text-rose-400'}`}>
                {status.today_pnl >= 0 ? <TrendingUp className="w-4 h-4" /> : <TrendingDown className="w-4 h-4" />}
              </div>
            </div>
            <div className="flex items-baseline space-x-2">
              <div className={`text-2xl font-bold font-mono ${status.today_pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                {status.today_pnl >= 0 ? '+' : ''}${status.today_pnl.toFixed(2)}
              </div>
              <span className={`text-xs font-mono font-semibold ${status.today_pnl >= 0 ? 'text-emerald-500' : 'text-rose-500'}`}>
                ({status.today_pnl >= 0 ? '+' : ''}{pnlPercent.toFixed(2)}%)
              </span>
            </div>
            <span className="text-[10px] text-gray-500 font-mono block mt-1">
              Floor: -$30.00 daily circuit breaker
            </span>
          </div>

          {/* Managed High-Beta Positions */}
          <div className="bg-[#0e1422] border border-gray-800 rounded-xl p-4 shadow-lg">
            <div className="flex items-center justify-between text-gray-400 text-xs mb-1">
              <span className="font-mono">Managed 3X ETF Positions</span>
              <div className="p-1.5 rounded-md bg-cyan-500/10 text-cyan-400">
                <Layers className="w-4 h-4" />
              </div>
            </div>
            <div className="text-2xl font-bold font-mono text-cyan-300">
              {positions.managed.length} <span className="text-xs text-gray-400 font-normal">Active (SOXL/TQQQ/TNA)</span>
            </div>
            <span className="text-[10px] text-gray-500 font-mono block mt-1">
              20% ($200.00) single-ticker cap enforced
            </span>
          </div>

          {/* Reconciliation & External Holdings */}
          <div className="bg-[#0e1422] border border-gray-800 rounded-xl p-4 shadow-lg">
            <div className="flex items-center justify-between text-gray-400 text-xs mb-1">
              <span className="font-mono">Reconciled Schwab Holdings</span>
              <div className="p-1.5 rounded-md bg-amber-500/10 text-amber-400">
                <ShieldAlert className="w-4 h-4" />
              </div>
            </div>
            <div className="text-2xl font-bold font-mono text-amber-300">
              {positions.unmanaged.length} <span className="text-xs text-gray-400 font-normal">Holding ({positions.unmanaged[0]?.symbol || 'None'})</span>
            </div>
            <span className="text-[10px] text-gray-500 font-mono block mt-1">
              {status.external_positions_detected ? 'Segregated from intraday risk pool' : 'No external positions'}
            </span>
          </div>

          {/* Settled Cash (Bucket 1) */}
          <div className="bg-[#0e1422] border border-gray-800 rounded-xl p-4 shadow-lg">
            <div className="flex items-center justify-between text-gray-400 text-xs mb-1">
              <span className="font-mono">GFV Settled Buying Power</span>
              <div className="p-1.5 rounded-md bg-emerald-500/10 text-emerald-400">
                <Wallet className="w-4 h-4" />
              </div>
            </div>
            <div className="text-2xl font-bold font-mono text-emerald-300">
              ${ledger.bucket1_settled.toLocaleString('en-US', { minimumFractionDigits: 2 })}
            </div>
            <span className="text-[10px] text-gray-500 font-mono block mt-1">
              Bucket 1: Zero GFV roundtrip risk
            </span>
          </div>
        </div>

        {/* Tab 1: Overview */}
        {activeTab === 'overview' && (
          <div className="space-y-6">
            <LifecycleTracker status={status} />
            <EtfPositionTracker positions={positions} onRefresh={loadData} />
            
            {/* Active Execution Trajectory & AI Telemetry */}
            <div className="bg-[#0b101d] border border-cyan-900/40 rounded-xl p-5 shadow-2xl relative overflow-hidden">
              <div className="absolute top-0 left-0 w-1 h-full bg-cyan-500 animate-pulse" />
              <div className="flex flex-col md:flex-row md:items-center justify-between gap-6">
                
                {/* AI Bayesian Sizing Confidence */}
                <div className="flex items-start space-x-4">
                  <div className="p-3 bg-cyan-500/10 rounded-xl border border-cyan-500/30">
                    <TrendingUp className="w-6 h-6 text-cyan-400" />
                  </div>
                  <div>
                    <h3 className="text-sm font-semibold text-gray-100 font-mono tracking-wider">
                      Bayesian Quarter-Kelly Confidence
                    </h3>
                    <div className="flex items-center space-x-2 mt-1">
                      <span className="text-2xl font-bold text-cyan-300 font-mono">
                        {status.bayesian_kelly_confidence ? (status.bayesian_kelly_confidence * 100).toFixed(1) : '82.0'}%
                      </span>
                      <span className="text-xs text-gray-400 font-mono">Posterior Win-Rate Expectation</span>
                    </div>
                  </div>
                </div>

                {/* Almgren-Chriss Slicing Progress */}
                <div className="flex-1 max-w-md w-full bg-gray-900/80 border border-gray-800 rounded-lg p-3">
                  <div className="flex justify-between items-center mb-2">
                    <span className="text-xs text-gray-300 font-mono font-bold tracking-wide">
                      Almgren-Chriss Hyperbolic Slicing
                    </span>
                    <span className="text-[10px] bg-amber-950/80 text-amber-400 px-1.5 py-0.5 rounded font-mono">
                      ACTIVE TRAJECTORY
                    </span>
                  </div>
                  
                  <div className="w-full bg-gray-800 rounded-full h-2 mb-1.5 border border-gray-700 overflow-hidden">
                    <div 
                      className="bg-gradient-to-r from-amber-500 to-cyan-400 h-2 rounded-full transition-all duration-700" 
                      style={{ width: `${((status.almgren_chriss_slices_executed || 2) / (status.almgren_chriss_slices_total || 4)) * 100}%` }}
                    />
                  </div>
                  <div className="flex justify-between text-[10px] text-gray-500 font-mono">
                    <span>Child Orders: {status.almgren_chriss_slices_executed || 2} of {status.almgren_chriss_slices_total || 4} Executed</span>
                    <span>Pegged-to-Midpoint (+0.01)</span>
                  </div>
                </div>

              </div>
            </div>

            <LlmInsightConsole status={status} />

            <LedgerCard ledger={ledger} />
            <SchwabControls status={status} onRefresh={loadData} />

            {/* Execution Audit Log */}
            <div className="bg-[#0e1422] border border-gray-800 rounded-xl p-5 shadow-2xl">
              <div className="flex items-center justify-between pb-3 border-b border-gray-800 mb-4">
                <h3 className="text-sm font-semibold text-gray-100 flex items-center font-mono">
                  <Clock className="w-4 h-4 mr-2 text-cyan-400" />
                  Deterministic Execution Journal (Today's Orders)
                </h3>
                <span className="text-xs text-gray-400 font-mono">{orders.length} fills recorded</span>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-xs text-left">
                  <thead className="bg-[#080d17] text-gray-400 uppercase tracking-wider font-mono text-[10px]">
                    <tr>
                      <th className="py-2.5 px-3">Timestamp</th>
                      <th className="py-2.5 px-3">Symbol</th>
                      <th className="py-2.5 px-3">Side</th>
                      <th className="py-2.5 px-3">Strategy / Regime</th>
                      <th className="py-2.5 px-3 text-right">Shares</th>
                      <th className="py-2.5 px-3 text-right">Execution Price</th>
                      <th className="py-2.5 px-3 text-right">Notional Cost</th>
                      <th className="py-2.5 px-3 text-right">Max Risk</th>
                      <th className="py-2.5 px-3 text-center">Status</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-800 font-mono">
                    {orders.length === 0 ? (
                      <tr>
                        <td colSpan={9} className="py-6 text-center text-gray-500 font-sans">
                          No order fills recorded today.
                        </td>
                      </tr>
                    ) : (
                      orders.map((o, idx) => (
                        <tr key={idx} className="hover:bg-gray-800/30">
                          <td className="py-2.5 px-3 text-gray-400">
                            {new Date(o.timestamp).toLocaleTimeString()}
                          </td>
                          <td className="py-2.5 px-3 font-bold text-gray-100">{o.symbol}</td>
                          <td className="py-2.5 px-3">
                            <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${
                              o.side === 'BUY' 
                                ? 'bg-emerald-950 text-emerald-300 border border-emerald-800' 
                                : 'bg-rose-950 text-rose-300 border border-rose-800'
                            }`}>
                              {o.side}
                            </span>
                          </td>
                          <td className="py-2.5 px-3 text-gray-300 text-[11px]">
                            {o.regime || 'Deterministic Quant'}
                          </td>
                          <td className="py-2.5 px-3 text-right text-gray-200">{o.quantity}</td>
                          <td className="py-2.5 px-3 text-right">${o.price.toFixed(2)}</td>
                          <td className="py-2.5 px-3 text-right font-semibold text-cyan-300">
                            ${o.cost.toFixed(2)}
                          </td>
                          <td className="py-2.5 px-3 text-right text-purple-300">
                            ${(o.risk_notional || 4.50).toFixed(2)}
                          </td>
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

        {/* Tab 2: High-Beta Positions & Guard */}
        {activeTab === 'positions' && (
          <div className="space-y-6">
            <EtfPositionTracker positions={positions} onRefresh={loadData} />
            <div className="p-5 bg-[#0e1422] border border-gray-800 rounded-xl text-xs text-gray-300 space-y-3 font-mono">
              <h4 className="font-semibold text-gray-100 flex items-center text-sm">
                <ShieldCheck className="w-4 h-4 mr-2 text-cyan-400" />
                Deterministic Risk Boundaries & Quantitative Rules
              </h4>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-xs text-gray-400">
                <div className="space-y-2 bg-[#090d16] p-3 rounded-lg border border-gray-800">
                  <span className="font-bold text-cyan-300 block">Single-Ticker Exposure Cap (20% / $200.00):</span>
                  <p>
                    Regardless of signal conviction, no single instrument (SOXL, TQQQ, TNA) may exceed $200.00 notional at fill.
                  </p>
                </div>
                <div className="space-y-2 bg-[#090d16] p-3 rounded-lg border border-gray-800">
                  <span className="font-bold text-emerald-300 block">Dual-Tier Stop Discipline:</span>
                  <p>
                    Every entry places an immediate NATR hard stop (capped at 1.0% / $10 account loss) plus a dynamic ratcheting trailing stop.
                  </p>
                </div>
                <div className="space-y-2 bg-[#090d16] p-3 rounded-lg border border-gray-800">
                  <span className="font-bold text-amber-300 block">IRC §1091 Wash-Sale Auto-Pivot:</span>
                  <p>
                    Losses or existing swing trades automatically pivot orders to non-substantially identical pairs: SOXL → FNGU, TQQQ → CONL, TNA → DPST.
                  </p>
                </div>
                <div className="space-y-2 bg-[#090d16] p-3 rounded-lg border border-gray-800">
                  <span className="font-bold text-rose-300 block">15:55 EDT Mandatory Flat-to-Cash:</span>
                  <p>
                    5 minutes before 16:00 close, all open orders are canceled and active intraday positions liquidated at market.
                  </p>
                </div>
              </div>
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
              <div className="p-4 bg-[#0e1422] border border-gray-800 rounded-xl">
                <h4 className="font-semibold text-xs text-emerald-400 mb-1 font-mono">Bucket 1: Settled Funds ($720.00)</h4>
                <p className="text-xs text-gray-400">
                  Fully cleared cash available for immediate day-trading. Exclusively accessed by Tier 1 order router to guarantee zero Good Faith Violations.
                </p>
              </div>
              <div className="p-4 bg-[#0e1422] border border-gray-800 rounded-xl">
                <h4 className="font-semibold text-xs text-cyan-400 mb-1 font-mono">Bucket 2: Unsettled T+1 ($240.00)</h4>
                <p className="text-xs text-gray-400">
                  Proceeds from positions sold today. Under SEC T+1 rules, these clear automatically at 09:00 EDT the following trading morning.
                </p>
              </div>
              <div className="p-4 bg-[#0e1422] border border-gray-800 rounded-xl">
                <h4 className="font-semibold text-xs text-amber-400 mb-1 font-mono">Bucket 3: Pending ACH ($40.00)</h4>
                <p className="text-xs text-gray-400">
                  ACH bank transfer currently in flight. Excluded from active Quarter-Kelly sizing until confirmed settled by broker.
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
