import React, { useState, useEffect } from 'react';
import { 
  ShieldCheck, 
  ShieldAlert, 
  Key, 
  Sliders, 
  AlertOctagon, 
  RefreshCw, 
  Zap, 
  Cpu, 
  CheckCircle2, 
  XCircle,
  Clock,
  Flame,
  Power,
  RotateCcw,
  Sparkles,
  Layers,
  ChevronDown
} from 'lucide-react';
import { EngineStatus } from '../types';
import { 
  exchangeOAuthCode, 
  toggleStrategy, 
  updateStrategyConfig, 
  triggerPortfolioScan,
  refreshOAuthToken,
  triggerLiquidationSweep,
  setEmergencyHalt
} from '../services/api';

interface SchwabControlsProps {
  status: EngineStatus;
  onRefresh: () => void;
}

export const SchwabControls: React.FC<SchwabControlsProps> = ({ status, onRefresh }) => {
  // OAuth Vault State
  const [authCode, setAuthCode] = useState('');
  const [authLoading, setAuthLoading] = useState(false);
  const [refreshLoading, setRefreshLoading] = useState(false);
  const [authFeedback, setAuthFeedback] = useState<{ success: boolean; msg: string } | null>(null);

  // 30-minute Access Token Countdown (starts at 24m 35s = 1475s for realistic simulation)
  const [accessTokenSeconds, setAccessTokenSeconds] = useState(1475);
  // 7-day Refresh Token Countdown (6 days, 14 hours)
  const [refreshTokenDays] = useState(6);
  const [refreshTokenHours] = useState(14);

  // Strategies & Controls
  const [regimeAEnabled, setRegimeAEnabled] = useState(true);
  const [regimeCEnabled, setRegimeCEnabled] = useState(true);
  const [llmMode, setLlmMode] = useState<'gemini-2.5-pro' | 'gemini-2.5-flash' | 'disabled'>('gemini-2.5-pro');
  const [isScanning, setIsScanning] = useState(false);

  // Emergency Overrides
  const [halted, setHalted] = useState(false);
  const [sweepLoading, setSweepLoading] = useState(false);
  const [emergencyFeedback, setEmergencyFeedback] = useState<{ type: 'halt' | 'sweep'; msg: string } | null>(null);

  // Countdown timer effect
  useEffect(() => {
    const timer = setInterval(() => {
      setAccessTokenSeconds((prev) => (prev > 0 ? prev - 1 : 1800));
    }, 1000);
    return () => clearInterval(timer);
  }, []);

  const formatCountdown = (totalSec: number) => {
    const m = Math.floor(totalSec / 60);
    const s = totalSec % 60;
    return `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
  };

  const handleOAuthSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!authCode.trim()) return;
    setAuthLoading(true);
    setAuthFeedback(null);
    try {
      const res = await exchangeOAuthCode(authCode.trim());
      setAuthFeedback({
        success: res.success,
        msg: res.message || (res.success ? 'OAuth token successfully vaulted!' : 'Token exchange failed'),
      });
      if (res.success) {
        setAuthCode('');
        setAccessTokenSeconds(1800);
        onRefresh();
      }
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Error communicating with engine';
      setAuthFeedback({ success: false, msg: message });
    } finally {
      setAuthLoading(false);
    }
  };

  const handleManualTokenRefresh = async () => {
    setRefreshLoading(true);
    setAuthFeedback(null);
    try {
      const res = await refreshOAuthToken();
      setAuthFeedback({
        success: res.success,
        msg: res.message,
      });
      if (res.success) {
        setAccessTokenSeconds(res.expires_in_seconds || 1800);
        onRefresh();
      }
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Token refresh failed';
      setAuthFeedback({ success: false, msg: message });
    } finally {
      setRefreshLoading(false);
    }
  };

  const handleToggleRegimeA = async () => {
    const next = !regimeAEnabled;
    setRegimeAEnabled(next);
    await toggleStrategy('regime_a', next);
  };

  const handleToggleRegimeC = async () => {
    const next = !regimeCEnabled;
    setRegimeCEnabled(next);
    await toggleStrategy('regime_c', next);
  };

  const handleLlmChange = async (mode: 'gemini-2.5-pro' | 'gemini-2.5-flash' | 'disabled') => {
    setLlmMode(mode);
    await updateStrategyConfig({ llm_mode: mode });
  };

  const handleScan = async () => {
    setIsScanning(true);
    await triggerPortfolioScan();
    setTimeout(() => {
      setIsScanning(false);
      onRefresh();
    }, 1200);
  };

  const handleMasterKillSwitch = async () => {
    const nextHaltState = !halted;
    setHalted(nextHaltState);
    const res = await setEmergencyHalt(nextHaltState);
    setEmergencyFeedback({
      type: 'halt',
      msg: res.message,
    });
    onRefresh();
  };

  const handleFlatToCashSweep = async () => {
    if (!window.confirm('Execute 15:55 Flat-to-Cash Sweep? This will cancel all open orders and liquidate all 3 managed ETF positions (SOXL, TQQQ, TNA) at market into Bucket 2 Cash.')) {
      return;
    }
    setSweepLoading(true);
    try {
      const res = await triggerLiquidationSweep();
      setEmergencyFeedback({
        type: 'sweep',
        msg: res.message,
      });
      onRefresh();
    } finally {
      setSweepLoading(false);
    }
  };

  const isConnected = status.auth_status === 'AUTHORIZED';
  const tokenProgressPct = (accessTokenSeconds / 1800) * 100;

  return (
    <div className="space-y-6">
      {/* Top Grid: Auth Vault & Algorithmic Switchboard */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Card 1: Schwab OAuth 2.0 Token Lifecycle */}
        <div className="bg-[#0e1422] border border-gray-800 rounded-xl p-6 shadow-2xl flex flex-col justify-between">
          <div>
            <div className="flex items-center justify-between pb-4 border-b border-gray-800">
              <div className="flex items-center space-x-3">
                <div className={`p-2.5 rounded-lg border ${isConnected ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20' : 'bg-amber-500/10 text-amber-400 border-amber-500/20'}`}>
                  <Key className="w-5 h-5" />
                </div>
                <div>
                  <h3 className="font-semibold text-gray-100 font-mono tracking-tight text-sm">
                    Schwab OAuth 2.0 Token Vault
                  </h3>
                  <p className="text-xs text-gray-400 font-mono">
                    AES-256 Vaulted PKCE Session Lifecycle
                  </p>
                </div>
              </div>

              {/* Status Badge */}
              <span className={`inline-flex items-center px-2.5 py-1 rounded-full text-[11px] font-mono font-bold ${
                isConnected
                  ? 'bg-emerald-950/80 text-emerald-300 border border-emerald-700/60'
                  : 'bg-amber-950/80 text-amber-300 border border-amber-700/60'
              }`}>
                {isConnected ? <ShieldCheck className="w-3.5 h-3.5 mr-1" /> : <ShieldAlert className="w-3.5 h-3.5 mr-1" />}
                {isConnected ? 'VAULT CONNECTED' : 'AUTH REQUIRED'}
              </span>
            </div>

            {/* Token Lifespan Timers */}
            <div className="grid grid-cols-2 gap-3 mt-4">
              {/* 30-min Access Token Countdown */}
              <div className="bg-[#111827] border border-gray-800/80 rounded-lg p-3">
                <div className="flex items-center justify-between text-xs text-gray-400 mb-1">
                  <span className="flex items-center font-mono">
                    <Clock className="w-3.5 h-3.5 mr-1 text-cyan-400" />
                    Access Token (30m)
                  </span>
                  <span className="font-mono text-cyan-300 font-bold">
                    {formatCountdown(accessTokenSeconds)}
                  </span>
                </div>
                {/* Progress bar */}
                <div className="w-full h-1.5 bg-gray-800 rounded-full overflow-hidden mt-2">
                  <div 
                    className="h-full bg-cyan-400 transition-all duration-1000"
                    style={{ width: `${tokenProgressPct}%` }}
                  />
                </div>
                <span className="text-[10px] text-gray-500 font-mono block mt-1">
                  Auto-rotates before 1800s expiry
                </span>
              </div>

              {/* 7-day Refresh Token Badge */}
              <div className="bg-[#111827] border border-gray-800/80 rounded-lg p-3">
                <div className="flex items-center justify-between text-xs text-gray-400 mb-1">
                  <span className="flex items-center font-mono">
                    <RotateCcw className="w-3.5 h-3.5 mr-1 text-emerald-400" />
                    Refresh Vault (7d)
                  </span>
                  <span className="font-mono text-emerald-300 font-bold">
                    {refreshTokenDays}d {refreshTokenHours}h
                  </span>
                </div>
                <div className="w-full h-1.5 bg-gray-800 rounded-full overflow-hidden mt-2">
                  <div className="h-full bg-emerald-400 w-[93%]" />
                </div>
                <span className="text-[10px] text-gray-500 font-mono block mt-1">
                  PBKDF2 key derivation secured
                </span>
              </div>
            </div>

            {/* Force Refresh & Manual Vaulting */}
            <div className="mt-4 space-y-3">
              <div className="flex items-center justify-between">
                <span className="text-xs text-gray-400 font-mono">Vault Controls:</span>
                <button
                  onClick={handleManualTokenRefresh}
                  disabled={refreshLoading}
                  className="px-3 py-1.5 bg-gray-800 hover:bg-gray-700 border border-gray-700 rounded-lg text-xs font-mono font-semibold text-gray-200 flex items-center transition-colors disabled:opacity-50"
                >
                  <RefreshCw className={`w-3.5 h-3.5 mr-1.5 ${refreshLoading ? 'animate-spin text-cyan-400' : 'text-gray-400'}`} />
                  {refreshLoading ? 'Refreshing Vault...' : 'Force Token Refresh (/auth/refresh)'}
                </button>
              </div>

              {/* Manual OAuth Exchange Form */}
              <form onSubmit={handleOAuthSubmit} className="space-y-2 pt-2 border-t border-gray-800/80">
                <label className="text-[11px] text-gray-400 font-mono block">
                  Paste Schwab PKCE Redirect Code to Vault Fresh Token:
                </label>
                <div className="flex space-x-2">
                  <input
                    type="text"
                    value={authCode}
                    onChange={(e) => setAuthCode(e.target.value)}
                    placeholder="https://127.0.0.1/?code=... or authorization string"
                    className="flex-1 bg-[#151c2e] border border-gray-700 rounded-lg px-3 py-2 text-xs font-mono text-gray-200 placeholder-gray-500 focus:outline-none focus:border-cyan-500"
                  />
                  <button
                    type="submit"
                    disabled={authLoading || !authCode.trim()}
                    className="px-4 py-2 rounded-lg text-xs font-mono font-semibold text-white bg-cyan-600 hover:bg-cyan-500 disabled:opacity-50 transition-colors flex items-center shadow"
                  >
                    {authLoading ? (
                      <RefreshCw className="w-3.5 h-3.5 animate-spin" />
                    ) : (
                      <Zap className="w-3.5 h-3.5 mr-1" />
                    )}
                    Vault
                  </button>
                </div>
              </form>

              {authFeedback && (
                <div className={`p-3 rounded-lg text-xs font-mono flex items-center space-x-2 ${
                  authFeedback.success 
                    ? 'bg-emerald-950/60 text-emerald-300 border border-emerald-800' 
                    : 'bg-rose-950/60 text-rose-300 border border-rose-800'
                }`}>
                  {authFeedback.success ? <CheckCircle2 className="w-4 h-4 flex-shrink-0" /> : <XCircle className="w-4 h-4 flex-shrink-0" />}
                  <span>{authFeedback.msg}</span>
                </div>
              )}
            </div>
          </div>
        </div>

        {/* Card 2: Algorithmic Regime Toggles & Quantitative Filters */}
        <div className="bg-[#0e1422] border border-gray-800 rounded-xl p-6 shadow-2xl flex flex-col justify-between">
          <div>
            <div className="flex items-center justify-between pb-4 border-b border-gray-800">
              <div className="flex items-center space-x-3">
                <div className="p-2.5 rounded-lg bg-cyan-500/10 text-cyan-400 border border-cyan-500/20">
                  <Sliders className="w-5 h-5" />
                </div>
                <div>
                  <h3 className="font-semibold text-gray-100 font-mono tracking-tight text-sm">
                    Algorithmic Regime Switchboard
                  </h3>
                  <p className="text-xs text-gray-400 font-mono">
                    Deterministic Quantitative Strategy Filters
                  </p>
                </div>
              </div>
              <span className="text-xs font-mono text-cyan-400 bg-cyan-950/80 px-2.5 py-1 rounded border border-cyan-800">
                Tier 1 Core
              </span>
            </div>

            {/* Regime Toggles */}
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 mt-4">
              {/* Regime A: 15m ORB */}
              <div className={`p-4 rounded-xl border transition-all ${
                regimeAEnabled
                  ? 'bg-emerald-950/20 border-emerald-600/60 text-gray-100 shadow-md'
                  : 'bg-gray-900/60 border-gray-800 text-gray-400'
              }`}>
                <div className="flex items-center justify-between mb-2">
                  <span className="text-xs font-mono font-bold uppercase tracking-wider text-emerald-400 flex items-center">
                    <Sparkles className="w-3.5 h-3.5 mr-1" />
                    Regime A: 15m ORB
                  </span>
                  <button
                    onClick={handleToggleRegimeA}
                    className={`px-2 py-0.5 rounded text-[10px] font-mono font-bold border transition-colors ${
                      regimeAEnabled
                        ? 'bg-emerald-500 text-black border-emerald-400'
                        : 'bg-gray-800 text-gray-400 border-gray-700'
                    }`}
                  >
                    {regimeAEnabled ? 'ACTIVE' : 'MUTED'}
                  </button>
                </div>
                <p className="text-[11px] text-gray-300 font-mono mb-2">
                  Opening Range Breakout (09:30–09:45 EDT)
                </p>
                <div className="space-y-1 text-[10px] font-mono text-gray-400 pt-2 border-t border-gray-800/80">
                  <div className="flex justify-between">
                    <span>Choppiness Index:</span>
                    <span className="text-emerald-400 font-semibold">CI &lt; 38.2 (Expansion)</span>
                  </div>
                  <div className="flex justify-between">
                    <span>Relative Volume:</span>
                    <span className="text-emerald-400 font-semibold">RVOL &ge; 1.5x</span>
                  </div>
                </div>
              </div>

              {/* Regime C: VWAP Mean Reversion */}
              <div className={`p-4 rounded-xl border transition-all ${
                regimeCEnabled
                  ? 'bg-blue-950/20 border-blue-600/60 text-gray-100 shadow-md'
                  : 'bg-gray-900/60 border-gray-800 text-gray-400'
              }`}>
                <div className="flex items-center justify-between mb-2">
                  <span className="text-xs font-mono font-bold uppercase tracking-wider text-blue-400 flex items-center">
                    <Layers className="w-3.5 h-3.5 mr-1" />
                    Regime C: VWAP MR
                  </span>
                  <button
                    onClick={handleToggleRegimeC}
                    className={`px-2 py-0.5 rounded text-[10px] font-mono font-bold border transition-colors ${
                      regimeCEnabled
                        ? 'bg-blue-500 text-black border-blue-400'
                        : 'bg-gray-800 text-gray-400 border-gray-700'
                    }`}
                  >
                    {regimeCEnabled ? 'ACTIVE' : 'MUTED'}
                  </button>
                </div>
                <p className="text-[11px] text-gray-300 font-mono mb-2">
                  Volatility Band Pullback Mean Reversion
                </p>
                <div className="space-y-1 text-[10px] font-mono text-gray-400 pt-2 border-t border-gray-800/80">
                  <div className="flex justify-between">
                    <span>Band Extension:</span>
                    <span className="text-blue-300 font-semibold">&plusmn;2.0 NATR / VWAP</span>
                  </div>
                  <div className="flex justify-between">
                    <span>Mean Reversion Guard:</span>
                    <span className="text-blue-300 font-semibold">CI &gt; 61.8, RSI &le; 28</span>
                  </div>
                </div>
              </div>
            </div>

            {/* Active Permissions Status Banner */}
            <div className="mt-4 p-3 bg-[#111827] border border-gray-800 rounded-lg flex items-center justify-between text-xs font-mono">
              <span className="text-gray-400">Strategy Permissions:</span>
              <span className="inline-flex items-center text-emerald-400 font-semibold">
                <CheckCircle2 className="w-3.5 h-3.5 mr-1" />
                {regimeAEnabled && regimeCEnabled ? 'DUAL-REGIME ROUTING ARMED' : regimeAEnabled ? 'REGIME A ONLY' : regimeCEnabled ? 'REGIME C ONLY' : 'ALL ROUTING PAUSED'}
              </span>
            </div>
          </div>

          {/* Book Scan Trigger */}
          <div className="pt-4 border-t border-gray-800/80">
            <button
              onClick={handleScan}
              disabled={isScanning}
              className="w-full py-2 px-3.5 bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-200 rounded-lg text-xs font-mono font-semibold flex items-center justify-center transition-colors"
            >
              <RefreshCw className={`w-3.5 h-3.5 mr-2 ${isScanning ? 'animate-spin text-cyan-400' : 'text-gray-400'}`} />
              {isScanning ? 'Scanning Sandbox Universe (SOXL, TQQQ, TNA)...' : 'Scan High-Beta Universe Signals'}
            </button>
          </div>
        </div>
      </div>

      {/* Middle Card: Tier 2 Gemini Governor Controls & Vertex AI Project Allocation */}
      <div className="bg-[#0e1422] border border-gray-800 rounded-xl p-6 shadow-2xl">
        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between pb-4 border-b border-gray-800 gap-3">
          <div className="flex items-center space-x-3">
            <div className="p-2.5 rounded-lg bg-purple-500/10 text-purple-400 border border-purple-500/20">
              <Cpu className="w-5 h-5" />
            </div>
            <div>
              <div className="flex items-center space-x-2">
                <h3 className="font-semibold text-gray-100 font-mono tracking-tight text-sm">
                  Tier 2 Gemini Strategy Governor
                </h3>
                <span className="px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-purple-950 text-purple-300 border border-purple-800">
                  Off-Market Daemon
                </span>
              </div>
              <p className="text-xs text-gray-400 font-mono mt-0.5">
                Vertex AI cloud model allocation targeting $300 GCP credit drawdown (Project: gen-lang-client-0334702303)
              </p>
            </div>
          </div>

          {/* Model Selector Dropdown */}
          <div className="flex items-center space-x-2">
            <span className="text-xs text-gray-400 font-mono">Governor Model:</span>
            <div className="relative">
              <select
                value={llmMode}
                onChange={(e) => handleLlmChange(e.target.value as 'gemini-2.5-pro' | 'gemini-2.5-flash' | 'disabled')}
                className="bg-[#111827] border border-purple-600/50 rounded-lg px-3 py-1.5 text-xs font-mono font-semibold text-purple-200 focus:outline-none focus:border-purple-400 pr-8 appearance-none cursor-pointer"
              >
                <option value="gemini-2.5-pro">gemini-2.5-pro (Deep Reflection)</option>
                <option value="gemini-2.5-flash">gemini-2.5-flash (Rapid Macro)</option>
                <option value="disabled">disabled (Pure Quant Engine)</option>
              </select>
              <ChevronDown className="w-3.5 h-3.5 text-purple-400 absolute right-2.5 top-2.5 pointer-events-none" />
            </div>
          </div>
        </div>

        {/* Governor Status & Scheduling Grid */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mt-4 font-mono">
          {/* Target Cloud Project */}
          <div className="p-3.5 bg-[#111827] border border-gray-800 rounded-lg">
            <span className="text-[10px] text-gray-400 uppercase block">GCP Vertex AI Project</span>
            <div className="text-xs font-bold text-gray-100 mt-1">
              gen-lang-client-0334702303
            </div>
            <span className="text-[10px] text-emerald-400 block mt-0.5">
              $300 Cloud Credit Active (Exp: 2026-12-29 / 261229)
            </span>
          </div>

          {/* Scheduled Execution Windows */}
          <div className="p-3.5 bg-[#111827] border border-gray-800 rounded-lg">
            <span className="text-[10px] text-gray-400 uppercase block">Autonomous Governor Schedule</span>
            <div className="text-xs text-gray-200 mt-1 space-y-0.5">
              <div><span className="text-purple-300 font-bold">08:35 EDT:</span> Pre-Market Macro Gatekeeper</div>
              <div><span className="text-purple-300 font-bold">16:15 EDT:</span> Post-Market Trade Compression</div>
            </div>
            <span className="text-[10px] text-gray-500 block mt-0.5">
              Runs strictly off-market to prevent Tier 1 jitter
            </span>
          </div>

          {/* Partner Model Safety Guard */}
          <div className="p-3.5 bg-[#111827] border border-purple-900/40 rounded-lg">
            <span className="text-[10px] text-gray-400 uppercase block">Strict Model Firewall</span>
            <div className="text-xs text-purple-300 font-bold mt-1">
              First-Party Gemini Models Only
            </div>
            <span className="text-[10px] text-gray-400 block mt-0.5">
              Third-party partner models blocked to eliminate out-of-pocket charges
            </span>
          </div>
        </div>
      </div>

      {/* Bottom Card: Circuit Breakers & Emergency Overrides */}
      <div className="bg-[#0e1422] border border-rose-900/40 rounded-xl p-6 shadow-2xl">
        <div className="flex items-center justify-between pb-4 border-b border-gray-800">
          <div className="flex items-center space-x-3">
            <div className="p-2.5 rounded-lg bg-rose-500/10 text-rose-400 border border-rose-500/20">
              <AlertOctagon className="w-5 h-5" />
            </div>
            <div>
              <h3 className="font-semibold text-gray-100 font-mono tracking-tight text-sm">
                Circuit Breakers & Emergency Overrides
              </h3>
              <p className="text-xs text-gray-400 font-mono">
                Hard deterministic safeguards: -3.0% ($30.00) account floor & 15:55 flat sweeps
              </p>
            </div>
          </div>
          <span className="text-xs font-mono text-rose-400 bg-rose-950/80 px-2.5 py-1 rounded border border-rose-800">
            SEC T+1 Safe
          </span>
        </div>

        {emergencyFeedback && (
          <div className="mt-4 p-3 bg-rose-950/50 border border-rose-800 rounded-lg text-xs font-mono text-rose-200 flex items-center space-x-2">
            <AlertOctagon className="w-4 h-4 text-rose-400 flex-shrink-0" />
            <span>{emergencyFeedback.msg}</span>
          </div>
        )}

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 mt-4">
          {/* Master Kill Switch */}
          <div className="p-4 bg-[#111827] border border-gray-800 rounded-xl flex flex-col justify-between">
            <div>
              <div className="flex items-center justify-between mb-2">
                <span className="text-xs font-mono font-bold text-rose-400 uppercase tracking-wider flex items-center">
                  <Power className="w-4 h-4 mr-1.5" />
                  Master Kill Switch
                </span>
                <span className={`px-2 py-0.5 rounded text-[10px] font-mono font-bold ${
                  halted ? 'bg-rose-500 text-white' : 'bg-gray-800 text-gray-400'
                }`}>
                  {halted ? 'HALTED' : 'STANDBY'}
                </span>
              </div>
              <p className="text-xs text-gray-400 font-mono">
                Instantly disconnects Tier 1 execution router. Suspends all incoming broker signals, rejects new orders, and holds active state.
              </p>
            </div>
            <button
              onClick={handleMasterKillSwitch}
              className={`mt-4 w-full py-2.5 px-4 rounded-lg text-xs font-mono font-bold transition-all shadow-lg flex items-center justify-center ${
                halted
                  ? 'bg-emerald-600 hover:bg-emerald-500 text-white'
                  : 'bg-rose-600 hover:bg-rose-500 text-white'
              }`}
            >
              <AlertOctagon className="w-4 h-4 mr-2" />
              {halted ? 'RESUME TRADING ENGINE' : 'ENGAGE MASTER KILL SWITCH'}
            </button>
          </div>

          {/* Manual 15:55 Flat-to-Cash Sweep */}
          <div className="p-4 bg-[#111827] border border-gray-800 rounded-xl flex flex-col justify-between">
            <div>
              <div className="flex items-center justify-between mb-2">
                <span className="text-xs font-mono font-bold text-amber-400 uppercase tracking-wider flex items-center">
                  <Flame className="w-4 h-4 mr-1.5" />
                  15:55 Flat-to-Cash Sweep
                </span>
                <span className="px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-amber-950 text-amber-300 border border-amber-800">
                  MARKET CLOSE
                </span>
              </div>
              <p className="text-xs text-gray-400 font-mono">
                Mandatory risk-elimination sweep. Cancels open working orders and sends market sell orders for SOXL, TQQQ, and TNA into Bucket 2 Cash.
              </p>
            </div>
            <button
              onClick={handleFlatToCashSweep}
              disabled={sweepLoading}
              className="mt-4 w-full py-2.5 px-4 rounded-lg text-xs font-mono font-bold bg-amber-600 hover:bg-amber-500 text-black transition-all shadow-lg flex items-center justify-center disabled:opacity-50"
            >
              <RefreshCw className={`w-4 h-4 mr-2 ${sweepLoading ? 'animate-spin' : ''}`} />
              {sweepLoading ? 'Sweeping Positions...' : 'TRIGGER 15:55 LIQUIDATION SWEEP'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};
