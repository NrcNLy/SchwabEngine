/**
 * src/components/AveStatusBadge.tsx
 * =================================
 * Telemetry status header displaying FSM state, SES regime, lambda factor, STALE warning,
 * and an interactive Shadow/Live operating mode toggle with strict safety confirmation.
 * Throttled to 4Hz via useAveStream.
 */

import React, { useEffect, useState } from 'react';
import { AveStatusMetrics } from '../types/ave';
import { Activity, AlertTriangle, CheckCircle2, Loader2, ShieldAlert, TrendingUp, Zap } from 'lucide-react';
import { AveOperatingMode, fetchAveMode, updateAveMode } from '../services/api';

interface AveStatusBadgeProps {
  metrics: AveStatusMetrics;
}

export const AveStatusBadge: React.FC<AveStatusBadgeProps> = ({ metrics }) => {
  const { symbol, state, price, upper, lower, supremum, lambdaFactor, regime, isStale } = metrics;

  const [mode, setMode] = useState<AveOperatingMode>('shadow');
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [showConfirmModal, setShowConfirmModal] = useState<boolean>(false);
  const [actionError, setActionError] = useState<string | null>(null);

  useEffect(() => {
    let isMounted = true;
    fetchAveMode()
      .then((res) => {
        if (isMounted && res.mode) {
          setMode(res.mode);
        }
      })
      .catch(() => {
        // Silently default to shadow mode
      });
    return () => {
      isMounted = false;
    };
  }, []);

  const handleToggleClick = (targetMode: AveOperatingMode) => {
    if (isLoading || targetMode === mode) return;

    if (targetMode === 'live') {
      setActionError(null);
      setShowConfirmModal(true);
    } else {
      void commitModeChange(targetMode);
    }
  };

  const commitModeChange = async (nextMode: AveOperatingMode) => {
    setIsLoading(true);
    setActionError(null);
    try {
      const result = await updateAveMode(nextMode);
      if (result.success) {
        setMode(nextMode);
        setShowConfirmModal(false);
      } else {
        setActionError(result.message || 'Failed to update operating mode.');
      }
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : 'Network error updating mode.');
    } finally {
      setIsLoading(false);
    }
  };

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
    <>
      <div className="flex flex-wrap items-center justify-between gap-3 bg-slate-900/90 border border-slate-800 p-3 rounded-lg text-sm text-slate-200">
        <div className="flex flex-wrap items-center gap-3">
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

        {/* Operating Mode Toggle Switch & Metrics */}
        <div className="flex flex-wrap items-center gap-5">
          {/* Interactive Shadow / Live Toggle */}
          <div className="flex items-center gap-2 bg-[#080d17] px-2 py-1 rounded-lg border border-gray-800 text-xs font-mono">
            <span className="text-gray-400 font-medium">MODE:</span>
            {isLoading ? (
              <div className="flex items-center gap-1 text-cyan-400 px-2 py-0.5">
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
                <span className="text-[11px]">Updating...</span>
              </div>
            ) : (
              <div className="flex items-center gap-1">
                <button
                  type="button"
                  onClick={() => handleToggleClick('shadow')}
                  className={`px-2 py-0.5 rounded text-[11px] font-bold uppercase transition-colors cursor-pointer border ${
                    mode === 'shadow'
                      ? 'bg-amber-950/80 text-amber-300 border-amber-700/80 shadow-sm'
                      : 'text-gray-400 hover:text-gray-200 border-transparent hover:bg-gray-800/40'
                  }`}
                >
                  Shadow
                </button>
                <button
                  type="button"
                  onClick={() => handleToggleClick('live')}
                  className={`px-2 py-0.5 rounded text-[11px] font-bold uppercase transition-colors cursor-pointer border ${
                    mode === 'live'
                      ? 'bg-rose-950 text-rose-300 border-rose-700 shadow-sm animate-pulse'
                      : 'text-gray-400 hover:text-rose-300 border-transparent hover:bg-rose-950/30'
                  }`}
                >
                  Live
                </button>
              </div>
            )}
          </div>

          {/* Metrics Row */}
          <div className="flex items-center gap-4 font-mono text-xs text-slate-400">
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
      </div>

      {/* Strict Confirmation Modal for LIVE Mode */}
      {showConfirmModal && (
        <div
          role="dialog"
          aria-modal="true"
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm p-4 font-sans"
        >
          <div className="bg-[#0c1220] border-2 border-rose-600 rounded-xl p-5 max-w-md w-full shadow-2xl space-y-4">
            <div className="flex items-start gap-3">
              <div className="p-2.5 rounded-lg bg-rose-500/10 text-rose-400 border border-rose-500/30 shrink-0">
                <AlertTriangle className="w-6 h-6 text-rose-500" />
              </div>
              <div>
                <h3 className="text-base font-bold text-gray-100 font-mono tracking-wide">
                  Confirm Live Market Execution
                </h3>
                <p className="text-xs text-rose-300/90 font-mono mt-1">
                  CRITICAL OPERATIONAL OVERRIDE
                </p>
              </div>
            </div>

            <div className="bg-rose-950/40 border border-rose-800/60 rounded-lg p-3.5">
              <p className="text-xs text-rose-200 font-mono leading-relaxed font-semibold">
                WARNING: Engaging LIVE mode authorizes autonomous market execution via the Schwab REST API. Proceed?
              </p>
            </div>

            {actionError && (
              <div className="text-xs font-mono text-rose-400 bg-rose-950/60 p-2.5 rounded border border-rose-800">
                {actionError}
              </div>
            )}

            <div className="flex items-center justify-end gap-3 pt-2">
              <button
                type="button"
                disabled={isLoading}
                onClick={() => {
                  setShowConfirmModal(false);
                  setActionError(null);
                }}
                className="px-4 py-2 rounded-lg bg-gray-800 hover:bg-gray-700 text-gray-300 text-xs font-mono font-semibold transition-colors cursor-pointer"
              >
                Cancel
              </button>
              <button
                type="button"
                disabled={isLoading}
                onClick={() => void commitModeChange('live')}
                className="px-4 py-2 rounded-lg bg-rose-600 hover:bg-rose-500 text-white text-xs font-mono font-bold flex items-center gap-2 shadow-lg shadow-rose-950 transition-colors cursor-pointer disabled:opacity-50"
              >
                {isLoading ? (
                  <>
                    <Loader2 className="w-3.5 h-3.5 animate-spin" />
                    <span>Engaging...</span>
                  </>
                ) : (
                  <>
                    <CheckCircle2 className="w-3.5 h-3.5" />
                    <span>Authorize LIVE Mode</span>
                  </>
                )}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
};
