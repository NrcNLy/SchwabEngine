import React, { useState } from 'react';
import { Terminal, Brain, Clock, Zap, FileText } from 'lucide-react';
import { EngineStatus, MacroLiquidityStateResponse } from '../types';

interface LlmInsightConsoleProps {
  status: EngineStatus;
  macroState?: MacroLiquidityStateResponse | null;
}

export const LlmInsightConsole: React.FC<LlmInsightConsoleProps> = ({ status, macroState }) => {
  const [activeTab, setActiveTab] = useState<'MARKET' | 'CREDIT'>('MARKET');
  const insight = status.llm_insight;

  if (!insight && !macroState) return null;

  return (
    <div className="bg-[#0b101d] border border-purple-900/40 rounded-xl shadow-2xl overflow-hidden flex flex-col">
      <div className="bg-[#111827] border-b border-gray-800 flex items-center justify-between">
        <div className="flex">
          {insight && (
            <button
              onClick={() => setActiveTab('MARKET')}
              className={`px-4 py-3 text-xs font-semibold font-mono tracking-wider flex items-center transition-colors ${
                activeTab === 'MARKET' ? 'text-purple-400 bg-purple-500/5 border-b-2 border-purple-500' : 'text-gray-500 hover:text-gray-300'
              }`}
            >
              <Brain className="w-4 h-4 mr-2" />
              Market Regime Analysis
            </button>
          )}
          {macroState && (
            <button
              onClick={() => setActiveTab('CREDIT')}
              className={`px-4 py-3 text-xs font-semibold font-mono tracking-wider flex items-center transition-colors ${
                activeTab === 'CREDIT' ? 'text-emerald-400 bg-emerald-500/5 border-b-2 border-emerald-500' : 'text-gray-500 hover:text-gray-300'
              }`}
            >
              <FileText className="w-4 h-4 mr-2" />
              Credit / Macro Extraction Audit
            </button>
          )}
        </div>

        {activeTab === 'MARKET' && insight && (
          <div className="px-4 flex items-center space-x-3 text-[10px] font-mono">
            <div className="flex items-center text-gray-400">
              <Zap className="w-3 h-3 text-amber-400 mr-1" />
              {insight.model}
            </div>
            <div className="flex items-center text-gray-400">
              <Clock className="w-3 h-3 text-emerald-400 mr-1" />
              {insight.latency_ms}ms Latency
            </div>
          </div>
        )}
      </div>

      {activeTab === 'MARKET' && insight && (
        <div className="grid grid-cols-1 lg:grid-cols-2 divide-y lg:divide-y-0 lg:divide-x divide-gray-800">
          <div className="p-4">
            <div className="flex items-center text-[10px] font-mono text-gray-500 uppercase tracking-widest mb-2">
              <Terminal className="w-3 h-3 mr-1.5" />
              System Prompt Dispatched
            </div>
            <div className="bg-black/40 rounded p-3 text-xs font-mono text-gray-300 leading-relaxed overflow-y-auto max-h-48 border border-gray-800">
              {insight.last_prompt}
            </div>
          </div>

          <div className="p-4 bg-[#0e1422]">
            <div className="flex items-center text-[10px] font-mono text-gray-500 uppercase tracking-widest mb-2">
              <Brain className="w-3 h-3 mr-1.5" />
              Structured JSON Parsed Output
            </div>
            <pre className="bg-[#080d17] rounded p-3 text-xs font-mono text-emerald-300 overflow-y-auto max-h-48 border border-gray-800 whitespace-pre-wrap">
              <code>{insight.last_response}</code>
            </pre>
          </div>
        </div>
      )}

      {activeTab === 'CREDIT' && macroState && (
        <div className="p-4 bg-[#0e1422]">
          <div className="flex items-center text-[10px] font-mono text-gray-500 uppercase tracking-widest mb-2">
            <FileText className="w-3 h-3 mr-1.5" />
            Raw Macro Liquidity State Payload
          </div>
          <pre className="bg-[#080d17] rounded p-3 text-xs font-mono text-emerald-300 overflow-y-auto max-h-96 border border-gray-800 whitespace-pre-wrap">
            <code>{JSON.stringify(macroState, null, 2)}</code>
          </pre>
        </div>
      )}
    </div>
  );
};
