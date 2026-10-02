import React from 'react';
import { 
  Activity, 
  Terminal, 
  TrendingUp, 
  Layers, 
  ShieldCheck, 
  Radio, 
  RefreshCw,
  Clock
} from 'lucide-react';
import { EngineStatus } from '../types';

interface DashboardLayoutProps {
  status: EngineStatus;
  onRefresh: () => void;
  isRefreshing: boolean;
  activeTab: 'overview' | 'positions' | 'controls' | 'ledger';
  setActiveTab: (tab: 'overview' | 'positions' | 'controls' | 'ledger') => void;
  children: React.ReactNode;
}

export const DashboardLayout: React.FC<DashboardLayoutProps> = ({
  status,
  onRefresh,
  isRefreshing,
  activeTab,
  setActiveTab,
  children,
}) => {
  const formatUptime = (seconds: number) => {
    const hrs = Math.floor(seconds / 3600);
    const mins = Math.floor((seconds % 3600) / 60);
    return `${hrs}h ${mins}m`;
  };

  const isLive = status.is_connected && status.engine_mode === 'LIVE_DAEMON';

  return (
    <div className="min-h-screen bg-[#070b12] text-gray-100 flex flex-col font-sans selection:bg-cyan-500/30 selection:text-cyan-200">
      {/* Top Telemetry Header */}
      <header className="border-b border-gray-800/80 bg-[#0d1322]/90 backdrop-blur sticky top-0 z-50">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex items-center justify-between h-16">
            {/* Logo & Platform Info */}
            <div className="flex items-center space-x-3">
              <div className="w-9 h-9 rounded-xl bg-gradient-to-tr from-cyan-600 via-blue-600 to-emerald-500 flex items-center justify-center shadow-lg shadow-cyan-500/20 border border-cyan-400/20">
                <Activity className="w-5 h-5 text-white animate-pulse" />
              </div>
              <div>
                <div className="flex items-center space-x-2">
                  <h1 className="font-bold text-base tracking-tight text-white font-mono">
                    SCHWAB<span className="text-cyan-400">ENGINE</span>
                  </h1>
                  <span className="px-1.5 py-0.5 text-[9px] uppercase font-mono tracking-wider font-bold rounded bg-cyan-950 text-cyan-300 border border-cyan-800/80">
                    Tier 1 Core
                  </span>
                  <span className="hidden sm:inline-block px-1.5 py-0.5 text-[9px] font-mono rounded bg-purple-950 text-purple-300 border border-purple-800/80">
                    Vertex AI 2.5
                  </span>
                </div>
                <p className="text-[10px] text-gray-400 font-mono">
                  Autonomous 3X ETF Execution · $1,000 Sandbox Matrix
                </p>
              </div>
            </div>

            {/* Live Engine & Environment Badges */}
            <div className="flex items-center space-x-3">
              {/* Engine Status / Daemon Mode */}
              <div className="flex items-center space-x-2 bg-gray-900/90 px-3 py-1.5 rounded-lg border border-gray-800 text-xs">
                <Radio className={`w-3.5 h-3.5 ${isLive ? 'text-emerald-400 animate-pulse' : 'text-cyan-400'}`} />
                <span className="text-gray-400 hidden sm:inline">Engine:</span>
                <span className={`font-mono font-semibold text-[11px] ${isLive ? 'text-emerald-400' : 'text-cyan-300'}`}>
                  {isLive ? 'LIVE (8080)' : 'SANDBOX SIM'}
                </span>
              </div>

              {/* Schwab Auth Badge */}
              <div className="hidden md:flex items-center space-x-2 bg-gray-900/90 px-3 py-1.5 rounded-lg border border-gray-800 text-xs">
                <ShieldCheck className={`w-3.5 h-3.5 ${status.auth_status === 'AUTHORIZED' ? 'text-emerald-400' : 'text-amber-400'}`} />
                <span className="text-gray-400">OAuth Vault:</span>
                <span className={`font-mono font-semibold text-[11px] ${status.auth_status === 'AUTHORIZED' ? 'text-emerald-400' : 'text-amber-400'}`}>
                  {status.auth_status}
                </span>
              </div>

              {/* Uptime */}
              <div className="hidden lg:block text-right text-xs font-mono">
                <span className="text-gray-500 block text-[9px]">UPTIME</span>
                <span className="text-gray-300 font-semibold">{formatUptime(status.uptime_seconds)}</span>
              </div>

              {/* Manual Refresh */}
              <button
                onClick={onRefresh}
                disabled={isRefreshing}
                className="p-2 rounded-lg bg-gray-800 hover:bg-gray-700 text-gray-300 hover:text-white transition-colors border border-gray-700"
                title="Poll Telemetry"
              >
                <RefreshCw className={`w-4 h-4 ${isRefreshing ? 'animate-spin text-cyan-400' : ''}`} />
              </button>
            </div>
          </div>
        </div>

        {/* Operating Window & Safeguard Strip */}
        <div className="bg-[#090e18] border-t border-b border-gray-800/80 px-4 sm:px-6 lg:px-8 py-1.5 flex flex-wrap items-center justify-between text-[11px] font-mono text-gray-400 gap-2">
          <div className="flex items-center space-x-2">
            <Clock className="w-3.5 h-3.5 text-cyan-400" />
            <span className="text-gray-300 font-semibold">Execution Windows:</span>
            <span className="text-gray-400">08:35 Pre-Market Macro</span>
            <span className="text-gray-600">→</span>
            <span className="text-emerald-400 font-semibold">09:30–15:55 Active Hours</span>
            <span className="text-gray-600">→</span>
            <span className="text-rose-400 font-semibold">15:55 Flat Sweep</span>
            <span className="text-gray-600">→</span>
            <span className="text-purple-400 font-semibold">16:15 Reflection</span>
          </div>

          <div className="flex items-center space-x-4">
            <div className="flex items-center space-x-1.5">
              <span className="text-gray-400">Circuit Breaker:</span>
              <span className="text-rose-400 font-bold">-$30.00 (-3.0%)</span>
            </div>
            <span className="text-gray-700 hidden sm:inline">|</span>
            <div className="hidden sm:flex items-center space-x-1.5">
              <span className="text-gray-400">Single Cap:</span>
              <span className="text-cyan-300 font-bold">$200.00 (20%)</span>
            </div>
          </div>
        </div>

        {/* Navigation Tabs */}
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 flex space-x-1 sm:space-x-4">
          <button
            onClick={() => setActiveTab('overview')}
            className={`py-3 px-3 text-xs font-mono font-medium border-b-2 flex items-center space-x-2 transition-all ${
              activeTab === 'overview'
                ? 'border-cyan-400 text-cyan-400 font-bold'
                : 'border-transparent text-gray-400 hover:text-gray-200 hover:border-gray-700'
            }`}
          >
            <TrendingUp className="w-3.5 h-3.5" />
            <span>Overview & Audit</span>
          </button>

          <button
            onClick={() => setActiveTab('positions')}
            className={`py-3 px-3 text-xs font-mono font-medium border-b-2 flex items-center space-x-2 transition-all ${
              activeTab === 'positions'
                ? 'border-cyan-400 text-cyan-400 font-bold'
                : 'border-transparent text-gray-400 hover:text-gray-200 hover:border-gray-700'
            }`}
          >
            <Layers className="w-3.5 h-3.5" />
            <span>High-Beta ETF Positions</span>
          </button>

          <button
            onClick={() => setActiveTab('controls')}
            className={`py-3 px-3 text-xs font-mono font-medium border-b-2 flex items-center space-x-2 transition-all ${
              activeTab === 'controls'
                ? 'border-cyan-400 text-cyan-400 font-bold'
                : 'border-transparent text-gray-400 hover:text-gray-200 hover:border-gray-700'
            }`}
          >
            <Terminal className="w-3.5 h-3.5" />
            <span>Schwab Vault & AI Controls</span>
          </button>

          <button
            onClick={() => setActiveTab('ledger')}
            className={`py-3 px-3 text-xs font-mono font-medium border-b-2 flex items-center space-x-2 transition-all ${
              activeTab === 'ledger'
                ? 'border-cyan-400 text-cyan-400 font-bold'
                : 'border-transparent text-gray-400 hover:text-gray-200 hover:border-gray-700'
            }`}
          >
            <ShieldCheck className="w-3.5 h-3.5" />
            <span>3-Bucket GFV Ledger</span>
          </button>
        </div>
      </header>

      {/* Main Content Area */}
      <main className="flex-1 max-w-7xl w-full mx-auto px-4 sm:px-6 lg:px-8 py-6">
        {children}
      </main>

      {/* Footer */}
      <footer className="border-t border-gray-900 bg-[#060a10] py-4 text-center text-xs text-gray-600 font-mono">
        SchwabEngine · Production GCP VM 8080 · Vertex AI gen-lang-client-0334702303 · Google AI Studio Build Mode
      </footer>
    </div>
  );
};
