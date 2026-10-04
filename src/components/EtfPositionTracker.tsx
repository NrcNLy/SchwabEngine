import React from 'react';
import { ShieldCheck } from 'lucide-react';
import { EnvName, LedgerSnapshot, Position, PositionsResponse } from '../types';
import { EnvToggle } from './EnvToggle';
import { pnlTone, signedUsd, usd } from '../utils/format';

interface EtfPositionTrackerProps {
  env: EnvName;
  onEnvChange: (env: EnvName) => void;
  positions: PositionsResponse | null;
  ledger: LedgerSnapshot | null;
}

const REGIME_LABEL: Record<string, string> = {
  A: 'Trend',
  B: 'Mean reversion',
  C: 'Choppy',
  UNKNOWN: 'Unclassified',
};

const PositionCard: React.FC<{ pos: Position; cap: number | null }> = ({ pos, cap }) => {
  const usage = cap && cap > 0 ? Math.min(100, (pos.notional_value / cap) * 100) : null;
  const overCap = cap !== null && cap > 0 && pos.notional_value > cap + 0.005;
  const stop = pos.hard_stop_price ?? pos.stop_price ?? null;

  return (
    <div className="bg-[#0b101c] border border-gray-800 rounded-xl p-3.5 font-mono">
      <div className="flex items-start justify-between">
        <div>
          <div className="flex items-center gap-2">
            <span className="text-sm font-bold text-gray-100">{pos.symbol}</span>
            <span
              className={`px-1.5 py-0.5 rounded text-[9px] font-bold border ${
                pos.managed
                  ? 'bg-cyan-950 text-cyan-300 border-cyan-800/60'
                  : 'bg-gray-900 text-gray-400 border-gray-700'
              }`}
            >
              {pos.managed ? 'ENGINE' : 'EXTERNAL'}
            </span>
          </div>
          <div className="text-[10px] text-gray-500 mt-0.5">
            {pos.quantity} sh @ {usd(pos.entry_price)}
            {pos.regime ? ` · ${REGIME_LABEL[pos.regime] ?? pos.regime}` : ''}
          </div>
        </div>
        <div className="text-right">
          <div className={`text-sm font-bold ${pnlTone(pos.unrealized_pnl)}`}>{signedUsd(pos.unrealized_pnl)}</div>
          <div className="text-[10px] text-gray-500">Last {usd(pos.current_price)}</div>
        </div>
      </div>

      <div className="mt-3">
        <div className="flex justify-between text-[10px] text-gray-400 mb-1">
          <span>Exposure {usd(pos.notional_value)}</span>
          <span className={overCap ? 'text-rose-400 font-bold' : ''}>Cap {cap === null ? '—' : usd(cap)}</span>
        </div>
        <div className="h-1.5 rounded-full bg-gray-900 overflow-hidden">
          <div
            className={`h-full transition-all duration-500 ${overCap ? 'bg-rose-500' : (usage ?? 0) > 85 ? 'bg-amber-400' : 'bg-cyan-500'}`}
            style={{ width: `${usage ?? 0}%` }}
          />
        </div>
      </div>

      {pos.managed && (
        <div className="mt-3 grid grid-cols-2 gap-2 text-[10px]">
          <div className="bg-gray-900/60 rounded-lg px-2 py-1.5">
            <div className="text-gray-500">Stop</div>
            <div className="text-rose-300 font-semibold">{stop === null ? '—' : usd(stop)}</div>
          </div>
          <div className="bg-gray-900/60 rounded-lg px-2 py-1.5">
            <div className="text-gray-500">Target</div>
            <div className="text-emerald-300 font-semibold">{pos.target_price ? usd(pos.target_price) : '—'}</div>
          </div>
        </div>
      )}
    </div>
  );
};

export const EtfPositionTracker: React.FC<EtfPositionTrackerProps> = ({ env, onEnvChange, positions, ledger }) => {
  const cap = ledger && ledger.available ? ledger.max_single_exposure : null;
  const capPct = ledger && ledger.available && ledger.total_nlv > 0 ? (ledger.max_single_exposure / ledger.total_nlv) * 100 : null;
  const rows: Position[] = positions ? [...positions.managed, ...positions.unmanaged] : [];
  const unavailable = positions?.data_source === 'UNAVAILABLE';

  return (
    <section className="bg-[#0e1422] border border-gray-800 rounded-xl p-4 sm:p-5 shadow-2xl">
      <div className="flex items-center justify-between gap-3 pb-3 border-b border-gray-800">
        <div className="flex items-center gap-2.5">
          <div className="p-2 rounded-lg bg-cyan-500/10 text-cyan-400 border border-cyan-500/20">
            <ShieldCheck className="w-4 h-4" />
          </div>
          <h3 className="font-semibold text-gray-100 text-sm font-mono uppercase tracking-wider">Risk Guard</h3>
        </div>
        <EnvToggle env={env} onChange={onEnvChange} />
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-3 gap-2 mt-3 font-mono">
        <div className="bg-[#0b101c] border border-gray-800 rounded-lg px-3 py-2">
          <div className="text-[10px] text-gray-500">Single-ticker cap</div>
          <div className="text-sm font-bold text-cyan-300">{usd(cap)}</div>
          <div className="text-[10px] text-gray-500">{capPct === null ? '—' : `${capPct.toFixed(1)}% of NLV`}</div>
        </div>
        <div className="bg-[#0b101c] border border-gray-800 rounded-lg px-3 py-2">
          <div className="text-[10px] text-gray-500">Open positions</div>
          <div className="text-sm font-bold text-gray-100">{rows.length}</div>
          <div className="text-[10px] text-gray-500">{positions ? `${positions.managed.length} engine` : '—'}</div>
        </div>
        <div className="bg-[#0b101c] border border-gray-800 rounded-lg px-3 py-2 col-span-2 sm:col-span-1">
          <div className="text-[10px] text-gray-500">Max risk / trade</div>
          <div className="text-sm font-bold text-gray-100">{usd(ledger && ledger.available ? ledger.max_risk_per_trade : null)}</div>
          <div className="text-[10px] text-gray-500">
            Daily floor {usd(ledger && ledger.available ? ledger.daily_drawdown_limit : null)}
          </div>
        </div>
      </div>

      <div className="mt-4">
        {unavailable ? (
          <div className="text-xs text-gray-400 font-mono border border-gray-800 bg-[#0b101c] rounded-lg p-5 text-center">
            Live Schwab positions are unavailable: the engine is not connected to the brokerage in this mode.
          </div>
        ) : positions === null ? (
          <div className="text-xs text-gray-500 font-mono p-5 text-center">Loading positions…</div>
        ) : rows.length === 0 ? (
          <div className="text-xs text-gray-500 font-mono border border-gray-800 bg-[#0b101c] rounded-lg p-5 text-center">
            No open positions.
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
            {rows.map((p) => (
              <PositionCard key={`${p.symbol}-${p.managed ? 'm' : 'u'}`} pos={p} cap={cap} />
            ))}
          </div>
        )}
      </div>
    </section>
  );
};
