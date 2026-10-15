/**
 * src/components/AveEnvelopeChart.tsx
 * ===================================
 * High-performance canvas chart for Adaptive Volatility Envelope using uPlot.
 *
 * M3 Viewport Redesign:
 * 1. Full-bleed edge-to-edge container filling 100% width and responsive flex height.
 * 2. Series legend docked as semi-transparent HUD overlay in top-right corner.
 * 3. M3 Dark Theme tonal color palette (Upper=Bull, Lower=Bear, Supremum=Sky, Band=Cone fill).
 * 4. Zero React re-renders on incoming ticks: consumes from Float64Array ring buffer via rAF.
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

export const AveEnvelopeChart: React.FC<AveEnvelopeChartProps> = ({ symbol, wsUrl, height }) => {
  const chartRef = useRef<HTMLDivElement>(null);
  const uplotInstanceRef = useRef<uPlot | null>(null);
  const rafIdRef = useRef<number | null>(null);

  const { bufferRef, dirtyRef, statusMetrics } = useAveStream({ symbol, wsUrl });

  useEffect(() => {
    if (!chartRef.current) return;

    const containerWidth = chartRef.current.clientWidth || 800;
    const containerHeight = chartRef.current.clientHeight || height || 450;

    const opts: uPlot.Options = {
      width: containerWidth,
      height: containerHeight,
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
          stroke: '#98a2b5', // on-surface-variant
          grid: { stroke: '#202a3e', width: 1 }, // surface-container-high
          ticks: { stroke: '#5c667a', width: 1 }, // outline
        },
        {
          scale: 'y',
          stroke: '#98a2b5',
          grid: { stroke: '#202a3e', width: 1 },
          ticks: { stroke: '#5c667a', width: 1 },
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
          label: 'Upper U(t)',
          stroke: '#6dd58c', // finance.bull
          width: 1.5,
          dash: [6, 4],
          points: { show: false },
        },
        {
          label: 'Lower L(t)',
          stroke: '#ff897d', // finance.bear
          width: 1.5,
          dash: [6, 4],
          points: { show: false },
        },
        {
          label: 'Supremum M(t)',
          stroke: '#7dd3fc', // finance.supremum
          width: 1,
          dash: [3, 3],
          points: { show: false },
        },
      ],
      bands: [
        {
          // Shaded probability cone between series 2 (Upper) and series 3 (Lower)
          series: [2, 3],
          fill: 'rgba(103, 214, 237, 0.08)', // M3 cone fill
        },
      ],
    };

    // Initial empty data arrays
    const initialData = bufferRef.current.getUPlotData();
    const u = new uPlot(opts, initialData as any, chartRef.current);
    uplotInstanceRef.current = u;

    // Responsive ResizeObserver updating width and height dynamically
    const resizeObserver = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const { width: w, height: h } = entry.contentRect;
        if (w > 0 && h > 0) {
          u.setSize({
            width: w,
            height: h,
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

    // Tab visibility listener
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
    <div className="flex flex-col w-full h-full flex-1 gap-2.5 p-3 select-none overflow-hidden">
      {/* M3 Status Header with Asymmetric Safety Toggle & Tabular Metrics */}
      <AveStatusBadge metrics={statusMetrics} />

      {/* Full-Bleed Canvas Viewport with Top-Right HUD Legend */}
      <div className="w-full flex-1 relative min-h-[420px] rounded-2xl bg-surface-container-low shadow-m3-2 overflow-hidden">
        <div ref={chartRef} className="w-full h-full absolute inset-0" />
      </div>
    </div>
  );
};
