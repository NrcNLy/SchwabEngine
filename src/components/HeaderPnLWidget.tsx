import React, { useState } from 'react';
import { Clock, ChevronDown } from 'lucide-react';
import { EngineStatus } from '../types';

interface HeaderPnLWidgetProps {
  status: EngineStatus;
}

type Timeframe = 'Day' | '1W' | 'MTD' | 'ATD' | 'Custom';

export const HeaderPnLWidget: React.FC<HeaderPnLWidgetProps> = ({ status }) => {
  const [isOpen, setIsOpen] = useState(false);
  const [selectedTimeframe, setSelectedTimeframe] = useState<Timeframe>('Day');

  const timeframes: { label: Timeframe; title: string }[] = [
    { label: 'Day', title: 'Most Recent Trading Session' },
    { label: '1W', title: 'Last Week' },
    { label: 'MTD', title: 'Month to Date' },
    { label: 'ATD', title: 'All-Time / Inception' },
    { label: 'Custom', title: 'Custom Date Range' },
  ];

  // Mock unrealized based on status.today_pnl for display purposes
  const unrealized = (status.today_pnl || 0) * 0.45;
  const isRealizedPositive = status.today_pnl >= 0;
  const isUnrealizedPositive = unrealized >= 0;

  return (
    <div className="relative z-50">
      <div 
        className="flex items-center space-x-4 bg-gray-900/80 px-3 py-1.5 rounded-xl border border-gray-800 shadow-sm cursor-pointer hover:bg-gray-800 transition-colors"
        onClick={() => setIsOpen(!isOpen)}
      >
        <div className="flex flex-col items-end">
          <div className="flex items-center space-x-2">
            <span className="text-[10px] text-gray-400 font-mono">REALIZED</span>
            <span className={`font-mono font-bold text-xs ${isRealizedPositive ? 'text-emerald-400' : 'text-rose-400'}`}>
              {isRealizedPositive ? '+' : ''}${status.today_pnl.toFixed(2)}
            </span>
          </div>
          <div className="flex items-center space-x-2 mt-0.5">
            <span className="text-[10px] text-gray-500 font-mono">UNREALIZED</span>
            <span className={`font-mono font-semibold text-[11px] ${isUnrealizedPositive ? 'text-emerald-500/80' : 'text-rose-500/80'}`}>
              {isUnrealizedPositive ? '+' : ''}${unrealized.toFixed(2)}
            </span>
          </div>
        </div>

        <div className="flex items-center space-x-1 pl-3 border-l border-gray-700/50">
          <Clock className="w-3.5 h-3.5 text-cyan-400" />
          <span className="text-xs font-mono font-semibold text-gray-300">{selectedTimeframe}</span>
          <ChevronDown className={`w-3.5 h-3.5 text-gray-500 transition-transform ${isOpen ? 'rotate-180' : ''}`} />
        </div>
      </div>

      {isOpen && (
        <div className="absolute top-full right-0 mt-2 w-56 bg-[#0d1322] border border-gray-800 rounded-xl shadow-2xl overflow-hidden backdrop-blur-xl">
          <div className="px-3 py-2 border-b border-gray-800/80 bg-gray-900/50">
            <span className="text-xs font-mono text-gray-400">P&L Timeframe</span>
          </div>
          <div className="p-1">
            {timeframes.map((tf) => (
              <button
                key={tf.label}
                onClick={() => {
                  setSelectedTimeframe(tf.label);
                  setIsOpen(false);
                }}
                className={`w-full text-left px-3 py-2 text-xs font-mono rounded-lg transition-colors flex justify-between items-center ${
                  selectedTimeframe === tf.label
                    ? 'bg-cyan-900/20 text-cyan-400 font-bold'
                    : 'text-gray-400 hover:bg-gray-800 hover:text-gray-200'
                }`}
              >
                <span>{tf.title}</span>
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-900/50">{tf.label}</span>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
};
