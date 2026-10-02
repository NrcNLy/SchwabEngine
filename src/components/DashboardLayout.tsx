import React from 'react';
import { 
  Activity, 
  Terminal, 
  TrendingUp, 
  Layers, 
  ShieldCheck, 
  Radio, 
  RefreshCw 
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

  return (
    <div className="min-h-screen bg-[#090d16] text-gray-100 flex flex-col">
      {/* Top Header */}
      <header className="border-b border-gray-800 bg-[#0d1322]/90 backdrop-blur sticky top-0 z-50">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex items-center justify-between h-16">
            {/* Logo & Brand */}
            <div className="flex items-center space-x-3">
              <div className="w-9 h-9 rounded-xl bg-gradient-to-tr from-cyan-600 to-emerald-500 flex items-center justify-center shadow-lg shadow-cyan-500/20">
                <Activity className="w-5 h-5 text-white" />
              </div>
              <div>
                <div className="flex items-center space-x-2">
                  <h1 className="font-bold text-base tracking-tight text-white font-mono">
                    SCHWAB<span className="text-cyan-400">ENGINE</span>
                  </h1>
                  <span className="px-1.5 py-0.5 text-[9px] uppercase font-mono tracking-wider font-semibold rounded bg-cyan-950 text-cyan-300 border border-cyan-800">
                    Live Edge
                  </span>
                </div>
                <p className="text-[10px] text-gray-400">Autonomous ETF Execution & Risk Gateway</p>
              </div>
            </div>

            {/* Live Status Indicators */}
            <div className="flex items-center space-x-4">
              {/* Engine Status Badge */}
              <div className="hidden sm:flex items-center space-x-2 bg-gray-900/80 px-3 py-1.5 rounded-lg border border-gray-800 text-xs">
                <Radio className={`w-3.5 h-3.5 ${status.status === 'ONLINE' ? 'text-emerald-400 animate-pulse' : 'text-amber-400'}`} />
                <span className="text-gray-400">Engine:</span>
                <span className={`font-mono font-semibold ${status.status === 'ONLINE' ? 'text-emerald-400' : 'text-amber-400'}`}>
                  {status.status}
                </span>
              </div>

              {/* Schwab Auth Badge */}
              <div className="hidden md:flex items-center space-x-2 bg-gray-900/80 px-3 py-1.5 rounded-lg border border-gray-800 text-xs">
                <ShieldCheck className={`w-3.5 h-3.5 ${status.auth_status === 'AUTHORIZED' ? 'text-emerald-400' : 'text-amber-400'}`} />
                <span className="text-gray-400">Schwab OAuth:</span>
                <span className={`font-mono font-semibold ${status.auth_status === 'AUTHORIZED' ? 'text-emerald-400' : 'text-amber-400'}`}>
                  {status.auth_status}
                </span>
              </div>

              {/* Uptime */}
              <div className="hidden lg:block text-right text-xs font-mono">
                <span className="text-gray-500 block text-[10px]">UPTIME</span>
                <span className="text-gray-300 font-semibold">{formatUptime(status.uptime_seconds)}</span>
              </div>

              {/* Manual Refresh */}
              <button
                onClick={onRefresh}
                disabled={isRefreshing}
                className="p-2 rounded-lg bg-gray-800 hover:bg-gray-700 text-gray-300 hover:text-white transition-colors border border-gray-700"
                title="Refresh State"
              >
                <RefreshCw className={`w-4 h-4 ${isRefreshing ? 'animate-spin text-cyan-400' : ''}`} />
              </button>
            </div>
          </div>
        </div>

        {/* Navigation Tabs */}
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 border-t border-gray-800/80 flex space-x-1 sm:space-x-4">
          <button
            onClick={() => setActiveTab('overview')}
            className={`py-3 px-3 text-xs font-medium border-b-2 flex items-center space-x-2 transition-all ${
              activeTab === 'overview'
                ? 'border-cyan-400 text-cyan-400 font-semibold'
                : 'border-transparent text-gray-400 hover:text-gray-200 hover:border-gray-700'
            }`}
          >
            <TrendingUp className="w-3.5 h-3.5" />
            <span>Overview</span>
          </button>

          <button
            onClick={() => setActiveTab('positions')}
            className={`py-3 px-3 text-xs font-medium border-b-2 flex items-center space-x-2 transition-all ${
              activeTab === 'positions'
                ? 'border-cyan-400 text-cyan-400 font-semibold'
                : 'border-transparent text-gray-400 hover:text-gray-200 hover:border-gray-700'
            }`}
          >
            <Layers className="w-3.5 h-3.5" />
            <span>ETF Positions & Guard</span>
          </button>

          <button
            onClick={() => setActiveTab('controls')}
            className={`py-3 px-3 text-xs font-medium border-b-2 flex items-center space-x-2 transition-all ${
              activeTab === 'controls'
                ? 'border-cyan-400 text-cyan-400 font-semibold'
                : 'border-transparent text-gray-400 hover:text-gray-200 hover:border-gray-700'
            }`}
          >
            <Terminal className="w-3.5 h-3.5" />
            <span>Schwab Controls & AI</span>
          </button>

          <button
            onClick={() => setActiveTab('ledger')}
            className={`py-3 px-3 text-xs font-medium border-b-2 flex items-center space-x-2 transition-all ${
              activeTab === 'ledger'
                ? 'border-cyan-400 text-cyan-400 font-semibold'
                : 'border-transparent text-gray-400 hover:text-gray-200 hover:border-gray-700'
            }`}
          >
            <ShieldCheck className="w-3.5 h-3.5" />
            <span>GFV Capital Ledger</span>
          </button>
        </div>
      </header>

      {/* Main Content Area */}
      <main className="flex-1 max-w-7xl w-full mx-auto px-4 sm:px-6 lg:px-8 py-6">
        {children}
      </main>

      {/* Footer */}
      <footer className="border-t border-gray-900 bg-[#070b12] py-4 text-center text-xs text-gray-600 font-mono">
        SchwabEngine · Production GCP VM 8080 · Google AI Studio Build Mode Compatible
      </footer>
    </div>
  );
};
