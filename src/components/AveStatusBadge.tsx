/**
 * src/components/AveStatusBadge.tsx
 * =================================
 * Telemetry status header displaying FSM state, SES regime, lambda factor, STALE warning,
 * dynamic tabular metrics, and the M3 Asymmetric SafetyModeSwitch.
 * Throttled to 4Hz via useAveStream.
 */

import React from 'react';
import { AveStatusMetrics } from '../types/ave';
import { Activity, AlertTriangle, ShieldAlert, TrendingUp, Zap } from 'lucide-react';
import { SafetyModeSwitch } from './SafetyModeSwitch';

interface AveStatusBadgeProps {
  metrics: AveStatusMetrics;
}

export const AveStatusBadge: React.FC<AveStatusBadgeProps> = ({ metrics }) => {
  const { symbol, state, price, upper, lower, supremum, lambdaFactor, regime, isStale } = metrics;

  // M3 State Color Mapping (Tonal container backgrounds, no hard borders)
  const getStateChip = (s: string) => {
    switch (s) {
      case 'TRACKING':
        return 'bg-primary/20 text-primary';
      case 'TIGHTENING':
        return 'bg-finance-warning-container text-finance-on-warning-container animate-pulse';
      case 'TRIGGERED':
      case 'EXIT_PENDING':
        return 'bg-error-container text-on-error-container animate-pulse';
      case 'SETTLED':
        return 'bg-finance-bull-container text-finance-on-bull-container';
      case 'ORPHANED':
        return 'bg-secondary-container text-on-secondary-container';
      default:
        return 'bg-surface-container-high text-on-surface-variant';
    }
  };

  const getRegimeBadge = (r: string) => {
    switch (r) {
      case 'FAVORABLE':
        return (
          <span className="flex items-center gap-1 text-xs px-2.5 py-0.5 rounded-full bg-finance-bull-container text-finance-bull font-sans font-medium">
            <TrendingUp className="w-3 h-3" /> FAVORABLE
          </span>
        );
      case 'DEFENSIVE':
        return (
          <span className="flex items-center gap-1 text-xs px-2.5 py-0.5 rounded-full bg-error-container text-error font-sans font-medium">
            <ShieldAlert className="w-3 h-3" /> DEFENSIVE
          </span>
        );
      default:
        return (
          <span className="flex items-center gap-1 text-xs px-2.5 py-0.5 rounded-full bg-finance-warning-container text-finance-warning font-sans font-medium">
            <Activity className="w-3 h-3" /> CHOP
          </span>
        );
    }
  };

  const lambdaPercent = Math.max(0, Math.min(100, Math.round(lambdaFactor * 100)));

  return (
    <div className="flex flex-wrap items-center justify-between gap-3 bg-surface-container/95 p-3 rounded-2xl shadow-m3-1 text-sm text-on-surface">
      {/* Left: Identity, FSM State, Regime, Stale Warning */}
      <div className="flex flex-wrap items-center gap-2.5">
        <div className="flex items-center gap-1.5 font-bold text-base tracking-wide text-on-surface">
          <Zap className="w-4 h-4 text-primary" />
          <span>{symbol}</span>
          <span className="text-on-surface-variant font-normal text-xs ml-0.5">AVE</span>
        </div>

        {/* FSM State Chip */}
        <span className={`px-2.5 py-0.5 text-xs font-semibold rounded-full font-sans tracking-wide ${getStateChip(state)}`}>
          {state}
        </span>

        {/* SES Regime Chip */}
        {getRegimeBadge(regime)}

        {/* STALE Alert */}
        {isStale && (
          <span className="flex items-center gap-1 text-xs px-2.5 py-0.5 rounded-full bg-finance-stale/20 text-finance-stale font-sans font-semibold animate-pulse">
            <AlertTriangle className="w-3 h-3 text-finance-stale" />
            STALE (&gt;2s)
          </span>
        )}
      </div>

      {/* Right: Operating Safety Mode Switch & Tabular Live Metrics */}
      <div className="flex flex-wrap items-center gap-4 sm:gap-6">
        {/* Asymmetric Safety Mode Switch */}
        <SafetyModeSwitch />

        {/* Tabular Numerics Metrics Strip */}
        <div className="flex items-center gap-4 sm:gap-5 text-xs">
          <div className="flex flex-col items-start leading-tight">
            <span className="font-sans text-[10px] font-medium tracking-wider uppercase text-on-surface-variant">
              Price
            </span>
            <span className="font-mono tabular-nums font-bold text-on-surface">
              ${price.toFixed(2)}
            </span>
          </div>

          <div className="flex flex-col items-start leading-tight">
            <span className="font-sans text-[10px] font-medium tracking-wider uppercase text-on-surface-variant">
              Upper
            </span>
            <span className="font-mono tabular-nums font-semibold text-finance-bull">
              ${upper.toFixed(2)}
            </span>
          </div>

          <div className="flex flex-col items-start leading-tight">
            <span className="font-sans text-[10px] font-medium tracking-wider uppercase text-on-surface-variant">
              Lower
            </span>
            <span className="font-mono tabular-nums font-semibold text-finance-bear">
              ${lower.toFixed(2)}
            </span>
          </div>

          <div className="flex flex-col items-start leading-tight">
            <span className="font-sans text-[10px] font-medium tracking-wider uppercase text-on-surface-variant">
              Supremum
            </span>
            <span className="font-mono tabular-nums font-semibold text-finance-supremum">
              ${supremum.toFixed(2)}
            </span>
          </div>

          <div className="flex flex-col items-start leading-tight">
            <div className="flex items-center gap-1.5">
              <span className="font-sans text-[10px] font-medium tracking-wider uppercase text-on-surface-variant">
                Cone λ
              </span>
              <span className="font-mono tabular-nums font-bold text-on-surface text-[11px]">
                {lambdaPercent}%
              </span>
            </div>
            <div className="w-16 bg-surface-container-high rounded-full h-1.5 overflow-hidden mt-1">
              <div
                className={`h-full transition-all duration-300 ${
                  lambdaPercent > 50
                    ? 'bg-primary'
                    : lambdaPercent > 20
                    ? 'bg-finance-warning'
                    : 'bg-error'
                }`}
                style={{ width: `${lambdaPercent}%` }}
              />
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};
