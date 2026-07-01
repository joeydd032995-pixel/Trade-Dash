"use client";

/**
 * Reusable WebSocket-with-reconnect hook, shared across every WS-consuming
 * dashboard panel (WebSocketAlertPanel today; watchlist/regime/flow/etc.
 * panels in later phases per CLAUDE.md §5).
 *
 * Discipline enforced here (per CLAUDE.md's "WebSocket reconnect
 * discipline" section):
 *   - exponential backoff on disconnect (capped, with jitter)
 *   - no assumption that any client-side state survived a reconnect --
 *     callers are expected to treat a fresh `open` as a resync point
 *   - an explicit, always-visible connection status ("connecting" |
 *     "open" | "closed" | "error") so a caller can never silently render
 *     stale data as if it were live
 */

import { useCallback, useEffect, useRef, useState } from "react";

export type WsStatus = "connecting" | "open" | "closed" | "error";

export interface UseReconnectingWebSocketOptions {
  /** Full ws(s):// URL to connect to. Pass `null`/`undefined` to disable. */
  url: string | null | undefined;
  /** Called once per parsed message. Parse errors are swallowed (and logged) so one bad payload can't kill the connection. */
  onMessage: (data: unknown) => void;
  /** Base backoff delay in ms. Default 1000. */
  baseDelayMs?: number;
  /** Max backoff delay in ms (cap). Default 30000. */
  maxDelayMs?: number;
}

export interface UseReconnectingWebSocketResult {
  status: WsStatus;
  /** Number of consecutive reconnect attempts since the last successful open. */
  attempt: number;
}

/**
 * Exponential backoff with jitter: base * 2^attempt, capped, +/- up to 20%
 * jitter so many clients reconnecting after a shared outage don't all
 * retry in lockstep.
 */
function computeBackoffMs(attempt: number, baseDelayMs: number, maxDelayMs: number): number {
  const raw = baseDelayMs * Math.pow(2, attempt);
  const capped = Math.min(raw, maxDelayMs);
  const jitterFactor = 0.8 + Math.random() * 0.4; // 0.8x - 1.2x
  return Math.round(capped * jitterFactor);
}

export function useReconnectingWebSocket(
  options: UseReconnectingWebSocketOptions
): UseReconnectingWebSocketResult {
  const { url, onMessage, baseDelayMs = 1000, maxDelayMs = 30000 } = options;

  const [status, setStatus] = useState<WsStatus>("connecting");
  const [attempt, setAttempt] = useState(0);

  // Keep the latest onMessage in a ref so effect below doesn't need to
  // depend on (and reconnect for) a caller-provided closure identity change.
  const onMessageRef = useRef(onMessage);
  onMessageRef.current = onMessage;

  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const attemptRef = useRef(0);
  const unmountedRef = useRef(false);

  const clearReconnectTimer = useCallback(() => {
    if (reconnectTimerRef.current !== null) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
  }, []);

  useEffect(() => {
    unmountedRef.current = false;

    if (!url) {
      setStatus("closed");
      return () => {
        unmountedRef.current = true;
      };
    }

    function connect() {
      if (unmountedRef.current || !url) return;

      setStatus("connecting");
      let socket: WebSocket;
      try {
        socket = new WebSocket(url);
      } catch {
        setStatus("error");
        scheduleReconnect();
        return;
      }
      wsRef.current = socket;

      socket.onopen = () => {
        if (unmountedRef.current) return;
        attemptRef.current = 0;
        setAttempt(0);
        setStatus("open");
      };

      socket.onmessage = (event: MessageEvent) => {
        if (unmountedRef.current) return;
        try {
          const parsed = JSON.parse(event.data);
          onMessageRef.current(parsed);
        } catch (err) {
          // eslint-disable-next-line no-console
          console.error("useReconnectingWebSocket: failed to parse message", err);
        }
      };

      socket.onerror = () => {
        if (unmountedRef.current) return;
        setStatus("error");
      };

      socket.onclose = () => {
        if (unmountedRef.current) return;
        setStatus("closed");
        wsRef.current = null;
        scheduleReconnect();
      };
    }

    function scheduleReconnect() {
      if (unmountedRef.current) return;
      clearReconnectTimer();
      const currentAttempt = attemptRef.current;
      const delay = computeBackoffMs(currentAttempt, baseDelayMs, maxDelayMs);
      attemptRef.current = currentAttempt + 1;
      setAttempt(attemptRef.current);
      reconnectTimerRef.current = setTimeout(connect, delay);
    }

    connect();

    return () => {
      unmountedRef.current = true;
      clearReconnectTimer();
      const socket = wsRef.current;
      wsRef.current = null;
      if (socket) {
        // Prevent the onclose handler from scheduling a reconnect after
        // this hook instance has already torn down.
        socket.onopen = null;
        socket.onmessage = null;
        socket.onerror = null;
        socket.onclose = null;
        socket.close();
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [url, baseDelayMs, maxDelayMs]);

  return { status, attempt };
}
