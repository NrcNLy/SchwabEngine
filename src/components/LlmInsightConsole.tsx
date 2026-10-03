import React from 'react';
import { Terminal, Brain, Clock, Zap } from 'lucide-react';
import { EngineStatus } from '../types';

interface LlmInsightConsoleProps {
  status: EngineStatus;
}

export const LlmInsightConsole: React.FC<LlmInsightConsoleProps> = ({ status }) => {
  const insight = status.llm_insight;

  if (!insight) return null;

  return (
    <div className="bg-[#0b101d] border border-purple-900/40 rounded-xl shadow-2xl overflow-hidden">
      <div className="bg-[#111827] px-4 py-3 border-b border-gray-800 flex items-center justify-between">
        <div className="flex items-center space-x-2">
          <Brain className="w-4 h-4 text-purple-400" />
          <h3 className="text-xs font-semibold text-gray-100 font-mono tracking-wider">
            Tier 2 Governor: LLM Intelligence Console
          </h3>
        </div>
        <div className="flex items-center space-x-3 text-[10px] font-mono">
          <div className="flex items-center text-gray-400">
            <Zap className="w-3 h-3 text-amber-400 mr-1" />
            {insight.model}
          </div>
          <div className="flex items-center text-gray-400">
            <Clock className="w-3 h-3 text-emerald-400 mr-1" />
            {insight.latency_ms}ms Latency
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 divide-y lg:divide-y-0 lg:divide-x divide-gray-800">
        {/* Left: System Prompt */}
        <div className="p-4">
          <div className="flex items-center text-[10px] font-mono text-gray-500 uppercase tracking-widest mb-2">
            <Terminal className="w-3 h-3 mr-1.5" />
            System Prompt Dispatched
          </div>
          <div className="bg-black/40 rounded p-3 text-xs font-mono text-gray-300 leading-relaxed overflow-y-auto max-h-48 border border-gray-800">
            {insight.last_prompt}
          </div>
        </div>

        {/* Right: Structured JSON Output */}
        <div className="p-4 bg-[#0e1422]">
          <div className="flex items-center text-[10px] font-mono text-gray-500 uppercase tracking-widest mb-2">
            <Brain className="w-3 h-3 mr-1.5" />
            Structured JSON Parsed Output
          </div>
          <pre className="bg-[#080d17] rounded p-3 text-xs font-mono text-emerald-300 overflow-y-auto max-h-48 border border-gray-800">
            <code>{insight.last_response}</code>
          </pre>
        </div>
      </div>
    </div>
  );
};
