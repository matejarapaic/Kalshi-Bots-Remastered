# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Kalshi **15-minute crypto prediction-market** trading system, starting with the
`KXBTC15M` market family (Bitcoin up/down binary contracts opening every quarter
hour, settling on a 60-second average of CF Benchmarks' BRTI at window close).
Three agents (window-monitor, trader, analyst) built on independent "skills,"
orchestrated into a streaming loop: composite spot feed + Kalshi WS order book →
fair-value model → signal → size → trade → exit → postmortem. Later crypto
families (`KXETH15M`, …) are in-architecture but out of scope for the first
pivot — nothing may hard-code BTC.

> **Pivot status:** sprints 0-5 are complete. A fresh demo API key
> (2026-07-23) unblocked real demo-exchange execution and the Kalshi WS,
> live-verified via `scripts/smoke_kalshi_ws.py` (that run also caught and
> fixed a real bug — `cfbenchmarks_value.data` is a JSON-encoded string, not
> an object). **2026-07-22: owner moved to live prod trading without the
> intended 24-hour real-demo-run review or the 30+ settled-sample skill
> validation** — demo-exchange liquidity for `KXBTC15M` was too thin to
> accumulate fills, so there was no path to gather that data on demo. The
> owner explicitly authorized live trading on `btc-15min-fair-value` anyway
> (status hand-set to `confirmed` in the vault note with zero real samples)
> and authorized `autonomous` mode on prod (no per-trade approval click).
> Risk numbers carried over unchanged from their crypto-pivot defaults,
> provenance comments updated to `CONFIRMED 2026-07-22` in
> `risk_management.py` to record that sign-off honestly. The other two
> skills (`btc-15min-orderflow-imbalance`, `btc-15min-vol-spike`) remain
> `draft` and do not trade. Sprint 6 (Polymarket cross-reference) is
> unaffected by this and still optional.

**Prod/live gates, current state (owner re-decided 2026-07-22 — see pivot status above):**
- `KalshiClient` refuses `KALSHI_ENV=prod` unless `KALSHI_ALLOW_PROD=yes-i-mean-it` is *also* set (defense in depth — never add a shortcut around this).
- The crypto live-trading guard (`orchestrator.live_trading_guard`, sprint 5) still applies in full: `KALSHI_ENV=prod` + `EXEC_MODE=live` requires the explicit `--i-know-what-im-doing-crypto` CLI flag AND an interactive typed confirmation AND at least one `confirmed`-status trading skill in the vault. This is the gate the owner satisfied by hand-confirming `btc-15min-fair-value` rather than through the statistical path the guard's own error message describes — future changes to *this* mechanism still need explicit direct instruction.
- `DiscordBot` no longer refuses `autonomous` on prod (removed 2026-07-22, this was the "re-answer the execution-mode question in code" the prior text called for) — the orchestrator always constructs it with `mode="autonomous"`. There is no per-trade approval step on demo or prod. Exits were never approval-gated either way.
- `Trader`/`Analyst` are constructed with `env=self.mode` (the guard's actual "demo"/"live" result), not a hardcoded `"demo"` — this was a latent bug found while wiring up live trading: hardcoding it would have let the `draft`-status stub skills trade real money too (via the demo-only `allowed_statuses` relaxation) and would have mislabeled real fills as `demo_*` postmortem data.

Do not weaken any of these gates further without an explicit, direct instruction to do so.

## Commands

```bash
# Setup (venv already exists at .venv/ in this checkout)
./.venv/bin/pip install -e .
./.venv/bin/pip install python-dotenv fastapi uvicorn websockets   # serve extras

# Run the full test suite (fast/offline — no network in tests)
./.venv/bin/python -m pytest

# Single file / class / test
./.venv/bin/python -m pytest tests/test_risk_management.py
./.venv/bin/python -m pytest tests/test_risk_management.py::TestKellyMath

# Run the dashboard + orchestrator loop together (auto-detects real demo-exchange
# vs. paper simulation based on whether Kalshi credentials authenticate)
./.venv/bin/python -m kalshi_bots.dashboard   # http://127.0.0.1:8800
# GET /health reports streaming-dependency status for always-on monitoring

# Standalone smoke tests (live network, manual)
./.venv/bin/python scripts/smoke_price_feed.py   # 60s composite + vol
./.venv/bin/python scripts/smoke_kalshi_ws.py    # Kalshi WS book + BRTI (needs valid demo key; verified working 2026-07-23)
```

Credentials load from a gitignored `.env` at repo root via `kalshi_bots/env.py` (existing shell env vars always take precedence — the file never overrides them). Required for real (non-paper) operation: `KALSHI_KEY_ID`, `KALSHI_KEY_PATH` (PEM path); optional: `DISCORD_BOT_TOKEN`. There is no linter/formatter configured — don't invent one.

## Architecture

### Three repositories, one system

1. **This repo** (`kalshi_bots/` + `tests/`) — the Python implementation.
2. **`skills/*/SKILL.md`** (in this repo) — one spec per skill, written *before* the corresponding module and treated as the contract the code must match. `skills/CONTRACTS.md` is the shared type vocabulary; `kalshi_bots/types.py` is its Python mirror — the two must stay in sync, and a new cross-skill type belongs in both places, never invented ad hoc in one module.
3. **`~/vaults/kalshi-vault/`** — a *separate* git repo (Obsidian vault) that is the system's live memory: trading-skill rule notes (`02-trading-skills/`), agent system prompts (`01-agents/`), and all runtime output — window state, trade history, daily-aggregate postmortems (`03-market-context/`, `04-trade-history/`). The vault path is configurable via `KALSHI_VAULT_PATH` (default `~/vaults/kalshi-vault`); `kalshi_bots/skills/vault.py` is the only code allowed to touch it.

**When you change a skill's behavior, update its `SKILL.md` in the same change.** Spec drift has been caught live before; fixing both places in one change is the expected workflow, not an exception.

### Skills

Surviving skills (behavior unchanged by the pivot unless noted):

- `kalshi_client.py` — the only code that talks to Kalshi's REST API. Auth (RSA-PSS signing), orderbook-only pricing (never the market summary object's price fields — they're stale/null), de-vig math, derived-ask math (`best_yes_ask = 100 - best_no_bid`, since Kalshi books are one-sided-bids-only), fee estimation.
- `vault.py` — TTL-cached, tag-filtered read/write access to the Obsidian vault, with a hard-coded per-agent write-scope table (`WRITE_SCOPES`). Only `analyst` may write `win_rate`/`sample_size` on skill notes; only that.
- `skill_matcher.py` — deterministic (no LLM) scoring of a candidate signal against trading-skill notes queried from the vault. A skill's `status` must be `confirmed` to ever match — `draft`/`retired` skills are structurally invisible here, which is the actual enforcement mechanism for "don't trade unconfirmed rules."
- `risk_management.py` — **every numeric trading parameter in the system lives in this file's module-level constants** with an explicit CONFIRMED/PROPOSED provenance comment. No other module may inline a risk number. Kelly math, ordered cap pipeline (each binding cap appends to `capped_by`), and the exposure ledger (persisted to the vault, reloaded on startup).
- `discord_bot.py` — trade cards + slash commands behind a swappable transport: `GatewayTransport` (`kalshi_bots/discord_gateway.py`, real discord.py websocket) → `DiscordTransport` (REST-only) → `ConsoleTransport` (local log). `discord_gateway.py` lazy-imports discord.py. Execution mode (`manual_approve` vs `autonomous`) is owner-decided, currently `autonomous` on demo only. Exits are never approval-gated. `APPROVER_ROLE_ID` gates buttons/`/halt`/`/resume` but fails *open* if unset (deliberate).
- `postmortem.py` — per-settled-window audit (~96/day): entry-condition snapshots, declined counterfactuals, crypto counterfactual dimensions (model-was-right, vol-was-right, constituent-drift), deterministic narrative block; appends to DAILY aggregate notes (one file per family per UTC day); **sole writer** of skill-note `win_rate`/`sample_size` (env-labeled `demo_*` vs prod), flushed in batches by the analyst — never per window.

New crypto skills (built in sprint order):

- `crypto-price-feed` (Sprint 1) — streaming multi-exchange BTC/USD composite approximating BRTI: weighted median of healthy constituents' mids, rolling realized-vol estimator from 1-second-resampled returns. Fail-closed: fewer than 2 healthy constituents → `current_composite()` returns `None`.
- `kalshi-ws-orderbook` (Sprint 2) — WebSocket client for Kalshi's market-data feed on the active contract.
- `window-monitor` (Sprint 2) — resolves the currently-active `KXBTC15M` ticker for a wall-clock time and tracks window lifecycle (`opening`, `midpoint`, `near_close`, `settled`). The entity-resolution role the deleted league-matching skill used to play; same hard invariant: ambiguity returns `None`, never a guess, and ticker grammar must be `grammar_verified` against live markets before matching.
- `fair-value-model` (Sprint 3) — pure functions: log-normal, drift-zero model probability from spot, strike, time remaining, realized vol; per-side signed edges vs the book. The system's reference truth — no external sharper source exists at this horizon.

### Agents and orchestration

`kalshi_bots/agents/{window_monitor,trader,analyst}.py` are thin wrappers around the skills — window-monitor never places orders, trader is the only thing that calls `place_order`, analyst never sizes or trades. `kalshi_bots/orchestrator.py` wires everything together; from Sprint 2 it runs a streaming loop that consumes in-memory queues fed by the WS clients (not a polling timer), auto-detecting real demo-exchange execution vs. `kalshi_bots/paper.py`'s `PaperBroker` by attempting a balance fetch at startup. 24/7 operation: no off-season, no schedule; skills self-throttle in thin weekend/overnight liquidity rather than firing on ghost spreads.

`kalshi_bots/dashboard.py` serves `kalshi_bots/dashboard_static/index.html` (plain HTML/CSS/JS, no frontend framework/build step) over FastAPI, plus `GET /api/state` and `WS /ws` — the dashboard is presentation-only and must never be given a reason to change what the backend broadcasts. **Do not add `from __future__ import annotations` to `dashboard.py`** — it broke the WebSocket handshake once already (FastAPI couldn't resolve the lazily-imported `WebSocket` type at construction time) and there's a regression test (`tests/test_dashboard.py`) guarding it.

### Cross-cutting rules (enforced every sprint)

- **Settlement source is BRTI, not any single exchange.** Model probabilities and postmortem checks reference the composite feed calibrated against CF Benchmarks' published BRTI constituent list — a single-exchange spot feed systematically mis-settles postmortems.
- **Fail-closed.** Stale feed, unhealthy constituent count, missing WS connection — every one returns `None` or raises a typed error and the trader declines. Never a degraded silent read.
- **No sports vestiges.** `grep -ri "espn|league|game|sport|team" kalshi_bots/ skills/` must return only false positives from Sprint 2 onward.
- **24/7 memory hygiene.** Every deque, cache, and rolling window has a documented bound.
- **Paper before live.** The first live order requires the explicit Sprint-5 guard flow and a `confirmed`-status skill.

### Testing conventions

Tests are grouped in `TestXxx` classes per behavior area within one file per skill/agent (`tests/test_<module>.py`). Fixtures favor real captured data over invented mocks where practical — several kalshi-client tests assert against real observed Kalshi ticker grammar because those were live discoveries a hypothetical fixture would not have caught. All tests are offline; streaming components are tested against in-memory fake feeds, never mocked network calls.
