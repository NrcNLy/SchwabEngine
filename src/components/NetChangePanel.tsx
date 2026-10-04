import React, { useEffect, useRef, useState } from 'react';
import { ChevronDown } from 'lucide-react';
import { EngineStatus } from '../types';
import { pnlTone, signedPct, signedUsd, usd } from '../utils/format';

interface NetChangePanelProps {
  status: EngineStatus | null;
}

/**
 * Header metric that replaces the old LIVE button: portfolio net change
 * (NLV - prior-close NLV) with a tap-to-expand Realized / Unrealized breakdown.
 */
export const NetChangePanel: React.FC<NetChangePanelProps> = ({ status }) => {
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

  const usdChange = status?.net_change_usd ?? null;
  const pctChange = status?.net_change_pct ?? null;
  const hasBaseline = usdChange !== null;

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-label="Net change details"
        className="flex items-center gap-2 pl-3 pr-2 py-1.5 rounded-xl border border-gray-800 bg-gray-900/70 hover:bg-gray-800/80 transition-colors"
      >
        <div className="flex flex-col items-end leading-tight">
          <span className={`font-mono font-bold text-sm ${pnlTone(usdChange)}`}>{signedUsd(usdChange)}</span>
          <span className={`font-mono text-[10px] ${pnlTone(pctChange)}`}>{signedPct(pctChange)}</span>
        </div>
        <ChevronDown className={`w-3.5 h-3.5 text-gray-500 transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>

      {open && (
        <div className="absolute right-0 top-full mt-2 w-64 z-50 bg-[#0d1322] border border-gray-800 rounded-xl shadow-2xl p-3 font-mono">
          <Row label="Net liquidation" value={usd(status?.nlv)} tone="text-gray-100" />
          <Row label="Realized today" value={signedUsd(status?.today_realized_pnl)} tone={pnlTone(status?.today_realized_pnl)} />
          <Row label="Unrealized" value={signedUsd(status?.unrealized_pnl)} tone={pnlTone(status?.unrealized_pnl)} />
          <div className="mt-2 pt-2 border-t border-gray-800 text-[10px] text-gray-500 leading-snug">
            {hasBaseline
              ? 'Net change = current NLV minus the prior-close NLV. Deposits and withdrawals are included.'
              : 'Prior-close baseline not captured yet; net change appears after the first anchor is stored.'}
          </div>
        </div>
      )}
    </div>
  );
};

const Row: React.FC<{ label: string; value: string; tone: string }> = ({ label, value, tone }) => (
  <div className="flex items-center justify-between py-1">
    <span className="text-[11px] text-gray-400">{label}</span>
    <span className={`text-xs font-bold ${tone}`}>{value}</span>
  </div>
);
