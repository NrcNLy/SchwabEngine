import React, { useState } from 'react';
import {
  AlertOctagon,
  CheckCircle2,
  Flame,
  Key,
  Power,
  RefreshCw,
  RotateCcw,
  ShieldAlert,
  ShieldCheck,
  XCircle,
  Zap,
} from 'lucide-react';
import { ActionResult, EngineStatus } from '../types';
import { exchangeOAuthCode, refreshOAuthToken, setEmergencyHalt, triggerLiquidationSweep } from '../services/api';
import { formatCountdown } from '../utils/format';

interface SchwabControlsProps {
  status: EngineStatus | null;
  onRefresh: () => void;
}

const AUTH_BADGE: Record<EngineStatus['auth_status'], { label: string; tone: string; ok: boolean }> = {
  AUTHORIZED: { label: 'Connected', tone: 'bg-emerald-950/80 text-emerald-300 border-emerald-700/60', ok: true },
  NEEDS_AUTH: { label: 'Auth required', tone: 'bg-amber-950/80 text-amber-300 border-amber-700/60', ok: false },
  EXPIRED: { label: 'Token expired', tone: 'bg-rose-950/80 text-rose-300 border-rose-700/60', ok: false },
  NOT_REQUIRED: { label: 'Not required (simulation)', tone: 'bg-gray-900 text-gray-300 border-gray-700', ok: true },
};

const Feedback: React.FC<{ result: ActionResult | null }> = ({ result }) =>
  result ? (
    <div
      className={`p-3 rounded-lg text-xs font-mono flex items-start gap-2 border ${
        result.success ? 'bg-emerald-950/60 text-emerald-300 border-emerald-800' : 'bg-rose-950/60 text-rose-300 border-rose-800'
      }`}
    >
      {result.success ? <CheckCircle2 className="w-4 h-4 shrink-0" /> : <XCircle className="w-4 h-4 shrink-0" />}
      <span>{result.message}</span>
    </div>
  ) : null;

export const SchwabControls: React.FC<SchwabControlsProps> = ({ status, onRefresh }) => {
  const [authCode, setAuthCode] = useState('');
  const [authLoading, setAuthLoading] = useState(false);
  const [refreshLoading, setRefreshLoading] = useState(false);
  const [authResult, setAuthResult] = useState<ActionResult | null>(null);

  const [haltLoading, setHaltLoading] = useState(false);
  const [sweepLoading, setSweepLoading] = useState(false);
  const [emergencyResult, setEmergencyResult] = useState<ActionResult | null>(null);

  const authBadge = status ? AUTH_BADGE[status.auth_status] : null;
  const halted = status?.system_state === 'HALTED';
  const simulated = status?.engine_mode === 'SANDBOX_SIMULATION';
  const offline = status === null;

  const submitCode = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!authCode.trim()) return;
    setAuthLoading(true);
    setAuthResult(null);
    const res = await exchangeOAuthCode(authCode.trim());
    setAuthResult(res);
    if (res.success) {
      setAuthCode('');
      onRefresh();
    }
    setAuthLoading(false);
  };

  const forceRefresh = async () => {
    setRefreshLoading(true);
    setAuthResult(null);
    const res = await refreshOAuthToken();
    setAuthResult(res);
    if (res.success) onRefresh();
    setRefreshLoading(false);
  };

  const toggleHalt = async () => {
    setHaltLoading(true);
    const res = await setEmergencyHalt(!halted);
    setEmergencyResult(res);
    setHaltLoading(false);
    onRefresh();
  };

  const liquidate = async () => {
    const scope = simulated ? 'all simulated positions' : 'all engine-managed positions at market';
    if (!window.confirm(`Halt new entries, cancel the engine's working orders and liquidate ${scope}?`)) return;
    setSweepLoading(true);
    const res = await triggerLiquidationSweep();
    setEmergencyResult(res);
    setSweepLoading(false);
    onRefresh();
  };

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
      <section className="bg-[#0e1422] border border-gray-800 rounded-xl p-4 sm:p-5 shadow-2xl space-y-4">
        <div className="flex items-center justify-between gap-3 pb-3 border-b border-gray-800">
          <div className="flex items-center gap-2.5">
            <div className={`p-2 rounded-lg border ${authBadge?.ok ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20' : 'bg-amber-500/10 text-amber-400 border-amber-500/20'}`}>
              <Key className="w-4 h-4" />
            </div>
            <h3 className="font-semibold text-gray-100 text-sm font-mono uppercase tracking-wider">Schwab Authorization</h3>
          </div>
          {authBadge && (
            <span className={`inline-flex items-center px-2.5 py-1 rounded-full text-[11px] font-mono font-bold border ${authBadge.tone}`}>
              {authBadge.ok ? <ShieldCheck className="w-3.5 h-3.5 mr-1" /> : <ShieldAlert className="w-3.5 h-3.5 mr-1" />}
              {authBadge.label}
            </span>
          )}
        </div>

        <div className="bg-[#111827] border border-gray-800/80 rounded-lg p-3 flex items-center justify-between font-mono">
          <span className="flex items-center text-xs text-gray-400">
            <RotateCcw className="w-3.5 h-3.5 mr-1.5 text-emerald-400" />
            Refresh token remaining
          </span>
          <span className="text-sm font-bold text-emerald-300">
            {status?.auth_status === 'NOT_REQUIRED' ? 'n/a' : formatCountdown(status?.auth_expires_in_s)}
          </span>
        </div>

        <div className="flex items-center justify-between">
          <span className="text-xs text-gray-400 font-mono">Renew access token now</span>
          <button
            type="button"
            onClick={forceRefresh}
            disabled={refreshLoading || offline}
            className="px-3 py-1.5 bg-gray-800 hover:bg-gray-700 border border-gray-700 rounded-lg text-xs font-mono font-semibold text-gray-200 flex items-center transition-colors disabled:opacity-50"
          >
            <RefreshCw className={`w-3.5 h-3.5 mr-1.5 ${refreshLoading ? 'animate-spin text-cyan-400' : 'text-gray-400'}`} />
            {refreshLoading ? 'Refreshing…' : 'Refresh token'}
          </button>
        </div>

        <form onSubmit={submitCode} className="space-y-2 pt-3 border-t border-gray-800/80">
          <label className="text-[11px] text-gray-400 font-mono block" htmlFor="oauth-code">
            Paste the Schwab redirect URL or authorization code
          </label>
          <div className="flex gap-2">
            <input
              id="oauth-code"
              type="text"
              value={authCode}
              onChange={(e) => setAuthCode(e.target.value)}
              placeholder="https://127.0.0.1/?code=…"
              className="flex-1 min-w-0 bg-[#151c2e] border border-gray-700 rounded-lg px-3 py-2 text-xs font-mono text-gray-200 placeholder-gray-500 focus:outline-none focus:border-cyan-500"
            />
            <button
              type="submit"
              disabled={authLoading || offline || !authCode.trim()}
              className="px-4 py-2 rounded-lg text-xs font-mono font-semibold text-white bg-cyan-600 hover:bg-cyan-500 disabled:opacity-50 transition-colors flex items-center"
            >
              {authLoading ? <RefreshCw className="w-3.5 h-3.5 animate-spin" /> : <Zap className="w-3.5 h-3.5 mr-1" />}
              Vault
            </button>
          </div>
        </form>

        <Feedback result={authResult} />
      </section>

      <section className="bg-[#0e1422] border border-rose-900/40 rounded-xl p-4 sm:p-5 shadow-2xl space-y-4">
        <div className="flex items-center gap-2.5 pb-3 border-b border-gray-800">
          <div className="p-2 rounded-lg bg-rose-500/10 text-rose-400 border border-rose-500/20">
            <AlertOctagon className="w-4 h-4" />
          </div>
          <h3 className="font-semibold text-gray-100 text-sm font-mono uppercase tracking-wider">Emergency Controls</h3>
        </div>

        <Feedback result={emergencyResult} />

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div className="p-4 bg-[#111827] border border-gray-800 rounded-xl flex flex-col justify-between gap-3">
            <div className="flex items-center justify-between">
              <span className="text-xs font-mono font-bold text-rose-400 uppercase tracking-wider flex items-center">
                <Power className="w-4 h-4 mr-1.5" />
                Kill switch
              </span>
              <span
                className={`px-2 py-0.5 rounded text-[10px] font-mono font-bold ${
                  halted ? 'bg-rose-500 text-white' : 'bg-gray-800 text-gray-400'
                }`}
              >
                {offline ? 'UNKNOWN' : halted ? 'HALTED' : 'ARMED'}
              </span>
            </div>
            <p className="text-[11px] text-gray-400 font-mono">Stops new entries. Open positions keep their stops.</p>
            <button
              type="button"
              onClick={toggleHalt}
              disabled={haltLoading || offline}
              className={`w-full py-2.5 px-4 rounded-lg text-xs font-mono font-bold transition-all flex items-center justify-center disabled:opacity-50 ${
                halted ? 'bg-emerald-600 hover:bg-emerald-500 text-white' : 'bg-rose-600 hover:bg-rose-500 text-white'
              }`}
            >
              {haltLoading ? <RefreshCw className="w-4 h-4 mr-2 animate-spin" /> : <AlertOctagon className="w-4 h-4 mr-2" />}
              {halted ? 'Resume engine' : 'Halt new entries'}
            </button>
          </div>

          <div className="p-4 bg-[#111827] border border-gray-800 rounded-xl flex flex-col justify-between gap-3">
            <div className="flex items-center justify-between">
              <span className="text-xs font-mono font-bold text-amber-400 uppercase tracking-wider flex items-center">
                <Flame className="w-4 h-4 mr-1.5" />
                Flatten now
              </span>
            </div>
            <p className="text-[11px] text-gray-400 font-mono">Halts entries, cancels the engine's working orders, sells engine-managed positions.</p>
            <button
              type="button"
              onClick={liquidate}
              disabled={sweepLoading || offline}
              className="w-full py-2.5 px-4 rounded-lg text-xs font-mono font-bold bg-amber-600 hover:bg-amber-500 text-black transition-all flex items-center justify-center disabled:opacity-50"
            >
              <RefreshCw className={`w-4 h-4 mr-2 ${sweepLoading ? 'animate-spin' : ''}`} />
              {sweepLoading ? 'Flattening…' : 'Flatten positions'}
            </button>
          </div>
        </div>
      </section>
    </div>
  );
};
