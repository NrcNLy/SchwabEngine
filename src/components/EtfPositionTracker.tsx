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
  Activity,
  ArrowRightLeft
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

  const totalManagedNotional = positions.managed.reduce((acc, p) => acc + (p.notional_value || (p.quantity * p.current_price)), 0);

  const displayList: Position[] = 
    activeTab === 'managed' ? positions.managed :
    activeTab === 'unmanaged' ? positions.unmanaged :
    [...positions.managed, ...positions.unmanaged];

  return (
    <div className="bg-[#0e1422] border border-gray-800 rounded-xl overflow-hidden shadow-2xl">
      {/* Header & Tabs */}
      <div className="p-5 border-b border-gray-800 bg-[#111827]/80 backdrop-blur flex flex-col lg:flex-row lg:items-center lg:justify-between gap-4">
        <div>
          <div className="flex items-center space-x-3">
            <div className="p-2.5 rounded-lg bg-cyan-500/10 text-cyan-400 border border-cyan-500/20">
              <Layers className="w-5 h-5" />
            </div>
            <div>
              <div className="flex items-center space-x-2.5">
                <h2 className="text-base font-semibold text-gray-100 font-mono tracking-tight">
                  High-Beta ETF Portfolio & Risk Guard
                </h2>
                <span className="px-2 py-0.5 text-[10px] font-mono font-bold bg-cyan-950 text-cyan-300 rounded border border-cyan-800">
                  $1,000 Sandbox
                </span>
              </div>
              <p className="text-xs text-gray-400 mt-0.5">
                Enforcing 20% single-ticker exposure ($200.00 cap), dual-tier stops, and IRC §1091 wash-sale pivots
              </p>
            </div>
          </div>
        </div>

        {/* Tab Filters & Metric Badges */}
        <div className="flex flex-wrap items-center gap-3">
          {/* Managed Capital Utilization */}
          <div className="hidden sm:flex items-center space-x-2 bg-gray-900/90 border border-gray-800 px-3 py-1.5 rounded-lg text-xs font-mono">
            <span className="text-gray-400">Deployed:</span>
            <span className="text-cyan-300 font-semibold">${totalManagedNotional.toFixed(2)}</span>
            <span className="text-gray-500">/ $1,000</span>
          </div>

          <div className="bg-gray-900 p-1 rounded-lg border border-gray-800 flex text-xs font-mono">
            <button
              onClick={() => setActiveTab('all')}
              className={`px-3 py-1 rounded-md transition-all ${
                activeTab === 'all' 
                  ? 'bg-cyan-600 text-white font-semibold shadow' 
                  : 'text-gray-400 hover:text-gray-200'
              }`}
            >
              All ({positions.managed.length + positions.unmanaged.length})
            </button>
            <button
              onClick={() => setActiveTab('managed')}
              className={`px-3 py-1 rounded-md transition-all ${
                activeTab === 'managed' 
                  ? 'bg-cyan-600 text-white font-semibold shadow' 
                  : 'text-gray-400 hover:text-gray-200'
              }`}
            >
              Managed Sandbox ({positions.managed.length})
            </button>
            <button
              onClick={() => setActiveTab('unmanaged')}
              className={`px-3 py-1 rounded-md transition-all ${
                activeTab === 'unmanaged' 
                  ? 'bg-cyan-600 text-white font-semibold shadow' 
                  : 'text-gray-400 hover:text-gray-200'
              }`}
            >
              Schwab Core ({positions.unmanaged.length})
            </button>
          </div>

          <div className={`px-3 py-1.5 rounded-lg border text-xs font-mono font-bold flex items-center ${
            grandTotalPnl >= 0 
              ? 'bg-emerald-950/40 text-emerald-400 border-emerald-800/60' 
              : 'bg-rose-950/40 text-rose-400 border-rose-800/60'
          }`}>
            {grandTotalPnl >= 0 ? <TrendingUp className="w-3.5 h-3.5 mr-1" /> : <TrendingDown className="w-3.5 h-3.5 mr-1" />}
            {grandTotalPnl >= 0 ? '+' : ''}${grandTotalPnl.toFixed(2)}
          </div>
        </div>
      </div>

      {/* High-Beta Universe & Wash-Sale Rotation Strip */}
      <div className="bg-[#0b101d] px-5 py-2.5 border-b border-gray-800/80 flex flex-wrap items-center justify-between text-[11px] font-mono text-gray-400 gap-2">
        <div className="flex items-center space-x-2">
          <ArrowRightLeft className="w-3.5 h-3.5 text-cyan-400" />
          <span className="text-gray-300 font-semibold">IRC §1091 Wash-Sale Tracking Matrix:</span>
        </div>
        <div className="flex items-center space-x-4">
          <div className="flex items-center space-x-1">
            <span className="text-cyan-300 font-bold">SOXL</span>
            <span className="text-gray-500">→</span>
            <span className="text-emerald-400">FNGU</span>
          </div>
          <span className="text-gray-700">|</span>
          <div className="flex items-center space-x-1">
            <span className="text-cyan-300 font-bold">TQQQ</span>
            <span className="text-gray-500">→</span>
            <span className="text-emerald-400">CONL</span>
          </div>
          <span className="text-gray-700">|</span>
          <div className="flex items-center space-x-1">
            <span className="text-cyan-300 font-bold">TNA</span>
            <span className="text-gray-500">→</span>
            <span className="text-emerald-400">DPST</span>
          </div>
        </div>
      </div>

      {/* Positions Table */}
      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs">
          <thead className="bg-[#080d17] text-gray-400 uppercase tracking-wider font-mono border-b border-gray-800 text-[10px]">
            <tr>
              <th className="py-3 px-4">Instrument & Strategy</th>
              <th className="py-3 px-4">Portfolio Segregation</th>
              <th className="py-3 px-4 text-right">Shares</th>
              <th className="py-3 px-4 text-right">Entry / Last</th>
              <th className="py-3 px-4">20% Exposure Gauge ($200 Cap)</th>
              <th className="py-3 px-4 text-right">Dual-Tier Stops</th>
              <th className="py-3 px-4 text-right">Target</th>
              <th className="py-3 px-4 text-right">Unrealized P&L</th>
              <th className="py-3 px-4 text-center">Reconciliation Guard</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-800/80 font-mono">
            {displayList.length === 0 ? (
              <tr>
                <td colSpan={9} className="py-8 text-center text-gray-500 font-sans">
                  No active ETF positions found matching filter.
                </td>
              </tr>
            ) : (
              displayList.map((pos) => {
                const cost = pos.entry_price || pos.avg_cost || 0;
                const notional = pos.notional_value || (pos.quantity * pos.current_price);
                const exposurePct = pos.exposure_pct || ((notional / 1000.0) * 100);
                const pnl = pos.unrealized_pnl || 0;
                const pnlPct = cost > 0 ? ((pos.current_price - cost) / cost) * 100 : 0;
                const isProfitable = pnl >= 0;

                const hardStop = pos.hard_stop_price || pos.stop_price;
                const trailingStop = pos.trailing_stop_price;

                const isCapBreached = exposurePct > 20.0;

                return (
                  <tr key={pos.symbol} className="hover:bg-gray-800/30 transition-colors">
                    {/* Symbol & Regime */}
                    <td className="py-3.5 px-4">
                      <div className="flex items-center space-x-2">
                        <span className="font-bold text-sm text-gray-100 tracking-wide font-mono">
                          {pos.symbol}
                        </span>
                        {pos.regime && (
                          <span className={`px-1.5 py-0.5 rounded text-[10px] font-mono font-bold ${
                            pos.regime === 'A'
                              ? 'bg-emerald-950 text-emerald-300 border border-emerald-800'
                              : 'bg-blue-950 text-blue-300 border border-blue-800'
                          }`}>
                            {pos.regime === 'A' ? 'Regime A (ORB)' : 'Regime C (VWAP)'}
                          </span>
                        )}
                      </div>
                      {pos.rotation_pair && (
                        <div className="flex items-center space-x-1 text-[10px] text-gray-400 mt-1">
                          <span className="text-gray-500">Pair:</span>
                          <span className="text-emerald-400 font-semibold">{pos.rotation_pair}</span>
                          {pos.wash_sale_armed && (
                            <span className="px-1 py-0.2 rounded bg-amber-950/80 text-amber-300 border border-amber-800/60 text-[9px]">
                              Pivot Armed
                            </span>
                          )}
                        </div>
                      )}
                    </td>

                    {/* Classification */}
                    <td className="py-3.5 px-4 font-sans">
                      {pos.managed ? (
                        <span className="inline-flex items-center px-2 py-0.5 rounded text-[11px] font-medium bg-cyan-950/80 text-cyan-300 border border-cyan-800/60">
                          <Activity className="w-3 h-3 mr-1 text-cyan-400" />
                          Managed Sandbox
                        </span>
                      ) : (
                        <span className="inline-flex items-center px-2 py-0.5 rounded text-[11px] font-medium bg-amber-950/80 text-amber-300 border border-amber-800/60">
                          <ExternalLink className="w-3 h-3 mr-1 text-amber-400" />
                          External Schwab Holding
                        </span>
                      )}
                    </td>

                    {/* Quantity */}
                    <td className="py-3.5 px-4 text-right text-gray-200">
                      {pos.quantity} shs
                    </td>

                    {/* Entry / Last */}
                    <td className="py-3.5 px-4 text-right">
                      <div className="text-gray-100 font-semibold">${pos.current_price.toFixed(2)}</div>
                      <div className="text-[10px] text-gray-400 font-normal">Entry: ${cost.toFixed(2)}</div>
                    </td>

                    {/* 20% Exposure Gauge */}
                    <td className="py-3.5 px-4 min-w-[170px]">
                      {pos.managed ? (
                        <div className="space-y-1">
                          <div className="flex items-center justify-between text-[11px]">
                            <span className="text-gray-300 font-semibold">${notional.toFixed(2)}</span>
                            <span className={`font-bold ${isCapBreached ? 'text-rose-400' : 'text-cyan-300'}`}>
                              {exposurePct.toFixed(1)}% / 20%
                            </span>
                          </div>
                          {/* Exposure Meter Bar */}
                          <div className="w-full h-1.5 bg-gray-800 rounded-full overflow-hidden relative">
                            {/* 20% threshold guide line */}
                            <div 
                              className={`h-full rounded-full transition-all duration-300 ${
                                isCapBreached ? 'bg-rose-500' : exposurePct > 18.0 ? 'bg-amber-400' : 'bg-cyan-400'
                              }`}
                              style={{ width: `${Math.min((exposurePct / 20.0) * 100, 100)}%` }}
                            />
                          </div>
                          <span className="text-[9px] text-gray-500 block">
                            {isCapBreached ? 'Exposure Cap Breached!' : `$${(200.0 - notional).toFixed(2)} headroom`}
                          </span>
                        </div>
                      ) : (
                        <div className="text-[11px] text-gray-400">
                          <span>${notional.toFixed(2)} (External Schwab)</span>
                        </div>
                      )}
                    </td>

                    {/* Dual-Tier Stops */}
                    <td className="py-3.5 px-4 text-right">
                      {pos.managed ? (
                        <div className="space-y-0.5">
                          {trailingStop && (
                            <div className="flex items-center justify-end space-x-1 text-emerald-400">
                              <Shield className="w-3 h-3 text-emerald-400" />
                              <span className="font-semibold">${trailingStop.toFixed(2)}</span>
                              <span className="text-[9px] text-gray-500">(Trail)</span>
                            </div>
                          )}
                          {hardStop && (
                            <div className="flex items-center justify-end space-x-1 text-rose-300">
                              <Shield className="w-3 h-3 text-rose-400" />
                              <span>${hardStop.toFixed(2)}</span>
                              <span className="text-[9px] text-gray-500">(NATR)</span>
                            </div>
                          )}
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
                          <ShieldCheck className="w-3 h-3 mr-1" />
                          Active Guard
                        </span>
                      ) : pos.stop_order_id ? (
                        <span className="inline-flex items-center text-[10px] text-cyan-400 bg-cyan-950/60 px-2 py-0.5 rounded border border-cyan-800">
                          <ShieldCheck className="w-3 h-3 mr-1" />
                          Schwab GTC Stop
                        </span>
                      ) : (
                        <span className="inline-flex items-center text-[10px] text-amber-400 bg-amber-950/60 px-2 py-0.5 rounded border border-amber-800">
                          <AlertTriangle className="w-3 h-3 mr-1" />
                          Unprotected
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
