import React from 'react';
import { Settings, Play, Archive, BrainCircuit } from 'lucide-react';
import { EngineStatus } from '../types';

interface LifecycleTrackerProps {
  status: EngineStatus | null;
}

const PHASES = [
  { id: 'PRE_MARKET', label: 'Prepare', time: '08:35', icon: Settings },
  { id: 'CORE_SESSION', label: 'Trade', time: '09:30', icon: Play },
  { id: 'SWEEP', label: 'Flatten', time: '15:50', icon: Archive },
  { id: 'REFLECTION', label: 'Review', time: '16:15', icon: BrainCircuit },
] as const;

export const LifecycleTracker: React.FC<LifecycleTrackerProps> = ({ status }) => {
  const currentPhase = status?.lifecycle_phase ?? 'OFFLINE';

  return (
    <section className="bg-[#0e1422] border border-gray-800 rounded-xl p-4 sm:p-5 shadow-2xl">
      <div className="relative flex items-center justify-between w-full">
        <div className="absolute left-5 right-5 top-5 h-0.5 bg-gray-800 z-0" />
        {PHASES.map((phase) => {
          const isActive = phase.id === currentPhase;
          const Icon = phase.icon;
          return (
            <div key={phase.id} className="relative z-10 flex flex-col items-center w-1/4">
              <div
                className={`w-10 h-10 rounded-full border-2 flex items-center justify-center transition-all ${
                  isActive
                    ? 'bg-cyan-950 border-cyan-400 text-cyan-300 shadow-[0_0_15px_rgba(34,211,238,0.4)]'
                    : 'bg-[#0e1422] border-gray-700 text-gray-500'
                }`}
              >
                <Icon className={`w-4 h-4 ${isActive ? 'text-cyan-400' : 'text-gray-500'}`} />
              </div>
              <span className={`mt-2 text-[10px] font-mono font-bold uppercase tracking-wider ${isActive ? 'text-cyan-400' : 'text-gray-500'}`}>
                {phase.label}
              </span>
              <span className="text-[11px] font-mono text-gray-400">{phase.time}</span>
            </div>
          );
        })}
      </div>
      <p className="mt-3 text-[10px] font-mono text-gray-500 text-center">Eastern time. Positions are flat by 15:55.</p>
    </section>
  );
};
