import React from 'react';
import { 
  DollarSign, 
  Wallet, 
  Clock, 
  CheckCircle2,
  Lock
} from 'lucide-react';
import { LedgerSnapshot } from '../types';

interface LedgerCardProps {
  ledger: LedgerSnapshot;
}

export const LedgerCard: React.FC<LedgerCardProps> = ({ ledger }) => {
  const totalLiquidity = ledger.total_nlv || ledger.total_equity || (ledger.bucket1_settled + ledger.bucket2_unsettled + ledger.bucket3_pending);
  
  const bucket1Pct = totalLiquidity > 0 ? (ledger.bucket1_settled / totalLiquidity) * 100 : 0;
  const bucket2Pct = totalLiquidity > 0 ? (ledger.bucket2_unsettled / totalLiquidity) * 100 : 0;
  const bucket3Pct = totalLiquidity > 0 ? (ledger.bucket3_pending / totalLiquidity) * 100 : 0;

  return (
    <div className="bg-[#0e1422] border border-gray-800 rounded-xl p-6 shadow-2xl space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between pb-4 border-b border-gray-800 gap-4">
        <div className="flex items-center space-x-3">
          <div className="p-2.5 rounded-lg bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
            <Wallet className="w-5 h-5" />
          </div>
          <div>
            <div className="flex items-center space-x-2">
              <h3 className="font-semibold text-gray-100 text-sm font-mono uppercase tracking-wider">
                Dynamic NLV & Capital Ledger
              </h3>
              <span className="px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-emerald-950 text-emerald-300 border border-emerald-800">
                Zero GFV Risk
              </span>
            </div>
            <p className="text-xs text-gray-400 mt-0.5">
              T+1 temporal locks engaged for unsettled Bucket 2 allocations.
            </p>
          </div>
        </div>

        <div className="text-right font-mono">
          <span className="text-xs text-gray-400 block">Total Account NLV</span>
          <span className="text-xl font-bold text-gray-100">
            ${totalLiquidity.toLocaleString('en-US', { minimumFractionDigits: 2 })}
          </span>
        </div>
      </div>

      {/* Segmented Proportional Visual Bar */}
      <div>
        <div className="flex items-center justify-between text-xs font-mono text-gray-400 mb-2">
          <span className="flex items-center">
            <span className="w-2.5 h-2.5 rounded-full bg-emerald-400 inline-block mr-1.5" />
            Bucket 1 Settled ({bucket1Pct.toFixed(1)}%)
          </span>
          <span className="flex items-center">
            <span className="w-2.5 h-2.5 rounded-full bg-cyan-400 inline-block mr-1.5" />
            Bucket 2 Unsettled ({bucket2Pct.toFixed(1)}%)
          </span>
          <span className="flex items-center">
            <span className="w-2.5 h-2.5 rounded-full bg-amber-400 inline-block mr-1.5" />
            Bucket 3 Pending ({bucket3Pct.toFixed(1)}%)
          </span>
        </div>

        {/* Multi-Segment Bar */}
        <div className="w-full h-3 bg-gray-900 rounded-full overflow-hidden flex border border-gray-800 p-0.5">
          <div 
            style={{ width: `${bucket1Pct}%` }} 
            className="bg-emerald-500 h-full rounded-l-full transition-all duration-500 relative group"
            title={`Bucket 1 (Settled): $${ledger.bucket1_settled.toFixed(2)}`}
          />
          <div 
            style={{ width: `${bucket2Pct}%` }} 
            className="bg-cyan-500 h-full transition-all duration-500 relative group flex items-center justify-center overflow-hidden"
            title={`Bucket 2 (Unsettled): $${ledger.bucket2_unsettled.toFixed(2)}`}
          >
            {ledger.bucket2_unsettled > 0 && (
                <div className="w-full h-full opacity-30 flex items-center justify-center">
                  <Lock className="w-2 h-2 text-black" />
                </div>
            )}
          </div>
          <div 
            style={{ width: `${bucket3Pct}%` }} 
            className="bg-amber-500 h-full rounded-r-full transition-all duration-500 relative group"
            title={`Bucket 3 (Pending): $${ledger.bucket3_pending.toFixed(2)}`}
          />
        </div>
      </div>

      {/* 3 Buckets Grid */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        {/* Bucket 1 */}
        <div className="p-4 bg-[#111827]/90 rounded-xl border border-emerald-900/40 relative overflow-hidden">
          <div className="absolute top-0 right-0 w-16 h-16 bg-emerald-500/5 rounded-full -mr-6 -mt-6 pointer-events-none" />
          <div className="flex items-center justify-between text-emerald-400 text-xs font-semibold mb-2">
            <span className="flex items-center">
              <DollarSign className="w-4 h-4 mr-1" />
              Bucket 1: Settled
            </span>
            <span className="text-[10px] bg-emerald-950 px-1.5 py-0.5 rounded border border-emerald-800/60 font-mono">
              Cleared
            </span>
          </div>
          <div className="text-xl font-bold font-mono text-emerald-300">
            ${ledger.bucket1_settled.toLocaleString('en-US', { minimumFractionDigits: 2 })}
          </div>
          <p className="text-[11px] text-gray-400 mt-1">
            Immediate buying power unconditionally safe for day-trading.
          </p>
        </div>

        {/* Bucket 2 */}
        <div className="p-4 bg-[#111827]/90 rounded-xl border border-cyan-900/40 relative overflow-hidden">
          <div className="absolute top-0 right-0 w-16 h-16 bg-cyan-500/5 rounded-full -mr-6 -mt-6 pointer-events-none" />
          <div className="flex items-center justify-between text-cyan-400 text-xs font-semibold mb-2">
            <span className="flex items-center">
              <Clock className="w-4 h-4 mr-1" />
              Bucket 2: Unsettled
            </span>
            {ledger.bucket2_unsettled > 0 ? (
                <span className="flex items-center text-[10px] bg-rose-950/60 text-rose-400 px-1.5 py-0.5 rounded border border-rose-800/60 font-mono">
                  <Lock className="w-3 h-3 mr-1" /> T+1 Lock
                </span>
            ) : (
                <span className="text-[10px] bg-cyan-950 px-1.5 py-0.5 rounded border border-cyan-800/60 font-mono">
                  T+1 Clearing
                </span>
            )}
          </div>
          <div className="text-xl font-bold font-mono text-cyan-300">
            ${ledger.bucket2_unsettled.toLocaleString('en-US', { minimumFractionDigits: 2 })}
          </div>
          <p className="text-[11px] text-gray-400 mt-1">
            Proceeds awaiting NSCC settlement. Exit locks applied.
          </p>
        </div>

        {/* Bucket 3 */}
        <div className="p-4 bg-[#111827]/90 rounded-xl border border-amber-900/40 relative overflow-hidden">
          <div className="absolute top-0 right-0 w-16 h-16 bg-amber-500/5 rounded-full -mr-6 -mt-6 pointer-events-none" />
          <div className="flex items-center justify-between text-amber-400 text-xs font-semibold mb-2">
            <span className="flex items-center">
              <Clock className="w-4 h-4 mr-1" />
              Bucket 3: Pending
            </span>
            <span className="text-[10px] bg-amber-950 px-1.5 py-0.5 rounded border border-amber-800/60 font-mono">
              In Transit
            </span>
          </div>
          <div className="text-xl font-bold font-mono text-amber-300">
            ${ledger.bucket3_pending.toLocaleString('en-US', { minimumFractionDigits: 2 })}
          </div>
          <p className="text-[11px] text-gray-400 mt-1">
            Transfers in flight. Excluded from quantitative risk sizing.
          </p>
        </div>
      </div>

      {/* Quantitative Risk Ceilings & Quarter-Kelly Sizing Box */}
      <div className="bg-[#0b101c] border border-gray-800 rounded-xl p-4 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        {/* Single-Ticker Exposure Cap */}
        <div className="border-l-2 border-cyan-500 pl-3">
          <span className="text-[10px] text-gray-400 uppercase font-mono block">20% Single-Ticker Cap</span>
          <div className="text-base font-bold font-mono text-cyan-300">
            ${ledger.max_single_exposure.toFixed(2)}
          </div>
          <span className="text-[10px] text-gray-500 font-mono">Max dynamic limit</span>
        </div>

        {/* Quarter-Kelly Sizing */}
        <div className="border-l-2 border-emerald-500 pl-3">
          <span className="text-[10px] text-gray-400 uppercase font-mono block">Quarter-Kelly NATR Size</span>
          <div className="text-base font-bold font-mono text-emerald-300">
            ${ledger.quarter_kelly_size.toFixed(2)}
          </div>
          <span className="text-[10px] text-gray-500 font-mono">Volatility-adjusted order size</span>
        </div>

        {/* 1% Per-Trade Risk Cap */}
        <div className="border-l-2 border-purple-500 pl-3">
          <span className="text-[10px] text-gray-400 uppercase font-mono block">1.0% Per-Trade Risk</span>
          <div className="text-base font-bold font-mono text-purple-300">
            ${ledger.max_risk_per_trade.toFixed(2)}
          </div>
          <span className="text-[10px] text-gray-500 font-mono">Max loss at NATR hard stop</span>
        </div>

        {/* Daily Circuit Breaker */}
        <div className="border-l-2 border-rose-500 pl-3">
          <span className="text-[10px] text-gray-400 uppercase font-mono block">-3.0% Daily Breaker</span>
          <div className="text-base font-bold font-mono text-rose-400">
            ${ledger.daily_drawdown_limit.toFixed(2)}
          </div>
          <span className="text-[10px] text-gray-500 font-mono">Triggers emergency liquidation</span>
        </div>
      </div>

      {/* GFV Safety Verification Footnote */}
      <div className="bg-emerald-950/20 border border-emerald-900/40 rounded-lg p-3 flex items-start space-x-3 text-xs">
        <CheckCircle2 className="w-4 h-4 text-emerald-400 mt-0.5 flex-shrink-0" />
        <div className="text-gray-300">
          <span className="font-semibold text-emerald-300 font-mono">SEC Rule 15c3-3 / Dynamic GFV Protection Active:</span> The execution engine now utilizes Bucket 2 unsettled capital to maximize velocity. Temporal locks are mathematically applied to any position funded by unsettled cash, preventing algorithmic liquidation until the 09:00 EDT T+1 clearing threshold is reached.
        </div>
      </div>
    </div>
  );
};
