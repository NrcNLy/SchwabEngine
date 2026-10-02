import React, { useState } from 'react';
import { 
  TrendingUp, 
  TrendingDown, 
  ShieldCheck, 
  AlertTriangle, 
  Layers, 
  ExternalLink,
  Target,
  Shield,
  Activity
} from 'lucide-react';
import { Position, PositionsResponse } from '../types';

interface EtfPositionTrackerProps {
  positions: PositionsResponse;
  onRefresh: () => void;
}

export const EtfPositionTracker: React.FC<EtfPositionTrackerProps> = ({ positions }) => {
  const [activeTab, setActiveTab] = useState<'all' | 'managed' | 'unmanaged'>('all');

  const managedTotalPnl = positions.managed.reduce((acc, p) => acc + (p.unrealized_pnl || 0), 0);
  const unmanagedTotalPnl = positions.unmanaged.reduce((acc, p) => acc + (p.unrealized_pnl || 0), 0);
  const grandTotalPnl = managedTotalPnl + unmanagedTotalPnl;

  const displayList: Position[] = 
    activeTab === 'managed' ? positions.managed :
    activeTab === 'unmanaged' ? positions.unmanaged :
    [...positions.managed, ...positions.unmanaged];

  return (
    <div className="bg-[#111827] border border-gray-800 rounded-xl overflow-hidden shadow-lg shadow-black/40">
      {/* Header & Tabs */}
      <div className="p-5 border-b border-gray-800 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
        <div>
          <div className="flex items-center space-x-3">
            <div className="p-2 rounded-lg bg-emerald-500/10 text-emerald-400">
              <Layers className="w-5 h-5" />
            </div>
            <div>
              <h2 className="text-base font-semibold text-gray-100 flex items-center">
                ETF Position Tracker & Reconciliation
                <span className="ml-2.5 px-2 py-0.5 text-[10px] font-mono bg-gray-800 text-gray-300 rounded-md border border-gray-700">
                  {positions.managed.length} Managed · {positions.unmanaged.length} Unmanaged
                </span>
              </h2>
              <p className="text-xs text-gray-400">
                Real-time mark-to-market valuations, dual-tier stops, and automated reconciliation
              </p>
            </div>
          </div>
        </div>

        {/* Tab Filters & PnL pill */}
        <div className="flex items-center space-x-3">
          <div className="bg-gray-900 p-1 rounded-lg border border-gray-800 flex text-xs">
            <button
              onClick={() => setActiveTab('all')}
              className={`px-3 py-1 rounded-md transition-all ${activeTab === 'all' ? 'bg-cyan-600 text-white font-medium shadow' : 'text-gray-400 hover:text-gray-200'}`}
            >
              All ({positions.managed.length + positions.unmanaged.length})
            </button>
            <button
              onClick={() => setActiveTab('managed')}
              className={`px-3 py-1 rounded-md transition-all ${activeTab === 'managed' ? 'bg-cyan-600 text-white font-medium shadow' : 'text-gray-400 hover:text-gray-200'}`}
            >
              Engine ({positions.managed.length})
            </button>
            <button
              onClick={() => setActiveTab('unmanaged')}
              className={`px-3 py-1 rounded-md transition-all ${activeTab === 'unmanaged' ? 'bg-cyan-600 text-white font-medium shadow' : 'text-gray-400 hover:text-gray-200'}`}
            >
              Schwab Account ({positions.unmanaged.length})
            </button>
          </div>

          <div className={`px-3 py-1.5 rounded-lg border text-xs font-mono font-semibold flex items-center ${
            grandTotalPnl >= 0 ? 'bg-emerald-950/40 text-emerald-400 border-emerald-800/60' : 'bg-rose-950/40 text-rose-400 border-rose-800/60'
          }`}>
            {grandTotalPnl >= 0 ? <TrendingUp className="w-3.5 h-3.5 mr-1" /> : <TrendingDown className="w-3.5 h-3.5 mr-1" />}
            ${grandTotalPnl.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
          </div>
        </div>
      </div>

      {/* Positions Table */}
      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs">
          <thead className="bg-[#0b1120] text-gray-400 uppercase tracking-wider font-mono border-b border-gray-800">
            <tr>
              <th className="py-3 px-4">Instrument</th>
              <th className="py-3 px-4">Classification</th>
              <th className="py-3 px-4 text-right">Shares</th>
              <th className="py-3 px-4 text-right">Entry / Avg Cost</th>
              <th className="py-3 px-4 text-right">Last Price</th>
              <th className="py-3 px-4 text-right">Hard Stop</th>
              <th className="py-3 px-4 text-right">Profit Target</th>
              <th className="py-3 px-4 text-right">Unrealized P&L</th>
              <th className="py-3 px-4 text-center">Safety Guard</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-800 font-mono">
            {displayList.length === 0 ? (
              <tr>
                <td colSpan={9} className="py-8 text-center text-gray-500 font-sans">
                  No active ETF positions found matching filter.
                </td>
              </tr>
            ) : (
              displayList.map((pos) => {
                const cost = pos.entry_price || pos.avg_cost || 0;
                const pnl = pos.unrealized_pnl || 0;
                const pnlPct = cost > 0 ? ((pos.current_price - cost) / cost) * 100 : 0;
                const isProfitable = pnl >= 0;

                return (
                  <tr key={pos.symbol} className="hover:bg-gray-800/40 transition-colors">
                    {/* Symbol & Regime */}
                    <td className="py-3.5 px-4">
                      <div className="flex items-center space-x-2 font-sans">
                        <span className="font-bold text-sm text-gray-100 tracking-wide font-mono">
                          {pos.symbol}
                        </span>
                        {pos.regime && (
                          <span className={`px-1.5 py-0.5 rounded text-[10px] font-mono font-semibold ${
                            pos.regime === 'A'
                              ? 'bg-emerald-950 text-emerald-300 border border-emerald-800'
                              : 'bg-blue-950 text-blue-300 border border-blue-800'
                          }`}>
                            Regime {pos.regime}
                          </span>
                        )}
                      </div>
                    </td>

                    {/* Classification */}
                    <td className="py-3.5 px-4 font-sans">
                      {pos.managed ? (
                        <span className="inline-flex items-center px-2 py-0.5 rounded text-[11px] font-medium bg-cyan-950/80 text-cyan-300 border border-cyan-800/50">
                          <Activity className="w-3 h-3 mr-1" /> Managed
                        </span>
                      ) : (
                        <span className="inline-flex items-center px-2 py-0.5 rounded text-[11px] font-medium bg-amber-950/80 text-amber-300 border border-amber-800/50">
                          <ExternalLink className="w-3 h-3 mr-1" /> External Schwab
                        </span>
                      )}
                    </td>

                    {/* Quantity */}
                    <td className="py-3.5 px-4 text-right text-gray-200">
                      {pos.quantity.toLocaleString()}
                    </td>

                    {/* Entry / Cost */}
                    <td className="py-3.5 px-4 text-right text-gray-300">
                      ${cost.toFixed(2)}
                    </td>

                    {/* Current Price */}
                    <td className="py-3.5 px-4 text-right font-semibold text-gray-100">
                      ${pos.current_price.toFixed(2)}
                    </td>

                    {/* Stop Price */}
                    <td className="py-3.5 px-4 text-right text-rose-300">
                      {pos.stop_price ? (
                        <div className="flex items-center justify-end space-x-1">
                          <Shield className="w-3 h-3 text-rose-400" />
                          <span>${pos.stop_price.toFixed(2)}</span>
                        </div>
                      ) : (
                        <span className="text-gray-500 font-sans">—</span>
                      )}
                    </td>

                    {/* Target Price */}
                    <td className="py-3.5 px-4 text-right text-emerald-300">
                      {pos.target_price ? (
                        <div className="flex items-center justify-end space-x-1">
                          <Target className="w-3 h-3 text-emerald-400" />
                          <span>${pos.target_price.toFixed(2)}</span>
                        </div>
                      ) : (
                        <span className="text-gray-500 font-sans">—</span>
                      )}
                    </td>

                    {/* Unrealized P&L */}
                    <td className="py-3.5 px-4 text-right">
                      <span className={`font-semibold ${isProfitable ? 'text-emerald-400' : 'text-rose-400'}`}>
                        {isProfitable ? '+' : ''}${pnl.toFixed(2)}
                      </span>
                      <span className={`block text-[10px] ${isProfitable ? 'text-emerald-500' : 'text-rose-500'}`}>
                        ({isProfitable ? '+' : ''}{pnlPct.toFixed(2)}%)
                      </span>
                    </td>

                    {/* Safety Guard Indicator */}
                    <td className="py-3.5 px-4 text-center font-sans">
                      {pos.managed ? (
                        <span className="inline-flex items-center text-[10px] text-emerald-400 bg-emerald-950/60 px-2 py-0.5 rounded border border-emerald-800">
                          <ShieldCheck className="w-3 h-3 mr-1" /> Active Guard
                        </span>
                      ) : pos.stop_order_id ? (
                        <span className="inline-flex items-center text-[10px] text-cyan-400 bg-cyan-950/60 px-2 py-0.5 rounded border border-cyan-800">
                          <ShieldCheck className="w-3 h-3 mr-1" /> Protective Stop Set
                        </span>
                      ) : (
                        <span className="inline-flex items-center text-[10px] text-amber-400 bg-amber-950/60 px-2 py-0.5 rounded border border-amber-800">
                          <AlertTriangle className="w-3 h-3 mr-1" /> Unprotected
                        </span>
                      )}
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
};
