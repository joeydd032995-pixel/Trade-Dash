# CLAUDE.md

This file gives Claude Code (and any other agent working in this repo) the
context needed to work productively on **Trade-Dash**. It consolidates the
project's planning documents (overview, PRD, MMP, key decisions, code
reference, libraries, TODOs/open questions, and design notes).

---

## 1. Project Overview

**Name:** Unified Trading Signal Dashboard — News + Signal Correlation & Research Engine

**One-line description:** A full-stack, real-time trading intelligence
platform that ingests news, options flow, whale transfers, on-chain metrics,
macro events, political trades, and technical signals; correlates them
per-asset using a weighted confluence formula; delivers scored alerts to a
live dashboard; and provides an agentic research workspace, ML forecasting
lab, distributed backtesting harness, and bounded broker execution layer for
stocks and crypto.

**Target user:** Quantitative developer / independent trader building a
self-hosted, API-driven trading intelligence and research system.

### Goals
1. Aggregate every meaningful signal type (stocks + crypto) into one canonical event stream.
2. Bidirectionally correlate news articles with statistical signals (Greeks, strategy levels, funding, on-chain flows).
3. Score each asset with a normalized confluence score `C` in `[-1, 1]`.
4. Fire multi-signal alerts to Slack, Telegram, Discord, and a WebSocket panel.
5. Provide a backtesting harness that validates strategy expectancy before live deployment.
6. Provide a Greeks/options income panel covering Delta, Gamma, Theta, Vega, Rho, IV Rank, GEX, and liquidation heatmaps.
7. Maintain a unified trade journal keyed to every signal for post-trade review.
8. Provide an agentic research workspace with persistent memory, research goals, hypothesis tracking, evidence capture, and multi-agent swarm presets.
9. Provide an algorithm-ready dataset factory with Redis + S3/Minio archival and offline replay for AI training pipelines.
10. Provide a reusable TA engine covering RSI, MACD, Ichimoku, SMA, EMA, MFI, OBV, VWAP, and Momentum for both batch scans and streaming alerts.
11. Provide an ML forecasting lab with LSTM, RNN, ARIMA, and classical regression baselines fused with sentiment features.
12. Provide strategy export to Pine Script and MQL5 for external execution.
13. Provide intelligence-ops briefing synthesis, cross-stream macro correlation, and optional Tauri 2 desktop packaging for always-on monitoring.

### Scope
- **Asset classes:** US equities, crypto spot/perps, forex, futures (v2).
- **Execution:** Paper first; live connectors are opt-in, bounded by mandates.
- **Data sources:** Polygon, Finnhub, FMP, CoinGlass, Glassnode, Nansen, Arkham, Deribit, Tradier, SEC EDGAR, CCXT, AKShare, Tushare, IEX Cloud, FinViz, CoinGecko, Lookonchain, Whale Alert, RSS feeds.
- **Agent runtimes:** Ollama (local), OpenRouter, Groq, MCP-compatible servers.

### Architecture Layers
| Layer | Responsibility |
|---|---|
| Ingest | RSS pollers, API pollers (Finnhub/Polygon/CoinGlass/Arkham), WebSocket feeds, broker export importers |
| NLP | Entity extraction, sentiment, topic classification, urgency scoring, dedup (lexicon dev → finbert/spaCy prod) |
| TA Engine | RSI, MACD, Ichimoku, SMA, EMA, MFI, OBV, VWAP, Momentum — batch + streaming |
| ML Lab | Preprocessing, sentiment-feature fusion, LSTM/RNN/ARIMA/sklearn forecasting, eval notebooks |
| Signal Factory | Typed event constructors for all 10 non-news signal categories |
| Correlation | Weighted, time-decayed, directional confluence scorer |
| Agent Layer | Multi-agent research swarm: goals, evidence, hypotheses, persistent memory, run cards, MCP tool bindings |
| Dataset Factory | Algorithm-ready dataset compilation, Redis/S3/Minio publish |
| Storage | PostgreSQL/TimescaleDB + pgvector, Redis, S3/Minio |
| API | FastAPI REST + WebSocket |
| Frontend | Next.js/React, TailwindCSS, TradingView widgets, AG Grid, Plotly, globe.gl, deck.gl, MapLibre GL, optional Tauri 2 desktop |
| Alerts | Slack, Telegram, Discord, email, SMS (Twilio), WebSocket |
| Export | Pine Script exporter, MQL5 exporter |

### Source Repositories Analyzed (pattern references)
- `PlaceNL/self-algorithmic-trading` — algo-trading structural patterns
- `reuniware/CryptoForex-Trader-Framework` — Python + MQL5 crypto/forex hybrid
- `CyberPunk/currency-news-analysis` — news NLP for currency signals
- `CryptoSignal/Crypto-Signal` — TA indicator alerts + multi-channel delivery
- `scorpionhiccup/StockPricePrediction` — LSTM/RNN/ARIMA price forecasting
- `AlgoTraders/stock-analysis-engine` — distributed backtesting, IEX data, S3/Redis
- `HKUDS/Vibe-Trading` — agentic research workspace + multi-market
- `koala73/worldmonitor` — intelligence-ops briefing, globe/geo UI

---

## 2. Product Requirements (PRD)

### Problem Statement
Traders operating across stocks and crypto must monitor news, options flow,
on-chain data, derivatives positioning, political disclosures, and technical
signals from dozens of disparate, unintegrated sources. Signal noise is high,
context is fragmented, and no system automatically asks: *Does the
statistical data confirm this news? Is this anomaly explained by any public
catalyst? What does the research history say about this setup?* Existing
tools handle one layer (chart, screener, on-chain, or agent) but none unify
all layers under a single scored, auditable, agentic intelligence system.

### Objectives
1. Centralize all signal types under one normalized event schema.
2. Eliminate manual cross-referencing of news vs. market data.
3. Score asset-level conviction using multi-source confluence.
4. Surface unexplained statistical anomalies (dark signal queue) as highest priority.
5. Provide a paper-tradeable backtesting loop before any capital is committed.
6. Provide an agentic research workspace that builds structured knowledge over time, not just ephemeral chat.
7. Support portable strategy export and bounded live execution.

### Functional Requirements

**Ingestion**
- FR-01 Ingest raw news articles from Finnhub, RSS feeds, WS news APIs.
- FR-02 Ingest live signal events: `greek_anomaly`, `options_flow`, `strategy_break`, `on_chain_move`, `funding_extreme`, `whale_transfer`, `macro_event`, `political_trade`, `insider_trade`.
- FR-03 Deduplicate articles by MD5 fingerprint and cosine similarity.
- FR-04 Extract tickers, asset class, sentiment, topic, urgency per article.
- FR-05 Monitor feed freshness; mark sources stale after configurable TTL.
- FR-06 Support broker export import for journal and shadow-account workflows.

**Correlation Engine**
- FR-07 Compute per-asset confluence score `C` using the weighted, time-decayed, directional formula: `C = Σ(w·s·d·δ) / Σ|w·s·δ|`, output in `[-1, 1]`.
- FR-08 Support configurable signal weights and half-life decay per instance.
- FR-09 Return full per-signal audit trail (weight, decay, contribution) with every score.
- FR-10 Expose `agreement_ratio` and `conflict_ratio` per asset.
- FR-11 Flag assets with high-magnitude statistical signals and zero news coverage as "dark signals" in a dedicated queue.

**TA Engine**
- FR-12 Expose reusable indicator modules: RSI, MACD, Ichimoku Cloud, SMA, EMA, MFI, OBV, VWAP, Momentum for batch and real-time use.
- FR-13 Emit TA-native alerts to Slack, Telegram, Discord, email, and SMS.
- FR-14 Support configurable indicator parameters and multi-asset scan profiles.

**ML Forecasting Lab**
- FR-15 Support supervised forecasting with LSTM, RNN, ARIMA, and sklearn regression baselines.
- FR-16 Support sentiment-feature fusion: news and social signals as input features alongside OHLCV.
- FR-17 Provide preprocessing, normalization, and model evaluation utilities.
- FR-18 Expose model predictions as typed signal events into the correlation engine.

**Alerts**
- FR-19 Fire alerts when `|C| >= threshold` (default 0.65) and `signal_count >= min_signals` (default 3).
- FR-20 Route high/critical alerts to Slack, Telegram, Discord, email, SMS.
- FR-21 Broadcast all alerts over WebSocket to connected dashboard clients.
- FR-22 Persist alerts to PostgreSQL with severity, delivery channel, and acknowledgement tracking.

**Greeks & Options Panel**
- FR-23 Display Delta, Gamma, Theta, Vega, Rho per option position.
- FR-24 Show IV Rank and IV Percentile per ticker.
- FR-25 Show portfolio net Greeks aggregated across all positions.
- FR-26 Display GEX levels for stock options.
- FR-27 Display liquidation heatmap for crypto perps (CoinGlass).

**Backtesting Harness**
- FR-28 Replay historical signals from the `events` table chronologically.
- FR-29 Tag every simulated trade by setup, regime, catalyst, venue.
- FR-30 Compute `E = (W × R_win) − (L × R_loss)` per setup × regime combination.
- FR-31 Walk-forward validate with rolling train/test windows (60-day / 20-day).
- FR-32 Gate live deployment on: `trades≥30`, `Sharpe≥1.0`, `maxDD≤15%`, `WF≥0.7`.
- FR-33 Generate minute-level algorithm-ready datasets; publish to Redis/S3/Minio.
- FR-34 Support distributed backtesting workers via Celery and Kubernetes jobs.

**Agentic Research Workspace**
- FR-35 Provide CLI, Web, and MCP-server research interfaces.
- FR-36 Support structured research goals, evidence rows, hypothesis registry, and persistent memory across sessions.
- FR-37 Provide multi-agent swarm presets: quant, macro, crypto, risk teams.
- FR-38 Emit reproducible run cards with benchmark context and validation output.
- FR-39 Support multi-market data loaders: A-share, HK, US, crypto, futures, forex, options via CCXT, AKShare, Tushare, yfinance, IEX, Tradier.

**Trade Journal & Shadow Account**
- FR-40 Ingest broker exports; compute behavioral diagnostics.
- FR-41 Infer recurring entry/exit rules from journal history.
- FR-42 Compare real trades against a rules-based shadow strategy.
- FR-43 Produce personalized diagnostics reports.

**Strategy Export**
- FR-44 Export compatible strategies or rule sets to TradingView Pine Script.
- FR-45 Export compatible strategies to MetaTrader 5 / MQL5.

**Intelligence Briefing**
- FR-46 Generate AI-synthesized briefs with source attribution.
- FR-47 Track source freshness by feed group; enforce freshness gates.
- FR-48 Surface cross-stream correlations across finance, geopolitics, and macro signals relevant to position context.

**Dashboard Panels**
- FR-49 WebSocket alert panel: severity, confluence, signal count, contributors.
- FR-50 Watchlist movers with relative volume and catalyst tags.
- FR-51 Market regime panel: index trend, breadth, VIX, rates, DXY, BTC dom.
- FR-52 News panel filtered by ticker, sector, macro theme, crypto narrative.
- FR-53 Calendar panel: earnings, macro releases, token unlocks, governance.
- FR-54 Flow panel: options sweeps, dark pool, whale transfers, OI/funding.
- FR-55 Risk/execution panel: open positions, heat, correlations, max daily loss.
- FR-56 Research workspace panel: goals, evidence, hypotheses, run cards.
- FR-57 TA scanner panel: indicator triggers, alert routing per symbol.
- FR-58 Dataset ops panel: extraction, replay, archive, restore controls.
- FR-59 Briefing panel: AI briefs, freshness status, cross-stream correlation.

**Live Trading Connector**
- FR-60 Connector profiles support paper/live modes, bounded mandates, read-only.
- FR-61 Audit ledger records all connector actions.
- FR-62 Instant halt mechanism interruptible at any time.

**Desktop / Always-On**
- FR-63 Optional Tauri 2 desktop shell for macOS, Windows, Linux.
- FR-64 Service-worker caching for offline and low-latency operation.

### Non-Functional Requirements
- NFR-01 Ingest → WebSocket alert latency < 5 seconds.
- NFR-02 Support ≥ 10 concurrent WebSocket clients without dropped messages.
- NFR-03 All raw payloads stored for audit and replay; never mutate raw data.
- NFR-04 Hard risk limits enforced outside signal layer; dashboard never executes orders directly.
- NFR-05 All API keys in environment variables; never hardcoded.
- NFR-06 Feed health keys auto-expire after 5 minutes.
- NFR-07 Every major backtest emits a structured run card artifact.
- NFR-08 Distributed workers support Kubernetes, docker-compose, and S3 publishing for large dataset and training jobs.
- NFR-09 Live connectors: opt-in only, mandate-bounded, audit-logged, halt-interruptible.
- NFR-10 Reproducibility: same inputs + same config = same run card output.

---

## 3. Minimum Marketable Product (MMP) & Roadmap

The MMP is the smallest deliverable that provides real, daily value to a
quant developer running a live trading operation.

### Phase 1 — Core Signal Loop (MMP v1.0) — **not started**
- [ ] Canonical `Event` schema (asset, asset_class, event_type, direction, confidence, timestamp, source, meta)
- [ ] NLP Pipeline: ticker extraction, sentiment, topic, urgency, dedup
- [ ] `CorrelationScorer`: weighted, time-decayed, directional confluence formula
- [ ] `SignalFactory`: all 10 signal type constructors
- [ ] `UnifiedPipeline`: `ingest_articles()`, `ingest_signal_events()`, `correlate()`, `alert_payloads()`
- [ ] FastAPI: `POST /ingest/articles`, `POST /ingest/signals`, `GET /correlate/{asset}`, `GET /correlate`, `GET /alerts`, `GET /feed-health`, `POST /demo/seed`, `WS /ws/alerts`
- [ ] Redis event bus: `publish_event`, `subscribe_alerts`, heartbeat, feed health
- [ ] `FinnhubNewsPoller`, `CoinGlassFundingPoller`, `PolygonOptionsPoller`
- [ ] `AlertRouter`: Slack + Telegram handlers
- [ ] `WebSocketAlertPanel.tsx`: live alert stream UI component
- [ ] PostgreSQL schema: 9 tables, full indexes, TimescaleDB + pgvector ready

**Status:** The repository is empty — nothing in Phase 1 (or any later phase) has
been implemented yet. The items above and the TODOs in § 7 are the build
order for getting MMP v1.0 to a working state from scratch: scaffold the
canonical `Event` schema and pipeline classes, stand up Postgres/TimescaleDB/pgvector
+ Redis, wire real API keys, run migrations, build the FastAPI service and
Redis event bus, build the pollers and alert router, build the WebSocket panel
and mount it in the dashboard, then run a full E2E integration test and
implement `freshness_guard.py`.

### Phase 2 — TA Engine + Dataset Factory (v1.1)
- [ ] `ta_engine.py`: RSI, MACD, Ichimoku, SMA, EMA, MFI, OBV, VWAP, Momentum
- [ ] TA alert delivery: Slack, Telegram, Discord, email, SMS (Twilio)
- [ ] `dataset_factory.py`: minute + daily + options + news datasets
- [ ] Redis + S3/Minio dataset publish and replay
- [ ] Greeks panel: Pydantic models, Tradier + Deribit pollers, options tracker
- [ ] Backtesting harness: vectorbt integration + walk-forward validator
- [ ] Full Next.js dashboard layout: watchlist, regime, news, calendar, flow, risk panels
- [ ] Semantic dedup via sentence-transformers + pgvector
- [ ] Congressional/political trade poller (House/Senate STOCK Act)
- [ ] Insider Form 4 poller (SEC EDGAR)
- [ ] CVD + order book heatmap panel
- [ ] GEX level overlay
- [ ] Liquidation heatmap widget
- [ ] Dark signal queue UI panel

### Phase 3 — Agentic Research + ML Lab (v1.2)
- [ ] `agent_orchestrator.py`: multi-agent swarm with goals, evidence, hypotheses, persistent memory, run cards, MCP tool bindings
- [ ] `ResearchWorkspace.tsx`: goal-driven sessions, evidence cards, hypothesis views, artifact links
- [ ] Multi-market data loaders: CCXT, AKShare, Tushare, yfinance, IEX, Tradier
- [ ] `forecast_lab.py`: preprocessing, sentiment-feature fusion, LSTM/RNN/ARIMA/sklearn evaluation
- [ ] Trade journal import, shadow-account comparison, rule extraction
- [ ] Distributed backtesting workers (Celery + Kubernetes)
- [ ] `TechnicalScannerPanel.tsx`
- [ ] `DatasetOpsPanel.tsx`
- [ ] Expectancy heatmap by setup × regime

### Phase 4 — Intelligence Ops + Export (v2.0)
- [ ] `intelligence_brief_service.py` + `freshness_guard.py`
- [ ] `BriefingPanel.tsx`: AI briefs, freshness status, cross-stream correlation
- [ ] Pine Script exporter
- [ ] MQL5 exporter
- [ ] Regime classifier: ADX + BB width + ATR z-score automated labeling
- [ ] Paper trading sandbox tied to live signal pipeline
- [ ] Mobile command layer: emergency close + priority alerts + lite chart
- [ ] Multi-condition alert builder UI
- [ ] Stablecoin issuance/redemption tracker
- [ ] Social sentiment anomaly detection (Santiment / LunarCrush)
- [ ] globe.gl / deck.gl / MapLibre GL macro geo-visualization
- [ ] Tauri 2 desktop shell + service-worker caching

### Phase 5 — Bounded Live Execution (v2.1)
- [ ] `connector_profiles.py`: paper/live modes, mandates, audit ledger, halt
- [ ] IBKR API, Alpaca, Bybit, OKX live connector adapters
- [ ] Post-trade review loop: journal → scorer → expectancy update

### MMP Acceptance Criteria (Phase 1)
1. Pipeline ingests ≥ 3 live article sources and ≥ 2 signal sources.
2. Confluence score fires for BTC and ≥ 1 equity within 60s of a real event.
3. WebSocket panel renders alert within 5 seconds of score computation.
4. Slack or Telegram notification delivered within 10 seconds of alert.
5. All events and alerts persisted to PostgreSQL with zero data loss on restart.
6. Feed health monitor accurately reports source staleness.

---

## 4. Key Architecture Decisions

| ID | Decision | Rationale |
|---|---|---|
| AD-01 | Single canonical `Event` schema for every signal type (`asset`, `asset_class`, `event_type`, `direction`, `confidence`, `timestamp`, `source`, `meta` JSONB) | One scorer handles any mix without type-specific branching |
| AD-02 | Bidirectional correlation — news scans for statistical confirmation AND statistical anomalies scan for news explanation; the dark signal queue (anomaly + no news) is highest-priority | Informed money moves before public information |
| AD-03 | Exponential time decay: `decay = exp(−ln(2) / half_life_minutes × age_minutes)`. Default half-life: 180 min equities, 60–90 min crypto | A 6-hour-old whale transfer is less actionable than a 12-minute-old one |
| AD-04 | Normalized confluence score `C = Σ(w·s·d·δ) / Σ|w·s·δ|`, not a raw sum or simple average; direction weighting is built in | Prevents high-volume low-quality sources from dominating |
| AD-05 | Signal weights baseline (see § 8 below) | Whale moves / unusual options flow are historically the most leading indicators; TA confirms but doesn't initiate |
| AD-06 | Lexicon NLP in dev → transformer model in production. Dev: BULL/BEAR word lists, zero deps. Prod swap: `ProsusAI/finbert` (sentiment), `sentence-transformers/all-MiniLM-L6-v2` (dedup), `spaCy en_core_web_sm` (NER). Interface identical | Swap function body, not contract |
| AD-07 | Redis as event bus + cache. Channels: `events` (raw stream), `alerts` (scored payloads). Rolling 500-event buffer for late joiners. Per-asset alert cache TTL 1h. Feed health keys TTL 5 min | — |
| AD-08 | PostgreSQL + TimescaleDB + pgvector. Hypertable on `events.ts`. JSONB on `events.meta` and `alerts.payload`. `vector(384)` on `articles.embedding` for semantic dedup | — |
| AD-09 | Backtesting deployment gate: `trade_count ≥ 30`, `Sharpe ≥ 1.0`, `maxDD ≤ 15%`, `WF consistency ≥ 0.7` | Mirrors ensemble validation logic in the MLB NRFI/YRFI engine |
| AD-10 | Separation of signal and execution — dashboard and correlation engine NEVER execute orders directly; hard risk limits live outside the signal layer | Safety |
| AD-11 | Agentic research as structured knowledge — research sessions persist goals, evidence, hypotheses, run cards, memory; not just chat logs (modeled after Vibe-Trading) | Compounding knowledge across sessions is the actual edge |
| AD-12 | Swap-ready ML interfaces — `forecast_lab.py` exposes the same `Event` output regardless of whether the model is LSTM, ARIMA, or sklearn | Model experiments don't break downstream consumers |
| AD-13 | Connector-first execution — broker adapters define paper/live modes, mandates, audit logs, and halt controls before any live capital is reachable | Safety boundaries must be structural, not procedural |
| AD-14 | Strategy export as portability layer — Pine Script and MQL5 exporters let research outputs be validated on external platforms independently | — |
| AD-15 | Intelligence-ops pattern from worldmonitor — source freshness gates run before any briefing or scoring job; macro geo-signals (globe.gl, deck.gl) surface non-market context relevant to position risk | — |

### Strategy Families Covered
Buy & Hold, DCA, Trend Following, Momentum, Mean Reversion, Breakout,
Pullback, Scalping, Day Trading, Swing Trading, Position Trading,
Event-Driven, Statistical Arb/Pairs, Sector Rotation, Market Neutral,
Options Income/Premium Selling, Volatility Trading, News/Sentiment,
Copy/Mirror, On-Chain Flow, Political/Insider Shadowing, Arbitrage,
TA-Native Signal Alerts, ML Forecast-Driven.

### Data Sources Mapped
- **Stocks:** Polygon, Finnhub, FMP, Alpha Vantage, Twelve Data, Tradier, CBOE, SEC EDGAR, IEX Cloud, FinViz.
- **Crypto:** CoinGlass, CoinGecko, Glassnode, Nansen, Arkham, Lookonchain, Whale Alert, Deribit, Binance WS, CoinMarketCal, CCXT exchanges.
- **News:** Finnhub, Reuters RSS, Bloomberg, Benzinga Pro, FinancialJuice.
- **Political:** House/Senate STOCK Act disclosures, OmniFolio, Capitol Gains, politiciantrades.info.
- **On-chain:** Glassnode, Nansen, Dune, DeFiLlama, CryptoQuant, IntoTheBlock.
- **Multi-market:** AKShare (A-share/HK), Tushare (CN), yfinance, mootdx.
- **Macro/Geo:** worldmonitor-style RSS/API aggregation, geography-tagged feeds.

### Connection to Existing Projects
- **MLB NRFI/YRFI engine** → `CorrelationScorer` mirrors ensemble confidence voting.
- **Poker bot backtest** → Walk-forward loop mirrors auto-backtest-after-session.
- **Polymarket WS sync** → `UnifiedPipeline` event bus extends existing WS logic.
- **Trade journal schema** → setup/regime/catalyst/venue tags apply across stocks, crypto, and sports betting simultaneously.
- **OpenClaw / Clawdbot** → Agent orchestrator extends existing agent framework.

---

## 5. Code Reference (Planned File Layout — none of this exists yet)

### Core Pipeline (Python)
- **`unified_pipeline.py`** — Classes: `Event`, `Article`, `NLPResult`, `NLPPipeline`, `SignalFactory`, `CorrelationScorer`, `UnifiedPipeline`. Methods: `ingest_articles()`, `ingest_signal_events()`, `correlate(asset)`, `correlate_all()`, `alert_payloads(threshold, min_signals)`. `NLPPipeline` dedupes and emits `Event`s from `Article`s; `SignalFactory` constructs typed non-news Events; `CorrelationScorer` applies the weighted time-decayed confluence formula; `UnifiedPipeline` merges both streams and orchestrates scoring.
- **`nlp_pipeline.py`** — Stages: ticker extraction → sentiment scoring → topic classification → urgency scoring → deduplication → Event emission. Prod swap points: sentiment → `ProsusAI/finbert`; dedup → sentence-transformers + pgvector cosine; NER → spaCy `en_core_web_sm`.
- **`correlation_scorer.py`** — Formula: `C = Σ(w·s·d·δ) / Σ|w·s·δ|`. Inputs: `List[Event]`, `anchor_asset`, optional `now`. Outputs: `CorrelationResult` (`confluence_score`, `weighted_signals`, `agreement_ratio`, `conflict_ratio`, `signal_count`).
- **`api_service.py`** (FastAPI) — Routes: `POST /ingest/articles`, `POST /ingest/signals`, `GET /correlate/{asset}`, `GET /correlate`, `GET /alerts`, `GET /feed-health`, `POST /demo/seed`, `WS /ws/alerts`. Start: `uvicorn api_service:app --host 0.0.0.0 --port 8000 --reload`.
- **`event_bus.py`** (asyncio + Redis) — Classes: `EventBus`, `FeedPoller`, `FinnhubNewsPoller`, `CoinGlassFundingPoller`, `PolygonOptionsPoller`, `AlertRouter`. Handlers: `slack_handler`, `telegram_handler`. Entry: `asyncio.run(run_all(api_keys))`.

### Database (SQL)
- **`schema.sql`** (PostgreSQL / TimescaleDB / pgvector) — Tables: `assets`, `articles` (fingerprint dedup, `vector(384)` embedding), `events` (canonical event stream, JSONB meta, GIN index), `correlation_scores`, `alerts` (severity, delivery channel, ACK), `positions` (live + paper, setup/regime/catalyst tags), `trade_journal` (R-multiple, setup × regime scoring), `greeks_snapshots`, `onchain_flows`, `feed_health` (heartbeat hash, auto-expiring TTL). Extensions: `pgvector`; `-- SELECT create_hypertable('events', 'ts');` to enable TimescaleDB.

### TA Engine (Python)
- **`ta_engine.py`** — Indicators: RSI, MACD, Ichimoku Cloud (tenkan/kijun/senkou/chikou), SMA, EMA, MFI, OBV, VWAP, Momentum. Both batch (DataFrame in → signals out) and streaming (per-candle update with alert emission). Emits typed Events into `UnifiedPipeline`. Delivery: Slack, Telegram, Discord, email, SMS (Twilio).

### ML Forecasting Lab (Python / Jupyter)
- **`forecast_lab.py`** (or `.ipynb`) — Stages: load → preprocess → feature extraction → sentiment fusion → normalize → train → evaluate → emit. Models: LSTM, RNN, ARIMA, sklearn regression baselines. Output: typed `ml_forecast` Event → `UnifiedPipeline`. Libraries: tensorflow/keras or pytorch, scikit-learn, statsmodels, numpy, pandas, matplotlib, seaborn.

### Agentic Research Workspace (Python)
- **`agent_orchestrator.py`** — Patterns: research goals, evidence rows, hypothesis registry, persistent memory, multi-agent swarm presets, run cards, MCP tool bindings. Swarm presets: quant, macro, crypto, risk. Data loaders: CCXT, AKShare, Tushare, yfinance, IEX Cloud, Tradier. Runtimes: Ollama (local), OpenRouter, Groq, MCP-compatible servers.

### Dataset Factory (Python)
- **`dataset_factory.py`** — Inputs: minute OHLCV, daily OHLCV, options chain, news, earnings, dividends, financials. Processing: normalize, align timestamps, join news sentiment features. Outputs: Redis (hot cache), file (CSV/Parquet), S3/Minio (archive). Workers: Celery tasks with Kubernetes job dispatch for large runs.

### Trade Journal & Shadow Account (Python)
- **`shadow_account.py`** — Inputs: broker export CSVs (IBKR, Alpaca, Tastytrade formats). Processing: parse → behavioral diagnostics → rule extraction → shadow strategy backtest → comparison report. Output: personalized diagnostics JSON + `trade_journal` DB rows.

### Strategy Exporters (Python)
- **`exporters/pine_exporter.py`** — Output: valid Pine Script v5 strategy or indicator scripts from internal rule objects.
- **`exporters/mql5_exporter.py`** — Output: MetaTrader 5 / MQL5 Expert Advisor **skeleton** from internal rules (signal-injection only by default — see OQ-12). Context: CryptoForex-Trader-Framework hybrid Python + MQL5 pattern.

### Intelligence Briefing (Python)
- **`intelligence_brief_service.py`** — Input: scored events + freshness-validated feeds. Output: AI-synthesized brief with source attribution, severity, cross-stream correlation summaries. Pattern: worldmonitor intelligence-ops + LLM summarization layer.
- **`freshness_guard.py`** — Checks `feed_health` table before any briefing or scoring job runs; blocks or warns if staleness threshold exceeded per source group.

### Live Connector Layer (Python)
- **`connector_profiles.py`** — Profiles: paper, live (opt-in), read-only. Attributes: mandate controls, audit ledger, instant halt, account/position/order abstractions. Adapters: IBKR API, Alpaca, Bybit, OKX (Phase 5).

### Frontend Components (TypeScript / React / Next.js)
- **`WebSocketAlertPanel.tsx`** — auto-connect/reconnect, severity badge (low/medium/high/critical), confluence score, signal count, top-4 contributing signals per alert, dedup, 50-alert rolling buffer. Env: `NEXT_PUBLIC_API_HOST`.
- **`ResearchWorkspace.tsx`** — goal-driven research sessions, evidence cards, hypothesis views, artifact links, run card display.
- **`TechnicalScannerPanel.tsx`** — TA indicator trigger display, alert routing status per symbol, multi-indicator scan table.
- **`DatasetOpsPanel.tsx`** — dataset extraction controls, replay status, archive/restore, Redis + S3/Minio storage indicators.
- **`BriefingPanel.tsx`** — AI brief display, source freshness indicators, cross-stream correlation summaries, macro geo-signal context.
- **`src-tauri/`** (Rust / Tauri 2) — optional desktop shell for macOS/Windows/Linux; always-on monitoring, local packaging, native notifications.

### Formulas
```
Confluence Score:
  C = Σ_i(w_i · s_i · d_i · δ_i) / Σ_i|w_i · s_i · δ_i|
  where:
    w_i   = signal type weight
    s_i   = confidence (0.0–1.0)
    d_i   = direction (+1 bullish, −1 bearish, 0 neutral)
    δ_i   = exp(−ln(2) / half_life_min · age_min)
  Output: [−1.0, 1.0]

Expectancy:
  E = (W × R_win) − (L × R_loss)
  where W = win rate, L = loss rate, R = average R-multiple

Deployment Gate:
  trade_count ≥ 30  AND  Sharpe ≥ 1.0
  AND  max_drawdown ≤ 0.15  AND  walk_forward_consistency ≥ 0.70
```

---

## 6. Libraries, Tools & Frameworks

### Python Backend — Core
`fastapi`, `uvicorn`, `pydantic`, `redis[asyncio]` (async client, formerly aioredis), `asyncpg`, `aiohttp`, `celery`, `kombu`

### Python — NLP & AI
`spacy` (NER, `en_core_web_sm`), `transformers` (`ProsusAI/finbert` prod sentiment), `sentence-transformers` (`all-MiniLM-L6-v2` dedup), `torch` / `tensorflow`+`keras` (LSTM/RNN), `scikit-learn`, `statsmodels` (ARIMA), `numpy`, `pandas`, `ollama` (local LLM runtime), `openai`/`openrouter` SDKs (cloud LLM routing), `groq` (low-latency inference), MCP Python SDK (agent tool bindings)

### Python — Trading & TA
`vectorbt` (fastest signal-based backtesting, NumPy/Numba), `TA-Lib` (200+ indicators, requires C lib), `ccxt` (100+ exchange unified API), `yfinance`, `alpaca-trade-api` / `alpaca-py`, `ibapi` / `ib_insync`

### Python — Multi-Market Data Loaders
`akshare`, `tushare`, `mootdx`, `iexfinance`, `finviz` (finvizfinance), Futu/Moomoo (`py-futu-api`)

### Python — Utilities
`rich`, `prompt_toolkit`, `hashlib` (stdlib MD5 dedup), `twilio`, `boto3`, `minio`

### Data Science
`numpy`, `pandas`, `matplotlib`, `seaborn`, `Jupyter`/`JupyterLab`

### Data APIs — Stocks
Polygon.io, Finnhub, Financial Modeling Prep (FMP), Alpha Vantage, Twelve Data, IEX Cloud, Tradier, CBOE, FinViz, SEC EDGAR (`edgartools`, full-text search API)

### Data APIs — Crypto
CoinGlass, CoinGecko, Glassnode, Nansen, Arkham, Lookonchain (no official API — social only), Whale Alert, Deribit, CoinMarketCal, DeFiLlama, CryptoQuant, IntoTheBlock, Binance WS, Bybit, OKX, Bitget, Hyperliquid, Kraken

### Data APIs — News & Macro
Reuters RSS (`feedparser`), Bloomberg (BLPAPI), Benzinga Pro, FinancialJuice, Investing.com (investpy archived — use fork or scraping), Santiment (`sanpy`), LunarCrush

### Data APIs — Political/Disclosure
House.gov / Senate.gov STOCK Act disclosures, OmniFolio, Capitol Gains, politiciantrades.info

### Frontend
Next.js, React, TypeScript, Vite (non-Next.js surfaces), TailwindCSS, TradingView Widgets / Lightweight Charts, AG Grid, Plotly.js / `react-plotly.js`, globe.gl (+ Three.js), deck.gl, MapLibre GL, Transformers.js (client-side WASM inference), Protocol Buffers (high-frequency WS payloads)

### Infrastructure
PostgreSQL, TimescaleDB (`timescale/timescaledb-ha` Docker image), pgvector (+ `pgvector-python`), Redis, Upstash Redis (serverless option), Minio, AWS S3 (`boto3`), Docker, docker-compose, Kubernetes (+ `kubernetes-client/python`, k3s for lightweight self-hosted), Vercel, Tauri 2

### Alert Delivery
Slack Incoming Webhooks (`slack-sdk`), Telegram Bot API (`python-telegram-bot`), Discord Webhooks (`discord.py` / `discord-webhook`), Twilio SMS, Email (SendGrid / `smtplib` + Gmail App Password)

### Execution & Export
MQL5 / MetaTrader 5 (`MetaTrader5` pip package, Python bridges), Pine Script v5 (TradingView docs + VS Code extension)

### Quick Start
```bash
# 1. Install Python dependencies
pip install fastapi uvicorn asyncpg pydantic "redis[asyncio]" aiohttp \
    numpy celery minio boto3 ccxt yfinance rich prompt_toolkit twilio

# 2. Install NLP production stack
pip install spacy transformers sentence-transformers torch scikit-learn \
    statsmodels ta-lib
python -m spacy download en_core_web_sm

# 3. Install frontend dependencies
cd frontend && npm install

# 4. Set environment variables
export REDIS_URL=redis://localhost:6379/0
export DATABASE_URL=postgresql://user:pass@localhost:5432/trading
export FINNHUB_API_KEY=your_key
export COINGLASS_API_KEY=your_key
export POLYGON_API_KEY=your_key
export SLACK_WEBHOOK_URL=your_webhook
export TELEGRAM_TOKEN=your_token
export TELEGRAM_CHAT_ID=your_chat_id
export OPENROUTER_API_KEY=your_key     # for cloud LLM
export OLLAMA_BASE_URL=http://localhost:11434  # for local LLM

# 5. Run database migrations
psql $DATABASE_URL -f schema.sql

# 6. Start services
uvicorn api_service:app --host 0.0.0.0 --port 8000 --reload
python event_bus.py
celery -A dataset_factory worker --loglevel=info
npm run dev

# 7. Seed and test
curl -X POST http://localhost:8000/demo/seed
# Open ws://localhost:8000/ws/alerts in any WebSocket client
```

### Budget Tiers
- **Free:** DeFiLlama, CoinMarketCal, Whale Alert, Dune community, SEC EDGAR, Investing.com, yfinance, AKShare
- **Mid-tier (~$50–150/mo):** Finnhub paid, Polygon Starter, CoinGlass paid, TradingView paid, Nansen entry, Glassnode entry
- **Pro (~$300–600/mo):** Full Nansen + Glassnode + CryptoQuant + Polygon Options + Bloomberg Terminal (optional)

→ **Decision:** Start with Mid-tier for MMP v1.0. Polygon + CoinGlass cover ~80% of the signal surface. Scale to Pro only after backtests show Nansen/Glassnode meaningfully improve confluence accuracy.

---

## 7. Open TODOs

### Phase 1 — Immediate (MMP v1.0)
- [ ] Wire `FinnhubNewsPoller`, `CoinGlassFundingPoller`, `PolygonOptionsPoller` to real API keys and test live ingestion.
- [ ] Set up PostgreSQL + TimescaleDB + pgvector (Docker or managed).
- [ ] Set up Redis instance (local or Upstash).
- [ ] Set environment variables: `REDIS_URL`, `DATABASE_URL`, `FINNHUB_API_KEY`, `COINGLASS_API_KEY`, `POLYGON_API_KEY`, `SLACK_WEBHOOK_URL`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`.
- [ ] Run `schema.sql` migrations.
- [ ] Replace in-memory `UnifiedPipeline` with DB-backed event store in `api_service.py`.
- [ ] Add `WebSocketAlertPanel.tsx` to Next.js dashboard page layout.
- [ ] Full end-to-end integration test: article ingest → NLP → scorer → WebSocket panel → Slack/Telegram.
- [ ] Implement `freshness_guard.py` and wire into scoring job.

### Phase 2 — TA Engine + Dataset Factory (v1.1)
- [ ] Build `ta_engine.py` with streaming + batch modes.
- [ ] Wire TA alert delivery to Slack, Telegram, Discord, Twilio SMS, email.
- [ ] Build `dataset_factory.py` with Redis + S3/Minio publish/replay.
- [ ] Build Greeks panel: Pydantic models, Tradier + Deribit pollers, IV Rank, IV Percentile, GEX levels, options income tracker.
- [ ] Build backtesting harness: vectorbt integration + walk-forward validator + expectancy heatmap by setup × regime.
- [ ] Build full Next.js dashboard: watchlist, regime, news, calendar, flow, risk, CVD, liquidation heatmap panels.
- [ ] Replace lexicon NLP with finbert sentiment + spaCy NER.
- [ ] Replace BoW dedup with sentence-transformers + pgvector.
- [ ] Add congressional/political trade poller (House/Senate STOCK Act).
- [ ] Add insider Form 4 poller (SEC EDGAR full-text search).
- [ ] Add dark signal queue panel.
- [ ] Add `TechnicalScannerPanel.tsx` and `DatasetOpsPanel.tsx`.

### Phase 3 — Agentic Research + ML Lab (v1.2)
- [ ] Build `agent_orchestrator.py`: goals, evidence, hypotheses, persistent memory, run cards, MCP tool bindings, swarm presets.
- [ ] Build `ResearchWorkspace.tsx` UI panel.
- [ ] Wire multi-market data loaders: CCXT, AKShare, Tushare, yfinance.
- [ ] Build `forecast_lab.py`: preprocessing, sentiment-feature fusion, LSTM/RNN/ARIMA/sklearn baselines, evaluation utilities.
- [ ] Build `shadow_account.py`: broker import, behavioral diagnostics, rule extraction, shadow strategy backtest.
- [ ] Add Celery workers + Kubernetes job dispatch for distributed backtests.
- [ ] Integrate Ollama / OpenRouter / Groq into agent runtime.

### Phase 4 — Intelligence Ops + Export (v2.0)
- [ ] Build `intelligence_brief_service.py` + `BriefingPanel.tsx`.
- [ ] Build `pine_exporter.py` and `mql5_exporter.py`.
- [ ] Build regime classifier: ADX + BB width + ATR z-score.
- [ ] Add paper trading sandbox tied to live signal pipeline.
- [ ] Add mobile command layer: emergency close + priority alerts.
- [ ] Add multi-condition alert builder UI.
- [ ] Add stablecoin issuance/redemption tracker.
- [ ] Add social sentiment anomaly detection (Santiment / LunarCrush).
- [ ] Add globe.gl / deck.gl / MapLibre GL macro geo-visualization.
- [ ] Package Tauri 2 desktop shell + service-worker caching.

### Phase 5 — Bounded Live Execution (v2.1)
- [ ] Build `connector_profiles.py` with mandate controls, audit ledger, halt.
- [ ] Wire IBKR API, Alpaca, Bybit, OKX live connector adapters.
- [ ] Build post-trade review loop: journal → scorer → expectancy update.

---

## 8. Resolved Open Questions (decisions to follow)

These were open design questions; each has a verdict that should guide
implementation unless explicitly revisited with the user.

| # | Question | Verdict |
|---|---|---|
| OQ-01 | Append-only event store + materialized view vs. live query for correlation scores? | **Append-only + materialized view.** Use `correlation_scores` as score history, Redis as hot-read cache for the dashboard. |
| OQ-02 | Crypto half-life 60–90 min vs. equity 180 min — auto-adaptive? | **Fixed baseline first** (crypto 90 min, equity 180 min). Add an `adaptive` flag (ATR z-score driven: `half_life = base_half_life / (1 + k * atr_z_score)`) in Phase 3 once the ATR pipeline is live. |
| OQ-03 | Should `political_trade` weight increase during sector earnings blackout windows? | **Yes, in Phase 2** — boost weight from 1.1 to 1.5–1.7 when an earnings blackout window is active for that sector. Requires a `blackout_calendar` table keyed to sector + reporting period. |
| OQ-04 | Backtesting harness: on-demand API trigger or nightly cron? | **Both.** Nightly cron (~2 AM CDT) for the full portfolio-level expectancy run; on-demand `POST /backtest/run` (Celery job, polled via `GET /backtest/{run_id}`) for agent-triggered or manual spot checks. |
| OQ-05 | Paper trading: live signal feed or configurable time-delayed feed? | **Live feed for the Phase 4 paper sandbox**; **historical replay against stored `events`** for strategy development/backtesting (Phase 2). Both share the `UnifiedPipeline` interface — only the event source changes. |
| OQ-06 | Store all correlation scores or only alert-threshold crossings? | **Store all scores, with TimescaleDB compression** for rows older than 7 days. Full recent-history resolution + compressed long-term storage. |
| OQ-07 | API cost budget for mid-tier sources? | **Start at Mid-tier (~$50–150/mo)** — Polygon Starter + Finnhub paid + CoinGlass paid covers ~80% of signal surface. Scale to Pro only after backtests validate ROI of Nansen/Glassnode. |
| OQ-08 | Should ML forecast Events enter the correlation scorer at training time or only inference time? | **Both, with strict lookahead prevention** — predictions only use data available at `t`, never `t+n`. Mirrors MLB model ensemble forward-validation. |
| OQ-09 | Agent memory backend: in-process dict, Redis, or vector store? | **Redis + vector store hybrid (Chroma/Qdrant).** Redis for hot structured state (goals, open positions, active alerts); Chroma (local, zero-config) for long-term semantic research memory. Embeddings served via local Ollama. |
| OQ-10 | Dataset factory: batch Celery or streaming Kafka/Flink? | **Batch Celery + Kubernetes jobs for Phase 2 MVP.** Kafka/Flink only justified if sub-second ML feature pipelines are needed at high asset counts (Phase 3+ consideration). |
| OQ-11 | Pine Script exporter: strategies, indicators, or both? | **Both** — exporter detects rule type and emits the appropriate Pine v5 template. |
| OQ-12 | MQL5 exporter: full EA or skeleton + signal injection? | **Skeleton + signal injection only by default**, with risk management/position sizing left as stubs requiring manual completion. Add an `--full-ea` flag for advanced users. Matches AD-10/AD-13 safety-first philosophy. |
| OQ-13 | Tauri 2 desktop: full parity with web or alert + chart subset? | **Alert + chart subset.** Desktop shows live alert stream, watchlist, regime panel, risk panel. Research/briefing/dataset-ops panels stay web-only. |
| OQ-14 | Briefing generation: fixed schedule or event-triggered? | **Scheduled baseline + event-triggered supplements.** Pre-market (6:30 AM CDT), mid-day (12:00 PM CDT), post-market (5:00 PM CDT) scheduled briefs; supplemental briefs only when `severity = critical AND signal_count >= 5`. `freshness_guard.py` is a prerequisite for both. |

---

## 9. Design Principles (apply these when writing code here)

1. **Signals interrogate each other** — every anomaly asks for news confirmation; every news event asks if the market is already pricing it in.
2. **Unexplained = highest priority** — the dark signal queue (anomaly + no news) is the most edge-rich event category; do not deprioritize it.
3. **Separation of concerns** — the pipeline scores and alerts; it never executes or manages risk directly (see AD-10).
4. **Auditability first** — every derived score links to raw source events; nothing is overwritten or mutated (append-only, see AD-08/OQ-01).
5. **Swap-ready interfaces** — NLP, dedup, TA, and ML functions accept/return the same types; production upgrades are function-body swaps, not refactors (see AD-06, AD-12).
6. **Compounding knowledge** — research sessions persist goals, evidence, hypotheses, and run cards, not just chat logs (see AD-11).
7. **Safety by structure** — live connectors require mandate controls, audit logs, and halt mechanisms before any capital is reachable (see AD-13).

### Signal Weight Reference (baseline, see AD-05 / OQ-03 for adjustments)
```
whale_transfer   1.7    options_flow    1.6
on_chain_move    1.5    funding_extreme 1.5
greek_anomaly    1.4    insider_trade   1.4
ml_forecast      1.3    strategy_break  1.3
macro_event      1.2    ta_signal       1.1
political_trade  1.1    news            1.0
```

### Repo Integration Summary (where external patterns map to internal modules)
- `PlaceNL/self-algorithmic-trading` → connector and strategy module structure.
- `reuniware/CryptoForex-Trader-Framework` → `mql5_exporter.py` and forex adapter patterns.
- `CyberPunk/currency-news-analysis` → `nlp_pipeline.py` design.
- `CryptoSignal/Crypto-Signal` → `ta_engine.py` and multi-channel alert routing.
- `scorpionhiccup/StockPricePrediction` → `forecast_lab.py` (LSTM/RNN/ARIMA, sentiment fusion).
- `AlgoTraders/stock-analysis-engine` → `dataset_factory.py` and Celery worker design.
- `HKUDS/Vibe-Trading` → `agent_orchestrator.py` and `ResearchWorkspace.tsx`.
- `koala73/worldmonitor` → `intelligence_brief_service.py`, `freshness_guard.py`, `BriefingPanel.tsx`, desktop packaging.

---

## 10. File Manifest

**Nothing has been built yet.** The repository is empty (no source files,
no commits beyond this CLAUDE.md). Everything below is planned, per the
roadmap in § 3 and the TODOs in § 7.

**Phase 1 (build first):**
```
unified_pipeline.py          Full pipeline: NLP + signals + scorer
nlp_pipeline.py              Standalone NLP module
correlation_scorer.py        Standalone confluence scorer
api_service.py               FastAPI REST + WebSocket service
event_bus.py                 Redis pub/sub + pollers + alert router
schema.sql                   PostgreSQL / TimescaleDB / pgvector schema
components/WebSocketAlertPanel.tsx   Next.js WebSocket alert panel component
```

**Phase 2+ (later phases):**
```
src/ta_engine.py
src/dataset_factory.py
src/agent_orchestrator.py
src/forecast_lab.py
src/shadow_account.py
src/intelligence_brief_service.py
src/freshness_guard.py
src/connector_profiles.py
src/exporters/pine_exporter.py
src/exporters/mql5_exporter.py
components/ResearchWorkspace.tsx
components/TechnicalScannerPanel.tsx
components/DatasetOpsPanel.tsx
components/BriefingPanel.tsx
src-tauri/                          (Tauri 2 desktop shell)
```

---

## 11. Domain Expert & Reviewer Subagents

`.claude/agents/` contains 16 domain-expert/reviewer pairs (32 files total),
one pair per module named in § 5's Code Reference, spanning all 5 phases.
Each expert agent owns one module's implementation and cites the exact
AD/FR/NFR/OQ items that constrain it; each paired reviewer agent checks
diffs to that module against the same constraints and reports findings
without editing code. Delegate to these proactively when working in their
domain rather than re-deriving the same conventions inline:

| Domain | Module(s) | Phase |
|---|---|---|
| `event-pipeline` | `unified_pipeline.py` (Event, SignalFactory, UnifiedPipeline) | 1 |
| `nlp-pipeline` | `nlp_pipeline.py` (sentiment/NER/dedup, lexicon→prod swap) | 1/2 |
| `correlation-scorer` | `correlation_scorer.py` (confluence formula, dark signal queue) | 1 |
| `api-service` | `api_service.py` (FastAPI REST + WebSocket) | 1 |
| `event-bus` | `event_bus.py` (Redis pub/sub, pollers, AlertRouter) | 1/2 |
| `database-schema` | `schema.sql` (Postgres/TimescaleDB/pgvector) | 1 |
| `ta-engine` | `ta_engine.py` (RSI/MACD/Ichimoku/etc., batch+streaming) | 2 |
| `backtesting-harness` | vectorbt walk-forward validator, deployment gate | 2 |
| `dataset-factory` | `dataset_factory.py` (Celery/Redis/S3/Minio) | 2 |
| `ml-forecast-lab` | `forecast_lab.py` (LSTM/RNN/ARIMA/sklearn) | 3 |
| `agent-orchestrator` | `agent_orchestrator.py` (swarm, memory, MCP) | 3 |
| `shadow-account` | `shadow_account.py` (broker import, rule extraction) | 3 |
| `strategy-exporters` | `exporters/pine_exporter.py`, `exporters/mql5_exporter.py` | 4 |
| `intelligence-briefing` | `intelligence_brief_service.py`, `freshness_guard.py` | 4 |
| `connector-layer` | `connector_profiles.py` (paper/live, mandates, halt) | 5 |
| `frontend-dashboard` | Next.js/React panels (`*.tsx`), Tauri 2 shell | 1-4 |

The `connector-layer` pair is the highest-stakes review surface in the repo
(it gates real capital) — its reviewer is instructed to apply maximum
scrutiny and re-verify before declaring a diff clean.

## 12. Working Conventions for Claude Code in This Repo

- This repository contains no application code yet — only this CLAUDE.md.
  Phase 1 has not been started; nothing described in § 3/§ 5/§ 10 has been
  built. When implementing Phase 1 TODOs, establish the directory layout
  implied by § 10 (e.g. `src/`, `frontend/`, `components/`, root-level
  `api_service.py`, `event_bus.py`, `schema.sql`) rather than inventing a
  different structure.
- Treat § 4 (Key Decisions) and § 8 (Resolved Open Questions) as binding
  defaults — implement against them rather than re-litigating the same
  trade-offs, unless the user asks to revisit one.
- Never hardcode API keys or webhook URLs (NFR-05) — always read from
  environment variables as listed in § 6 Quick Start / § 7 TODOs.
- Keep NLP/TA/ML/dedup function signatures stable across dev↔prod swaps
  (AD-06, AD-12, design principle 5) — production upgrades should be
  drop-in replacements of function bodies.
- Any new signal type must emit the canonical `Event` schema (AD-01) and be
  registered with a weight in the Signal Weight Reference (§ 9) so it flows
  through the existing `CorrelationScorer` without special-casing.
- The dashboard/API layer must never place or manage trades directly
  (AD-10, NFR-04) — execution only happens through the bounded connector
  layer planned for Phase 5 (`connector_profiles.py`).
