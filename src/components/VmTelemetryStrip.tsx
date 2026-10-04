import React from 'react';
import { Activity, Database, Wifi, Clock } from 'lucide-react';
import { EngineStatus } from '../types';

interface VmTelemetryStripProps {
  status: EngineStatus | null;
}

export const VmTelemetryStrip: React.FC<VmTelemetryStripProps> = ({ status }) => {
  const vm = status?.vm_stats;
  if (!vm) return null;

  const ping = vm.api_ping_ms;
  const pingTone = ping === null ? 'text-gray-500' : ping < 250 ? 'text-emerald-400' : 'text-amber-400';

  return (
    <div className="bg-[#0b101c] border-b border-gray-800/80 px-3 sm:px-6 lg:px-8 py-1 flex items-center justify-end gap-4 text-[10px] font-mono text-gray-400">
      <div className="flex items-center gap-1" title="CPU utilization">
        <Activity className="w-3 h-3 text-cyan-400" />
        <span className="text-gray-200">{vm.cpu_pct.toFixed(0)}%</span>
      </div>
      <div className="flex items-center gap-1" title="Memory utilization">
        <Database className="w-3 h-3 text-purple-400" />
        <span className="text-gray-200">{vm.mem_pct.toFixed(0)}%</span>
      </div>
      <div className="flex items-center gap-1" title="Schwab API latency (measured by the broker sync)">
        <Wifi className={`w-3 h-3 ${pingTone}`} />
        <span className="text-gray-200">{ping === null ? '—' : `${Math.round(ping)}ms`}</span>
      </div>
      <div className="flex items-center gap-1" title="Engine uptime">
        <Clock className="w-3 h-3 text-gray-500" />
        <span className="text-gray-300">{vm.uptime_string}</span>
      </div>
    </div>
  );
};
