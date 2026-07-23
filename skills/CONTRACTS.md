# Shared Type Contract

Every `SKILL.md` in this directory expresses its inputs and outputs in terms of the
types below, implemented as Python dataclasses in a single shared module
(`kalshi_bots/types.py`) — the two files must stay in sync. A spec that needs a new
type defines it in its own "New types" section using the same conventions; a spec
may never redefine a type listed here.

## Global conventions

- **Probabilities** are `float` in `[0.0, 1.0]`. Never percentages, never cents.
- **Kalshi contract prices** are integer cents `1–99` (`Cents = int`). Conversion
  between price and probability happens ONLY in `kalshi-client` (de-vig).
  Sub-cent reality: the crypto series use `tapered_deci_cent` tick structure
  ($0.001 steps below $0.10 / above $0.90) and fractional contract counts;
  `kalshi-client` aggregates to whole cents at the boundary (sizes floor, never
  round up; our order prices are whole cents — valid ticks in every band). This
  is a documented approximation, revisit if tail-price P&L precision starts to
  matter.
- **Crypto spot prices** (BTC/USD, strikes, BRTI values) are `float` dollars —
  market data, not ledger money.
- **Money** is integer cents (`bankroll_cents: int`), never floats.
- **Timestamps** are timezone-aware UTC `datetime`. Every externally-fetched datum
  carries `fetched_at`/`computed_at` (our clock) and, where the source provides
  one, `source_ts`. Staleness checks always use our clock (monotonic where the
  consumer allows it).
- **Errors**: each skill raises its own typed exceptions (`KalshiClientError`,
  `VaultError`, `CryptoPriceFeedError`, …) — no bare exceptions cross a skill
  boundary. Data problems never raise on the read path: they degrade `health()`
  and gate reads to `None` (fail closed).

## Core types

```python
Prob = float          # 0.0–1.0
Cents = int           # 1–99
Side = Literal["yes", "no"]
Phase = Literal["opening", "midpoint", "near_close", "settled"]
SignalType = Literal[
    "window-open", "phase-change", "fair-value-candidate", "window-close",
]

@dataclass
class MarketRef:
    family: str                 # market family/category label, e.g. "crypto"
    series_ticker: str          # e.g. KXBTC15M (grammar verified at runtime)
    event_ticker: str
    market_ticker: str
    yes_label: str              # what YES resolves for (subtitle / ticker suffix)
    title: str
    close_ts: datetime | None
    settlement_notes: str | None

@dataclass
class DepthLevel:
    price: Cents
    quantity: int

@dataclass
class OrderbookSnapshot:
    # THE book snapshot type — built by kalshi-client's build_snapshot from
    # either transport (REST fetch or the WS client's live ladder). Books are
    # one-sided bids per side; asks derived as 100 - other side's bid.
    market: MarketRef
    yes_bid: Cents | None
    yes_ask: Cents | None       # DERIVED as 100 - no_bid
    no_bid: Cents | None
    no_ask: Cents | None
    yes_book: list[DepthLevel]  # asks available to a YES buyer
    no_book: list[DepthLevel]
    devigged_yes_prob: Prob | None  # None when book too empty to de-vig
    spread_cents: int | None
    fetched_at: datetime

@dataclass
class WindowRef:
    # One 15-minute contract window. strike is None until the window opens
    # (Kalshi stamps floor_strike at open with the prior settlement value).
    series_ticker: str
    event_ticker: str
    market_ticker: str
    opens_at: datetime
    closes_at: datetime
    strike: float | None = None

@dataclass
class CryptoSignal:
    # window-monitor output. A signal is a flag, never a vouch — the trader
    # re-verifies everything against fresh data at decision time.
    signal_type: SignalType
    series_ticker: str
    market_ticker: str | None
    window: WindowRef | None
    phase: Phase | None
    payload: dict               # per-type shape documented in window-monitor
    emitted_at: datetime

@dataclass
class FairValueEstimate:
    model_prob_up: Prob
    model_prob_down: Prob
    market_ask_cents: Cents | None   # best ask for the "up" (YES) side
    edge_cents: float | None         # model - market, signed; None if no ask
    sigma_used: float                # annualized realized vol input
    spot_used: float
    strike: float
    time_remaining_s: float
    computed_at: datetime

@dataclass
class BookHealth:
    market_ticker: str
    connected: bool
    subscribed: bool
    last_update_age_s: float | None
    seq_gap: bool
    healthy: bool

@dataclass
class BrtiState:
    # Latest cfbenchmarks_value tick for BRTI (the settlement index itself).
    # settlement_forming = Kalshi's last_60s_windowed_average_15min, present
    # only in each window's final minute, computed with settlement windowing.
    value: float | None
    avg_60s: float | None
    settlement_forming: float | None
    ts: datetime | None
    fetched_at: datetime
    raw: dict

@dataclass
class CompositeSpot:
    mid: float                      # weighted median of healthy constituents' mids
    bid: float
    ask: float
    source_ts: dict[str, datetime]  # per-exchange source tick timestamps
    computed_at: datetime
    constituents_healthy: int
    constituent_count: int

@dataclass
class ConstituentHealth:
    name: str
    connected: bool
    last_tick_age_s: float | None   # None = never ticked since start
    healthy: bool

@dataclass
class FeedHealth:
    constituents: list[ConstituentHealth]
    healthy_count: int
    constituent_count: int
    composite_available: bool

@dataclass
class SkillMatch:
    skill_name: str
    score: Prob                 # matcher fit score 0–1
    confidence_threshold: Prob  # from skill frontmatter
    passed: bool                # score >= confidence_threshold
    reasons: list[str]          # human-auditable scoring rationale

@dataclass
class SizingRequest:
    skill_name: str
    market: MarketRef
    side: Side
    entry_price: Cents
    model_prob: Prob            # our estimate of P(side wins)
    book_depth_at_entry: int    # contracts within the skill's depth window
    signal: CryptoSignal
    event_id: str = ""          # correlation key (event ticker)
    is_live: bool = True        # 24/7 crypto: effectively always True

@dataclass
class SizingResult:
    contracts: int              # 0 is a valid, final answer
    limit_price: Cents
    kelly_fraction_used: float | None   # None for flat-sized skills
    capped_by: list[str]        # names of every cap that bound
    est_fee_cents_total: int

@dataclass
class ExposureSummary:
    bankroll_cents: int
    open_cost_cents: int
    by_event: dict[str, int]
    by_skill: dict[str, int]
    open_positions: int
    daily_realized_pnl_cents: int
    halted: bool
    halt_reason: str | None

@dataclass
class OrderRequest:
    market_ticker: str
    side: Side
    action: Literal["buy", "sell"]
    contracts: int
    limit_price: Cents
    client_order_id: str        # idempotency key, generated by trader

@dataclass
class OrderResult:
    order_id: str
    status: Literal["resting", "filled", "partial", "canceled", "rejected"]
    filled_contracts: int
    avg_fill_price: Cents | None
    fee_cents: int
    raw: dict                   # full API response for the trade note

@dataclass
class Position:
    market_ticker: str
    side: Side
    contracts: int
    avg_price: Cents
    fees_paid_cents: int
    raw: dict

@dataclass
class Fill:
    order_id: str
    market_ticker: str
    side: Side
    action: str
    contracts: int
    price: Cents
    taker_fee_cents: int
    ts: datetime
    raw: dict

@dataclass
class Settlement:
    market_ticker: str
    result: Literal["yes", "no", "void"]
    settled_ts: datetime | None
    revenue_cents: int
    raw: dict

@dataclass
class VaultNote:
    path: str                   # vault-relative, e.g. "02-trading-skills/x.md"
    frontmatter: dict
    body: str
    mtime: datetime

@dataclass
class VaultQuery:
    directory: str                       # vault-relative prefix
    frontmatter_filters: dict            # exact-match, e.g. {"status": "confirmed"}
    tag_filters: list[str]               # list-membership, e.g. market_conditions

@dataclass
class TradeCard:
    client_order_id: str
    skill_name: str
    market: MarketRef
    side: Side
    action: Literal["buy", "sell"]
    sizing: SizingResult
    snapshot: dict
    is_live: bool

@dataclass
class ApprovalOutcome:
    decision: Literal["approved", "rejected", "expired", "undeliverable"]
    decided_by: str | None
    decided_at: datetime | None
    card_message_id: str | None

@dataclass
class PostmortemReport:
    # shape adapted fully in sprint-4 (batching, crypto counterfactuals)
    family: str                 # series ticker, e.g. KXBTC15M
    event_id: str               # event ticker of the audited window
    trades_audited: int
    entry_violations: int
    exit_deviations: int
    declined_candidates: int
    counterfactual_pnl_cents: int
    realized_pnl_cents: int
    settlement_status: Literal["settled", "pending", "voided", "mismatch"]
    threshold_flags: list[str]
    note_path: str
```

## Cross-cutting rules restated (specs must not contradict these)

1. Orderbook truth only: prices come from the orderbook (REST endpoint or WS
   ladder); Kalshi summary fields (`yes_ask` etc. on the market object) are
   frequently null/stale — never read them for trading decisions.
2. `best_yes_ask = 100 - best_no_bid` when the YES ask side is empty (and
   symmetrically for NO). The derivation lives in `kalshi-client` only.
3. No skill reads vault files from disk on a live trading cycle — all vault access
   through the `vault` skill's TTL cache.
4. Ambiguity → `None`, never a guess: window-monitor, and any skill consuming it,
   must treat an unresolved active window as "do not trade right now."
5. Every numeric trading parameter lives in `risk-management`'s named parameter
   table or a skill note's frontmatter — never inline in another skill's logic.
6. All trading on `KALSHI_ENV=demo` until the sprint-5 live-trading guard flow is
   explicitly exercised by a human against a `confirmed`-status skill.
7. Settlement source is BRTI (composite/`cfbenchmarks_value`), not any single
   exchange; model inputs and postmortem checks reference it.
8. Fail-closed everywhere: stale feed, unhealthy constituent count, seq gap,
   missing WS — reads return `None` and the trader declines. Never a degraded
   silent read.
9. 24/7 memory hygiene: every deque, cache, and rolling buffer has a documented
   bound.
