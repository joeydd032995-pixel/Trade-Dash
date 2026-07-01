import WebSocketAlertPanel from "../components/WebSocketAlertPanel";

/**
 * Phase 1 dashboard page. Per CLAUDE.md's roadmap, `WebSocketAlertPanel`
 * (FR-49) is the only panel in scope this phase -- watchlist, regime,
 * news, calendar, flow, risk, research, scanner, dataset-ops, and briefing
 * panels are Phase 2+ and intentionally not mounted here yet.
 */
export default function DashboardPage() {
  return (
    <main className="mx-auto flex min-h-screen max-w-3xl flex-col gap-6 px-4 py-8">
      <header>
        <h1 className="text-2xl font-bold text-gray-100">Trade-Dash</h1>
        <p className="text-sm text-gray-500">
          Unified Trading Signal Dashboard — Phase 1 (MMP v1.0)
        </p>
      </header>

      <WebSocketAlertPanel />
    </main>
  );
}
