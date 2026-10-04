import React, { useCallback, useEffect, useRef, useState } from 'react';
import { CheckCircle2, Clock, Layers, ShieldAlert, TrendingDown, TrendingUp, Wallet } from 'lucide-react';
import {
  EngineStatus,
  EnvName,
  LedgerSnapshot,
  LiquidityPolicy,
  MacroLiquidityStateResponse,
  PositionsResponse,
  RegimeSummary,
  TradeOrder,
} from './types';
import {
  fetchAllPositions,
  fetchLedger,
  fetchLiquidityPolicy,
  fetchMacroLiquidityState,
  fetchOrders,
  fetchRegimeSummary,
  fetchStatus,
} from './services/api';
import { DashboardLayout, DashboardTab } from './components/DashboardLayout';
import { SchwabControls } from './components/SchwabControls';
import { EtfPositionTracker } from './components/EtfPositionTracker';
import { LedgerCard } from './components/LedgerCard';
import { LifecycleTracker } from './components/LifecycleTracker';
import { LlmInsightConsole } from './components/LlmInsightConsole';
import { MacroLiquidityCard } from './components/MacroLiquidityCard';
import { pnlTone, signedPct, signedUsd, usd } from './utils/format';

const POLL_INTERVAL_MS = 5000;

const MetricCard: React.FC<{
  label: string;
  value: string;
  valueTone?: string;
  hint?: string;
  icon: React.ReactNode;
}> = ({ label, value, valueTone = 'text-gray-100', hint, icon }) => (
  <div className="bg-[#0e1422] border border-gray-800 rounded-xl p-3 shadow-lg">
    <div className="flex items-center justify-between text-gray-400 text-[11px] mb-1">
      <span className="font-mono">{label}</span>
      {icon}
    </div>
    <div className={`text-xl font-bold font-mono ${valueTone}`}>{value}</div>
    {hint && <span className="text-[10px] text-gray-500 font-mono block mt-0.5">{hint}</span>}
  </div>
);

export function App() {
  const [activeTab, setActiveTab] = useState<DashboardTab>('overview');
  const [isRefreshing, setIsRefreshing] = useState(false);

  /** Operator's explicit choice. Null means "use the engine's default environment". */
  const [userEnv, setUserEnv] = useState<EnvName | null>(null);

  const [status, setStatus] = useState<EngineStatus | null>(null);
  const [engineError, setEngineError] = useState<string | null>(null);
  const [partialErrors, setPartialErrors] = useState<string[]>([]);
  const [positions, setPositions] = useState<PositionsResponse | null>(null);
  const [ledger, setLedger] = useState<LedgerSnapshot | null>(null);
  const [orders, setOrders] = useState<TradeOrder[] | null>(null);
  const [macroState, setMacroState] = useState<MacroLiquidityStateResponse | null>(null);
  const [policy, setPolicy] = useState<LiquidityPolicy | null>(null);
  const [regime, setRegime] = useState<RegimeSummary | null>(null);
  const [regimeFailed, setRegimeFailed] = useState(false);

  const env: EnvName = userEnv ?? status?.env ?? 'active';
  const requestSeq = useRef(0);

  const loadData = useCallback(async () => {
    const seq = ++requestSeq.current;
    setIsRefreshing(true);
    try {
      let s: EngineStatus;
      try {
        s = await fetchStatus(userEnv ?? undefined);
      } catch (err: unknown) {
        if (seq !== requestSeq.current) return;
        setEngineError(err instanceof Error ? err.message : 'Engine unreachable.');
        return;
      }
      if (seq !== requestSeq.current) return;
      const target: EnvName = userEnv ?? s.env;

      const [p, l, o, m, pol, r] = await Promise.allSettled([
        fetchAllPositions(target),
        fetchLedger(target),
        fetchOrders(target),
        fetchMacroLiquidityState(),
        fetchLiquidityPolicy(),
        fetchRegimeSummary(),
      ]);
      if (seq !== requestSeq.current) return;

      const failed: string[] = [];
      setStatus(s);
      setEngineError(null);

      if (p.status === 'fulfilled') setPositions(p.value);
      else {
        setPositions(null);
        failed.push('positions');
      }
      if (l.status === 'fulfilled') setLedger(l.value);
      else {
        setLedger(null);
        failed.push('ledger');
      }
      if (o.status === 'fulfilled') setOrders(o.value);
      else {
        setOrders(null);
        failed.push('orders');
      }
      if (m.status === 'fulfilled') setMacroState(m.value);
      else {
        setMacroState(null);
        failed.push('document intelligence');
      }
      if (pol.status === 'fulfilled') setPolicy(pol.value);
      else failed.push('liquidity policy');
      if (r.status === 'fulfilled') {
        setRegime(r.value);
        setRegimeFailed(false);
      } else {
        setRegime(null);
        setRegimeFailed(true);
      }
      setPartialErrors(failed);
    } finally {
      if (seq === requestSeq.current) setIsRefreshing(false);
    }
  }, [userEnv]);

  useEffect(() => {
    void loadData();
    const interval = setInterval(() => void loadData(), POLL_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [loadData]);

  const handleEnvChange = (next: EnvName) => {
    if (next === env && userEnv === next) return;
    setPositions(null);
    setLedger(null);
    setOrders(null);
    setUserEnv(next);
  };

  const nlv = status?.nlv ?? null;
  const todayPnl = status?.today_pnl ?? null;
  const todayPct = todayPnl !== null && nlv !== null && nlv > 0 ? (todayPnl / nlv) * 100 : null;
  const unavailable = status?.data_source === 'UNAVAILABLE';
  const capHint = ledger && ledger.available ? `${usd(ledger.max_single_exposure)} cap per ticker` : undefined;

  return (
    <DashboardLayout
      status={status}
      engineError={engineError}
      env={env}
      onEnvChange={handleEnvChange}
      onRefresh={() => void loadData()}
      isRefreshing={isRefreshing}
      activeTab={activeTab}
      setActiveTab={setActiveTab}
    >
      <div className="space-y-5">
        {unavailable && !engineError && (
          <div className="text-xs font-mono text-amber-200 bg-amber-950/40 border border-amber-900/60 rounded-lg px-4 py-2.5">
            Active (Schwab) data is unavailable because the engine is running in simulation. Switch to Sandbox to see the simulated pipeline.
          </div>
        )}
        {partialErrors.length > 0 && !engineError && (
          <div className="text-xs font-mono text-rose-200 bg-rose-950/40 border border-rose-900/60 rounded-lg px-4 py-2.5">
            Could not load: {partialErrors.join(', ')}.
          </div>
        )}

        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
          <MetricCard
            label="Today P&L"
            value={signedUsd(todayPnl)}
            valueTone={pnlTone(todayPnl)}
            hint={
              todayPct !== null
                ? `${signedPct(todayPct)} · floor ${usd(status?.circuit_breaker_limit)}`
                : status
                  ? `Floor ${usd(status.circuit_breaker_limit)}`
                  : undefined
            }
            icon={
              <div className={`p-1 rounded-md ${(todayPnl ?? 0) >= 0 ? 'bg-emerald-500/10 text-emerald-400' : 'bg-rose-500/10 text-rose-400'}`}>
                {(todayPnl ?? 0) >= 0 ? <TrendingUp className="w-3.5 h-3.5" /> : <TrendingDown className="w-3.5 h-3.5" />}
              </div>
            }
          />
          <MetricCard
            label="Engine positions"
            value={positions ? String(positions.managed.length) : '—'}
            valueTone="text-cyan-300"
            hint={capHint}
            icon={
              <div className="p-1 rounded-md bg-cyan-500/10 text-cyan-400">
                <Layers className="w-3.5 h-3.5" />
              </div>
            }
          />
          <MetricCard
            label="External holdings"
            value={positions ? String(positions.unmanaged.length) : '—'}
            valueTone="text-amber-300"
            hint={positions && positions.unmanaged.length > 0 ? positions.unmanaged.map((p) => p.symbol).join(', ') : 'None detected'}
            icon={
              <div className="p-1 rounded-md bg-amber-500/10 text-amber-400">
                <ShieldAlert className="w-3.5 h-3.5" />
              </div>
            }
          />
          <MetricCard
            label="Settled buying power"
            value={ledger && ledger.available ? usd(ledger.bucket1_settled) : '—'}
            valueTone="text-emerald-300"
            hint={ledger && ledger.available ? `Max order ${usd(ledger.buying_power?.max_order_notional)}` : undefined}
            icon={
              <div className="p-1 rounded-md bg-emerald-500/10 text-emerald-400">
                <Wallet className="w-3.5 h-3.5" />
              </div>
            }
          />
        </div>

        {activeTab === 'overview' && (
          <div className="space-y-5">
            <LifecycleTracker status={status} />
            <LlmInsightConsole summary={regime} unavailable={regimeFailed} />

            <section className="bg-[#0e1422] border border-gray-800 rounded-xl p-4 sm:p-5 shadow-2xl">
              <div className="flex items-center justify-between pb-3 border-b border-gray-800 mb-3">
                <h3 className="text-sm font-semibold text-gray-100 flex items-center font-mono uppercase tracking-wider">
                  <Clock className="w-4 h-4 mr-2 text-cyan-400" />
                  Today's orders
                </h3>
                <span className="text-xs text-gray-400 font-mono">{orders ? `${orders.length} recorded` : '—'}</span>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-xs text-left">
                  <thead className="bg-[#080d17] text-gray-400 uppercase tracking-wider font-mono text-[10px]">
                    <tr>
                      <th className="py-2 px-3">Time</th>
                      <th className="py-2 px-3">Symbol</th>
                      <th className="py-2 px-3">Side</th>
                      <th className="py-2 px-3">Regime</th>
                      <th className="py-2 px-3 text-right">Shares</th>
                      <th className="py-2 px-3 text-right">Price</th>
                      <th className="py-2 px-3 text-right">Notional</th>
                      <th className="py-2 px-3 text-center">Status</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-800 font-mono">
                    {orders === null ? (
                      <tr>
                        <td colSpan={8} className="py-6 text-center text-gray-500">
                          Loading orders…
                        </td>
                      </tr>
                    ) : orders.length === 0 ? (
                      <tr>
                        <td colSpan={8} className="py-6 text-center text-gray-500">
                          No orders recorded today.
                        </td>
                      </tr>
                    ) : (
                      orders.map((o, idx) => (
                        <tr key={`${o.timestamp}-${o.symbol}-${idx}`} className="hover:bg-gray-800/30">
                          <td className="py-2 px-3 text-gray-400">{new Date(o.timestamp).toLocaleTimeString()}</td>
                          <td className="py-2 px-3 font-bold text-gray-100">{o.symbol}</td>
                          <td className="py-2 px-3">
                            <span
                              className={`px-2 py-0.5 rounded text-[10px] font-bold border ${
                                o.side === 'BUY'
                                  ? 'bg-emerald-950 text-emerald-300 border-emerald-800'
                                  : 'bg-rose-950 text-rose-300 border-rose-800'
                              }`}
                            >
                              {o.side}
                            </span>
                          </td>
                          <td className="py-2 px-3 text-gray-300 text-[11px]">{o.regime || '—'}</td>
                          <td className="py-2 px-3 text-right text-gray-200">{o.quantity}</td>
                          <td className="py-2 px-3 text-right">{usd(o.price)}</td>
                          <td className="py-2 px-3 text-right font-semibold text-cyan-300">{usd(o.cost)}</td>
                          <td className="py-2 px-3 text-center">
                            <span className="inline-flex items-center text-[10px] text-emerald-400">
                              <CheckCircle2 className="w-3 h-3 mr-1" />
                              {o.status}
                            </span>
                          </td>
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              </div>
            </section>
          </div>
        )}

        {activeTab === 'positions' && (
          <EtfPositionTracker env={env} onEnvChange={handleEnvChange} positions={positions} ledger={ledger} />
        )}

        {activeTab === 'ledger' && (
          <div className="grid grid-cols-1 xl:grid-cols-2 gap-5">
            <LedgerCard ledger={ledger} policy={policy} onPolicySaved={(saved) => { setPolicy(saved); void loadData(); }} />
            <MacroLiquidityCard macroState={macroState} onUploadComplete={() => void loadData()} />
          </div>
        )}

        {activeTab === 'controls' && <SchwabControls status={status} onRefresh={() => void loadData()} />}
      </div>
    </DashboardLayout>
  );
}

export default App;
