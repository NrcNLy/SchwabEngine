import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Clock,
  Layers,
  ShieldAlert,
  TrendingDown,
  TrendingUp,
  Wallet,
} from 'lucide-react';
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
import { AveEnvelopeChart } from './components/AveEnvelopeChart';
import { pnlTone, signedPct, signedUsd, usd } from './utils/format';

const POLL_INTERVAL_MS = 5000;

const MetricCard: React.FC<{
  label: string;
  value: string;
  valueTone?: string;
  hint?: string;
  icon: React.ReactNode;
}> = ({ label, value, valueTone = 'text-on-surface', hint, icon }) => (
  <div className="bg-surface-container rounded-2xl p-3.5 shadow-m3-1 flex flex-col justify-between">
    <div className="flex items-center justify-between text-on-surface-variant text-[11px] mb-1">
      <span className="font-sans font-medium uppercase tracking-wider">{label}</span>
      {icon}
    </div>
    <div className={`text-xl font-bold font-mono tabular-nums ${valueTone}`}>{value}</div>
    {hint && <span className="text-[10px] text-on-surface-variant font-sans block mt-0.5">{hint}</span>}
  </div>
);

export function App() {
  const [activeTab, setActiveTab] = useState<DashboardTab>('overview');
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [hudCollapsed, setHudCollapsed] = useState(false);

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
  const [selectedAveSymbol, setSelectedAveSymbol] = useState<string>('SOXL');
  const [customSymbol, setCustomSymbol] = useState<string>('');

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

  const aveSymbols = Array.from(
    new Set([
      ...(positions?.managed.map((p) => p.symbol) ?? []),
      'SOXL',
      'TQQQ',
      'TNA',
      'SPY',
    ])
  );

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
      activeSymbol={selectedAveSymbol}
      onSelectSymbol={setSelectedAveSymbol}
      availableSymbols={aveSymbols}
    >
      {/* AVE SQUEEZE: Full Bleed Layout with Compact 32px Collapsible HUD */}
      {activeTab === 'ave' ? (
        <div className="flex flex-col w-full h-full min-h-0 select-none">
          {/* Compact 32px Collapsible HUD Strip */}
          <div className="shrink-0 bg-surface-container/80 backdrop-blur-md border-b border-outline-variant/15 px-3 py-1 flex items-center justify-between text-xs select-none z-10">
            <div className="flex items-center gap-3 sm:gap-6 overflow-x-auto py-0.5">
              {/* P&L */}
              <div className="flex items-center gap-1.5 shrink-0">
                <span className="font-sans text-[10px] uppercase tracking-wider text-on-surface-variant">P&L:</span>
                <span className={`font-mono tabular-nums font-bold ${pnlTone(todayPnl)}`}>
                  {signedUsd(todayPnl)}
                </span>
                {todayPct !== null && (
                  <span className={`font-mono tabular-nums text-[10px] ${pnlTone(todayPct)}`}>
                    ({signedPct(todayPct)})
                  </span>
                )}
              </div>

              {/* Positions */}
              <div className="flex items-center gap-1.5 shrink-0">
                <span className="font-sans text-[10px] uppercase tracking-wider text-on-surface-variant">Positions:</span>
                <span className="font-mono tabular-nums font-bold text-primary">
                  {positions ? String(positions.managed.length) : '—'}
                </span>
              </div>

              {/* External */}
              <div className="flex items-center gap-1.5 shrink-0">
                <span className="font-sans text-[10px] uppercase tracking-wider text-on-surface-variant">External:</span>
                <span className="font-mono tabular-nums font-bold text-tertiary">
                  {positions ? String(positions.unmanaged.length) : '—'}
                </span>
              </div>

              {/* Settled Buying Power */}
              <div className="flex items-center gap-1.5 shrink-0">
                <span className="font-sans text-[10px] uppercase tracking-wider text-on-surface-variant">Buying Power:</span>
                <span className="font-mono tabular-nums font-bold text-finance-bull">
                  {ledger && ledger.available ? usd(ledger.bucket1_settled) : '—'}
                </span>
              </div>
            </div>

            {/* Right: Symbol Quick Selector Chips + Custom Input */}
            <div className="flex items-center gap-2 shrink-0">
              <div className="hidden md:flex items-center gap-1 bg-surface-container-high/60 p-0.5 rounded-lg">
                {aveSymbols.map((sym) => (
                  <button
                    key={sym}
                    onClick={() => setSelectedAveSymbol(sym)}
                    className={`px-2 py-0.5 rounded-md text-[11px] font-mono font-bold transition-colors cursor-pointer ${
                      selectedAveSymbol === sym
                        ? 'bg-primary/20 text-primary font-bold'
                        : 'text-on-surface-variant hover:text-on-surface'
                    }`}
                  >
                    {sym}
                  </button>
                ))}
              </div>

              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  if (customSymbol.trim()) {
                    setSelectedAveSymbol(customSymbol.trim().toUpperCase());
                    setCustomSymbol('');
                  }
                }}
                className="flex items-center gap-1"
              >
                <input
                  type="text"
                  placeholder="CUSTOM..."
                  value={customSymbol}
                  onChange={(e) => setCustomSymbol(e.target.value)}
                  className="w-20 px-2 py-0.5 bg-surface-container-high rounded text-[11px] font-mono text-on-surface uppercase focus:outline-none focus:ring-1 focus:ring-primary/50"
                />
                <button
                  type="submit"
                  className="px-2 py-0.5 bg-surface-container-highest hover:bg-surface-bright text-on-surface rounded text-[11px] font-sans font-medium cursor-pointer"
                >
                  Go
                </button>
              </form>

              <button
                type="button"
                onClick={() => setHudCollapsed((prev) => !prev)}
                title={hudCollapsed ? 'Expand HUD details' : 'Collapse HUD'}
                className="p-1 rounded-md text-on-surface-variant hover:text-on-surface hover:bg-surface-container-high transition-colors"
              >
                {hudCollapsed ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronUp className="w-3.5 h-3.5" />}
              </button>
            </div>
          </div>

          {/* Full-Bleed uPlot Chart Container */}
          <div className="flex-1 w-full min-h-0 overflow-hidden flex flex-col">
            <AveEnvelopeChart symbol={selectedAveSymbol} />
          </div>
        </div>
      ) : (
        /* STANDARD VIEWS: Padded Grid with 4 Global Metric Cards */
        <div className="space-y-5">
          {unavailable && !engineError && (
            <div className="text-xs font-mono text-amber-200 bg-amber-950/40 border border-amber-900/60 rounded-xl px-4 py-2.5">
              Active (Schwab) data is unavailable because the engine is running in simulation. Switch to Sandbox to see the simulated pipeline.
            </div>
          )}
          {partialErrors.length > 0 && !engineError && (
            <div className="text-xs font-mono text-rose-200 bg-rose-950/40 border border-rose-900/60 rounded-xl px-4 py-2.5">
              Could not load: {partialErrors.join(', ')}.
            </div>
          )}

          {/* 4 Standard Global KPI Cards */}
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
                <div className={`p-1 rounded-lg ${(todayPnl ?? 0) >= 0 ? 'bg-finance-bull-container text-finance-bull' : 'bg-error-container text-error'}`}>
                  {(todayPnl ?? 0) >= 0 ? <TrendingUp className="w-3.5 h-3.5" /> : <TrendingDown className="w-3.5 h-3.5" />}
                </div>
              }
            />
            <MetricCard
              label="Engine positions"
              value={positions ? String(positions.managed.length) : '—'}
              valueTone="text-primary"
              hint={capHint}
              icon={
                <div className="p-1 rounded-lg bg-primary-container text-primary">
                  <Layers className="w-3.5 h-3.5" />
                </div>
              }
            />
            <MetricCard
              label="External holdings"
              value={positions ? String(positions.unmanaged.length) : '—'}
              valueTone="text-tertiary"
              hint={positions && positions.unmanaged.length > 0 ? positions.unmanaged.map((p) => p.symbol).join(', ') : 'None detected'}
              icon={
                <div className="p-1 rounded-lg bg-tertiary-container text-tertiary">
                  <ShieldAlert className="w-3.5 h-3.5" />
                </div>
              }
            />
            <MetricCard
              label="Settled buying power"
              value={ledger && ledger.available ? usd(ledger.bucket1_settled) : '—'}
              valueTone="text-finance-bull"
              hint={ledger && ledger.available ? `Max order ${usd(ledger.buying_power?.max_order_notional)}` : undefined}
              icon={
                <div className="p-1 rounded-lg bg-finance-bull-container text-finance-bull">
                  <Wallet className="w-3.5 h-3.5" />
                </div>
              }
            />
          </div>

          {activeTab === 'overview' && (
            <div className="space-y-5">
              <LifecycleTracker status={status} />
              <LlmInsightConsole summary={regime} unavailable={regimeFailed} />

              <section className="bg-surface-container rounded-2xl p-4 sm:p-5 shadow-m3-2">
                <div className="flex items-center justify-between pb-3 border-b border-outline-variant/20 mb-3">
                  <h3 className="text-sm font-semibold text-on-surface flex items-center font-sans uppercase tracking-wider">
                    <Clock className="w-4 h-4 mr-2 text-primary" />
                    Today's orders
                  </h3>
                  <span className="text-xs text-on-surface-variant font-mono">{orders ? `${orders.length} recorded` : '—'}</span>
                </div>
                <div className="overflow-x-auto">
                  <table className="w-full text-xs text-left">
                    <thead className="bg-surface-container-low text-on-surface-variant uppercase tracking-wider font-sans text-[10px]">
                      <tr>
                        <th className="py-2.5 px-3">Time</th>
                        <th className="py-2.5 px-3">Symbol</th>
                        <th className="py-2.5 px-3">Side</th>
                        <th className="py-2.5 px-3">Regime</th>
                        <th className="py-2.5 px-3 text-right">Shares</th>
                        <th className="py-2.5 px-3 text-right">Price</th>
                        <th className="py-2.5 px-3 text-right">Notional</th>
                        <th className="py-2.5 px-3 text-center">Status</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-outline-variant/15 font-mono">
                      {orders === null ? (
                        <tr>
                          <td colSpan={8} className="py-6 text-center text-on-surface-variant font-sans">
                            Loading orders…
                          </td>
                        </tr>
                      ) : orders.length === 0 ? (
                        <tr>
                          <td colSpan={8} className="py-6 text-center text-on-surface-variant font-sans">
                            No orders recorded today.
                          </td>
                        </tr>
                      ) : (
                        orders.map((o, idx) => (
                          <tr key={`${o.timestamp}-${o.symbol}-${idx}`} className="hover:bg-surface-container-high/40">
                            <td className="py-2 px-3 text-on-surface-variant">{new Date(o.timestamp).toLocaleTimeString()}</td>
                            <td className="py-2 px-3 font-bold text-on-surface">{o.symbol}</td>
                            <td className="py-2 px-3">
                              <span
                                className={`px-2 py-0.5 rounded text-[10px] font-bold ${
                                  o.side === 'BUY'
                                    ? 'bg-finance-bull-container text-finance-bull'
                                    : 'bg-error-container text-error'
                                }`}
                              >
                                {o.side}
                              </span>
                            </td>
                            <td className="py-2 px-3 text-on-surface-variant font-sans text-[11px]">{o.regime || '—'}</td>
                            <td className="py-2 px-3 text-right tabular-nums text-on-surface">{o.quantity}</td>
                            <td className="py-2 px-3 text-right tabular-nums">{usd(o.price)}</td>
                            <td className="py-2 px-3 text-right tabular-nums font-semibold text-primary">{usd(o.cost)}</td>
                            <td className="py-2 px-3 text-center">
                              <span className="inline-flex items-center text-[10px] text-finance-bull font-sans">
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
            <EtfPositionTracker
              env={env}
              onEnvChange={handleEnvChange}
              positions={positions}
              ledger={ledger}
              onInspectAve={(sym) => {
                setSelectedAveSymbol(sym);
                setActiveTab('ave');
              }}
            />
          )}

          {activeTab === 'ledger' && (
            <div className="grid grid-cols-1 xl:grid-cols-2 gap-5">
              <LedgerCard ledger={ledger} policy={policy} onPolicySaved={(saved) => { setPolicy(saved); void loadData(); }} />
              <MacroLiquidityCard macroState={macroState} onUploadComplete={() => void loadData()} />
            </div>
          )}

          {activeTab === 'controls' && <SchwabControls status={status} onRefresh={() => void loadData()} />}
        </div>
      )}
    </DashboardLayout>
  );
}

export default App;
