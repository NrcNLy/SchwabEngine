import React from 'react';
import { Compass, TrendingDown, TrendingUp, Minus } from 'lucide-react';
import { RegimeSummary } from '../types';
import { SHOW_DEBUG } from '../utils/format';

interface LlmInsightConsoleProps {
  summary: RegimeSummary | null;
  unavailable: boolean;
}

const BIAS_STYLE: Record<string, { tone: string; Icon: React.ComponentType<{ className?: string }> }> = {
  bullish: { tone: 'text-emerald-300 bg-emerald-950/60 border-emerald-800/60', Icon: TrendingUp },
  bearish: { tone: 'text-rose-300 bg-rose-950/60 border-rose-800/60', Icon: TrendingDown },
  neutral: { tone: 'text-gray-300 bg-gray-900 border-gray-700', Icon: Minus },
};

/** Plain-language market regime summary. Advisory only; raw payloads stay behind the dev flag. */
export const LlmInsightConsole: React.FC<LlmInsightConsoleProps> = ({ summary, unavailable }) => {
  const bias = summary ? BIAS_STYLE[summary.bias] ?? BIAS_STYLE.neutral : BIAS_STYLE.neutral;
  const BiasIcon = bias.Icon;

  return (
    <section className="bg-[#0e1422] border border-gray-800 rounded-xl p-4 sm:p-5 shadow-2xl">
      <div className="flex items-center justify-between gap-3 pb-3 border-b border-gray-800">
        <div className="flex items-center gap-2.5">
          <div className="p-2 rounded-lg bg-purple-500/10 text-purple-400 border border-purple-500/20">
            <Compass className="w-4 h-4" />
          </div>
          <h3 className="font-semibold text-gray-100 text-sm font-mono uppercase tracking-wider">Market Regime</h3>
        </div>
        {summary?.stale && (
          <span className="px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-amber-950 text-amber-300 border border-amber-800/60">
            Stale{summary.age_hours !== null ? ` · ${Math.round(summary.age_hours)}h old` : ''}
          </span>
        )}
      </div>

      {summary === null ? (
        <p className="mt-4 text-xs font-mono text-gray-500">
          {unavailable ? 'Regime summary is unavailable right now.' : 'Loading regime summary…'}
        </p>
      ) : (
        <div className="mt-4 space-y-3">
          <div className="flex flex-wrap items-center gap-2 font-mono">
            <span className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg border text-xs font-bold ${bias.tone}`}>
              <BiasIcon className="w-3.5 h-3.5" />
              {summary.bias_label}
            </span>
            <span className="px-2.5 py-1 rounded-lg border border-cyan-900/60 bg-cyan-950/50 text-cyan-300 text-xs font-bold">
              {summary.regime_label}
            </span>
            <span className="px-2.5 py-1 rounded-lg border border-gray-700 bg-gray-900 text-gray-300 text-xs">
              {summary.volatility_label}
            </span>
          </div>

          <p className="text-sm text-gray-300 leading-snug">{summary.regime_description}</p>

          {summary.engine_regimes.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {summary.engine_regimes.map((r) => (
                <span key={r.symbol} className="px-2 py-0.5 rounded text-[10px] font-mono bg-gray-900 border border-gray-800 text-gray-300">
                  {r.symbol} · {r.label ?? r.regime}
                </span>
              ))}
            </div>
          )}

          <p className="text-[11px] text-gray-500 font-mono">{summary.advisory_note}</p>

          {SHOW_DEBUG && (
            <details className="text-[10px] font-mono text-gray-500">
              <summary className="cursor-pointer text-gray-400">Developer: raw payload</summary>
              <pre className="mt-2 p-2 bg-[#070b12] border border-gray-800 rounded overflow-x-auto text-gray-400">
                {JSON.stringify(summary, null, 2)}
              </pre>
            </details>
          )}
        </div>
      )}
    </section>
  );
};
