"use client";

/**
 * WebSocketAlertPanel — FR-49, CLAUDE.md §5.
 *
 * Connects to `WS /ws/alerts` on the FastAPI backend (api_service.py's
 * `ws_alerts` route). Each message is exactly one `AlertPayload` JSON
 * object (api_service.py's `AlertPayloadOut`, no envelope) -- see
 * `ConnectionRegistry.broadcast()` / `_drain_to_socket()`.
 *
 * Behavior implemented per spec:
 *   - auto-connect/reconnect with exponential backoff (via `lib/ws.ts`)
 *   - visible connection-status UI (never silently show stale data as live)
 *   - severity badges: low/medium/high/critical (severity gate in
 *     unified_pipeline.py never actually emits "low" today, but the badge
 *     still renders a sane color if it ever does)
 *   - confluence score, signal count, top-4 contributing signals per alert
 *   - dedup incoming alerts against the ENTIRE rolling buffer (same asset +
 *     same confluence_score + same signal_count + same top_contributors
 *     are treated as a repeat regardless of what else interleaved in
 *     between -- see dedup key note below)
 *   - 50-alert rolling buffer, oldest dropped first
 *   - resync via `GET /alerts` on mount and on every reconnect, so alerts
 *     fired while disconnected aren't silently lost from the buffer
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useReconnectingWebSocket, WsStatus } from "../lib/ws";

export interface AlertContributor {
  event_type: string;
  asset: string;
  source: string;
  direction: number; // -1 | 0 | 1
  confidence: number; // 0.0 - 1.0
  weight: number;
  decay: number;
  contribution: number;
}

export interface AlertPayload {
  asset: string;
  confluence_score: number; // -1.0 to 1.0
  signal_count: number;
  severity: string; // "low" | "medium" | "high" | "critical"
  agreement_ratio: number;
  conflict_ratio: number;
  top_contributors: AlertContributor[];
}

/** Locally-tagged alert: the backend payload carries no explicit id, so we
 * assign one client-side purely for React `key`s / rolling-buffer bookkeeping. */
interface DisplayAlert extends AlertPayload {
  _localId: string;
  _receivedAt: number;
}

const MAX_ALERTS = 50;

/**
 * Builds the `/ws/alerts` URL from `NEXT_PUBLIC_API_HOST` (per CLAUDE.md's
 * Quick Start env var). Never hardcodes `localhost` in a way that breaks
 * non-local deployment -- if the env var is unset we fall back to
 * `localhost:8000` only as a local-dev convenience, and that fallback is
 * visibly surfaced (see the "using default host" notice in the panel).
 *
 * Protocol selection: if the configured host itself specifies a scheme
 * (`http://` / `https://`), map it to `ws://` / `wss://` respectively.
 * If no scheme is given, assume plain `ws://` (typical for local dev /
 * same-origin http deployments); an operator serving the dashboard over
 * https should set `NEXT_PUBLIC_API_HOST=https://host` explicitly so this
 * resolves to `wss://`.
 */
export function buildAlertsWsUrl(apiHost: string | undefined): { url: string; usedDefault: boolean } {
  const usedDefault = !apiHost;
  const host = apiHost || "localhost:8000";

  let scheme = "ws";
  let rest = host;

  if (/^https:\/\//i.test(host)) {
    scheme = "wss";
    rest = host.replace(/^https:\/\//i, "");
  } else if (/^http:\/\//i.test(host)) {
    scheme = "ws";
    rest = host.replace(/^http:\/\//i, "");
  } else if (/^wss:\/\//i.test(host)) {
    scheme = "wss";
    rest = host.replace(/^wss:\/\//i, "");
  } else if (/^ws:\/\//i.test(host)) {
    scheme = "ws";
    rest = host.replace(/^ws:\/\//i, "");
  }

  rest = rest.replace(/\/+$/, "");

  return { url: `${scheme}://${rest}/ws/alerts`, usedDefault };
}

/** Same host-resolution logic as `buildAlertsWsUrl`, but for the HTTP(S)
 * `GET /alerts` endpoint -- used to seed/reconcile the buffer on mount and
 * on every reconnect, since the WS stream alone can silently miss alerts
 * fired while the socket was down (see the resync effect below). */
export function buildAlertsHttpUrl(apiHost: string | undefined): { url: string; usedDefault: boolean } {
  const usedDefault = !apiHost;
  const host = apiHost || "localhost:8000";

  let scheme = "http";
  let rest = host;

  if (/^https:\/\//i.test(host)) {
    scheme = "https";
    rest = host.replace(/^https:\/\//i, "");
  } else if (/^http:\/\//i.test(host)) {
    scheme = "http";
    rest = host.replace(/^http:\/\//i, "");
  } else if (/^wss:\/\//i.test(host)) {
    scheme = "https";
    rest = host.replace(/^wss:\/\//i, "");
  } else if (/^ws:\/\//i.test(host)) {
    scheme = "http";
    rest = host.replace(/^ws:\/\//i, "");
  }

  rest = rest.replace(/\/+$/, "");

  return { url: `${scheme}://${rest}/alerts`, usedDefault };
}

/** Dedup key for an incoming payload. The backend assigns no alert id, so
 * we build a reasonable composite key from fields that identify "the same
 * alert" for a given asset at a given moment: asset + severity +
 * confluence_score + signal_count + the ordered list of contributing
 * event_type/source pairs. Any buffer entry with the same key (searched
 * across the ENTIRE rolling buffer, not just the most recent entry --
 * an interleaved alert for a different asset must not defeat dedup) is
 * treated as a repeat of the same alert and only its timestamp is
 * refreshed (and it's bumped to the front), rather than growing the
 * buffer with a visual duplicate. */
function dedupKey(payload: AlertPayload): string {
  const contributorsKey = payload.top_contributors
    .map((c) => `${c.event_type}:${c.source}:${c.direction}`)
    .join("|");
  return [
    payload.asset,
    payload.severity,
    payload.confluence_score.toFixed(4),
    payload.signal_count,
    contributorsKey,
  ].join("::");
}

function severityBadgeClasses(severity: string): string {
  const normalized = severity.toLowerCase();
  switch (normalized) {
    case "critical":
      return "bg-red-700 text-red-50 border border-red-400";
    case "high":
      return "bg-orange-600 text-orange-50 border border-orange-300";
    case "medium":
      return "bg-yellow-500 text-yellow-950 border border-yellow-300";
    case "low":
      return "bg-blue-600 text-blue-50 border border-blue-300";
    default:
      return "bg-gray-600 text-gray-50 border border-gray-300";
  }
}

function connectionStatusClasses(status: WsStatus): string {
  switch (status) {
    case "open":
      return "bg-green-600 text-green-50";
    case "connecting":
      return "bg-yellow-500 text-yellow-950";
    case "error":
      return "bg-red-700 text-red-50";
    case "closed":
    default:
      return "bg-gray-600 text-gray-50";
  }
}

function connectionStatusLabel(status: WsStatus): string {
  switch (status) {
    case "open":
      return "Connected";
    case "connecting":
      return "Connecting...";
    case "error":
      return "Connection error";
    case "closed":
    default:
      return "Disconnected";
  }
}

function directionLabel(direction: number): string {
  if (direction > 0) return "bullish";
  if (direction < 0) return "bearish";
  return "neutral";
}

function directionClasses(direction: number): string {
  if (direction > 0) return "text-green-400";
  if (direction < 0) return "text-red-400";
  return "text-gray-400";
}

function confluenceScoreClasses(score: number): string {
  if (score > 0) return "text-green-400";
  if (score < 0) return "text-red-400";
  return "text-gray-300";
}

let localIdCounter = 0;
function nextLocalId(): string {
  localIdCounter += 1;
  return `alert-${Date.now()}-${localIdCounter}`;
}

/** Validates and normalizes an unknown payload (from either the WS stream
 * or a `GET /alerts` response entry) into an `AlertPayload`, or returns
 * `null` if it doesn't match the expected shape. Shared by both ingestion
 * paths so they can never drift from each other. */
function normalizeAlertPayload(data: unknown): AlertPayload | null {
  if (!data || typeof data !== "object") return null;
  const payload = data as Partial<AlertPayload>;

  if (
    typeof payload.asset !== "string" ||
    typeof payload.confluence_score !== "number" ||
    typeof payload.signal_count !== "number" ||
    typeof payload.severity !== "string" ||
    !Array.isArray(payload.top_contributors)
  ) {
    return null;
  }

  return {
    asset: payload.asset,
    confluence_score: payload.confluence_score,
    signal_count: payload.signal_count,
    severity: payload.severity,
    agreement_ratio: typeof payload.agreement_ratio === "number" ? payload.agreement_ratio : 0,
    conflict_ratio: typeof payload.conflict_ratio === "number" ? payload.conflict_ratio : 0,
    top_contributors: payload.top_contributors.slice(0, 4) as AlertContributor[],
  };
}

export default function WebSocketAlertPanel() {
  const [alerts, setAlerts] = useState<DisplayAlert[]>([]);

  const { url } = useMemo(
    () => buildAlertsWsUrl(process.env.NEXT_PUBLIC_API_HOST),
    []
  );
  const { url: httpAlertsUrl } = useMemo(
    () => buildAlertsHttpUrl(process.env.NEXT_PUBLIC_API_HOST),
    []
  );

  /** Merge one normalized payload into the rolling buffer: dedup against
   * the ENTIRE buffer (not just the most recent entry -- an alert for a
   * different asset interleaved in between must not defeat dedup for an
   * unchanged repeat), bump a matched entry to the front with a refreshed
   * timestamp, otherwise insert as new and trim to MAX_ALERTS. */
  const mergeAlert = useCallback((payload: AlertPayload) => {
    const key = dedupKey(payload);
    setAlerts((prev) => {
      const existingIdx = prev.findIndex((a) => dedupKey(a) === key);
      if (existingIdx !== -1) {
        const refreshed: DisplayAlert = { ...prev[existingIdx], _receivedAt: Date.now() };
        const withoutExisting = prev.filter((_, i) => i !== existingIdx);
        return [refreshed, ...withoutExisting];
      }

      const withNew: DisplayAlert[] = [
        { ...payload, _localId: nextLocalId(), _receivedAt: Date.now() },
        ...prev,
      ];

      // 50-alert rolling buffer: drop oldest once it exceeds MAX_ALERTS.
      if (withNew.length > MAX_ALERTS) {
        return withNew.slice(0, MAX_ALERTS);
      }
      return withNew;
    });
  }, []);

  const handleMessage = useCallback(
    (data: unknown) => {
      const normalized = normalizeAlertPayload(data);
      if (!normalized) {
        // eslint-disable-next-line no-console
        console.error("WebSocketAlertPanel: received malformed alert payload", data);
        return;
      }
      mergeAlert(normalized);
    },
    [mergeAlert]
  );

  const { status } = useReconnectingWebSocket({
    url,
    onMessage: handleMessage,
  });

  /** Resync: `GET /alerts` returns every currently-active alert, most
   * urgent first (api_service.py's `get_alerts` route). The WS stream is
   * broadcast-only with no server-side per-client backlog beyond a small
   * bounded queue, so any alert fired while this client was disconnected
   * (initial mount, or a drop/reconnect) is otherwise gone forever with no
   * indication anything was missed. Fetching current state here and
   * merging it through the same dedup path closes that gap: on
   * (re)connect the panel is reconciled with whatever is still active,
   * even if the live broadcast announcing it was missed. */
  const fetchActiveAlerts = useCallback(async () => {
    try {
      const res = await fetch(httpAlertsUrl, { cache: "no-store" });
      if (!res.ok) return;
      const body = (await res.json()) as { alerts?: unknown[] };
      if (!Array.isArray(body.alerts)) return;
      for (const raw of body.alerts) {
        const normalized = normalizeAlertPayload(raw);
        if (normalized) mergeAlert(normalized);
      }
    } catch (err) {
      // eslint-disable-next-line no-console
      console.error("WebSocketAlertPanel: GET /alerts resync failed", err);
    }
  }, [httpAlertsUrl, mergeAlert]);

  useEffect(() => {
    void fetchActiveAlerts();
  }, [fetchActiveAlerts]);

  const prevStatusRef = useRef<WsStatus>(status);
  useEffect(() => {
    if (status === "open" && prevStatusRef.current !== "open") {
      void fetchActiveAlerts();
    }
    prevStatusRef.current = status;
  }, [status, fetchActiveAlerts]);

  const usedDefaultHost = !process.env.NEXT_PUBLIC_API_HOST;

  return (
    <section className="flex flex-col gap-3 rounded-lg border border-gray-800 bg-gray-950 p-4">
      <header className="flex items-center justify-between gap-2">
        <h2 className="text-lg font-semibold text-gray-100">Live Alerts</h2>
        <div className="flex items-center gap-2">
          <span className="text-xs text-gray-500">{alerts.length}/{MAX_ALERTS}</span>
          <span
            className={`rounded-full px-2 py-1 text-xs font-medium ${connectionStatusClasses(status)}`}
            data-testid="ws-connection-status"
          >
            {connectionStatusLabel(status)}
          </span>
        </div>
      </header>

      {usedDefaultHost && (
        <p className="rounded border border-yellow-700 bg-yellow-950/40 px-2 py-1 text-xs text-yellow-400">
          NEXT_PUBLIC_API_HOST is not set; defaulting to localhost:8000. Set it for non-local deployments.
        </p>
      )}

      {status !== "open" && (
        <p className="rounded border border-gray-700 bg-gray-900 px-2 py-1 text-xs text-gray-400">
          {status === "connecting" && "Connecting to alert stream..."}
          {status === "error" && "Connection error -- retrying with backoff."}
          {status === "closed" && "Disconnected from alert stream -- retrying with backoff."}
        </p>
      )}

      {alerts.length === 0 ? (
        <p className="py-8 text-center text-sm text-gray-500">
          {status === "open"
            ? "Connected. Waiting for the first alert..."
            : "No alerts yet."}
        </p>
      ) : (
        <ul className="flex flex-col gap-2" data-testid="alert-list">
          {alerts.map((alert) => (
            <li
              key={alert._localId}
              className="rounded-md border border-gray-800 bg-gray-900 p-3"
              data-testid="alert-item"
            >
              <div className="flex items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                  <span className="font-mono text-base font-bold text-gray-100">
                    {alert.asset}
                  </span>
                  <span
                    className={`rounded px-2 py-0.5 text-xs font-semibold uppercase tracking-wide ${severityBadgeClasses(alert.severity)}`}
                  >
                    {alert.severity}
                  </span>
                </div>
                <span className="text-xs text-gray-500">
                  {new Date(alert._receivedAt).toLocaleTimeString()}
                </span>
              </div>

              <div className="mt-2 flex flex-wrap items-center gap-4 text-sm">
                <span>
                  Confluence:{" "}
                  <span className={`font-semibold ${confluenceScoreClasses(alert.confluence_score)}`}>
                    {alert.confluence_score.toFixed(3)}
                  </span>
                </span>
                <span className="text-gray-300">
                  Signals: <span className="font-semibold">{alert.signal_count}</span>
                </span>
                <span className="text-gray-400">
                  Agreement: {(alert.agreement_ratio * 100).toFixed(0)}%
                </span>
                <span className="text-gray-400">
                  Conflict: {(alert.conflict_ratio * 100).toFixed(0)}%
                </span>
              </div>

              {alert.top_contributors.length > 0 && (
                <div className="mt-2 border-t border-gray-800 pt-2">
                  <p className="mb-1 text-xs uppercase tracking-wide text-gray-500">
                    Top contributors
                  </p>
                  <ul className="flex flex-col gap-1">
                    {alert.top_contributors.slice(0, 4).map((c, idx) => (
                      <li
                        key={`${alert._localId}-contrib-${idx}`}
                        className="flex flex-wrap items-center gap-2 text-xs text-gray-300"
                      >
                        <span className="font-medium text-gray-200">{c.event_type}</span>
                        <span className="text-gray-500">via {c.source}</span>
                        <span className={directionClasses(c.direction)}>
                          {directionLabel(c.direction)}
                        </span>
                        <span className="text-gray-500">
                          conf {c.confidence.toFixed(2)} &middot; w {c.weight.toFixed(2)} &middot; decay{" "}
                          {c.decay.toFixed(2)}
                        </span>
                        <span className="text-gray-400">
                          contrib {c.contribution.toFixed(3)}
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
