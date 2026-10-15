/**
 * src/hooks/useTelemetryStream.ts
 * ===============================
 * Real-time WebSocket hook connecting to /ws/telemetry with automatic reconnection (#68).
 * Streams live Yang-Zhang volatility envelope coordinates, macro SES scores, and settlement balances.
 */

import { useEffect, useRef, useState } from 'react';

export interface TelemetryRegime {
  ses: number;
  classification: string;
  bias: string;
}

export interface TelemetryBalances {
  total_nlv: number;
  settled_cash: number;
  unsettled_cash: number;
  gfv_count: number;
}

export interface EnvelopeCoordinate {
  s0: number;
  high_watermark: number;
  trail_stop: number;
  catastrophic_floor: number;
  target: number;
}

export interface TelemetryPayload {
  timestamp: string;
  engine_state: 'STANDBY' | 'RUNNING' | 'FLUSHED' | string;
  regime: TelemetryRegime;
  balances: TelemetryBalances;
  envelopes: Record<string, EnvelopeCoordinate>;
}

export interface UseTelemetryStreamResult {
  telemetry: TelemetryPayload | null;
  isConnected: boolean;
  error: string | null;
  reconnect: () => void;
}

export function useTelemetryStream(customWsUrl?: string): UseTelemetryStreamResult {
  const [telemetry, setTelemetry] = useState<TelemetryPayload | null>(null);
  const [isConnected, setIsConnected] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const socketRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const delayRef = useRef<number>(1000);

  useEffect(() => {
    let isMounted = true;

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const host = window.location.host || 'localhost:8080';
    const wsUrl = customWsUrl || `${protocol}//${host}/ws/telemetry`;

    function connect() {
      if (!isMounted) return;

      if (socketRef.current) {
        try {
          socketRef.current.close();
        } catch {
          // ignore
        }
      }

      try {
        const ws = new WebSocket(wsUrl);
        socketRef.current = ws;

        ws.onopen = () => {
          if (!isMounted) return;
          setIsConnected(true);
          setError(null);
          delayRef.current = 1000;
        };

        ws.onmessage = (event: MessageEvent) => {
          if (!isMounted) return;
          try {
            const data = JSON.parse(event.data);
            if (data.type === 'ping') {
              ws.send(JSON.stringify({ type: 'pong' }));
              return;
            }
            setTelemetry(data);
          } catch (err) {
            console.debug('Failed to parse telemetry message:', err);
          }
        };

        ws.onclose = () => {
          if (!isMounted) return;
          setIsConnected(false);
          scheduleReconnect();
        };

        ws.onerror = () => {
          if (!isMounted) return;
          setError('WebSocket connection error');
          try {
            ws.close();
          } catch {
            // ignore
          }
        };
      } catch (err: unknown) {
        if (!isMounted) return;
        setIsConnected(false);
        setError(err instanceof Error ? err.message : 'WebSocket initialization failed');
        scheduleReconnect();
      }
    }

    function scheduleReconnect() {
      if (!isMounted) return;
      if (reconnectTimeoutRef.current) return;

      reconnectTimeoutRef.current = setTimeout(() => {
        reconnectTimeoutRef.current = null;
        connect();
      }, delayRef.current);

      delayRef.current = Math.min(delayRef.current * 1.5, 10000);
    }

    connect();

    return () => {
      isMounted = false;
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
      }
      if (socketRef.current) {
        try {
          socketRef.current.close();
        } catch {
          // ignore
        }
      }
    };
  }, [customWsUrl]);

  const reconnect = () => {
    if (socketRef.current) {
      try {
        socketRef.current.close();
      } catch {
        // ignore
      }
    }
  };

  return { telemetry, isConnected, error, reconnect };
}
