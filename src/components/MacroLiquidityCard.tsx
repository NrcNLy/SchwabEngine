import React, { useState } from 'react';
import { 
  Building2, 
  ShieldCheck, 
  ShieldAlert, 
  Upload, 
  Target,
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

  const { state: collateral_state, snapshot, liquidity_targets } = macroState;
  const isSolvent = collateral_state.is_solvent;
  
  const totalDebt = collateral_state.active_promotional_debt;
  const totalBackstop = collateral_state.total_liquid_backstop;
  const maxScale = Math.max(totalDebt, totalBackstop) * 1.1;
  
  const backstopPct = maxScale > 0 ? (totalBackstop / maxScale) * 100 : 0;
  const debtPct = maxScale > 0 ? (totalDebt / maxScale) * 100 : 0;

  return (
    <>
      <div className="bg-[#0e1422] border border-gray-800 rounded-xl p-6 shadow-2xl flex flex-col space-y-6">
        
        {/* Header Section */}
        <div className="flex items-start justify-between">
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
                    <ShieldCheck className="w-3 h-3 mr-1" /> RECORD
                  </span>
                ) : (
                  <span className="px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-rose-950 text-rose-300 border border-rose-800 flex items-center">
                    <ShieldAlert className="w-3 h-3 mr-1" /> DEFICIT
                  </span>
                )}
              </div>
              <p className="text-xs text-gray-400 mt-0.5">
                Passive archival float evaluation. Zero sizing influence.
              </p>
            </div>
          </div>

          <div className="flex items-center space-x-3">
            <button 
              onClick={() => setIsModalOpen(true)}
              className="flex items-center px-3 py-1.5 bg-[#1a2333] hover:bg-[#232f45] border border-indigo-900/50 text-indigo-300 text-xs font-mono rounded transition-colors"
            >
              <Upload className="w-3.5 h-3.5 mr-1.5" />
              Ingest
            </button>
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
               Net Buffer: <span className={collateral_state.net_collateral_buffer >= 0 ? "text-emerald-400" : "text-rose-400 font-bold"}>
                 ${collateral_state.net_collateral_buffer.toLocaleString(undefined, {minimumFractionDigits: 2})}
               </span>
             </span>
          </div>
        </div>

        {/* Manual Liquidity Targets */}
        <div>
          <div className="flex justify-between items-center mb-3">
            <h4 className="text-xs font-semibold text-gray-400 uppercase tracking-wider font-mono flex items-center">
              <Target className="w-4 h-4 mr-1.5 text-cyan-500" />
              Manual Liquidity Goals
            </h4>
          </div>
          {liquidity_targets && liquidity_targets.length > 0 ? (
            <div className="space-y-3">
              {liquidity_targets.map(target => {
                const settled = collateral_state.settled_cash;
                const pct = Math.min(100, Math.max(0, (settled / target.target_amount) * 100));
                const daysLeft = Math.max(0, Math.floor((new Date(target.target_date).getTime() - new Date().getTime()) / 86400000));
                const isBehind = pct < 50 && daysLeft < 30; // simple heuristic for behind pace

                return (
                  <div key={target.target_id} className="bg-[#111827] border border-gray-800 rounded-lg p-3 relative overflow-hidden">
                    <div className="flex justify-between items-start mb-2">
                      <div>
                        <div className="text-sm font-bold text-gray-200 font-mono flex items-center">
                          {target.label}
                          {isBehind && <span className="ml-2 w-2 h-2 rounded-full bg-rose-500" title="Behind Pace" />}
                        </div>
                        <div className="text-[11px] text-gray-500 font-mono">Target Date: {target.target_date} ({daysLeft}d left)</div>
                      </div>
                      <div className="text-right">
                        <div className="text-sm font-bold text-cyan-300 font-mono">${target.target_amount.toLocaleString(undefined, {minimumFractionDigits: 2})}</div>
                        <div className="text-[10px] text-gray-500 font-mono">Current: ${settled.toLocaleString(undefined, {minimumFractionDigits: 2})}</div>
                      </div>
                    </div>
                    
                    {/* Progress Bar */}
                    <div className="w-full h-1.5 bg-gray-900 rounded-full overflow-hidden mt-2">
                      <div 
                        className={`h-full transition-all duration-500 ${pct >= 100 ? 'bg-emerald-500' : isBehind ? 'bg-rose-500' : 'bg-cyan-500'}`}
                        style={{ width: `${pct}%` }}
                      />
                    </div>
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="text-xs text-gray-500 font-mono p-4 border border-gray-800 rounded bg-[#111827]/50 text-center">
              No manual targets defined.
            </div>
          )}
        </div>

        {/* Latest Document Audit */}
        {snapshot && (
          <div className="bg-[#0b101c] border border-gray-800 rounded-xl p-4">
             <div className="flex items-center justify-between mb-2">
               <div className="flex items-center space-x-2">
                 <FileText className="w-4 h-4 text-indigo-400" />
                 <span className="text-xs font-semibold text-gray-300 uppercase tracking-wider font-mono">Latest Ingest</span>
                 <span className="px-1.5 py-0.5 bg-indigo-950 text-indigo-300 border border-indigo-800/50 rounded text-[10px] font-mono">
                   {snapshot.document_class}
                 </span>
               </div>
               <span className="text-[10px] text-gray-500 font-mono">
                 {snapshot.report_date}
               </span>
             </div>
             <div className="text-xs font-mono text-gray-400">
               Institution: <span className="text-gray-300">{snapshot.institution_or_bureau}</span>
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
