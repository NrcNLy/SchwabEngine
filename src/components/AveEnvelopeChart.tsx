/**
 * src/components/AveEnvelopeChart.tsx
 * ===================================
 * High-performance canvas chart for Adaptive Volatility Envelope using uPlot.
 *
 * Design Invariants:
 * 1. Zero React re-renders on incoming ticks: consumes from Float64Array ring buffer.
 * 2. requestAnimationFrame loop polls dirtyRef. If true, calls uPlot.setData() with typed arrays.
 * 3. Five series: [0] Time (x-axis), [1] Price, [2] Upper Bound, [3] Lower Bound, [4] Supremum.
 * 4. Band fill: Shaded semi-transparent probability cone between Upper Bound and Lower Bound traces.
 * 5. Tab visibility guard: pauses rAF loop when document.hidden is true.
 */

import React, { useEffect, useRef } from 'react';
import uPlot from 'uplot';
import 'uplot/dist/uPlot.min.css';
import { useAveStream } from '../hooks/useAveStream';
import { AveStatusBadge } from './AveStatusBadge';

interface AveEnvelopeChartProps {
  symbol: string;
  wsUrl?: string;
  height?: number;
}

export const AveEnvelopeChart: React.FC<AveEnvelopeChartProps> = ({ symbol, wsUrl, height = 450 }) => {
  const chartRef = useRef<HTMLDivElement>(null);
  const uplotInstanceRef = useRef<uPlot | null>(null);
  const rafIdRef = useRef<number | null>(null);

  const { bufferRef, dirtyRef, statusMetrics } = useAveStream({ symbol, wsUrl });

  useEffect(() => {
    if (!chartRef.current) return;

    const containerWidth = chartRef.current.clientWidth || 800;

    const opts: uPlot.Options = {
      width: containerWidth,
      height: height,
      title: `${symbol.toUpperCase()} Adaptive Volatility Envelope (Live)`,
      mode: 1,
      pxAlign: 0,
      cursor: {
        drag: { x: true, y: true },
        sync: { key: 'ave' },
      },
      legend: {
        show: true,
      },
      scales: {
        x: {
          time: true,
          auto: true,
        },
        y: {
          auto: true,
        },
      },
      axes: [
        {
          scale: 'x',
          stroke: '#94a3b8',
          grid: { stroke: '#1e293b', width: 1 },
          ticks: { stroke: '#334155', width: 1 },
        },
        {
          scale: 'y',
          stroke: '#94a3b8',
          grid: { stroke: '#1e293b', width: 1 },
          ticks: { stroke: '#334155', width: 1 },
          values: (_u, vals) => vals.map((v) => `$${v.toFixed(2)}`),
        },
      ],
      series: [
        {
          label: 'Time',
        },
        {
          label: `${symbol.toUpperCase()} Price`,
          stroke: '#ffffff',
          width: 2,
          points: { show: false },
        },
        {
          label: 'Upper Bound U(t)',
          stroke: 'rgba(52, 211, 153, 0.9)', // Emerald
          width: 1.5,
          dash: [6, 4],
          points: { show: false },
        },
        {
          label: 'Lower Bound L(t)',
          stroke: 'rgba(239, 68, 68, 0.9)', // Red
          width: 1.5,
          dash: [6, 4],
          points: { show: false },
        },
        {
          label: 'Supremum M(t)',
          stroke: 'rgba(56, 189, 248, 0.7)', // Cyan
          width: 1,
          dash: [3, 3],
          points: { show: false },
        },
      ],
      bands: [
        {
          // Shaded probability cone between series 2 (Upper) and series 3 (Lower)
          series: [2, 3],
          fill: 'rgba(14, 165, 233, 0.12)', // Subtle cyan/blue shaded cone
        },
      ],
    };

    // Initial empty data arrays
    const initialData = bufferRef.current.getUPlotData();
    const u = new uPlot(opts, initialData as any, chartRef.current);
    uplotInstanceRef.current = u;

    // Handle container resize
    const resizeObserver = new ResizeObserver((entries) => {
      for (const entry of entries) {
        if (entry.contentRect.width > 0) {
          u.setSize({
            width: entry.contentRect.width,
            height: height,
          });
        }
      }
    });
    resizeObserver.observe(chartRef.current);

    // requestAnimationFrame rendering loop
    let isRunning = true;

    function renderLoop() {
      if (!isRunning) return;

      if (dirtyRef.current && uplotInstanceRef.current && !document.hidden) {
        dirtyRef.current = false;
        const currentData = bufferRef.current.getUPlotData();
        if (currentData[0].length > 0) {
          uplotInstanceRef.current.setData(currentData as any);
        }
      }

      rafIdRef.current = requestAnimationFrame(renderLoop);
    }

    rafIdRef.current = requestAnimationFrame(renderLoop);

    // Pause / resume loop on tab visibility changes
    function onVisibilityChange() {
      if (document.hidden) {
        if (rafIdRef.current) cancelAnimationFrame(rafIdRef.current);
      } else {
        dirtyRef.current = true;
        rafIdRef.current = requestAnimationFrame(renderLoop);
      }
    }
    document.addEventListener('visibilitychange', onVisibilityChange);

    return () => {
      isRunning = false;
      document.removeEventListener('visibilitychange', onVisibilityChange);
      if (rafIdRef.current) {
        cancelAnimationFrame(rafIdRef.current);
      }
      resizeObserver.disconnect();
      u.destroy();
      uplotInstanceRef.current = null;
    };
  }, [symbol, height]);

  return (
    <div className="flex flex-col gap-3 w-full bg-slate-950/80 p-4 rounded-xl border border-slate-800 shadow-2xl backdrop-blur-sm">
      <AveStatusBadge metrics={statusMetrics} />
      <div className="w-full relative overflow-hidden rounded-lg bg-slate-900/60 p-2 border border-slate-800/80">
        <div ref={chartRef} className="w-full h-full flex items-center justify-center" />
      </div>
    </div>
  );
};
