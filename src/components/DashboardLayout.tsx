import React from 'react';
import { Activity, Layers, RefreshCw, ShieldCheck, SlidersHorizontal, TrendingUp } from 'lucide-react';
import { EngineStatus, EnvName } from '../types';
import { VmTelemetryStrip } from './VmTelemetryStrip';
import { StatusPill } from './StatusPill';
import { NetChangePanel } from './NetChangePanel';
import { EnvToggle } from './EnvToggle';

export type DashboardTab = 'overview' | 'positions' | 'ledger' | 'controls';

interface DashboardLayoutProps {
  status: EngineStatus | null;
  engineError: string | null;
  env: EnvName;
  onEnvChange: (env: EnvName) => void;
  onRefresh: () => void;
  isRefreshing: boolean;
  activeTab: DashboardTab;
  setActiveTab: (tab: DashboardTab) => void;
  children: React.ReactNode;
}

const TABS: Array<{ id: DashboardTab; label: string; icon: React.ComponentType<{ className?: string }> }> = [
  { id: 'overview', label: 'Overview', icon: TrendingUp },
  { id: 'positions', label: 'Risk Guard', icon: ShieldCheck },
  { id: 'ledger', label: 'Liquidity', icon: Layers },
  { id: 'controls', label: 'Controls', icon: SlidersHorizontal },
];

export const DashboardLayout: React.FC<DashboardLayoutProps> = ({
  status,
  engineError,
  env,
  onEnvChange,
  onRefresh,
  isRefreshing,
  activeTab,
  setActiveTab,
  children,
}) => {
  return (
    <div className="min-h-screen bg-[#070b12] text-gray-100 flex flex-col font-sans selection:bg-cyan-500/30 selection:text-cyan-200">
      {env === 'sandbox' && (
        <div
          aria-hidden="true"
          className="pointer-events-none fixed inset-0 z-40 flex items-center justify-center overflow-hidden"
        >
          <span className="-rotate-[24deg] select-none font-mono font-black tracking-[0.4em] text-amber-300/[0.06] text-6xl sm:text-8xl">
            SANDBOX
          </span>
        </div>
      )}

      <VmTelemetryStrip status={status} />

      <header className="border-b border-gray-800/80 bg-[#0d1322]/95 backdrop-blur sticky top-0 z-50">
        <div className="max-w-7xl mx-auto px-3 sm:px-6 lg:px-8">
          <div className="flex items-center justify-between h-14 gap-3">
            <div className="flex items-center gap-2.5 min-w-0">
              <div className="w-8 h-8 shrink-0 rounded-lg bg-gradient-to-tr from-cyan-600 via-blue-600 to-emerald-500 flex items-center justify-center border border-cyan-400/20">
                <Activity className="w-4 h-4 text-white" />
              </div>
              <h1 className="font-bold text-base tracking-tight text-white font-mono truncate">SchwabEngine</h1>
              <StatusPill status={status} engineError={engineError} />
            </div>
            <NetChangePanel status={status} />
          </div>

          <div className="flex items-center gap-2 pb-2 -mx-1 px-1 overflow-x-auto">
            <EnvToggle env={env} onChange={onEnvChange} />
            <nav className="flex items-center gap-1 flex-1 min-w-0" aria-label="Sections">
              {TABS.map(({ id, label, icon: Icon }) => {
                const isActive = id === activeTab;
                return (
                  <button
                    key={id}
                    type="button"
                    onClick={() => setActiveTab(id)}
                    className={`flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-mono whitespace-nowrap transition-colors ${
                      isActive ? 'bg-cyan-950/70 text-cyan-300 border border-cyan-800/60' : 'text-gray-400 hover:text-gray-200 hover:bg-gray-800/60 border border-transparent'
                    }`}
                  >
                    <Icon className="w-3.5 h-3.5" />
                    {label}
                  </button>
                );
              })}
            </nav>
            <button
              type="button"
              onClick={onRefresh}
              aria-label="Refresh data"
              className="shrink-0 p-1.5 rounded-lg text-gray-400 hover:text-gray-100 hover:bg-gray-800/60 transition-colors"
            >
              <RefreshCw className={`w-4 h-4 ${isRefreshing ? 'animate-spin text-cyan-400' : ''}`} />
            </button>
          </div>
        </div>
      </header>

      {engineError && (
        <div className="bg-rose-950/60 border-b border-rose-900/60 px-4 py-2 text-xs font-mono text-rose-200 text-center">
          {engineError} Figures below are not live.
        </div>
      )}

      <main className="flex-1 max-w-7xl w-full mx-auto px-3 sm:px-6 lg:px-8 py-5">{children}</main>
    </div>
  );
};
