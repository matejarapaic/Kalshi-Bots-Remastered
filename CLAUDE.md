# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Kalshi sports prediction-market trading system: three agents (game-monitor, trader, analyst) built on nine independent "skills," orchestrated into a poll → signal → size → trade → exit → postmortem loop. Trading is sports-market-only (NFL/NBA/MLB), driven by live ESPN win-probability/game-state data compared against Kalshi orderbook prices and sportsbook consensus odds.

**The system is demo-only by design, enforced at multiple layers, not just by convention:**
- `Orchestrator.__init__` refuses to start unless `KALSHI_ENV=demo`.
- `KalshiClient` refuses `KALSHI_ENV=prod` unless `KALSHI_ALLOW_PROD=yes-i-mean-it` is *also* set (defense in depth — never add a shortcut around this).
- `DiscordBot` refuses `mode="autonomous"` whenever `KALSHI_ENV=prod`, with no override env var. Moving to prod requires re-answering the execution-mode question in code, not flipping a flag.

Do not weaken any of these gates without an explicit, direct instruction to do so.

## Commands

```bash
# Setup (venv already exists at .venv/ in this checkout)
./.venv/bin/pip install -e .
./.venv/bin/pip install python-dotenv fastapi uvicorn websockets   # serve extras

# Run the full test suite (175+ tests, all fast/offline — no network in tests)
./.venv/bin/python -m pytest

# Single file / class / test
./.venv/bin/python -m pytest tests/test_risk_management.py
./.venv/bin/python -m pytest tests/test_risk_management.py::TestKellyMath
./.venv/bin/python -m pytest tests/test_risk_management.py::TestKellyMath::test_worked_example

# Run the dashboard + orchestrator loop together (auto-detects real demo-exchange
# vs. paper simulation based on whether Kalshi credentials authenticate)
./.venv/bin/python -m kalshi_bots.dashboard   # http://127.0.0.1:8800

# Headless orchestrator only (no dashboard)
./.venv/bin/python -c "from kalshi_bots.orchestrator import Orchestrator; Orchestrator(leagues=['mlb']).run()"

# Bounded run (N cycles) for quick manual sanity checks
./.venv/bin/python -c "from kalshi_bots.orchestrator import Orchestrator; Orchestrator(leagues=['mlb']).run(cycles=2, poll_s=15)"
```

Credentials load from a gitignored `.env` at repo root via `kalshi_bots/env.py` (existing shell env vars always take precedence — the file never overrides them). Required for real (non-paper) operation: `KALSHI_KEY_ID`, `KALSHI_KEY_PATH` (PEM path), `ODDS_API_KEY`; optional: `DISCORD_BOT_TOKEN`. There is no linter/formatter configured — don't invent one.

## Architecture

### Three repositories, one system

1. **This repo** (`kalshi_bots/` + `tests/`) — the Python implementation.
2. **`skills/*/SKILL.md`** (in this repo) — one spec per skill, written *before* the corresponding module and treated as the contract the code must match. `skills/CONTRACTS.md` is the shared type vocabulary; `kalshi_bots/types.py` is its Python mirror — the two must stay in sync, and a new cross-skill type belongs in both places, never invented ad hoc in one module.
3. **`~/vaults/kalshi-vault/`** — a *separate* git repo (Obsidian vault) that is the system's live memory: league configuration/alias maps (`00-meta/league-config.md`), the four trading-skill rule notes with owner-confirmed thresholds (`02-trading-skills/`), agent system prompts (`01-agents/`), and all runtime output — daily slates, active-game state, trade history, postmortems (`03-market-context/`, `04-trade-history/`). The vault path is configurable via `KALSHI_VAULT_PATH` (default `~/vaults/kalshi-vault`); `kalshi_bots/skills/vault.py` is the only code allowed to touch it.

**When you change a skill's behavior, update its `SKILL.md` in the same change.** Spec drift has already been caught live once (league-matching's single-candidate start-time-window rule) and fixed in both places — that's the expected workflow, not an exception.

### Skill dependency order

The nine skills in `kalshi_bots/skills/` were built in this order and still depend on each other this way — read/modify in this order when tracing a bug:

1. `kalshi_client.py` — the only code that talks to Kalshi's API. Auth (RSA-PSS signing), orderbook-only pricing (never the market summary object's price fields — they're stale/null), de-vig math, derived-ask math (`best_yes_ask = 100 - best_no_bid`, since Kalshi books are one-sided-bids-only), fee estimation.
2. `vault.py` — TTL-cached, tag-filtered read/write access to the Obsidian vault, with a hard-coded per-agent write-scope table (`WRITE_SCOPES`). Only `analyst` may write `win_rate`/`sample_size` on skill notes; only that.
3. `espn_data.py` + `league_config.py` — polls ESPN's undocumented public scoreboard/summary endpoints; `league_config.py` parses `00-meta/league-config.md` out of the vault into `LeagueConfig`/`AliasRow` objects. ESPN's win-probability series carries no timestamps, so swing detection runs off an in-process poll-history ring buffer, not the feed's own clock.
4. `league_matching.py` — resolves ESPN game ↔ Kalshi market ticker via the alias map. **Ambiguity always returns `market=None`, never a guess** — this is a hard invariant, not a style preference. A league only matches if its `league-config.md` entry has `grammar_verified: true` (MLB is; NFL/NBA are not, pending a live verification sweep once those seasons start).
5. `odds_api.py` — The Odds API v4 client. American-odds-only parsing (decimal payloads raise `OddsFormatError` rather than silently misreading); per-book de-vig before averaging into consensus.
6. `skill_matcher.py` — deterministic (no LLM) scoring of a `CandidateSignal` against the four confirmed trading-skill notes queried from the vault. A skill's `status` must be `confirmed` to ever match — `draft`/`retired` skills are structurally invisible here, which is the actual enforcement mechanism for "don't trade unconfirmed rules."
7. `risk_management.py` — **every numeric trading parameter in the system lives in this file's module-level constants** (`PER_TRADE_CAP_PCT`, `SKILL_RISK_MULTIPLIER`, `TOTAL_EXPOSURE_CAP_PCT`, etc.) with an explicit CONFIRMED/PROPOSED provenance comment. No other module may inline a risk number. Kelly math, cap pipeline (12 ordered steps, each appending to `capped_by` when it binds), and the exposure ledger (persisted to the vault, reloaded on startup) all live here.
8. `discord_bot.py` — trade cards + slash commands behind a swappable transport (`ConsoleTransport` today; a real Discord transport is not yet implemented — see the module docstring). Execution mode (`manual_approve` vs `autonomous`) is a first-class, owner-decided setting, currently `autonomous` on demo only (enforced at construction time, see gates above). Exits are never approval-gated in either mode.
9. `postmortem.py` — runs on `game-final`, audits every trade's recorded entry-condition snapshot against what was actually true, computes counterfactuals for declined signals, and is the **sole writer** of skill-note `win_rate`/`sample_size` (env-labeled: `demo_*` fields vs. prod fields, so paper-trading stats never contaminate real ones).

### Agents and orchestration

`kalshi_bots/agents/{game_monitor,trader,analyst}.py` are thin wrappers around the skills above — game-monitor never places orders, trader is the only thing that calls `kalshi_client.place_order`, analyst never sizes or trades. `kalshi_bots/orchestrator.py` wires everything together and owns the poll loop (`run_cycle` → `run`), auto-detecting real demo-exchange execution vs. `kalshi_bots/paper.py`'s `PaperBroker` (a fill simulator against real public orderbooks, used when no valid credentials are present) by attempting a balance fetch at startup.

`kalshi_bots/dashboard.py` serves `kalshi_bots/dashboard_static/index.html` (plain HTML/CSS/JS, no frontend framework/build step) over FastAPI, plus `GET /api/state` and `WS /ws` — the dashboard is presentation-only and must never be given a reason to change what the backend broadcasts. **Do not add `from __future__ import annotations` to `dashboard.py`** — it broke the WebSocket handshake once already (FastAPI couldn't resolve the lazily-imported `WebSocket` type at construction time) and there's a regression test (`tests/test_dashboard.py`) guarding it.

### Known gaps (real, not hypothetical — read before touching restart/scheduling logic)

- `Trader.open_trades` (which drives exit-management/invalidation checks) is pure in-memory and is **not** reconstructed from the vault or the risk ledger on restart. A position still open when the process stops loses active exit management until manually handled — the risk ledger and Kalshi's own position stay accurate, but nothing will proactively exit it.
- `Orchestrator.run()` computes "today" once at startup and never re-checks for an ET-midnight rollover; a process left running past midnight keeps operating against a stale date (stale daily-slate note, stale per-day match cache in `league_matching.py`) until restarted.
- `Analyst.nightly_slate()` (a next-day slate preview) exists but is never called automatically by anything.

### Testing conventions

Tests are grouped in `TestXxx` classes per behavior area within one file per skill/agent (`tests/test_<module>.py`). Fixtures favor real captured data over invented mocks where practical — `tests/fixtures/espn_mlb_*.json` are live ESPN payloads captured during development, and several league-matching/kalshi-client tests assert against real observed Kalshi ticker grammar (e.g. the `AZ`/`CWS` Kalshi-vs-ESPN abbreviation divergence, the doubleheader `G2` ticker suffix) rather than synthetic examples, because those were live discoveries that a hypothetical fixture would not have caught.
