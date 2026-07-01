---
name: frontend-dashboard
description: Reference knowledge for Trade-Dash's Next.js/React dashboard — WebSocketAlertPanel, ResearchWorkspace, TechnicalScannerPanel, DatasetOpsPanel, BriefingPanel, watchlist/regime/news/calendar/flow/risk panels, and the Tauri 2 desktop shell scope.
when_to_use: building or fixing a dashboard panel, wiring WebSocket reconnect logic, scoping what ships in the Tauri desktop build
---

Trade-Dash's frontend lives under `components/` (see CLAUDE.md §2
FR-49..59/63-64, §5, §6, §8 OQ-13).

## Stack
Next.js (App Router) + React + TypeScript, TailwindCSS, TradingView
Widgets/Lightweight Charts, AG Grid, Plotly.js, globe.gl/deck.gl/MapLibre GL
(macro geo context), Transformers.js (only if client-side inference is
needed).

## Panel priorities
`WebSocketAlertPanel.tsx` (FR-49, Phase 1) is built first: auto-reconnect
with backoff, severity badges, confluence score, signal count, top-4
contributors, dedup by alert ID, 50-alert rolling buffer (drop oldest).
Reads backend host from `NEXT_PUBLIC_API_HOST` — never hardcode
`localhost`. Later panels (`ResearchWorkspace.tsx`, `TechnicalScannerPanel.tsx`,
`DatasetOpsPanel.tsx`, `BriefingPanel.tsx`) each pair with a backend module
— confirm response shapes with the owning domain before building.

## WebSocket discipline (every WS-consuming panel)
Exponential backoff on disconnect, resubscribe/resync on reconnect (assume
no carried-over state), visible connection-status UI.

## Tauri 2 scope (OQ-13: alert + chart subset only — resolved, don't
re-litigate)
Desktop ships **only**: WebSocketAlertPanel, watchlist, regime, risk.
Research/briefing/dataset-ops panels stay web-only.

## Every panel needs
Loading state, empty state, error/disconnected state — not just the happy
path.

## Delegate
- Deep implementation work → `frontend-dashboard-expert` agent.
- Pre-merge review → `frontend-dashboard-reviewer` agent.
