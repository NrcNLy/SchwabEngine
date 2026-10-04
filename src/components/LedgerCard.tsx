import React from 'react';
import { Lock, Unlock, Wallet } from 'lucide-react';
import { BuyingPowerBreakdown, LedgerSnapshot, LiquidityPolicy } from '../types';
import { InflowScheduleForm } from './InflowScheduleForm';
import { usd } from '../utils/format';

interface LedgerCardProps {
  ledger: LedgerSnapshot | null;
  policy: LiquidityPolicy | null;
  onPolicySaved: (policy: LiquidityPolicy) => void;
}

const Stat: React.FC<{ label: string; value: string; tone?: string; hint?: string }> = ({ label, value, tone = 'text-gray-100', hint }) => (
  <div className="bg-[#0b101c] border border-gray-800 rounded-lg px-3 py-2 font-mono">
    <div className="text-[10px] text-gray-500">{label}</div>
    <div className={`text-sm font-bold ${tone}`}>{value}</div>
    {hint && <div className="text-[10px] text-gray-500 mt-0.5">{hint}</div>}
  </div>
);

const ReserveSplit: React.FC<{ bp: BuyingPowerBreakdown; posteriorMin: number | null }> = ({ bp, posteriorMin }) => {
  const gateText = bp.high_probability
    ? 'Gate open: Regime A with a high-probability posterior.'
    : `Gate closed: needs Regime A and a posterior win-rate of ${posteriorMin === null ? 'the configured minimum' : `${(posteriorMin * 100).toFixed(0)}%`} or more.`;

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
      <div className="bg-[#0b101c] border border-gray-800 rounded-xl p-4 font-mono">
        <div className="flex items-center justify-between mb-2">
          <span className="text-xs font-bold text-amber-300 uppercase tracking-wider flex items-center">
            <Lock className="w-3.5 h-3.5 mr-1.5" />
            Hard reserve
          </span>
          <span className="text-sm font-bold text-amber-200">{usd(bp.hard_reserve)}</span>
        </div>
        <div className="space-y-1 text-[11px] text-gray-400">
          <div className="flex justify-between">
            <span>Unsettled T+1 proceeds</span>
            <span className="text-gray-200">{usd(bp.hard_reserve_unsettled_cash)}</span>
          </div>
          <div className="flex justify-between">
            <span>SWVXX redemption in flight</span>
            <span className="text-gray-200">{usd(bp.hard_reserve_swvxx_in_flight)}</span>
          </div>
        </div>
        <p className="mt-2 text-[10px] text-gray-500 leading-snug">Locked until settled. Never used for entries, so no good-faith violations.</p>
      </div>

      <div className="bg-[#0b101c] border border-gray-800 rounded-xl p-4 font-mono">
        <div className="flex items-center justify-between mb-2">
          <span className="text-xs font-bold text-emerald-300 uppercase tracking-wider flex items-center">
            {bp.high_probability ? <Unlock className="w-3.5 h-3.5 mr-1.5" /> : <Lock className="w-3.5 h-3.5 mr-1.5" />}
            Soft reserve
          </span>
          <span className="text-sm font-bold text-emerald-200">{usd(bp.soft_reserve_current)}</span>
        </div>
        <div className="space-y-1 text-[11px] text-gray-400">
          <div className="flex justify-between">
            <span>Target (policy)</span>
            <span className="text-gray-200">{usd(bp.soft_reserve_target)}</span>
          </div>
          <div className="flex justify-between">
            <span>Target after expected inflow</span>
            <span className="text-gray-200">{usd(bp.soft_reserve_target_effective)}</span>
          </div>
          <div className="flex justify-between">
            <span>Shortfall</span>
            <span className={bp.soft_reserve_shortfall > 0 ? 'text-amber-300' : 'text-gray-200'}>{usd(bp.soft_reserve_shortfall)}</span>
          </div>
          <div className="flex justify-between">
            <span>Drawable for a setup</span>
            <span className="text-gray-200">{usd(bp.soft_draw_available)}</span>
          </div>
        </div>
        <p className={`mt-2 text-[10px] leading-snug ${bp.high_probability ? 'text-emerald-400' : 'text-gray-500'}`}>{gateText}</p>
      </div>
    </div>
  );
};

export const LedgerCard: React.FC<LedgerCardProps> = ({ ledger, policy, onPolicySaved }) => {
  const bp = ledger?.buying_power ?? null;
  const available = ledger?.available === true;

  return (
    <section className="bg-[#0e1422] border border-gray-800 rounded-xl p-4 sm:p-5 shadow-2xl space-y-5">
      <div className="flex items-center justify-between gap-3 pb-3 border-b border-gray-800">
        <div className="flex items-center gap-2.5">
          <div className="p-2 rounded-lg bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
            <Wallet className="w-4 h-4" />
          </div>
          <h3 className="font-semibold text-gray-100 text-sm font-mono uppercase tracking-wider">Capital Ledger</h3>
        </div>
        <div className="text-right font-mono">
          <div className="text-[10px] text-gray-500">NLV</div>
          <div className="text-sm font-bold text-gray-100">{available ? usd(ledger?.total_nlv) : '—'}</div>
        </div>
      </div>

      {ledger === null ? (
        <div className="text-xs font-mono text-gray-500 text-center py-6">Loading ledger…</div>
      ) : !available ? (
        <div className="text-xs font-mono text-gray-400 border border-gray-800 bg-[#0b101c] rounded-lg p-5 text-center">
          No ledger for this environment. Active balances appear once the engine is connected to Schwab.
        </div>
      ) : (
        <>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-2">
            <Stat label="Settled cash" value={usd(ledger.bucket1_settled)} tone="text-emerald-300" hint="Available for entries" />
            <Stat label="Unsettled (T+1)" value={usd(ledger.bucket2_unsettled)} tone="text-amber-300" hint="Locked until settle" />
            <Stat label="Pending ACH" value={usd(ledger.bucket3_pending)} tone="text-cyan-300" hint="Not counted in sizing" />
            <Stat label="SWVXX" value={usd(ledger.swvxx_balance)} tone="text-indigo-300" hint="Redeems T+1" />
          </div>

          {bp && <ReserveSplit bp={bp} posteriorMin={policy?.risk_gate.high_prob_posterior_min ?? null} />}

          {bp && (
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-2">
              <Stat label="Tactical float" value={usd(bp.tactical_float)} hint="Base float plus any soft draw" />
              <Stat label="Max order" value={usd(bp.max_order_notional)} tone="text-cyan-300" hint="Lower of cap and float" />
              <Stat label="Quarter-Kelly size" value={usd(ledger.quarter_kelly_size)} />
              <Stat
                label="Daily floor"
                value={usd(ledger.daily_drawdown_limit)}
                tone="text-rose-300"
                hint={`Max risk ${usd(ledger.max_risk_per_trade)} / trade`}
              />
            </div>
          )}

          {bp?.sweep && (
            <div className="bg-[#0b101c] border border-indigo-900/40 rounded-xl p-4 font-mono">
              <div className="flex items-center justify-between mb-1">
                <span className="text-xs font-bold text-indigo-300 uppercase tracking-wider">SWVXX sweep</span>
                <span className="px-1.5 py-0.5 rounded text-[9px] font-bold bg-indigo-950 text-indigo-300 border border-indigo-800/60">
                  {bp.sweep.mode}
                </span>
              </div>
              <p className="text-[11px] text-gray-400 leading-snug">{bp.sweep.rationale}</p>
              {bp.sweep.amount > 0 && (
                <div className="mt-2 text-[11px] text-gray-300">
                  {bp.sweep.action === 'SWEEP_IN' ? 'Suggested sweep-in' : bp.sweep.action === 'REDEEM' ? 'Suggested redemption' : 'Suggested move'}{' '}
                  <span className="font-bold text-indigo-200">{usd(bp.sweep.amount)}</span>
                  {bp.sweep.place_by ? ` · place by ${bp.sweep.place_by}` : ''}
                  {bp.sweep.settles_on ? ` · settles ${bp.sweep.settles_on}` : ''}
                </div>
              )}
              <p className="mt-2 text-[10px] text-gray-500">Advisory only: the engine never places mutual-fund orders.</p>
            </div>
          )}
        </>
      )}

      <div className="pt-4 border-t border-gray-800 space-y-2">
        <InflowScheduleForm policy={policy} onSaved={onPolicySaved} />
        {bp && policy?.inflow.enabled && bp.next_inflow_date && (
          <p className="text-[10px] font-mono text-gray-500">
            Next inflow {bp.next_inflow_date}, settles {bp.next_inflow_settles ?? '—'}; counts {usd(bp.inflow_offset)} toward the soft-reserve target.
          </p>
        )}
      </div>
    </section>
  );
};
