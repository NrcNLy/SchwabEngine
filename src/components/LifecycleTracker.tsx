import React from 'react';
import { Settings, Play, Archive, BrainCircuit } from 'lucide-react';
import { EngineStatus } from '../types';

interface LifecycleTrackerProps {
  status: EngineStatus;
}

export const LifecycleTracker: React.FC<LifecycleTrackerProps> = ({ status }) => {
  const currentPhase = status.lifecycle_phase || 'OFFLINE';

  const phases = [
    {
      id: 'PRE_MARKET',
      label: 'Preparation',
      time: '08:35 EDT',
      desc: 'Macro Ingestion',
      icon: Settings,
    },
    {
      id: 'CORE_SESSION',
      label: 'Action',
      time: '09:30 EDT',
      desc: 'Event-Driven Execution',
      icon: Play,
    },
    {
      id: 'SWEEP',
      label: 'Recovery',
      time: '15:55 EDT',
      desc: 'Flat-to-Cash Sweep',
      icon: Archive,
    },
    {
      id: 'REFLECTION',
      label: 'Reflection',
      time: '16:15 EDT',
      desc: 'Post-Market Analysis',
      icon: BrainCircuit,
    }
  ];

  return (
    <div className="bg-[#0e1422] border border-gray-800 rounded-xl p-5 shadow-2xl">
      <div className="mb-4">
        <h3 className="text-sm font-semibold text-gray-100 font-mono">Algorithmic Lifecycle</h3>
        <p className="text-xs text-gray-500 font-mono mt-0.5">Daily operational pipeline tracking.</p>
      </div>

      <div className="relative flex items-center justify-between w-full">
        {/* Connector Line */}
        <div className="absolute left-0 top-1/2 -translate-y-1/2 w-full h-0.5 bg-gray-800 z-0" />

        {phases.map((phase) => {
          const isActive = phase.id === currentPhase;
          const Icon = phase.icon;
          
          return (
            <div key={phase.id} className="relative z-10 flex flex-col items-center">
              <div 
                className={`w-10 h-10 rounded-full border-2 flex items-center justify-center transition-all ${
                  isActive 
                    ? 'bg-cyan-950 border-cyan-400 text-cyan-300 shadow-[0_0_15px_rgba(34,211,238,0.4)] animate-pulse' 
                    : 'bg-[#0e1422] border-gray-700 text-gray-500'
                }`}
              >
                <Icon className={`w-4 h-4 ${isActive ? 'text-cyan-400' : 'text-gray-500'}`} />
              </div>
              
              <div className="mt-3 text-center">
                <span className={`block text-[10px] font-mono font-bold uppercase tracking-wider ${isActive ? 'text-cyan-400' : 'text-gray-500'}`}>
                  {phase.label}
                </span>
                <span className="block text-xs font-semibold text-gray-300 mt-0.5">
                  {phase.time}
                </span>
                <span className="block text-[10px] text-gray-500 hidden sm:block">
                  {phase.desc}
                </span>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
};
