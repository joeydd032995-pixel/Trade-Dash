---
name: agent-orchestrator
description: Reference knowledge for Trade-Dash's agentic research workspace (agent_orchestrator.py) — research goals, evidence, hypothesis registry, Redis+Chroma memory, swarm presets (quant/macro/crypto/risk), and MCP tool bindings.
when_to_use: building the memory backend, wiring a swarm preset, implementing a research goal/hypothesis workflow, exposing research operations as MCP tools
---

Trade-Dash's agent orchestrator lives in `agent_orchestrator.py` (see
CLAUDE.md §4 AD-11, §2 FR-35..39, §8 OQ-09).

## Structured knowledge, not chat logs (AD-11)
Every feature must persist queryable state (goal, evidence, hypothesis, run
card) — if it only helps within one session, it's chat, not research
infrastructure.

## Memory backend (OQ-09: Redis + Chroma hybrid)
- Redis: hot structured state — current goals, open positions, active
  alerts.
- Chroma (local, zero-config): long-term semantic memory — past research,
  retrieved by similarity. Embeddings via local Ollama
  (`OLLAMA_BASE_URL`) — don't require a cloud embedding API.
- Don't conflate: exact-key lookups → Redis; "similar to this hypothesis"
  → Chroma.

## Swarm presets (FR-37)
quant, macro, crypto, risk — each needs genuinely different tool
bindings/data access, not just a different prompt string on an identical
agent.

## MCP tool bindings (FR-35)
Expose goal/evidence/hypothesis/memory operations as MCP tools with stable
schemas — this is a public contract once exposed.

## Runtimes
Ollama (local, zero-key), OpenRouter, Groq — all behind one pluggable
interface; local-first must work standalone.

## Run cards (FR-38, NFR-07/10)
Reproducible: same inputs + config = same output.

## Delegate
- Deep implementation work → `agent-orchestrator-expert` agent.
- Pre-merge review → `agent-orchestrator-reviewer` agent.
