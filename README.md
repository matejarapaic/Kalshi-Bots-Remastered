# Kalshi Bots — 15-Minute Crypto Prediction-Market Trading System

An autonomous trading system for Kalshi's **15-minute crypto binary markets**,
starting with the `KXBTC15M` family: Bitcoin up/down contracts that open every
quarter hour and settle on a 60-second average of CF Benchmarks' BRTI index at
window close. Later crypto families (`KXETH15M`, …) are in-architecture but
out of scope for now — nothing may hard-code BTC.

The system runs a streaming loop 24/7:

```
composite spot feed (multi-exchange)  ─┐
                                       ├─►  fair-value model ─► signal ─► skill match
Kalshi WS order book (active window) ──┘         │
                                                 ▼
                              risk sizing ─► trade ─► exit ─► postmortem ─► tuner
```

> **⚠️ Current risk posture (read before running).** This system trades
> **real money autonomously** on Kalshi prod — no per-trade approval step.
> Two safety mechanisms have been removed by explicit owner direction:
>
> 1. **2026-07-30:** the tuner's relax path is no longer capped at the
>    human-approved baseline. A long enough winning or no-trade streak can
>    size positions larger, hold more positions, tolerate bigger per-position
>    losses, and require less entry edge than was originally signed off.
> 2. **2026-08-02:** the automatic daily-loss halt is removed. There is **no
>    daily bound on realized losses** — only per-trade/per-event/total-exposure
>    caps and the per-position stop-loss, all of which the tuner's relax path
>    can loosen.
>
> Do not weaken any remaining gate without explicit, direct owner instruction.
> See [CLAUDE.md](CLAUDE.md) for the full decision history.

---

## Table of contents

- [Architecture](#architecture)
- [Quick start](#quick-start)
- [Environment variables](#environment-variables)
- [Live-trading safety gates](#live-trading-safety-gates)
- [Risk parameters — the full reference](#risk-parameters--the-full-reference)
  - [Sizing & exposure](#sizing--exposure)
  - [Entry & exit quality gates](#entry--exit-quality-gates)
  - [The sizing pipeline (order of caps)](#the-sizing-pipeline-order-of-caps)
  - [Fees and the fee-aware edge floor](#fees-and-the-fee-aware-edge-floor)
- [The tuner — live parameter adjustment](#the-tuner--live-parameter-adjustment)
- [Other module constants](#other-module-constants)
- [The vault](#the-vault)
- [Testing](#testing)
- [Repository layout](#repository-layout)

---

## Architecture

### Three repositories, one system

1. **This repo** (`kalshi_bots/` + `tests/`) — the Python implementation.
2. **`skills/*/SKILL.md`** (also in this repo) — one spec per skill, written
   *before* the corresponding module and treated as the contract the code must
   match. `skills/CONTRACTS.md` is the shared type vocabulary;
   `kalshi_bots/types.py` is its Python mirror. The two must stay in sync.
   **When you change a skill's behavior, update its `SKILL.md` in the same
   change** — spec drift has been caught live before.
3. **`~/vaults/kalshi-vault/`** — a *separate* git repo (Obsidian vault) that
   is the system's live memory: trading-skill rule notes, agent system
   prompts, window state, trade history, and daily postmortem aggregates.
   Only `kalshi_bots/skills/vault.py` is allowed to touch it.

### Agents

Three thin agents in `kalshi_bots/agents/`, each wrapping the skills below,
wired together by `kalshi_bots/orchestrator.py`:

| Agent | Role | Hard boundary |
|---|---|---|
| `window_monitor` | Resolves the currently-active 15-min ticker from the wall clock and tracks its lifecycle (`opening` → `midpoint` → `near_close` → `settled`) | Never places orders |
| `trader` | Turns signals into sized, risk-checked orders; manages exits | The **only** code that calls `place_order` |
| `analyst` | Polls settlements, runs postmortems, maintains skill win-rate stats | Never sizes or trades |
| `tuner` | Adjusts risk parameters live in response to win/loss/no-trade streaks | Can only move parameters through the validated override layer |

The orchestrator runs a 1-second evaluation cadence over streaming state
(exchange WS feeds and the Kalshi order-book WS run as background tasks) — no
HTTP polling on the hot path. At startup it auto-detects real demo-exchange
execution vs. `PaperBroker` simulation by attempting a balance fetch.

### Skills

Each skill lives in `kalshi_bots/skills/<name>.py` with its spec in
`skills/<name>/SKILL.md`:

| Skill | What it does |
|---|---|
| `kalshi_client` | The only code that talks to Kalshi's REST API. RSA-PSS auth, orderbook-only pricing (the market summary's price fields are stale/null — never used), de-vig math, derived-ask math (`best_yes_ask = 100 − best_no_bid`, because Kalshi books are one-sided-bids-only), fee estimation. |
| `crypto_price_feed` | Streaming multi-exchange BTC/USD composite approximating BRTI: weighted median of healthy constituents' mids, plus a rolling realized-vol estimator over 1-second-resampled returns. Fails closed below 2 healthy constituents. |
| `kalshi_ws_orderbook` | WebSocket client for Kalshi's market-data feed on the active contract. |
| `window_monitor` | Ticker resolution + window lifecycle. Ambiguity returns `None`, never a guess; ticker grammar must be `grammar_verified` against live markets before matching. |
| `fair_value_model` | Pure functions: log-normal, drift-zero model probability from spot, strike, time remaining, and realized vol; per-side signed edges vs. the book. The system's reference truth — no sharper external source exists at this horizon. |
| `skill_matcher` | Deterministic (no LLM) scoring of a candidate signal against trading-skill notes in the vault. Only `confirmed`-status skills can ever match — this is the enforcement mechanism for "don't trade unconfirmed rules." |
| `risk_management` | **Every numeric trading parameter in the system lives here** (see [the reference below](#risk-parameters--the-full-reference)). Kelly math, the ordered cap pipeline, the exposure ledger (persisted to the vault, reloaded on startup), and the live-override layer the tuner drives. |
| `discord_bot` | Trade cards + slash commands behind a swappable transport (real gateway → REST-only → console log). Currently `autonomous` on demo and prod: no approval buttons on entries; exits were never approval-gated. |
| `postmortem` | Per-settled-window audit (~96/day): entry-condition snapshots, declined counterfactuals, crypto counterfactual dimensions, deterministic narrative. Sole writer of skill-note `win_rate`/`sample_size`. |
| `tuner` | Streak-driven live risk adjustment (see [The tuner](#the-tuner--live-parameter-adjustment)). |
| `vault` | TTL-cached, tag-filtered access to the Obsidian vault with a hard-coded per-agent write-scope table. |

### Trading skills (the rules that trade)

Trading *skills* in the vault sense are rule notes in
`02-trading-skills/` with a `status` field. Only `confirmed` skills trade:

| Skill note | Status | Trades? |
|---|---|---|
| `btc-15min-fair-value` | `confirmed` (owner hand-confirmed 2026-07-22 with zero settled samples — see CLAUDE.md) | **Yes, live** |
| `btc-15min-orderflow-imbalance` | `draft` | No |
| `btc-15min-vol-spike` | `draft` | No |

### Cross-cutting rules

- **Settlement source is BRTI, not any single exchange.** Everything
  references the composite feed calibrated against CF Benchmarks' constituent
  list.
- **Fail-closed.** Stale feed, unhealthy constituent count, missing WS —
  every degraded state returns `None` or raises a typed error and the trader
  declines. Never a silent degraded read.
- **24/7 memory hygiene.** Every deque, cache, and rolling window has a
  documented bound.
- **No sports vestiges.** This codebase was pivoted from a sports build;
  `grep -ri "espn|league|game|sport|team"` must return only false positives.

---

## Quick start

```bash
# Setup (a venv already exists at .venv/ in this checkout)
./.venv/bin/pip install -e .
./.venv/bin/pip install python-dotenv fastapi uvicorn websockets   # serve extras
./.venv/bin/pip install discord.py                                  # optional: slash commands/buttons

# Full test suite (fast, fully offline)
./.venv/bin/python -m pytest

# Single file / class
./.venv/bin/python -m pytest tests/test_risk_management.py
./.venv/bin/python -m pytest tests/test_risk_management.py::TestKellyMath

# Dashboard + orchestrator loop together (auto-detects real demo-exchange vs.
# paper simulation based on whether Kalshi credentials authenticate)
./.venv/bin/python -m kalshi_bots.dashboard    # http://127.0.0.1:8800
# GET /health reports streaming-dependency status for always-on monitoring

# Standalone smoke tests (live network, manual)
./.venv/bin/python scripts/smoke_price_feed.py    # 60s composite + vol
./.venv/bin/python scripts/smoke_kalshi_ws.py     # Kalshi WS book + BRTI (needs valid demo key)
./.venv/bin/python scripts/smoke_settlements.py   # settlements API
```

There is no linter or formatter configured — don't invent one. The dashboard
frontend (`kalshi_bots/dashboard_static/index.html`) is plain HTML/CSS/JS with
no build step, and it is presentation-only: it must never be a reason to
change what the backend broadcasts.

> **Known footgun:** do not add `from __future__ import annotations` to
> `dashboard.py`. It broke the WebSocket handshake once (FastAPI couldn't
> resolve the lazily-imported `WebSocket` type) and
> `tests/test_dashboard.py` guards against regression.

---

## Environment variables

Credentials load from a gitignored `.env` at the repo root via
`kalshi_bots/env.py`. Shell env vars always take precedence — the file never
overrides them.

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `KALSHI_KEY_ID` | For real (non-paper) operation | — | Kalshi API key ID |
| `KALSHI_KEY_PATH` | For real (non-paper) operation | — | Path to the RSA private key PEM |
| `KALSHI_ENV` | No | `demo` | `demo` or `prod`. Prod is multiply gated — see below |
| `KALSHI_ALLOW_PROD` | For prod | — | Must be exactly `yes-i-mean-it` or `KalshiClient` refuses to construct against prod |
| `EXEC_MODE` | For prod | — | Must be `live` when `KALSHI_ENV=prod` ("prod paper trading is not a thing") |
| `KALSHI_HOST_DEMO` / `KALSHI_HOST_PROD` | No | Kalshi's public hosts | REST host overrides |
| `KALSHI_WS_HOST_DEMO` / `KALSHI_WS_HOST_PROD` | No | Kalshi's public WS hosts | WebSocket host overrides |
| `KALSHI_RPS` | No | `5` | REST rate limit (requests/second, burst 10) |
| `KALSHI_VAULT_PATH` | No | `~/vaults/kalshi-vault` | Path to the Obsidian vault repo |
| `DISCORD_BOT_TOKEN` | No | — | Enables Discord transport (else console log) |
| `DISCORD_CHANNEL_ID` | With token | — | Channel for trade cards / notifications |
| `DISCORD_GUILD_ID` | No | — | Guild for slash-command registration |
| `APPROVER_ROLE_ID` | No | — | Role gating Discord buttons / `/halt` / `/resume`. **Fails open if unset** (deliberate) |
| `KALSHI_EXEC_MODE` | No | `manual_approve` | `DiscordBot`'s default mode when none is passed. The orchestrator always passes `autonomous` explicitly, so this only matters for standalone `DiscordBot` construction |

---

## Live-trading safety gates

Defense in depth, checked at startup. **Demo** (`KALSHI_ENV=demo`, the
default) passes straight through with no questions. **Prod** requires *all*
of the following, and any single miss is a hard `SystemExit`:

1. `EXEC_MODE=live` set in the environment.
2. The explicit CLI flag `--i-know-what-im-doing-crypto`.
3. An interactive typed confirmation: the exact phrase `TRADE LIVE`.
4. At least one `confirmed`-status trading skill note in the vault (draft
   skills never trade live money — this re-asserts the skill matcher's gate
   independently).
5. Even after all of the above, `KalshiClient` separately refuses prod unless
   `KALSHI_ALLOW_PROD=yes-i-mean-it` is set. Never add a shortcut around this.

After the guard passes, execution is **autonomous** — trade cards are posted
to Discord for visibility, but nothing waits for an approval click (owner
decision 2026-07-22). Exits were never approval-gated.

On startup the `RiskManager` also **reconciles** its persisted exposure
ledger against live Kalshi positions. Positions that settled while the
process was down are self-healed (P&L booked from our own recorded cost
basis); any genuinely unexplained mismatch **halts trading** until a human
`/resume`s. Manual halts survive restarts.

---

## Risk parameters — the full reference

Every numeric trading parameter in the system lives as a module-level
constant in [`kalshi_bots/skills/risk_management.py`](kalshi_bots/skills/risk_management.py)
with a provenance comment. **No other module may inline a risk number.**

Provenance statuses:

- **CONFIRMED** — owner-approved. Changing one requires owner sign-off by
  build rule.
- **PROPOSED** — pivot defaults or postmortem-derived additions awaiting
  owner sign-off (they are live and enforced meanwhile).

The values below are the **baselines**. The effective value at runtime is
`risk_management.current(name)` — the tuner may hold a live override (see
[The tuner](#the-tuner--live-parameter-adjustment)).

### Sizing & exposure

All percentages are of **bankroll**, defined as cash balance + cost of open
positions — so caps don't loosen as cash converts to positions.

| Parameter | Value | Status | Meaning |
|---|---|---|---|
| `BASE_KELLY_FRACTION` | `0.5` | CONFIRMED 2026-07-17 | Half-Kelly discipline: every stake is the full-Kelly fraction × 0.5 before any further scaling. Full Kelly for a binary at `c` cents with win prob `p` is `(p·100 − c) / (100 − c)`. |
| `SKILL_RISK_MULTIPLIER` | `fair-value: (1.0, 1.0)`, `orderflow-imbalance: (1.0, 1.0)`, `vol-spike: (0.5, 0.5)` | CONFIRMED 2026-07-22 | Per-skill `(live, non_live)` multiplier applied on top of the Kelly fraction. Crypto trades 24/7 so only the live column applies. A skill absent from this table can never size — unknown skills raise, never default. |
| `PER_TRADE_CAP_PCT` | `fair-value: 5`, `orderflow-imbalance: 5`, `vol-spike: 3` | CONFIRMED 2026-07-22 | Hard ceiling on the fraction of bankroll a single trade may commit, per skill, regardless of what Kelly says. |
| `PER_EVENT_EXPOSURE_CAP_PCT` | `5` | CONFIRMED 2026-07-17, re-confirmed 2026-07-22 | Max % of bankroll committed to one event (one 15-minute window), across all skills and sides. |
| `TOTAL_EXPOSURE_CAP_PCT` | `15` | CONFIRMED 2026-07-17, re-confirmed 2026-07-22 | Max % of bankroll committed across **all** open positions and unexpired sizing intents combined. |
| `CORRELATION_SCALE_SAME_EVENT` | `0.5` | CONFIRMED 2026-07-17 | If exposure already exists in the same window/event, the new trade's sizing fraction is halved before the caps apply — same-window positions are highly correlated. |
| `MAX_OPEN_POSITIONS` | `6` | CONFIRMED 2026-07-17 | Hard count limit on open positions + reserved intents. At the limit, sizing returns zero. |
| `MAX_CONTRACTS_PER_WINDOW` | `20` | CONFIRMED 2026-07-22 | Hard contract-count cap per 15-minute window ("draft-skill training wheels"). |
| `DEPTH_CONSUMPTION_MAX` | `0.25` | CONFIRMED 2026-07-17 | Never take more than this fraction of the book depth available at entry — being most of the book means moving the price against yourself and having nothing to exit into. |
| `SKILL_MIN_DEPTH` | `100` (all three skills) | CONFIRMED 2026-07-22 | Per-skill minimum book depth (contracts within 2¢ of entry) below which sizing returns zero. |
| `STOP_LOSS_PCT` | `50` | PROPOSED 2026-07-24 | Universal hard backstop, independent of any skill's own thesis-invalidation exits: exit any position whose mark-to-market value has fallen to this % of entry cost or worse (50 → exit once down 50%). |
| `INTENT_TTL_S` | `180` | — (documented limitation) | Sizing "intents" reserve cap headroom between sizing and fill for this many seconds, so two concurrent signals can't both size into the same headroom. Expired intents are pruned. |
| ~~`DAILY_LOSS_HALT_PCT`~~ | **removed** | Owner-directed 2026-08-02 | The automatic daily-loss halt is gone. Daily P&L buckets remain for reporting only. Restoring it means re-adding the constant, its corridor entry, the `_daily_halted()` check in the sizing pipeline, and its `TestHalts` coverage. |

### Entry & exit quality gates

| Parameter | Value | Status | Meaning |
|---|---|---|---|
| `MIN_EDGE_CENTS` | `4` | CONFIRMED 2026-07-22 | Minimum model-vs-ask divergence (in cents) required to enter. Also the floor input to the fee-aware edge requirement below. |
| `EXIT_EDGE_CENTS` | `1` | CONFIRMED 2026-07-22 | Once a held position's remaining edge drops below this, the thesis has played out — exit. |
| `SIGMA_PLAUSIBLE_MIN` | `0.18` | CONFIRMED 2026-08-02 (owner override; lowered from 0.20) | Lower bound of the annualized realized-vol band inside which the fair-value model is trusted. Below it, assume a broken feed or unmodeled regime and decline. The original 0.20 was rejecting the market's *typical* vol regime (median realized sigma over 47 documented windows was 0.187). |
| `SIGMA_PLAUSIBLE_MAX` | `2.00` | CONFIRMED 2026-07-22 | Upper bound of the same band — vol readings above it mean the model is not to be trusted. |
| `MIN_DEPTH_WITHIN_5C` | `100` | CONFIRMED 2026-07-22 | Entry gate: at least this many contracts within 5¢ on **each** side of the book. Thin books are ghost spreads, especially overnight/weekends. |
| `DEPTH_COLLAPSE_FRACTION` | `0.5` | CONFIRMED 2026-07-22 | Exit trigger: if either side's depth falls below `MIN_DEPTH_WITHIN_5C ×` this while holding, get out before liquidity vanishes entirely. |
| `ENTRY_PHASES` | `("midpoint",)` | CONFIRMED 2026-07-22 | Entries only in the window's midpoint phase — never `opening` (strike/book still settling, first 120 s) or `near_close` (gamma dominates, last 180 s). Non-numeric and deliberately not tunable. |
| `ATM_MIN_SIGMA_DISTANCE` | `0.5` | PROPOSED 2026-07-24 | Decline entries where the strike is within this many settlement-distribution standard deviations of spot — at the money the model is a coin flip, and the first live session's losers clustered exactly there. |
| `ENTRY_EDGE_SLIPPAGE_CENTS` | `1` | PROPOSED 2026-07-24 | Slippage buffer added on top of round-trip taker fees when deriving the fee-aware minimum edge (below). |
| `VELOCITY_THRESHOLD_PCT` | `0.004` | PROPOSED 2026-07-24 | If spot has moved ≥ 0.4% within the feed's short velocity window (60 s), the computed edge is likely chasing a gap rather than confirming a stable mispricing. |
| `VELOCITY_SIZE_SCALE` | `0.5` | PROPOSED 2026-07-24 | The sizing fraction multiplier applied once the velocity threshold is breached. |

### The sizing pipeline (order of caps)

`RiskManager.size()` runs these steps in order. Every cap that actually binds
appends its name to the result's `capped_by` list, so a fill's postmortem
records exactly which constraints shaped it. Steps that zero the size return
immediately with the reason:

1. **Fee-adjusted edge check** — `model_prob × 100` must exceed price + entry
   fee, else `no_edge`.
2. **Kelly fraction** — full Kelly × `BASE_KELLY_FRACTION` ×
   `SKILL_RISK_MULTIPLIER` (or flat `PER_TRADE_CAP_PCT` sizing for skills
   with a `None` multiplier — mechanism kept, no crypto skill uses it).
3. **Per-trade cap** — fraction clamped to `PER_TRADE_CAP_PCT`.
4. **Velocity scaling** — × `VELOCITY_SIZE_SCALE` if spot is moving fast.
5. **Correlation scaling** — × `CORRELATION_SCALE_SAME_EVENT` if same-event
   exposure exists.
6. **Per-event cap** — budget clamped to remaining `PER_EVENT_EXPOSURE_CAP_PCT`
   headroom.
7. **Total exposure cap** — budget clamped to remaining
   `TOTAL_EXPOSURE_CAP_PCT` headroom (positions + unexpired intents).
8. **Halt gate** — manual/reconcile halts zero everything (`halted`).
9. **Max open positions** — at `MAX_OPEN_POSITIONS`, zero
   (`max_open_positions`).
10. **Depth gates** — below `SKILL_MIN_DEPTH`, zero (`depth_min`); contract
    count clamped to `DEPTH_CONSUMPTION_MAX ×` available depth.
11. **Per-window contract cap** — clamped to `MAX_CONTRACTS_PER_WINDOW`.
12. **Integer floor** — fewer than 1 contract after all of the above → zero.

A successful sizing registers an **intent** (TTL `INTENT_TTL_S`) that
consumes cap headroom until it's filled, cancelled, or expires.

### Fees and the fee-aware edge floor

Kalshi's taker fee is quadratic in price — highest at the money:
`fee = ceil(0.07 × contracts × price × (1 − price))` per side
(`FEE_RATE = 0.07` in `kalshi_client.py`, named so a schedule change is one
edit).

`required_edge_cents(entry_price)` is the *effective* minimum edge to enter:

```
max( current("MIN_EDGE_CENTS"),  round_trip_taker_fee + ENTRY_EDGE_SLIPPAGE_CENTS )
```

This exists because a "4¢ edge" on a 50¢ coin flip that costs ~5¢ round-trip
in fees is negative-EV — exactly the failure mode of the first live session's
losers. Note it reads the edge floor via `current()`, so a tuner override on
`MIN_EDGE_CENTS` propagates here too.

---

## The tuner — live parameter adjustment

Spec: [`skills/tuner/SKILL.md`](skills/tuner/SKILL.md). The tuner never
invents a risk number — it steps existing parameters through
`risk_management.set_override()`, which validates every change against a
per-parameter corridor and appends to a bounded audit log
(`override_log`, last 100 entries).

**Overrides are session-scoped** (owner-directed 2026-08-02): they die with
the process. `Tuner.reset()`, called at every orchestrator start, discards
whatever overrides the previous session persisted — relaxed *and* tightened,
including a raised sigma floor — and zeroes the streak counters, announcing
what was discarded. Every session begins at the human-approved baselines.
This bounds relax-past-baseline excursions to one process lifetime, but the
flip side is real: a restart mid-loss-streak also wipes loss-driven
tightening, so the system comes back at full baseline sizing. The tuner-state
vault note is a within-session record only, never cross-session memory.

### Triggers

| Constant | Value | Status | Meaning |
|---|---|---|---|
| `LOSS_STREAK_TRIGGER` | `3` | PROPOSED 2026-07-28 | Consecutive settled **losing** windows → one tighten step on every policy param (fires again at 6, 9, …). |
| `NO_TRADE_STREAK_TRIGGER` | `3` | PROPOSED 2026-07-28 | Consecutive settled **tradeless** windows (~45 min) → one relax step (again at 6, 9, …). Every **winning** window also relaxes one step. |
| `TIGHTEN_STEP` | `0.85` | PROPOSED 2026-07-28 | Multiplicative step for lower-is-tighter params; relax divides by it. |
| `EDGE_STEP_CENTS` | `1` | PROPOSED 2026-07-28 | Additive step for raise-is-tighter params (`MIN_EDGE_CENTS`). |
| `SIGMA_LOSS_STREAK_TRIGGER` | `2` | CONFIRMED 2026-08-02 | Consecutive losing windows → raise `SIGMA_PLAUSIBLE_MIN` to the session-average realized vol. Raise-only; wins and no-trade streaks never lower it — only an owner edit or override-clear brings it back down. |
| `SIGMA_SESSION_WINDOWS` | `20` | CONFIRMED 2026-08-02 | Session-average lookback: bounded deque of the last N settled windows' realized vol (~5 h at 15-min cadence). |

### Corridors (`TUNABLE_BOUNDS`)

Each tunable parameter has a `(floor_mult, ceil_mult)` pair applied to its
baseline, measured along the **tighten** direction:

| Parameter | Bounds | Tighten direction |
|---|---|---|
| `BASE_KELLY_FRACTION` | (0.5, 1.0) | lower |
| `SKILL_RISK_MULTIPLIER` | (0.25, 1.0) | lower |
| `PER_TRADE_CAP_PCT` | (0.25, 1.0) | lower |
| `SKILL_MIN_DEPTH` | (1.0, 2.0) | raise |
| `MAX_CONTRACTS_PER_WINDOW` | (0.25, 1.0) | lower |
| `MIN_EDGE_CENTS` | (1.0, 2.0) | raise |
| `EXIT_EDGE_CENTS` | (1.0, 2.0) | raise |
| `SIGMA_PLAUSIBLE_MIN` | (1.0, 2.0) | raise |
| `SIGMA_PLAUSIBLE_MAX` | (0.5, 1.0) | lower |
| `MIN_DEPTH_WITHIN_5C` | (1.0, 2.0) | raise |
| `DEPTH_COLLAPSE_FRACTION` | (1.0, 2.0) | raise |
| `STOP_LOSS_PCT` | (0.5, 1.0) | lower |
| `VELOCITY_THRESHOLD_PCT` | (0.5, 1.0) | lower |
| `VELOCITY_SIZE_SCALE` | (0.5, 1.0) | lower |
| `TOTAL_EXPOSURE_CAP_PCT` | (0.5, 1.0) | lower |
| `PER_EVENT_EXPOSURE_CAP_PCT` | (0.5, 1.0) | lower |
| `CORRELATION_SCALE_SAME_EVENT` | (0.5, 1.0) | lower |
| `MAX_OPEN_POSITIONS` | (0.25, 1.0) | lower |
| `DEPTH_CONSUMPTION_MAX` | (0.5, 1.0) | lower |

**The corridor is direction-asymmetric** (owner-directed 2026-07-30):

- **Tighten** is hard-bounded by the multipliers above — the tuner can only
  make the system *more conservative* than baseline, up to the limit shown.
- **Relax is unbounded past baseline.** Its only floor is each parameter's
  own domain: raise-is-tighten params (edge, depth) floor at 0; lower-is-
  tighten params (sizing, exposure, position counts, stop-loss) have **no
  ceiling at all**. Enough consecutive winning or tradeless windows can push
  sizing, exposure, and loss tolerance past what any human signed off on.
  This was the deliberate removal of the design's original safety envelope —
  read the override-layer comment in `risk_management.py` before touching it.

`ENTRY_PHASES` is non-numeric and deliberately not tunable.
`SIGMA_PLAUSIBLE_MIN` is deliberately **not** a policy param — the streak
tighten/relax loops never touch it; it moves only through the raise-only
sigma-floor mechanism.

---

## Other module constants

These are *mechanical* constants (feed health, timing, protocol), not
trading parameters — they live next to the code they configure.

### `crypto_price_feed.py`

| Constant | Value | Meaning |
|---|---|---|
| `STALE_CONSTITUENT_S` | `2.0` | An exchange whose last tick is older than this is unhealthy |
| `MIN_HEALTHY_CONSTITUENTS` | `2` | Below this the composite fails closed (`current_composite()` → `None`) |
| `MAX_MID_DEVIATION` | `0.05` | BRTI "potentially erroneous data" parameter: a constituent mid deviating >5% from the median is excluded |
| `SAMPLE_INTERVAL_S` | `1.0` | Vol buffer resolution (1-second resample) |
| `DEFAULT_VOL_WINDOW_S` | `900` | Realized-vol window; matches the 15-minute contract |
| `MIN_VOL_SAMPLES` | `60` | Fail-closed: fewer samples → vol is `None` |
| `MIN_VOL_COVERAGE` | `0.5` | Samples must span at least half the window |
| `MAX_SAMPLES` | `3700` | Deque bound (3600 s window + slack) — 24/7 hygiene |
| `VELOCITY_WINDOW_S` | `60` | Short window behind `recent_move_pct` ("how fast is spot moving *right now*") |
| `RECONNECT_MAX_BACKOFF_S` | `30.0` | Exchange WS reconnect backoff ceiling |
| `SECONDS_PER_YEAR` | `31_536_000` | Vol annualization base (365 d) — must match `fair_value_model`'s tau base |

### `window_monitor.py`

| Constant | Value | Meaning |
|---|---|---|
| `DEFAULT_SERIES` | `"KXBTC15M"` | Default market family |
| `WINDOW_S` | `900` | Quarter-hour contract length |
| `OPENING_PHASE_S` | `120` | `[open, open+120s)`: strike/book still settling — no entries |
| `NEAR_CLOSE_PHASE_S` | `180` | `[close−180s, close)`: gamma zone — no entries |
| `VERIFY_CACHE_MAX` | `16` | Bounded ticker-verification cache (~4 windows/hour) |
| `NEGATIVE_TTL_S` | `10.0` | Retry window for failed ticker verifications |

### `kalshi_client.py`

| Constant | Value | Meaning |
|---|---|---|
| `FEE_RATE` | `0.07` | Kalshi taker-fee schedule coefficient |
| `TRADEABLE_STATUSES` | `{"active", "open"}` | Market statuses that count as tradeable |

### `orchestrator.py`

| Constant | Value | Meaning |
|---|---|---|
| `TICK_S` | `1.0` | Evaluation cadence over streaming state |
| `LIVE_FLAG` | `--i-know-what-im-doing-crypto` | Required CLI flag for live trading |
| `LIVE_CONFIRM_PHRASE` | `TRADE LIVE` | Required interactive confirmation phrase |

---

## The vault

The Obsidian vault (`KALSHI_VAULT_PATH`, default `~/vaults/kalshi-vault`) is
a separate git repo and the system's live memory:

| Directory | Contents |
|---|---|
| `00-meta/` | Orchestrator system prompt |
| `01-agents/` | Agent system prompts |
| `02-trading-skills/` | Trading-skill rule notes (the `status: confirmed` gate lives in these) |
| `03-market-context/` | Window state, the exposure ledger, tuner state |
| `04-trade-history/` | Trade notes, daily-aggregate postmortems (one file per family per UTC day) |

Write access is enforced by a hard-coded per-agent scope table
(`WRITE_SCOPES` in `vault.py`). Notably: only the **analyst** may write
`win_rate`/`sample_size` on skill notes, and postmortem is the sole writer of
those fields (env-labeled `demo_*` vs. prod — the two never mix).

> **Data-integrity note:** vault trade data from before 2026-07-29 has
> sign-flipped P&L on NO-side positions. The Kalshi settlements API is the
> ground truth for historical P&L, not the early vault notes.

---

## Testing

```bash
./.venv/bin/python -m pytest
```

- All tests are **offline** — no network. Streaming components are tested
  against in-memory fake feeds, never mocked network calls.
- One file per skill/agent (`tests/test_<module>.py`), grouped in `TestXxx`
  classes per behavior area.
- Fixtures favor **real captured data** over invented mocks — several
  kalshi-client tests assert against real observed ticker grammar, because
  those were live discoveries a hypothetical fixture would not have caught.
- Each skill's `SKILL.md` ends with testing requirements that the test file
  must cover.

---

## Repository layout

```
├── CLAUDE.md                    # Build rules + decision history (read this)
├── README.md                    # This file
├── pyproject.toml               # Python >= 3.11; deps: requests, PyYAML,
│                                #   cryptography, websockets, orjson, numpy
├── kalshi_bots/
│   ├── orchestrator.py          # Streaming loop, live-trading guard, wiring
│   ├── dashboard.py             # FastAPI: /, /api/state, WS /ws, /health
│   ├── dashboard_static/        # Plain HTML/CSS/JS, no build step
│   ├── paper.py                 # PaperBroker simulation fallback
│   ├── env.py                   # .env loader (shell vars always win)
│   ├── types.py                 # Python mirror of skills/CONTRACTS.md
│   ├── discord_gateway.py       # Real discord.py transport (lazy import)
│   ├── timefmt.py
│   ├── agents/                  # window_monitor, trader, analyst, tuner
│   └── skills/                  # One module per skill (see Architecture)
├── skills/                      # SKILL.md specs — the contracts the code
│   └── CONTRACTS.md             #   must match; update spec + code together
├── tests/                       # Offline test suite
├── scripts/                     # Live-network smoke tests (manual)
└── secrets/                     # Gitignored key material
```
