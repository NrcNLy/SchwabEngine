import React, { useEffect, useRef, useState } from 'react';
import { AlertTriangle, Flame, Loader2, ShieldCheck, X } from 'lucide-react';
import { AveOperatingMode, fetchAveMode, updateAveMode } from '../services/api';

export interface SafetyModeSwitchProps {
  mode?: AveOperatingMode;
  onModeChange?: (nextMode: AveOperatingMode) => Promise<boolean | void>;
  className?: string;
}

const HOLD_DURATION_MS = 1500; // 1.5-second physical hold-to-arm
const SVG_RADIUS = 14;
const SVG_CIRCUMFERENCE = 2 * Math.PI * SVG_RADIUS;

export const SafetyModeSwitch: React.FC<SafetyModeSwitchProps> = ({
  mode: controlledMode,
  onModeChange,
  className = '',
}) => {
  const [internalMode, setInternalMode] = useState<AveOperatingMode>('shadow');
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [showConfirmModal, setShowConfirmModal] = useState<boolean>(false);
  const [actionError, setActionError] = useState<string | null>(null);

  // Hold-to-arm interaction state
  const [holdProgress, setHoldProgress] = useState<number>(0);
  const [isHolding, setIsHolding] = useState<boolean>(false);
  const holdStartRef = useRef<number | null>(null);
  const animFrameRef = useRef<number | null>(null);

  const activeMode = controlledMode ?? internalMode;

  useEffect(() => {
    let isMounted = true;
    fetchAveMode()
      .then((res) => {
        if (isMounted && res.mode) {
          setInternalMode(res.mode);
        }
      })
      .catch(() => {
        // Silently default to shadow mode
      });
    return () => {
      isMounted = false;
    };
  }, []);

  // Commit mode change to backend API
  const executeModeChange = async (targetMode: AveOperatingMode) => {
    setIsLoading(true);
    setActionError(null);
    try {
      if (onModeChange) {
        await onModeChange(targetMode);
        setInternalMode(targetMode);
        setShowConfirmModal(false);
      } else {
        const result = await updateAveMode(targetMode);
        if (result.success) {
          setInternalMode(targetMode);
          setShowConfirmModal(false);
        } else {
          setActionError(result.message || 'Failed to update operating mode.');
        }
      }
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : 'Network error updating operating mode.');
    } finally {
      setIsLoading(false);
    }
  };

  // Shadow -> Live: Open High-Friction Modal
  const handleArmLiveClick = () => {
    if (isLoading || activeMode === 'live') return;
    setActionError(null);
    setHoldProgress(0);
    setIsHolding(false);
    setShowConfirmModal(true);
  };

  // Live -> Shadow: ZERO FRICTION emergency 1-click kill switch
  const handleEmergencyKillSwitch = () => {
    if (isLoading) return;
    void executeModeChange('shadow');
  };

  // Hold-to-arm handlers
  const startHold = () => {
    if (isLoading) return;
    setIsHolding(true);
    holdStartRef.current = performance.now();

    const tick = (now: number) => {
      if (!holdStartRef.current) return;
      const elapsed = now - holdStartRef.current;
      const progress = Math.min(1, elapsed / HOLD_DURATION_MS);
      setHoldProgress(progress);

      if (progress >= 1) {
        // Successfully held for 1.5 seconds!
        setIsHolding(false);
        holdStartRef.current = null;
        void executeModeChange('live');
      } else {
        animFrameRef.current = requestAnimationFrame(tick);
      }
    };

    animFrameRef.current = requestAnimationFrame(tick);
  };

  const cancelHold = () => {
    if (holdStartRef.current !== null) {
      if (animFrameRef.current) {
        cancelAnimationFrame(animFrameRef.current);
      }
      holdStartRef.current = null;
      setIsHolding(false);
      setHoldProgress(0);
    }
  };

  useEffect(() => {
    return () => {
      if (animFrameRef.current) cancelAnimationFrame(animFrameRef.current);
    };
  }, []);

  const strokeDashoffset = SVG_CIRCUMFERENCE * (1 - holdProgress);

  return (
    <div className={`flex items-center gap-2 select-none ${className}`}>
      {isLoading ? (
        <div className="flex items-center gap-1.5 px-3 py-1 rounded-xl bg-surface-container text-primary text-xs font-sans">
          <Loader2 className="w-3.5 h-3.5 animate-spin" />
          <span className="font-medium">Switching...</span>
        </div>
      ) : activeMode === 'live' ? (
        /* EMERGENCY 1-CLICK DISARM KILL-SWITCH (Zero friction) */
        <button
          type="button"
          onClick={handleEmergencyKillSwitch}
          aria-label="Emergency Disarm: Revert to Shadow mode"
          title="Instant Emergency Kill-Switch: Revert to Shadow mode (No confirmation required)"
          className="px-3.5 py-1.5 rounded-xl bg-error hover:bg-error/90 text-on-error font-sans font-bold text-xs flex items-center gap-2 shadow-m3-3 animate-pulse transition-transform active:scale-95 cursor-pointer"
        >
          <Flame className="w-4 h-4 text-on-error" />
          <span>DISARM TO SHADOW (KILL)</span>
        </button>
      ) : (
        /* SHADOW MODE: Safe state with Arm Live Trigger */
        <div className="flex items-center p-0.5 rounded-xl bg-surface-container-high border border-outline-variant/30 text-xs font-sans">
          <div className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-surface-container text-finance-bull font-medium">
            <ShieldCheck className="w-3.5 h-3.5" />
            <span className="font-semibold tracking-wide">SHADOW</span>
          </div>

          <button
            type="button"
            onClick={handleArmLiveClick}
            aria-label="Arm Live Mode"
            title="Arm Live Autonomous Execution (Requires hold-to-confirm)"
            className="px-2.5 py-1 rounded-lg text-on-surface-variant hover:text-error hover:bg-error/10 font-medium transition-colors cursor-pointer flex items-center gap-1"
          >
            <Flame className="w-3 h-3 text-on-surface-variant hover:text-error" />
            <span>Arm Live</span>
          </button>
        </div>
      )}

      {/* M3 Modal Dialog with 1.5s Hold-to-Arm Physical Button */}
      {showConfirmModal && (
        <div
          role="dialog"
          aria-modal="true"
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm p-4 font-sans animate-fade-in"
        >
          <div className="bg-surface-container-high rounded-2xl shadow-m3-4 border border-outline-variant/30 p-5 sm:p-6 max-w-md w-full space-y-4">
            {/* Header */}
            <div className="flex items-start justify-between gap-3">
              <div className="flex items-start gap-3">
                <div className="p-2.5 rounded-xl bg-error/15 text-error shrink-0">
                  <AlertTriangle className="w-6 h-6" />
                </div>
                <div>
                  <span className="text-[11px] font-sans font-bold tracking-wider uppercase text-error block">
                    Critical Safety Override
                  </span>
                  <h3 className="text-base font-bold text-on-surface mt-0.5 font-sans">
                    Authorize Live Market Execution
                  </h3>
                </div>
              </div>

              <button
                type="button"
                onClick={() => {
                  cancelHold();
                  setShowConfirmModal(false);
                  setActionError(null);
                }}
                className="p-1 rounded-lg text-on-surface-variant hover:text-on-surface hover:bg-surface-container transition-colors"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            {/* Warning Message */}
            <div className="bg-error-container/40 rounded-xl p-3.5 border border-error/20">
              <p className="text-xs text-on-error-container font-sans leading-relaxed">
                <strong className="font-semibold">WARNING:</strong> Engaging LIVE mode authorizes autonomous market orders via the Schwab REST API. Real financial capital will be executed on live order books.
              </p>
            </div>

            {actionError && (
              <div className="text-xs font-mono text-error bg-error-container/60 p-2.5 rounded-xl border border-error/40">
                {actionError}
              </div>
            )}

            {/* Interactive Actions: Cancel & Physical 1.5s Hold-to-Arm Button */}
            <div className="flex items-center justify-end gap-3 pt-2">
              <button
                type="button"
                disabled={isLoading}
                onClick={() => {
                  cancelHold();
                  setShowConfirmModal(false);
                  setActionError(null);
                }}
                className="px-4 py-2 rounded-xl text-on-surface-variant hover:text-on-surface hover:bg-surface-container text-xs font-sans font-medium transition-colors cursor-pointer"
              >
                Cancel
              </button>

              {/* Physical Hold Button with circular SVG progress stroke */}
              <button
                type="button"
                disabled={isLoading}
                onMouseDown={startHold}
                onMouseUp={cancelHold}
                onMouseLeave={cancelHold}
                onTouchStart={startHold}
                onTouchEnd={cancelHold}
                className="relative select-none px-4 py-2.5 rounded-xl bg-error hover:bg-error/90 text-on-error text-xs font-sans font-bold flex items-center gap-2.5 shadow-m3-2 transition-transform active:scale-[0.98] cursor-pointer disabled:opacity-50 overflow-hidden"
              >
                {/* Circular SVG Stroke Progress Indicator */}
                <div className="relative w-6 h-6 shrink-0 flex items-center justify-center">
                  <svg className="w-6 h-6 -rotate-90 transform" viewBox="0 0 36 36">
                    <circle
                      cx="18"
                      cy="18"
                      r={SVG_RADIUS}
                      fill="none"
                      stroke="rgba(255, 255, 255, 0.25)"
                      strokeWidth="3.5"
                    />
                    <circle
                      cx="18"
                      cy="18"
                      r={SVG_RADIUS}
                      fill="none"
                      stroke="#ffffff"
                      strokeWidth="3.5"
                      strokeDasharray={SVG_CIRCUMFERENCE}
                      strokeDashoffset={strokeDashoffset}
                      strokeLinecap="round"
                    />
                  </svg>
                </div>

                <span>
                  {isLoading
                    ? 'Arming...'
                    : isHolding
                    ? `Hold... ${(1.5 * (1 - holdProgress)).toFixed(1)}s`
                    : 'Hold 1.5s to Arm Live'}
                </span>
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
