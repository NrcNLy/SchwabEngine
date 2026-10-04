import React, { useState } from 'react';
import { 
  Building2, 
  ShieldCheck, 
  ShieldAlert, 
  Upload,
  Calendar,
  AlertTriangle,
  FileText
} from 'lucide-react';
import { MacroLiquidityStateResponse } from '../types';
import { DocumentUploadModal } from './DocumentUploadModal';

interface MacroLiquidityCardProps {
  macroState: MacroLiquidityStateResponse | null;
  onUploadComplete: () => void;
}

export const MacroLiquidityCard: React.FC<MacroLiquidityCardProps> = ({ macroState, onUploadComplete }) => {
  const [isModalOpen, setIsModalOpen] = useState(false);

  if (!macroState) {
    return (
      <div className="bg-[#0e1422] border border-gray-800 rounded-xl p-6 shadow-2xl flex flex-col items-center justify-center min-h-[300px]">
        <Building2 className="w-8 h-8 text-gray-600 mb-4 animate-pulse" />
        <p className="text-gray-400 font-mono text-sm">Awaiting Macro Liquidity Telemetry...</p>
      </div>
    );
  }

  const { collateral_state, credit_report, promotional_debts } = macroState;
  const isSolvent = collateral_state.is_solvent;
  
  // Calculate buffer percentage for the gauge
  const totalDebt = collateral_state.active_promotional_debt;
  const totalBackstop = collateral_state.total_liquid_backstop;
  const maxScale = Math.max(totalDebt, totalBackstop) * 1.1; // 10% padding
  
  const backstopPct = maxScale > 0 ? (totalBackstop / maxScale) * 100 : 0;
  const debtPct = maxScale > 0 ? (totalDebt / maxScale) * 100 : 0;

  return (
    <>
      <div className="bg-[#0e1422] border border-gray-800 rounded-xl p-6 shadow-2xl space-y-6">
        {/* Header */}
        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between pb-4 border-b border-gray-800 gap-4">
          <div className="flex items-center space-x-3">
            <div className={`p-2.5 rounded-lg border ${isSolvent ? 'bg-indigo-500/10 text-indigo-400 border-indigo-500/20' : 'bg-rose-500/10 text-rose-400 border-rose-500/20'}`}>
              <Building2 className="w-5 h-5" />
            </div>
            <div>
              <div className="flex items-center space-x-2">
                <h3 className="font-semibold text-gray-100 text-sm font-mono uppercase tracking-wider">
                  Macro Liquidity Invariant
                </h3>
                {isSolvent ? (
                  <span className="px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-emerald-950 text-emerald-300 border border-emerald-800 flex items-center">
                    <ShieldCheck className="w-3 h-3 mr-1" /> SOLVENT
                  </span>
                ) : (
                  <span className="px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-rose-950 text-rose-300 border border-rose-800 flex items-center animate-pulse">
                    <ShieldAlert className="w-3 h-3 mr-1" /> COLLATERAL DEFICIT
                  </span>
                )}
              </div>
              <p className="text-xs text-gray-400 mt-0.5">
                Evaluates float solvency against promotional debt maturity timelines.
              </p>
            </div>
          </div>

          <div className="flex items-center space-x-3">
            <button 
              onClick={() => setIsModalOpen(true)}
              className="flex items-center px-3 py-1.5 bg-[#1a2333] hover:bg-[#232f45] border border-indigo-900/50 text-indigo-300 text-xs font-mono rounded transition-colors"
            >
              <Upload className="w-3.5 h-3.5 mr-1.5" />
              Ingest Document
            </button>
            <div className="text-right font-mono">
              <span className="text-xs text-gray-400 block">Risk Multiplier</span>
              <span className={`text-xl font-bold ${collateral_state.risk_multiplier >= 1.0 ? 'text-emerald-400' : collateral_state.risk_multiplier > 0 ? 'text-amber-400' : 'text-rose-500'}`}>
                {collateral_state.risk_multiplier.toFixed(2)}x
              </span>
            </div>
          </div>
        </div>

        {/* Net Collateral Invariant Gauge */}
        <div>
          <div className="flex justify-between text-xs font-mono mb-2">
            <span className="text-gray-400">Total Liquid Backstop: <span className="text-emerald-300">${totalBackstop.toLocaleString(undefined, {minimumFractionDigits: 2})}</span></span>
            <span className="text-gray-400">Active Debt Float: <span className="text-rose-400">${totalDebt.toLocaleString(undefined, {minimumFractionDigits: 2})}</span></span>
          </div>
          <div className="relative h-4 bg-gray-900 rounded-full border border-gray-800 overflow-hidden flex">
             <div className="absolute top-0 left-0 h-full bg-emerald-500/20 border-r border-emerald-500/50 transition-all duration-500" style={{ width: `${backstopPct}%` }} />
             <div className="absolute top-0 left-0 h-full bg-rose-500/40 border-r border-rose-500 transition-all duration-500" style={{ width: `${debtPct}%` }} />
          </div>
          <div className="mt-2 text-right">
             <span className="text-[11px] font-mono text-gray-500">
               Net Buffer: <span className={collateral_state.net_collateral_buffer >= 0 ? "text-emerald-400" : "text-rose-400 font-bold animate-pulse"}>
                 ${collateral_state.net_collateral_buffer.toLocaleString(undefined, {minimumFractionDigits: 2})}
               </span>
             </span>
          </div>
        </div>

        {/* Aggregate Bureau Utilization Meter */}
        {credit_report && (
          <div className="bg-[#0b101c] border border-gray-800 rounded-xl p-4">
             <div className="flex items-center justify-between mb-3">
               <div className="flex items-center space-x-2">
                 <FileText className="w-4 h-4 text-indigo-400" />
                 <span className="text-xs font-semibold text-gray-300 uppercase tracking-wider font-mono">Bureau Utilization</span>
                 <span className="px-1.5 py-0.5 bg-indigo-950 text-indigo-300 border border-indigo-800/50 rounded text-[10px] font-mono">
                   {credit_report.bureau}
                 </span>
               </div>
               <span className="text-[10px] text-gray-500 font-mono">
                 As of: {credit_report.report_date}
               </span>
             </div>
             
             {(() => {
               const util = credit_report.aggregate_utilization_pct;
               const utilColor = util < 30 ? 'bg-emerald-500' : util < 50 ? 'bg-amber-500' : 'bg-rose-500';
               const textColor = util < 30 ? 'text-emerald-400' : util < 50 ? 'text-amber-400' : 'text-rose-400';
               return (
                 <div>
                   <div className="flex justify-between text-[11px] font-mono text-gray-400 mb-1">
                     <span>${credit_report.total_revolving_balance.toLocaleString()} / ${credit_report.total_revolving_limit.toLocaleString()}</span>
                     <span className={`font-bold ${textColor}`}>{util.toFixed(1)}%</span>
                   </div>
                   <div className="w-full h-2 bg-gray-900 rounded-full overflow-hidden">
                     <div className={`h-full ${utilColor}`} style={{ width: `${Math.min(util, 100)}%` }} />
                   </div>
                 </div>
               );
             })()}
          </div>
        )}

        {/* Promotional Debt Timeline */}
        {promotional_debts.length > 0 && (
          <div>
            <h4 className="text-xs font-semibold text-gray-400 uppercase tracking-wider font-mono mb-3 flex items-center">
              <Calendar className="w-4 h-4 mr-1.5 text-gray-500" />
              Active 0% Balance Transfers
            </h4>
            <div className="space-y-3">
              {promotional_debts.map(debt => (
                <div key={debt.id} className="bg-[#111827] border border-gray-800 rounded-lg p-3 relative overflow-hidden">
                  <div className="flex justify-between items-start mb-2">
                    <div>
                      <div className="text-sm font-bold text-gray-200 font-mono">{debt.institution}</div>
                      <div className="text-[11px] text-gray-500 font-mono">Expires: {debt.expiration_date}</div>
                    </div>
                    <div className="text-right">
                      <div className="text-sm font-bold text-rose-300 font-mono">${debt.total_balance.toLocaleString(undefined, {minimumFractionDigits: 2})}</div>
                      <div className="flex items-center justify-end text-[10px] text-amber-500/80 font-mono mt-0.5">
                        <AlertTriangle className="w-3 h-3 mr-1" />
                        {debt.days_remaining} Days Left
                      </div>
                    </div>
                  </div>
                  
                  {/* Countdown Bar */}
                  <div className="w-full h-1.5 bg-gray-900 rounded-full overflow-hidden mt-2">
                    <div 
                      className={`h-full transition-all duration-500 ${debt.days_remaining < 30 ? 'bg-rose-500' : debt.days_remaining <= 90 ? 'bg-amber-500' : 'bg-emerald-500'}`}
                      style={{ width: `${Math.max(5, Math.min(100, (debt.days_remaining / 365) * 100))}%` }}
                    />
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      <DocumentUploadModal 
        isOpen={isModalOpen} 
        onClose={() => setIsModalOpen(false)} 
        onUploadSuccess={() => {
          setIsModalOpen(false);
          onUploadComplete();
        }} 
      />
    </>
  );
};
