import React, { useEffect, useRef, useState } from 'react';
import {
  Activity,
  Cpu,
  Clock,
  Database,
  Layers,
  PanelLeftClose,
  PanelLeftOpen,
  ShieldCheck,
  SlidersHorizontal,
  TrendingUp,
  Wifi,
} from 'lucide-react';
import { EngineStatus, EnvName } from '../types';
import { DashboardTab } from './DashboardLayout';
import { StatusPill } from './StatusPill';

export interface NavigationRailProps {
  activeTab: DashboardTab;
  setActiveTab: (tab: DashboardTab) => void;
  env: EnvName;
  onEnvChange: (env: EnvName) => void;
  status: EngineStatus | null;
  engineError: string | null;
}

const NAV_ITEMS: Array<{
  id: DashboardTab;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
}> = [
  { id: 'overview', label: 'Overview', icon: TrendingUp },
  { id: 'positions', label: 'Risk Guard', icon: ShieldCheck },
  { id: 'ave', label: 'AVE Squeeze', icon: Activity },
  { id: 'ledger', label: 'Liquidity', icon: Layers },
  { id: 'controls', label: 'Controls', icon: SlidersHorizontal },
];

export const NavigationRail: React.FC<NavigationRailProps> = ({
  activeTab,
  setActiveTab,
  env,
  onEnvChange,
  status,
  engineError,
}) => {
  // Pinned state defaults to false (72px collapsed by default, 220px expanded when pinned)
  const [isPinned, setIsPinned] = useState<boolean>(false);
  const [telemetryOpen, setTelemetryOpen] = useState<boolean>(false);
  const telemetryRef = useRef<HTMLDivElement>(null);

  // Close telemetry popover on outside click
  useEffect(() => {
    if (!telemetryOpen) return undefined;
    const handleOutsideClick = (e: MouseEvent | TouchEvent) => {
      if (telemetryRef.current && !telemetryRef.current.contains(e.target as Node)) {
        setTelemetryOpen(false);
      }
    };
    document.addEventListener('mousedown', handleOutsideClick);
    document.addEventListener('touchstart', handleOutsideClick);
    return () => {
      document.removeEventListener('mousedown', handleOutsideClick);
      document.removeEventListener('touchstart', handleOutsideClick);
    };
  }, [telemetryOpen]);

  const vm = status?.vm_stats;
  const ping = vm?.api_ping_ms ?? null;
  const pingTone = ping === null ? 'text-gray-500' : ping < 250 ? 'text-finance-bull' : 'text-finance-warning';

  return (
    <aside
      className={`h-screen flex flex-col justify-between bg-surface-container-low border-r border-outline-variant/20 shadow-m3-1 z-30 transition-all duration-300 ease-in-out select-none ${
        isPinned ? 'w-[220px]' : 'w-[72px]'
      }`}
      aria-label="Sidebar Navigation Rail"
    >
      {/* Top Header: Brand & Pin Toggle */}
      <div className="flex flex-col gap-2 pt-3 px-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2.5 min-w-0">
            <div className="w-9 h-9 shrink-0 rounded-xl bg-gradient-to-tr from-cyan-600 via-blue-600 to-emerald-500 flex items-center justify-center shadow-m3-1">
              <Activity className="w-4 h-4 text-white" />
            </div>
            {isPinned && (
              <div className="min-w-0 flex flex-col">
                <span className="font-sans font-bold text-sm tracking-tight text-on-surface truncate leading-tight">
                  SchwabEngine
                </span>
                <span className="font-sans text-[10px] text-on-surface-variant truncate">
                  Autonomous Edge
                </span>
              </div>
            )}
          </div>

          <button
            type="button"
            onClick={() => setIsPinned((prev) => !prev)}
            aria-label={isPinned ? 'Unpin navigation rail (collapse to 72px)' : 'Pin navigation rail (expand to 220px)'}
            title={isPinned ? 'Unpin rail (72px)' : 'Pin rail (220px)'}
            className="p-1.5 rounded-lg text-on-surface-variant hover:text-on-surface hover:bg-surface-container-high transition-colors"
          >
            {isPinned ? (
              <PanelLeftClose className="w-4 h-4" />
            ) : (
              <PanelLeftOpen className="w-4 h-4" />
            )}
          </button>
        </div>

        {/* Status Pill in pinned mode */}
        {isPinned && (
          <div className="flex items-center gap-2 mt-1 px-1 py-1 rounded-lg bg-surface-container/60">
            <StatusPill status={status} engineError={engineError} />
            <span className="font-sans text-xs text-on-surface-variant capitalize">
              {status && !engineError ? status.system_state.toLowerCase() : 'Offline'}
            </span>
          </div>
        )}
      </div>

      {/* Navigation Destinations */}
      <nav className="flex flex-col gap-1.5 px-2 my-auto" aria-label="Primary Destinations">
        {NAV_ITEMS.map(({ id, label, icon: Icon }) => {
          const isActive = id === activeTab;
          return (
            <button
              key={id}
              type="button"
              onClick={() => setActiveTab(id)}
              title={label}
              className={`group flex items-center transition-all ${
                isPinned
                  ? `w-full px-3 py-2.5 rounded-xl gap-3 text-xs font-sans font-medium ${
                      isActive
                        ? 'bg-primary/15 text-primary shadow-sm font-semibold'
                        : 'text-on-surface-variant hover:text-on-surface hover:bg-surface-container-high/60'
                    }`
                  : `flex-col py-1.5 px-1 rounded-xl text-[10px] font-sans ${
                      isActive ? 'text-primary font-semibold' : 'text-on-surface-variant hover:text-on-surface'
                    }`
              }`}
            >
              <div
                className={`flex items-center justify-center transition-colors ${
                  isPinned
                    ? 'w-6 h-6'
                    : `w-14 h-8 rounded-full ${
                        isActive
                          ? 'bg-primary/20 text-primary'
                          : 'text-on-surface-variant group-hover:text-on-surface group-hover:bg-surface-container-high/70'
                      }`
                }`}
              >
                <Icon className={isPinned ? 'w-4 h-4' : 'w-4 h-4'} />
              </div>
              <span className={isPinned ? 'truncate' : 'mt-1 text-[10px] tracking-tight leading-none truncate max-w-[64px]'}>
                {label}
              </span>
            </button>
          );
        })}
      </nav>

      {/* Bottom Footer: Environment Switcher & VM Telemetry Popover */}
      <div className="flex flex-col gap-2 p-3 border-t border-outline-variant/15">
        {/* Environment Selector */}
        {isPinned ? (
          <div
            role="radiogroup"
            aria-label="Environment"
            className="flex p-0.5 rounded-xl bg-surface-container border border-outline-variant/20"
          >
            <button
              type="button"
              role="radio"
              aria-checked={env === 'active'}
              onClick={() => onEnvChange('active')}
              className={`flex-1 py-1 rounded-lg text-xs font-sans font-medium transition-colors ${
                env === 'active'
                  ? 'bg-finance-bull-container text-finance-on-bull-container shadow-sm font-semibold'
                  : 'text-on-surface-variant hover:text-on-surface'
              }`}
            >
              Active
            </button>
            <button
              type="button"
              role="radio"
              aria-checked={env === 'sandbox'}
              onClick={() => onEnvChange('sandbox')}
              className={`flex-1 py-1 rounded-lg text-xs font-sans font-medium transition-colors ${
                env === 'sandbox'
                  ? 'bg-finance-warning-container text-finance-on-warning-container shadow-sm font-semibold'
                  : 'text-on-surface-variant hover:text-on-surface'
              }`}
            >
              Sandbox
            </button>
          </div>
        ) : (
          <button
            type="button"
            onClick={() => onEnvChange(env === 'active' ? 'sandbox' : 'active')}
            title={`Current: ${env.toUpperCase()} (Click to toggle)`}
            className={`w-full py-1.5 rounded-lg text-[10px] font-mono font-bold tracking-wider uppercase transition-colors text-center ${
              env === 'active'
                ? 'bg-finance-bull-container/80 text-finance-bull'
                : 'bg-finance-warning-container/80 text-finance-warning'
            }`}
          >
            {env === 'active' ? 'ACT' : 'SBX'}
          </button>
        )}

        {/* VM Telemetry Popover */}
        <div ref={telemetryRef} className="relative">
          <button
            type="button"
            onClick={() => setTelemetryOpen((prev) => !prev)}
            aria-expanded={telemetryOpen}
            aria-label="System telemetry details"
            title="System Telemetry"
            className={`w-full flex items-center rounded-xl p-2 transition-colors ${
              isPinned ? 'justify-between bg-surface-container hover:bg-surface-container-high' : 'justify-center hover:bg-surface-container-high'
            }`}
          >
            <div className="flex items-center gap-2">
              <Cpu className="w-4 h-4 text-primary" />
              {isPinned && (
                <span className="font-sans text-xs text-on-surface-variant">
                  System Health
                </span>
              )}
            </div>
            {isPinned ? (
              <span className={`font-mono text-[10px] font-semibold ${pingTone}`}>
                {ping === null ? '—' : `${Math.round(ping)}ms`}
              </span>
            ) : (
              <span
                className={`w-1.5 h-1.5 rounded-full absolute top-1.5 right-1.5 ${
                  ping === null ? 'bg-gray-500' : ping < 250 ? 'bg-finance-bull' : 'bg-finance-warning'
                }`}
              />
            )}
          </button>

          {/* Popover Flyout */}
          {telemetryOpen && (
            <div className="absolute left-full bottom-0 ml-2 w-64 bg-surface-container-highest rounded-2xl shadow-m3-4 border border-outline-variant/30 p-3.5 z-50 text-xs font-sans">
              <div className="flex items-center justify-between pb-2 border-b border-outline-variant/20 mb-2.5">
                <span className="font-sans font-semibold text-on-surface">VM Telemetry</span>
                <span className="font-mono text-[10px] text-on-surface-variant">Live</span>
              </div>

              {vm ? (
                <div className="space-y-2">
                  <div className="flex items-center justify-between">
                    <span className="flex items-center gap-1.5 text-on-surface-variant">
                      <Cpu className="w-3.5 h-3.5 text-primary" /> CPU
                    </span>
                    <span className="font-mono tabular-nums font-bold text-on-surface">
                      {vm.cpu_pct.toFixed(1)}%
                    </span>
                  </div>

                  <div className="flex items-center justify-between">
                    <span className="flex items-center gap-1.5 text-on-surface-variant">
                      <Database className="w-3.5 h-3.5 text-secondary" /> Memory
                    </span>
                    <span className="font-mono tabular-nums font-bold text-on-surface">
                      {vm.mem_pct.toFixed(1)}%
                    </span>
                  </div>

                  <div className="flex items-center justify-between">
                    <span className="flex items-center gap-1.5 text-on-surface-variant">
                      <Wifi className={`w-3.5 h-3.5 ${pingTone}`} /> Latency
                    </span>
                    <span className={`font-mono tabular-nums font-bold ${pingTone}`}>
                      {ping === null ? '—' : `${Math.round(ping)}ms`}
                    </span>
                  </div>

                  <div className="flex items-center justify-between">
                    <span className="flex items-center gap-1.5 text-on-surface-variant">
                      <Clock className="w-3.5 h-3.5 text-on-surface-variant" /> Uptime
                    </span>
                    <span className="font-mono text-on-surface text-[11px]">
                      {vm.uptime_string}
                    </span>
                  </div>
                </div>
              ) : (
                <div className="text-on-surface-variant text-[11px] py-2 text-center">
                  No telemetry stream available.
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </aside>
  );
};
