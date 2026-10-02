import React from 'react';
import { DollarSign, Wallet, Clock, Lock } from 'lucide-react';
import { LedgerSnapshot } from '../types';

interface LedgerCardProps {
  ledger: LedgerSnapshot;
}

export const LedgerCard: React.FC<LedgerCardProps> = ({ ledger }) => {
  const totalLiquidity = ledger.bucket1_settled + ledger.bucket2_unsettled + ledger.bucket3_pending;
  const settledPct = totalLiquidity > 0 ? (ledger.bucket1_settled / totalLiquidity) * 100 : 0;

  return (
    <div className="bg-[#111827] border border-gray-800 rounded-xl p-5 shadow-lg shadow-black/40">
      <div className="flex items-center justify-between pb-3 border-b border-gray-800">
        <div className="flex items-center space-x-3">
          <div className="p-2 rounded-lg bg-emerald-500/10 text-emerald-400">
            <Wallet className="w-5 h-5" />
          </div>
          <div>
            <h3 className="font-semibold text-gray-100 text-sm">3-Bucket Capital Ledger</h3>
            <p className="text-[11px] text-gray-400">Good Faith Violation (GFV) Prevention Engine</p>
          </div>
        </div>
        <div className="text-right">
          <span className="text-xs text-gray-400 block">Total Liquidity</span>
          <span className="text-base font-bold text-gray-100 font-mono">
            ${totalLiquidity.toLocaleString('en-US', { minimumFractionDigits: 2 })}
          </span>
        </div>
      </div>

      {/* Progress Bar */}
      <div className="mt-4">
        <div className="flex justify-between text-[11px] text-gray-400 mb-1">
          <span>Settled Cash Allocation</span>
          <span className="font-mono text-emerald-400">{settledPct.toFixed(1)}%</span>
        </div>
        <div className="w-full h-2 bg-gray-800 rounded-full overflow-hidden flex">
          <div 
            style={{ width: `${settledPct}%` }} 
            className="bg-emerald-500 h-full rounded-full transition-all duration-500" 
          />
          <div 
            style={{ width: `${totalLiquidity > 0 ? (ledger.bucket2_unsettled / totalLiquidity) * 100 : 0}%` }} 
            className="bg-cyan-500 h-full transition-all duration-500" 
          />
        </div>
      </div>

      {/* Buckets */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mt-4">
        <div className="p-3 bg-gray-900/80 rounded-lg border border-gray-800">
          <div className="flex items-center text-emerald-400 text-xs font-semibold mb-1">
            <DollarSign className="w-3.5 h-3.5 mr-1" /> Bucket 1 (Settled)
          </div>
          <div className="text-sm font-bold font-mono text-gray-100">
            ${ledger.bucket1_settled.toLocaleString('en-US', { minimumFractionDigits: 2 })}
          </div>
          <span className="text-[10px] text-gray-500">Immediate safe buying</span>
        </div>

        <div className="p-3 bg-gray-900/80 rounded-lg border border-gray-800">
          <div className="flex items-center text-cyan-400 text-xs font-semibold mb-1">
            <Clock className="w-3.5 h-3.5 mr-1" /> Bucket 2 (Unsettled)
          </div>
          <div className="text-sm font-bold font-mono text-gray-100">
            ${ledger.bucket2_unsettled.toLocaleString('en-US', { minimumFractionDigits: 2 })}
          </div>
          <span className="text-[10px] text-gray-500">T+1 clearing cycle</span>
        </div>

        <div className="p-3 bg-gray-900/80 rounded-lg border border-gray-800">
          <div className="flex items-center text-amber-400 text-xs font-semibold mb-1">
            <Clock className="w-3.5 h-3.5 mr-1" /> Bucket 3 (ACH)
          </div>
          <div className="text-sm font-bold font-mono text-gray-100">
            ${ledger.bucket3_pending.toLocaleString('en-US', { minimumFractionDigits: 2 })}
          </div>
          <span className="text-[10px] text-gray-500">Pending bank transit</span>
        </div>

        <div className="p-3 bg-gray-900/80 rounded-lg border border-gray-800">
          <div className="flex items-center text-purple-400 text-xs font-semibold mb-1">
            <Lock className="w-3.5 h-3.5 mr-1" /> Max Order Limit
          </div>
          <div className="text-sm font-bold font-mono text-purple-200">
            ${ledger.max_order_value.toLocaleString('en-US', { minimumFractionDigits: 2 })}
          </div>
          <span className="text-[10px] text-gray-500">Risk ceiling / trade</span>
        </div>
      </div>
    </div>
  );
};
