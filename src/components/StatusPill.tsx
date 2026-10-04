import React, { useEffect, useRef, useState } from 'react';
import { EngineStatus, SystemState } from '../types';

interface StatusPillProps {
  status: EngineStatus | null;
  engineError: string | null;
}

type PillView = {
  label: string;
  bar: string;
  glow: string;
  text: string;
};

const VIEWS: Record<SystemState | 'OFFLINE', PillView> = {
  OK: { label: 'Operational', bar: 'bg-emerald-400', glow: 'shadow-[0_0_8px_rgba(52,211,153,0.7)]', text: 'text-emerald-300' },
  SIMULATED: { label: 'Simulation', bar: 'bg-amber-400', glow: 'shadow-[0_0_8px_rgba(251,191,36,0.6)]', text: 'text-amber-300' },
  DEGRADED: { label: 'Degraded', bar: 'bg-amber-500', glow: 'shadow-[0_0_8px_rgba(245,158,11,0.7)]', text: 'text-amber-300' },
  DOWN: { label: 'Down', bar: 'bg-rose-500', glow: 'shadow-[0_0_8px_rgba(244,63,94,0.7)]', text: 'text-rose-300' },
  HALTED: { label: 'Halted', bar: 'bg-rose-500 animate-pulse', glow: 'shadow-[0_0_10px_rgba(244,63,94,0.9)]', text: 'text-rose-300' },
  OFFLINE: { label: 'Engine offline', bar: 'bg-gray-500', glow: '', text: 'text-gray-300' },
};

/**
 * Single 10px-high pill that replaces every textual status badge. The meaning is
 * available on tap/hover (popover) so no status text occupies header space.
 */
export const StatusPill: React.FC<StatusPillProps> = ({ status, engineError }) => {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return undefined;
    const onDown = (e: MouseEvent | TouchEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('touchstart', onDown);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('touchstart', onDown);
    };
  }, [open]);

  const key: SystemState | 'OFFLINE' = status && !engineError ? status.system_state : 'OFFLINE';
  const view = VIEWS[key];
  const reasons = engineError ? [engineError] : status?.system_reasons ?? [];

  return (
    <div ref={ref} className="relative flex items-center">
      <button
        type="button"
        aria-label={`System status: ${view.label}`}
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="flex items-center justify-center w-8 h-6 -mx-1"
      >
        <span className={`block h-[10px] w-[22px] rounded-full ${view.bar} ${view.glow}`} />
      </button>
      {open && (
        <div className="absolute left-0 top-full mt-2 w-64 z-50 bg-[#0d1322] border border-gray-800 rounded-xl shadow-2xl p-3">
          <div className={`text-xs font-mono font-bold ${view.text}`}>{view.label}</div>
          {reasons.length > 0 ? (
            <ul className="mt-1.5 space-y-1">
              {reasons.map((r) => (
                <li key={r} className="text-[11px] text-gray-400 leading-snug">
                  {r}
                </li>
              ))}
            </ul>
          ) : (
            <p className="mt-1.5 text-[11px] text-gray-500">All checks passing.</p>
          )}
        </div>
      )}
    </div>
  );
};
