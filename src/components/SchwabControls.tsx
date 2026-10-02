import React, { useState } from 'react';
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
  XCircle 
} from 'lucide-react';
import { EngineStatus } from '../types';
import { exchangeOAuthCode, toggleStrategy, updateStrategyConfig, triggerPortfolioScan } from '../services/api';

interface SchwabControlsProps {
  status: EngineStatus;
  onRefresh: () => void;
}

export const SchwabControls: React.FC<SchwabControlsProps> = ({ status, onRefresh }) => {
  const [authCode, setAuthCode] = useState('');
  const [authLoading, setAuthLoading] = useState(false);
  const [authFeedback, setAuthFeedback] = useState<{ success: boolean; msg: string } | null>(null);

  const [regimeAEnabled, setRegimeAEnabled] = useState(true);
  const [regimeCEnabled, setRegimeCEnabled] = useState(true);
  const [llmMode, setLlmMode] = useState<'pro' | 'flash' | 'disabled'>('pro');
  const [isScanning, setIsScanning] = useState(false);
  const [halted, setHalted] = useState(false);

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
        onRefresh();
      }
    } catch (err: any) {
      setAuthFeedback({ success: false, msg: err.message || 'Error communicating with engine' });
    } finally {
      setAuthLoading(false);
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

  const handleLlmChange = async (mode: 'pro' | 'flash' | 'disabled') => {
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

  const handleEmergencyHalt = () => {
    setHalted(!halted);
  };

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
      {/* Schwab OAuth Authentication & Status Card */}
      <div className="bg-[#111827] border border-gray-800 rounded-xl p-6 shadow-lg shadow-black/40">
        <div className="flex items-center justify-between pb-4 border-b border-gray-800">
          <div className="flex items-center space-x-3">
            <div className={`p-2.5 rounded-lg ${status.auth_status === 'AUTHORIZED' ? 'bg-emerald-500/10 text-emerald-400' : 'bg-amber-500/10 text-amber-400'}`}>
              <Key className="w-5 h-5" />
            </div>
            <div>
              <h3 className="font-semibold text-gray-100">Schwab API Authentication</h3>
              <p className="text-xs text-gray-400">OAuth 2.0 PKCE Session & Refresh Vault</p>
            </div>
          </div>
          <span className={`inline-flex items-center px-2.5 py-1 rounded-full text-xs font-medium ${
            status.auth_status === 'AUTHORIZED'
              ? 'bg-emerald-950/80 text-emerald-300 border border-emerald-700/50'
              : 'bg-amber-950/80 text-amber-300 border border-amber-700/50'
          }`}>
            {status.auth_status === 'AUTHORIZED' ? <ShieldCheck className="w-3.5 h-3.5 mr-1" /> : <ShieldAlert className="w-3.5 h-3.5 mr-1" />}
            {status.auth_status}
          </span>
        </div>

        <div className="mt-4 space-y-4">
          <p className="text-xs text-gray-300 leading-relaxed">
            Engine maintains automatic 7-day token rotation via encrypted file vault. Enter a fresh OAuth redirect code URL if manual re-authentication is required:
          </p>

          <form onSubmit={handleOAuthSubmit} className="space-y-3">
            <div className="relative">
              <input
                type="text"
                value={authCode}
                onChange={(e) => setAuthCode(e.target.value)}
                placeholder="Paste code or returned redirect URL from Schwab portal..."
                className="w-full bg-[#1e293b] border border-gray-700 rounded-lg px-3.5 py-2.5 text-xs text-gray-200 placeholder-gray-500 focus:outline-none focus:border-cyan-500 focus:ring-1 focus:ring-cyan-500"
              />
            </div>
            <button
              type="submit"
              disabled={authLoading || !authCode.trim()}
              className="w-full inline-flex items-center justify-center px-4 py-2.5 rounded-lg text-xs font-semibold text-white bg-cyan-600 hover:bg-cyan-500 disabled:opacity-50 disabled:cursor-not-allowed transition-colors shadow"
            >
              {authLoading ? (
                <RefreshCw className="w-4 h-4 mr-2 animate-spin" />
              ) : (
                <Zap className="w-4 h-4 mr-2" />
              )}
              Exchange & Vault Schwab OAuth Credentials
            </button>
          </form>

          {authFeedback && (
            <div className={`p-3 rounded-lg text-xs flex items-center space-x-2 ${
              authFeedback.success ? 'bg-emerald-950/60 text-emerald-300 border border-emerald-800' : 'bg-rose-950/60 text-rose-300 border border-rose-800'
            }`}>
              {authFeedback.success ? <CheckCircle2 className="w-4 h-4 flex-shrink-0" /> : <XCircle className="w-4 h-4 flex-shrink-0" />}
              <span>{authFeedback.msg}</span>
            </div>
          )}
        </div>
      </div>

      {/* Engine Strategy & Operational Controls */}
      <div className="bg-[#111827] border border-gray-800 rounded-xl p-6 shadow-lg shadow-black/40">
        <div className="flex items-center justify-between pb-4 border-b border-gray-800">
          <div className="flex items-center space-x-3">
            <div className="p-2.5 rounded-lg bg-cyan-500/10 text-cyan-400">
              <Sliders className="w-5 h-5" />
            </div>
            <div>
              <h3 className="font-semibold text-gray-100">Engine Directives & AI Controls</h3>
              <p className="text-xs text-gray-400">Active algorithmic filters and execution policies</p>
            </div>
          </div>
          <span className="text-xs font-mono text-cyan-400 bg-cyan-950/60 px-2 py-1 rounded border border-cyan-800/40">
            FAST-API 8080
          </span>
        </div>

        <div className="mt-4 space-y-4">
          {/* Strategy Switches */}
          <div className="grid grid-cols-2 gap-3">
            <button
              onClick={handleToggleRegimeA}
              className={`p-3 rounded-lg border text-left transition-all ${
                regimeAEnabled
                  ? 'bg-emerald-950/40 border-emerald-600/60 text-emerald-200'
                  : 'bg-gray-900 border-gray-800 text-gray-500'
              }`}
            >
              <div className="flex items-center justify-between mb-1">
                <span className="text-xs font-bold uppercase tracking-wider">Regime A</span>
                <span className={`w-2 h-2 rounded-full ${regimeAEnabled ? 'bg-emerald-400 animate-pulse' : 'bg-gray-600'}`} />
              </div>
              <p className="text-[11px] text-gray-400">Momentum & Vol Expansion</p>
            </button>

            <button
              onClick={handleToggleRegimeC}
              className={`p-3 rounded-lg border text-left transition-all ${
                regimeCEnabled
                  ? 'bg-blue-950/40 border-blue-600/60 text-blue-200'
                  : 'bg-gray-900 border-gray-800 text-gray-500'
              }`}
            >
              <div className="flex items-center justify-between mb-1">
                <span className="text-xs font-bold uppercase tracking-wider">Regime C</span>
                <span className={`w-2 h-2 rounded-full ${regimeCEnabled ? 'bg-blue-400 animate-pulse' : 'bg-gray-600'}`} />
              </div>
              <p className="text-[11px] text-gray-400">Mean Reversion Pullbacks</p>
            </button>
          </div>

          {/* Gemini LLM Gatekeeper Mode */}
          <div>
            <label className="block text-xs font-medium text-gray-300 mb-1.5 flex items-center">
              <Cpu className="w-3.5 h-3.5 mr-1.5 text-purple-400" />
              Gemini LLM Macro Gatekeeper Mode
            </label>
            <div className="grid grid-cols-3 gap-2">
              {(['pro', 'flash', 'disabled'] as const).map((m) => (
                <button
                  key={m}
                  onClick={() => handleLlmChange(m)}
                  className={`py-2 px-3 rounded-lg text-xs font-semibold capitalize border transition-all ${
                    llmMode === m
                      ? 'bg-purple-900/50 border-purple-500 text-purple-200'
                      : 'bg-gray-900 border-gray-800 text-gray-400 hover:border-gray-700'
                  }`}
                >
                  {m === 'disabled' ? 'Off' : `Gemini ${m}`}
                </button>
              ))}
            </div>
          </div>

          {/* Trigger Actions */}
          <div className="pt-2 flex items-center space-x-3">
            <button
              onClick={handleScan}
              disabled={isScanning}
              className="flex-1 py-2 px-3.5 bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-200 rounded-lg text-xs font-semibold flex items-center justify-center transition-colors"
            >
              <RefreshCw className={`w-3.5 h-3.5 mr-2 ${isScanning ? 'animate-spin text-cyan-400' : ''}`} />
              {isScanning ? 'Scanning Book...' : 'Trigger Portfolio Scan'}
            </button>

            <button
              onClick={handleEmergencyHalt}
              className={`py-2 px-3.5 border rounded-lg text-xs font-semibold flex items-center justify-center transition-colors ${
                halted
                  ? 'bg-rose-600 hover:bg-rose-500 text-white border-rose-500'
                  : 'bg-rose-950/40 hover:bg-rose-900/60 text-rose-300 border-rose-800/80'
              }`}
            >
              <AlertOctagon className="w-3.5 h-3.5 mr-1.5" />
              {halted ? 'Resume Trading' : 'Emergency Halt'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};
