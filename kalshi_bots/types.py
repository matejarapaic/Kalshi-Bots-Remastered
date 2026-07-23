"""Shared type vocabulary. Source of truth: skills/CONTRACTS.md.

Conventions (CONTRACTS.md): probabilities are floats in [0,1]; Kalshi contract
prices are integer cents 1-99 (sub-cent tail ticks aggregate to whole cents at
the client boundary — documented approximation); money is integer cents;
crypto spot prices are float dollars (market data, not ledger money);
timestamps are timezone-aware UTC.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

Prob = float
Cents = int
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
    yes_label: str              # what YES resolves for (ticker suffix / subtitle)
    title: str
    close_ts: datetime | None
    settlement_notes: str | None


@dataclass
class DepthLevel:
    price: Cents
    quantity: int


@dataclass
class OrderbookSnapshot:
    """THE book snapshot type — built by kalshi-client's build_snapshot from
    either transport (REST orderbook fetch or the WS client's live ladder).
    Books are one-sided bids per side; asks are derived (100 - other side's
    bid). devigged_yes_prob is None when the book is too empty to de-vig."""
    market: MarketRef
    yes_bid: Cents | None
    yes_ask: Cents | None
    no_bid: Cents | None
    no_ask: Cents | None
    yes_book: list[DepthLevel]
    no_book: list[DepthLevel]
    devigged_yes_prob: Prob | None
    spread_cents: int | None
    fetched_at: datetime


# --- window-monitor types ---

@dataclass
class WindowRef:
    """One 15-minute contract window. `strike` is None until the window opens
    (Kalshi sets it at open to the prior window's settlement value)."""
    series_ticker: str
    event_ticker: str
    market_ticker: str
    opens_at: datetime
    closes_at: datetime
    strike: float | None = None


@dataclass
class CryptoSignal:
    """Window-monitor output: lifecycle transitions and trade candidates.
    The trader re-verifies everything against fresh data — a signal is a flag,
    never a vouch."""
    signal_type: SignalType
    series_ticker: str
    market_ticker: str | None
    window: WindowRef | None
    phase: Phase | None
    payload: dict
    emitted_at: datetime


# --- fair-value-model types (placeholder sprint-2; model lands sprint-3) ---

@dataclass
class FairValueEstimate:
    model_prob_up: Prob
    model_prob_down: Prob
    market_ask_cents: Cents | None   # best ask for the "up" (YES) side
    edge_cents: float | None         # model - market, signed; None if no ask
    sigma_used: float                # annualized realized vol input
    spot_used: float                 # composite mid at evaluation
    strike: float
    time_remaining_s: float
    computed_at: datetime


# --- kalshi-ws-orderbook types ---

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
    """Latest cfbenchmarks_value tick for BRTI (the settlement index itself).
    settlement_forming is Kalshi's last_60s_windowed_average_15min — present
    only during the final minute before each quarter-hour close, computed with
    exactly the settlement windowing."""
    value: float | None
    avg_60s: float | None
    settlement_forming: float | None
    ts: datetime | None
    fetched_at: datetime
    raw: dict


# --- crypto-price-feed types ---
# BTC spot prices are float dollars (market data, not ledger money — the
# integer-cents rule applies to Kalshi contract prices and bankroll only).

@dataclass
class CompositeSpot:
    mid: float
    bid: float
    ask: float
    source_ts: dict[str, datetime]
    computed_at: datetime
    constituents_healthy: int
    constituent_count: int


@dataclass
class ConstituentHealth:
    name: str
    connected: bool
    last_tick_age_s: float | None
    healthy: bool


@dataclass
class FeedHealth:
    constituents: list[ConstituentHealth]
    healthy_count: int
    constituent_count: int
    composite_available: bool


# --- skill-matcher types ---

@dataclass
class SkillMatch:
    skill_name: str
    score: Prob
    confidence_threshold: Prob
    passed: bool
    reasons: list[str]


# --- risk-management types ---

@dataclass
class SizingRequest:
    skill_name: str
    market: MarketRef
    side: Side
    entry_price: Cents
    model_prob: Prob
    book_depth_at_entry: int
    signal: CryptoSignal
    event_id: str = ""              # correlation key (event ticker)
    is_live: bool = True


@dataclass
class SizingResult:
    contracts: int
    limit_price: Cents
    kelly_fraction_used: float | None
    capped_by: list[str]
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


# --- kalshi-client types ---

@dataclass
class OrderRequest:
    market_ticker: str
    side: Side
    action: Literal["buy", "sell"]
    contracts: int
    limit_price: Cents
    client_order_id: str


@dataclass
class OrderResult:
    order_id: str
    status: Literal["resting", "filled", "partial", "canceled", "rejected"]
    filled_contracts: int
    avg_fill_price: Cents | None
    fee_cents: int
    raw: dict


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


# --- vault types ---

@dataclass
class VaultNote:
    path: str
    frontmatter: dict
    body: str
    mtime: datetime


@dataclass
class VaultQuery:
    directory: str
    frontmatter_filters: dict = field(default_factory=dict)
    tag_filters: list[str] = field(default_factory=list)


# --- discord-bot types ---

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


# --- postmortem types ---

@dataclass
class PostmortemReport:
    family: str                    # series ticker, e.g. KXBTC15M
    event_id: str                  # event ticker of the audited window
    trades_audited: int
    entry_violations: int
    exit_deviations: int
    declined_candidates: int
    counterfactual_pnl_cents: int
    realized_pnl_cents: int
    settlement_status: Literal["settled", "pending", "voided", "mismatch"]
    threshold_flags: list[str]
    note_path: str
    # crypto counterfactual dimensions (sprint-4). Per-window these are
    # coin-flippy; they exist to be aggregated by the analyst.
    model_direction_hits: int = 0    # trades whose model side matched settlement
    vol_ratio: float | None = None   # window realized vol / mean sigma_used
    constituent_drift: bool = False  # a feed constituent degraded in-window
                                     # -> exclude window from aggregate learning
