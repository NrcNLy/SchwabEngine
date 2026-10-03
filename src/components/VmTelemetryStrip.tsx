import React from 'react';
import { Activity, Database, Wifi, Clock } from 'lucide-react';
import { EngineStatus } from '../types';

interface VmTelemetryStripProps {
  status: EngineStatus;
}

export const VmTelemetryStrip: React.FC<VmTelemetryStripProps> = ({ status }) => {
  const { vm_stats } = status;

  if (!vm_stats) return null;

  return (
    <div className="bg-[#0b101c] border-b border-gray-800/80 px-4 sm:px-6 lg:px-8 py-1.5 flex flex-wrap items-center justify-between text-[10px] sm:text-xs font-mono text-gray-400">
      <div className="flex items-center space-x-2">
        <span className="text-gray-500 font-bold uppercase tracking-widest">VM Telemetry</span>
      </div>
      
      <div className="flex items-center space-x-4 sm:space-x-6">
        <div className="flex items-center space-x-1.5" title="CPU Utilization">
          <Activity className="w-3.5 h-3.5 text-cyan-400" />
          <span className="text-gray-200">{vm_stats.cpu_pct.toFixed(1)}%</span>
        </div>
        
        <div className="flex items-center space-x-1.5" title="Memory Allocation">
          <Database className="w-3.5 h-3.5 text-purple-400" />
          <span className="text-gray-200">{vm_stats.mem_pct.toFixed(1)}%</span>
        </div>

        <div className="flex items-center space-x-1.5" title="Schwab API Latency">
          <Wifi className={`w-3.5 h-3.5 ${vm_stats.api_ping_ms < 50 ? 'text-emerald-400' : 'text-amber-400'}`} />
          <span className="text-gray-200">{vm_stats.api_ping_ms}ms</span>
        </div>

        <div className="flex items-center space-x-1.5" title="Daemon Uptime">
          <Clock className="w-3.5 h-3.5 text-gray-500" />
          <span className="text-gray-300">{vm_stats.uptime_string}</span>
        </div>
      </div>
    </div>
  );
};
