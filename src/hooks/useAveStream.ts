/**
 * src/hooks/useAveStream.ts
 * =========================
 * Custom React hook for streaming AVE telemetry without triggering React DOM thrashing.
 *
 * Design Invariants:
 * 1. High-frequency frames mutate `bufferRef` and set `dirtyRef = true` without calling `setState`.
 * 2. Status metrics (text badges) are throttled to 4Hz (every 250ms) to conserve main thread.
 * 3. Disconnection / reconnect backoff and automatic resync on packet gap.
 * 4. Stale timer triggers STALE warning if no tick arrives for > 2 seconds.
 */

import { useEffect, useRef, useState } from 'react';
import { AveCompactFrame, AveServerMessage, AveStatusMetrics, FSMState, RegimeType } from '../types/ave';
import { AveRingBuffer } from '../utils/ringBuffer';

interface UseAveStreamOptions {
  symbol: string;
  wsUrl?: string;
  bufferCapacity?: number;
}

export function useAveStream({ symbol, wsUrl, bufferCapacity = 500 }: UseAveStreamOptions) {
  const sym = symbol.toUpperCase();
  const bufferRef = useRef<AveRingBuffer>(new AveRingBuffer(bufferCapacity));
  const dirtyRef = useRef<boolean>(false);
  const lastSeqRef = useRef<number>(0);
  const socketRef = useRef<WebSocket | null>(null);

  const [statusMetrics, setStatusMetrics] = useState<AveStatusMetrics>({
    symbol: sym,
    state: 'INIT',
    stateCode: FSMState.INIT,
    price: 0,
    upper: 0,
    lower: 0,
    supremum: 0,
    lambdaFactor: 1.0,
    regime: 'CHOP',
    lastUpdateEpochMs: 0,
    isStale: true,
    seq: 0,
  });

  const latestMetricsRef = useRef<AveStatusMetrics>({ ...statusMetrics });

  useEffect(() => {
    let reconnectTimeout: ReturnType<typeof setTimeout>;
    let isMounted = true;

    // Resolve URL: default to current host /ws/ave/{sym}
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const host = window.location.host || 'localhost:8000';
    const resolvedUrl = wsUrl || `${protocol}//${host}/ws/ave/${sym}`;

    function connect() {
      if (!isMounted) return;

      const ws = new WebSocket(resolvedUrl);
      ws.binaryType = 'arraybuffer';
      socketRef.current = ws;

      const textDecoder = new TextDecoder('utf-8');

      ws.onmessage = (event: MessageEvent) => {
        let msg: AveServerMessage;
        try {
          if (event.data instanceof ArrayBuffer) {
            const jsonStr = textDecoder.decode(event.data);
            msg = JSON.parse(jsonStr);
          } else {
            msg = JSON.parse(event.data);
          }
        } catch {
          return;
        }

        if (msg.type === 'SNAPSHOT' && msg.symbol === sym) {
          bufferRef.current.clear();
          bufferRef.current.pushBatch(msg.data);
          dirtyRef.current = true;

          if (msg.data.length > 0) {
            const lastFrame = msg.data[msg.data.length - 1];
            updateMetricsFromFrame(lastFrame);
          }
        } else if (msg.type === 'FRAME' && msg.symbol === sym) {
          const frame = msg.frame;
          const seq = frame[0];

          // Packet loss / sequence gap detection: request resync if gap > 5
          if (lastSeqRef.current > 0 && seq > lastSeqRef.current + 5) {
            if (ws.readyState === WebSocket.OPEN) {
              ws.send(JSON.stringify({ action: 'subscribe', symbol: sym }));
            }
          }
          lastSeqRef.current = seq;

          bufferRef.current.push(frame);
          dirtyRef.current = true;
          updateMetricsFromFrame(frame);
        } else if (msg.type === 'FSM_EVENT' && msg.symbol === sym) {
          latestMetricsRef.current = {
            ...latestMetricsRef.current,
            state: msg.state,
            stateCode: (FSMState as any)[msg.state] ?? latestMetricsRef.current.stateCode,
            lastUpdateEpochMs: Date.now(),
            isStale: false,
          };
        }
      };

      ws.onclose = () => {
        if (!isMounted) return;
        latestMetricsRef.current = {
          ...latestMetricsRef.current,
          isStale: true,
        };
        reconnectTimeout = setTimeout(connect, 2000);
      };

      ws.onerror = () => {
        try {
          ws.close();
        } catch {}
      };
    }

    function updateMetricsFromFrame(frame: AveCompactFrame) {
      // [seq, epoch_ms, price, upper, lower, lambda, supremum, state_code]
      const seq = frame[0];
      const epochMs = frame[1];
      const price = frame[2];
      const upper = frame[3];
      const lower = frame[4];
      const lambdaVal = frame[5];
      const supremum = frame[6];
      const stateCode = frame[7];

      // Derive state name
      const stateName = (FSMState[stateCode] as keyof typeof FSMState) || 'TRACKING';

      // Regime heuristic based on lambda shaping or bounds spread
      let regime: RegimeType = 'CHOP';
      if (lambdaVal > 0.6) regime = 'FAVORABLE';
      else if (lambdaVal < 0.3) regime = 'DEFENSIVE';

      latestMetricsRef.current = {
        symbol: sym,
        state: stateName,
        stateCode,
        price,
        upper,
        lower,
        supremum,
        lambdaFactor: lambdaVal,
        regime,
        lastUpdateEpochMs: epochMs,
        isStale: false,
        seq,
      };
    }

    connect();

    // Throttled 4Hz (every 250ms) state update for React badges and text
    const throttleInterval = setInterval(() => {
      if (!isMounted) return;
      const now = Date.now();
      const ageMs = now - latestMetricsRef.current.lastUpdateEpochMs;
      const isStale = ageMs > 2000 || latestMetricsRef.current.isStale;

      setStatusMetrics((prev) => {
        const next = { ...latestMetricsRef.current, isStale };
        // Shallow comparison to avoid redundant re-renders
        if (
          prev.price === next.price &&
          prev.state === next.state &&
          prev.lambdaFactor === next.lambdaFactor &&
          prev.isStale === next.isStale &&
          prev.seq === next.seq
        ) {
          return prev;
        }
        return next;
      });
    }, 250);

    return () => {
      isMounted = false;
      clearTimeout(reconnectTimeout);
      clearInterval(throttleInterval);
      if (socketRef.current) {
        socketRef.current.close();
      }
    };
  }, [sym, wsUrl, bufferCapacity]);

  return {
    bufferRef,
    dirtyRef,
    statusMetrics,
  };
}
