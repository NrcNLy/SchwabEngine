/**
 * src/components/DualSleeveProjection.tsx
 * =======================================
 * Interactive projection widget for the "Dual-Sleeve Robo-Core" architecture (#95).
 * Self-contained: React + Tailwind only, chart rendered as responsive inline SVG.
 *
 * Trajectories:
 *   Baseline    = capital * (1 + core/100)^t
 *   Unmanaged   = capital * (1 + (sat - drag)/100)^t
 *   Dual-Sleeve = capital * (0.85 * (1 + core/100)^t + 0.15 * (1 + sat/100)^t)
 */

import React, { useId, useMemo, useState } from 'react';

const CORE_WEIGHT = 0.85;
const SAT_WEIGHT = 0.15;

const COLORS = {
  baseline: '#7dd3fc',
  unmanaged: '#ff897d',
  dual: '#6dd58c',
  grid: '#202a3e',
  axis: '#98a2b5',
} as const;

interface SliderSpec {
  key: 'years' | 'capital' | 'core' | 'sat' | 'drag';
  label: string;
  min: number;
  max: number;
  step: number;
  format: (v: number) => string;
}

const usd = (v: number): string =>
  v.toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });

const compactUsd = (v: number): string => {
  const abs = Math.abs(v);
  if (abs >= 1_000_000) return `$${(v / 1_000_000).toFixed(2)}M`;
  if (abs >= 1_000) return `$${(v / 1_000).toFixed(0)}k`;
  return `$${v.toFixed(0)}`;
};

const pct = (v: number): string => `${v > 0 ? '+' : ''}${v}%`;

const SLIDERS: SliderSpec[] = [
  { key: 'years', label: 'Time Horizon', min: 1, max: 10, step: 1, format: (v) => `${v} ${v === 1 ? 'year' : 'years'}` },
  { key: 'capital', label: 'Initial Capital', min: 10_000, max: 250_000, step: 1_000, format: usd },
  { key: 'core', label: '1x Core Annual Return', min: -10, max: 20, step: 0.5, format: pct },
  { key: 'sat', label: '3x LETF Gross Return', min: -30, max: 60, step: 1, format: pct },
  { key: 'drag', label: 'Unmanaged Volatility Drag', min: 0, max: 20, step: 0.5, format: (v) => `${v}%` },
];

interface Inputs {
  years: number;
  capital: number;
  core: number;
  sat: number;
  drag: number;
}

const DEFAULTS: Inputs = { years: 5, capital: 100_000, core: 8, sat: 24, drag: 12 };

/** Compound growth factor; floors the base at 0 so extreme negatives cannot go below total loss. */
const growth = (ratePct: number, t: number): number => Math.pow(Math.max(0, 1 + ratePct / 100), t);

interface Point {
  t: number;
  baseline: number;
  unmanaged: number;
  dual: number;
}

const project = (i: Inputs, t: number): Point => ({
  t,
  baseline: i.capital * growth(i.core, t),
  unmanaged: i.capital * growth(i.sat - i.drag, t),
  dual: i.capital * (CORE_WEIGHT * growth(i.core, t) + SAT_WEIGHT * growth(i.sat, t)),
});

const VB_W = 640;
const VB_H = 320;
const PAD = { top: 16, right: 20, bottom: 36, left: 64 };

const niceStep = (range: number, ticks: number): number => {
  const raw = range / ticks;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const nice = norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10;
  return nice * mag;
};

export const DualSleeveProjection: React.FC = () => {
  const [inputs, setInputs] = useState<Inputs>(DEFAULTS);
  const baseId = useId();

  const series = useMemo<Point[]>(
    () => Array.from({ length: inputs.years + 1 }, (_, t) => project(inputs, t)),
    [inputs],
  );

  const final = series[series.length - 1];
  const edge = final.dual - final.baseline;
  const edgePct = final.baseline > 0 ? (edge / final.baseline) * 100 : 0;
  const edgePositive = edge >= 0;

  // Chart geometry
  const chart = useMemo(() => {
    const all = series.flatMap((p) => [p.baseline, p.unmanaged, p.dual]);
    const rawMax = Math.max(...all, 1);
    const rawMin = Math.min(...all, 0);
    const step = niceStep(rawMax - rawMin || 1, 5);
    const yMin = Math.floor(rawMin / step) * step;
    const yMax = Math.ceil(rawMax / step) * step;
    const plotW = VB_W - PAD.left - PAD.right;
    const plotH = VB_H - PAD.top - PAD.bottom;
    const x = (t: number) => PAD.left + (inputs.years === 0 ? 0 : (t / inputs.years) * plotW);
    const y = (v: number) => PAD.top + plotH - ((v - yMin) / (yMax - yMin || 1)) * plotH;
    const yTicks: number[] = [];
    for (let v = yMin; v <= yMax + step / 2; v += step) yTicks.push(v);
    const path = (k: 'baseline' | 'unmanaged' | 'dual') =>
      series.map((p, idx) => `${idx === 0 ? 'M' : 'L'}${x(p.t).toFixed(1)},${y(p[k]).toFixed(1)}`).join(' ');
    return { x, y, yTicks, path };
  }, [series, inputs.years]);

  const setValue = (key: keyof Inputs) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setInputs((prev) => ({ ...prev, [key]: Number(e.target.value) }));

  const legend: { key: 'baseline' | 'unmanaged' | 'dual'; label: string; color: string; dashed?: boolean }[] = [
    { key: 'baseline', label: 'Baseline (100% Core)', color: COLORS.baseline },
    { key: 'unmanaged', label: 'Unmanaged (100% 3x LETF)', color: COLORS.unmanaged, dashed: true },
    { key: 'dual', label: 'Dual-Sleeve (85/15)', color: COLORS.dual },
  ];

  return (
    <section
      aria-label="Dual-Sleeve projection simulator"
      className="flex flex-col gap-4 w-full p-4 rounded-3xl bg-surface-container-low shadow-m3-2 text-[#e1e5ee] select-none"
    >
      <header className="flex flex-col gap-1">
        <h2 className="text-lg font-medium tracking-tight">Dual-Sleeve Projection</h2>
        <p className="text-sm text-[#98a2b5]">
          Simulated growth of {usd(inputs.capital)} over {inputs.years} {inputs.years === 1 ? 'year' : 'years'}.
        </p>
      </header>

      {/* Summary card */}
      <div
        className="flex flex-col gap-1 p-4 rounded-2xl border"
        style={{
          backgroundColor: edgePositive ? 'rgba(109, 213, 140, 0.10)' : 'rgba(255, 137, 125, 0.10)',
          borderColor: edgePositive ? 'rgba(109, 213, 140, 0.35)' : 'rgba(255, 137, 125, 0.35)',
        }}
        role="status"
        aria-live="polite"
      >
        <span className="text-xs uppercase tracking-wider text-[#98a2b5]">Dual-Sleeve Edge vs Baseline</span>
        <span
          className="text-3xl font-semibold tabular-nums"
          style={{ color: edgePositive ? COLORS.dual : COLORS.unmanaged }}
        >
          {edgePositive ? '+' : '-'}
          {usd(Math.abs(edge))}
        </span>
        <span className="text-xs text-[#98a2b5] tabular-nums">
          {edgePct >= 0 ? '+' : ''}
          {edgePct.toFixed(1)}% vs Baseline · Dual-Sleeve {usd(final.dual)} · Baseline {usd(final.baseline)} · Unmanaged{' '}
          {usd(final.unmanaged)}
        </span>
      </div>

      {/* Chart */}
      <div className="rounded-2xl bg-[#0b1020] p-2">
        <svg
          viewBox={`0 0 ${VB_W} ${VB_H}`}
          className="w-full h-auto"
          role="img"
          aria-label="Line chart comparing Baseline, Unmanaged and Dual-Sleeve portfolio value by year"
        >
          {chart.yTicks.map((v) => (
            <g key={v}>
              <line x1={PAD.left} x2={VB_W - PAD.right} y1={chart.y(v)} y2={chart.y(v)} stroke={COLORS.grid} />
              <text x={PAD.left - 8} y={chart.y(v) + 4} textAnchor="end" fontSize="11" fill={COLORS.axis}>
                {compactUsd(v)}
              </text>
            </g>
          ))}
          {series.map((p) => (
            <text key={p.t} x={chart.x(p.t)} y={VB_H - 14} textAnchor="middle" fontSize="11" fill={COLORS.axis}>
              {`Y${p.t}`}
            </text>
          ))}
          {legend.map((l) => (
            <g key={l.key}>
              <path
                d={chart.path(l.key)}
                fill="none"
                stroke={l.color}
                strokeWidth={l.key === 'dual' ? 3 : 2}
                strokeDasharray={l.dashed ? '6 4' : undefined}
                strokeLinejoin="round"
                strokeLinecap="round"
              />
              <circle cx={chart.x(final.t)} cy={chart.y(final[l.key])} r="4" fill={l.color} />
            </g>
          ))}
        </svg>
        <ul className="flex flex-wrap gap-x-4 gap-y-1 px-2 pb-1 text-xs text-[#98a2b5]">
          {legend.map((l) => (
            <li key={l.key} className="flex items-center gap-1.5">
              <span
                className="inline-block w-4 h-0.5 rounded"
                style={{ backgroundColor: l.color, opacity: l.dashed ? 0.7 : 1 }}
              />
              {l.label}
            </li>
          ))}
        </ul>
      </div>

      {/* Sliders */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-4">
        {SLIDERS.map((s) => {
          const id = `${baseId}-${s.key}`;
          return (
            <div key={s.key} className="flex flex-col gap-1">
              <div className="flex items-baseline justify-between">
                <label htmlFor={id} className="text-sm text-[#c4cad8]">
                  {s.label}
                </label>
                <output htmlFor={id} className="text-sm font-medium tabular-nums text-[#7dd3fc]">
                  {s.format(inputs[s.key])}
                </output>
              </div>
              <input
                id={id}
                type="range"
                min={s.min}
                max={s.max}
                step={s.step}
                value={inputs[s.key]}
                onChange={setValue(s.key)}
                className="w-full h-2 cursor-pointer"
                style={{ accentColor: COLORS.baseline }}
              />
              <div className="flex justify-between text-[10px] text-[#5c667a] tabular-nums">
                <span>{s.format(s.min)}</span>
                <span>{s.format(s.max)}</span>
              </div>
            </div>
          );
        })}
      </div>

      <div className="flex justify-end">
        <button
          type="button"
          onClick={() => setInputs(DEFAULTS)}
          className="px-4 py-1.5 rounded-full text-sm text-[#7dd3fc] border border-[#5c667a] hover:bg-[#202a3e] transition-colors"
        >
          Reset defaults
        </button>
      </div>

      <footer className="text-xs italic text-[#98a2b5] border-t border-[#202a3e] pt-3">
        Assumes the Dual-Sleeve router successfully filters out severe variance drain via the HMM Meta-Governor.
      </footer>
    </section>
  );
};

export default DualSleeveProjection;
