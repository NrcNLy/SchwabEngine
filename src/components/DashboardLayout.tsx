import React from 'react';
import { EngineStatus, EnvName } from '../types';
import { NavigationRail } from './NavigationRail';
import { TopAppBar } from './TopAppBar';

export type DashboardTab = 'overview' | 'positions' | 'ave' | 'ledger' | 'controls';

export interface DashboardLayoutProps {
  status: EngineStatus | null;
  engineError: string | null;
  env: EnvName;
  onEnvChange: (env: EnvName) => void;
  onRefresh: () => void;
  isRefreshing: boolean;
  activeTab: DashboardTab;
  setActiveTab: (tab: DashboardTab) => void;
  children: React.ReactNode;
  activeSymbol?: string;
  onSelectSymbol?: (symbol: string) => void;
  availableSymbols?: string[];
  metrics?: {
    price?: number;
    upper?: number;
    lower?: number;
    supremum?: number;
    lambdaFactor?: number;
    isStale?: boolean;
  };
  topBarActions?: React.ReactNode;
}

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
  activeSymbol,
  onSelectSymbol,
  availableSymbols,
  metrics,
  topBarActions,
}) => {
  return (
    <div className="min-h-screen h-screen w-screen overflow-hidden bg-surface-container-lowest text-on-surface flex flex-row font-sans selection:bg-primary/30 selection:text-primary">
      {/* Sandbox Watermark */}
      {env === 'sandbox' && (
        <div
          aria-hidden="true"
          className="pointer-events-none fixed inset-0 z-40 flex items-center justify-center overflow-hidden"
        >
          <span className="-rotate-[24deg] select-none font-mono font-black tracking-[0.4em] text-finance-warning/[0.04] text-6xl sm:text-8xl">
            SANDBOX
          </span>
        </div>
      )}

      {/* M3 Navigation Rail on Left Edge */}
      <NavigationRail
        activeTab={activeTab}
        setActiveTab={setActiveTab}
        env={env}
        onEnvChange={onEnvChange}
        status={status}
        engineError={engineError}
      />

      {/* Main App Container with Top App Bar and Edge-to-Edge Viewport */}
      <div className="flex-1 flex flex-col min-w-0 h-full overflow-hidden">
        <TopAppBar
          status={status}
          engineError={engineError}
          activeTab={activeTab}
          onRefresh={onRefresh}
          isRefreshing={isRefreshing}
          activeSymbol={activeSymbol}
          onSelectSymbol={onSelectSymbol}
          availableSymbols={availableSymbols}
          metrics={metrics}
          actions={topBarActions}
        />

        {/* Global Engine Error Banner */}
        {engineError && (
          <div className="bg-error-container/80 text-on-error-container px-4 py-2 text-xs font-mono text-center shrink-0 border-b border-error/30">
            {engineError} Figures below are not live.
          </div>
        )}

        {/* Dynamic Content Viewport: Full bleed for AVE tab, padded for standard views */}
        <main
          className={`flex-1 overflow-y-auto ${
            activeTab === 'ave' ? 'w-full p-0' : 'w-full p-4 sm:p-6 max-w-7xl mx-auto'
          }`}
        >
          {children}
        </main>
      </div>
    </div>
  );
};
