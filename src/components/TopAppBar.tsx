import React from 'react';
import { Activity, Layers, RefreshCw, ShieldCheck, SlidersHorizontal, TrendingUp } from 'lucide-react';
import { EngineStatus } from '../types';
import { DashboardTab } from './DashboardLayout';
import { StatusPill } from './StatusPill';
import { NetChangePanel } from './NetChangePanel';

export interface TopAppBarProps {
  status: EngineStatus | null;
  engineError: string | null;
  activeTab: DashboardTab;
  onRefresh: () => void;
  isRefreshing: boolean;
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
  actions?: React.ReactNode;
}

const TAB_TITLES: Record<DashboardTab, { label: string; icon: React.ComponentType<{ className?: string }> }> = {
  overview: { label: 'Overview', icon: TrendingUp },
  positions: { label: 'Risk Guard', icon: ShieldCheck },
  ave: { label: 'AVE Squeeze', icon: Activity },
  ledger: { label: 'Liquidity', icon: Layers },
  controls: { label: 'Controls', icon: SlidersHorizontal },
};

export const TopAppBar: React.FC<TopAppBarProps> = ({
  status,
  engineError,
  activeTab,
  onRefresh,
  isRefreshing,
  activeSymbol,
  onSelectSymbol,
  availableSymbols = ['SOXL', 'TQQQ', 'TNA', 'SPY'],
  metrics,
  actions,
}) => {
  const currentTabInfo = TAB_TITLES[activeTab];
  const TabIcon = currentTabInfo.icon;

  return (
    <header className="h-12 shrink-0 bg-surface-container/90 backdrop-blur-md border-b border-outline-variant/20 px-3 sm:px-5 flex items-center justify-between gap-3 z-20 select-none">
      {/* Left Section: Section Identity & Symbol Chips */}
      <div className="flex items-center gap-3 min-w-0">
        <div className="flex items-center gap-2">
          <TabIcon className="w-4 h-4 text-primary shrink-0" />
          <h2 className="font-sans font-semibold text-sm text-on-surface truncate">
            {currentTabInfo.label}
          </h2>
          <StatusPill status={status} engineError={engineError} />
        </div>

        {/* Dynamic Symbol Chips (Shown if on AVE tab and selector is provided) */}
        {activeTab === 'ave' && onSelectSymbol && (
          <div className="hidden sm:flex items-center gap-1.5 pl-2 border-l border-outline-variant/30">
            {availableSymbols.map((sym) => {
              const isSelected = activeSymbol === sym;
              return (
                <button
                  key={sym}
                  type="button"
                  onClick={() => onSelectSymbol(sym)}
                  className={`px-2.5 py-0.5 rounded-full text-xs font-mono font-bold transition-colors ${
                    isSelected
                      ? 'bg-primary/20 text-primary border border-primary/40 shadow-sm'
                      : 'text-on-surface-variant hover:text-on-surface hover:bg-surface-container-high'
                  }`}
                >
                  {sym}
                </button>
              );
            })}
          </div>
        )}
      </div>

      {/* Center Section: Live Streaming Metrics Bar (when provided) */}
      {metrics && metrics.price !== undefined && (
        <div className="hidden lg:flex items-center gap-4 font-mono text-xs text-on-surface-variant">
          <div className="flex items-center gap-1">
            <span className="font-sans text-[10px] uppercase tracking-wider text-on-surface-variant">Price:</span>
            <span className="tabular-nums font-bold text-on-surface">${metrics.price.toFixed(2)}</span>
          </div>
          {metrics.upper !== undefined && (
            <div className="flex items-center gap-1">
              <span className="font-sans text-[10px] uppercase tracking-wider text-on-surface-variant">Upper:</span>
              <span className="tabular-nums font-semibold text-finance-bull">${metrics.upper.toFixed(2)}</span>
            </div>
          )}
          {metrics.lower !== undefined && (
            <div className="flex items-center gap-1">
              <span className="font-sans text-[10px] uppercase tracking-wider text-on-surface-variant">Lower:</span>
              <span className="tabular-nums font-semibold text-finance-bear">${metrics.lower.toFixed(2)}</span>
            </div>
          )}
          {metrics.lambdaFactor !== undefined && (
            <div className="flex items-center gap-1">
              <span className="font-sans text-[10px] uppercase tracking-wider text-on-surface-variant">λ:</span>
              <span className="tabular-nums font-semibold text-primary">
                {(metrics.lambdaFactor * 100).toFixed(0)}%
              </span>
            </div>
          )}
        </div>
      )}

      {/* Right Section: Actions Slot, Net Change Popover, Refresh */}
      <div className="flex items-center gap-2.5 shrink-0">
        {actions}

        <NetChangePanel status={status} />

        <button
          type="button"
          onClick={onRefresh}
          aria-label="Refresh data"
          title="Refresh Dashboard Data"
          className="p-1.5 rounded-lg text-on-surface-variant hover:text-on-surface hover:bg-surface-container-high transition-colors"
        >
          <RefreshCw className={`w-4 h-4 ${isRefreshing ? 'animate-spin text-primary' : ''}`} />
        </button>
      </div>
    </header>
  );
};
