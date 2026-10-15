/**
 * src/components/AveStatusBadge.tsx
 * =================================
 * Telemetry status header displaying FSM state, SES regime, lambda factor, and STALE warning.
 * Throttled to 4Hz via useAveStream.
 */

import React from 'react';
import { AveStatusMetrics } from '../types/ave';
import { Activity, AlertTriangle, ShieldAlert, TrendingUp, Zap } from 'lucide-react';

interface AveStatusBadgeProps {
  metrics: AveStatusMetrics;
}

export const AveStatusBadge: React.FC<AveStatusBadgeProps> = ({ metrics }) => {
  const { symbol, state, price, upper, lower, supremum, lambdaFactor, regime, isStale } = metrics;

  // State color mapping
  const getStateColor = (s: string) => {
    switch (s) {
      case 'TRACKING':
        return 'bg-blue-500/20 text-blue-400 border-blue-500/30';
      case 'TIGHTENING':
        return 'bg-amber-500/20 text-amber-300 border-amber-500/30 animate-pulse';
      case 'TRIGGERED':
      case 'EXIT_PENDING':
        return 'bg-rose-500/20 text-rose-400 border-rose-500/30 animate-pulse';
      case 'SETTLED':
        return 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30';
      case 'ORPHANED':
        return 'bg-purple-500/20 text-purple-300 border-purple-500/30';
      default:
        return 'bg-slate-700/50 text-slate-300 border-slate-600/30';
    }
  };

  const getRegimeBadge = (r: string) => {
    switch (r) {
      case 'FAVORABLE':
        return (
          <span className="flex items-center gap-1 text-xs px-2 py-0.5 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
            <TrendingUp className="w-3 h-3" /> FAVORABLE
          </span>
        );
      case 'DEFENSIVE':
        return (
          <span className="flex items-center gap-1 text-xs px-2 py-0.5 rounded bg-rose-500/10 text-rose-400 border border-rose-500/20">
            <ShieldAlert className="w-3 h-3" /> DEFENSIVE
          </span>
        );
      default:
        return (
          <span className="flex items-center gap-1 text-xs px-2 py-0.5 rounded bg-amber-500/10 text-amber-400 border border-amber-500/20">
            <Activity className="w-3 h-3" /> CHOP
          </span>
        );
    }
  };

  const lambdaPercent = Math.max(0, Math.min(100, Math.round(lambdaFactor * 100)));

  return (
    <div className="flex flex-wrap items-center justify-between gap-3 bg-slate-900/90 border border-slate-800 p-3 rounded-lg text-sm text-slate-200">
      <div className="flex items-center gap-3">
        <div className="flex items-center gap-1.5 font-bold text-base tracking-wide text-white">
          <Zap className="w-4 h-4 text-cyan-400" />
          <span>{symbol}</span>
          <span className="text-slate-400 font-normal text-xs ml-1">AVE</span>
        </div>

        {/* FSM State Badge */}
        <span className={`px-2.5 py-0.5 text-xs font-semibold rounded-full border ${getStateColor(state)}`}>
          {state}
        </span>

        {/* SES Regime */}
        {getRegimeBadge(regime)}

        {/* STALE Warning */}
        {isStale && (
          <span className="flex items-center gap-1 text-xs px-2 py-0.5 rounded bg-rose-600/30 text-rose-300 border border-rose-500/50 animate-bounce">
            <AlertTriangle className="w-3 h-3 text-rose-400" />
            STALE (&gt;2s)
          </span>
        )}
      </div>

      {/* Metrics Row */}
      <div className="flex items-center gap-5 font-mono text-xs text-slate-400">
        <div>
          <span>PRICE: </span>
          <span className="text-white font-semibold">${price.toFixed(2)}</span>
        </div>
        <div>
          <span>UPPER: </span>
          <span className="text-emerald-400 font-semibold">${upper.toFixed(2)}</span>
        </div>
        <div>
          <span>LOWER: </span>
          <span className="text-rose-400 font-semibold">${lower.toFixed(2)}</span>
        </div>
        <div>
          <span>SUPREMUM: </span>
          <span className="text-cyan-300 font-semibold">${supremum.toFixed(2)}</span>
        </div>
        <div className="flex items-center gap-1.5">
          <span>CONE λ: </span>
          <div className="w-16 bg-slate-800 rounded-full h-2 overflow-hidden border border-slate-700">
            <div
              className={`h-full transition-all duration-300 ${
                lambdaPercent > 50 ? 'bg-cyan-500' : lambdaPercent > 20 ? 'bg-amber-400' : 'bg-rose-500'
              }`}
              style={{ width: `${lambdaPercent}%` }}
            />
          </div>
          <span className="text-slate-300 font-bold">{lambdaPercent}%</span>
        </div>
      </div>
    </div>
  );
};
