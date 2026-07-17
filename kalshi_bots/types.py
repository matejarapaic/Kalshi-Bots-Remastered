"""Shared type vocabulary. Source of truth: skills/CONTRACTS.md.

Conventions (CONTRACTS.md): probabilities are floats in [0,1]; prices are integer
cents 1-99; money is integer cents; timestamps are timezone-aware UTC.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

Prob = float
Cents = int
LeagueId = Literal["nfl", "nba", "mlb"]
GameStatus = Literal["scheduled", "in_progress", "final", "postponed", "suspended"]
InjuryStatus = Literal["OUT", "DOUBTFUL", "QUESTIONABLE", "PROBABLE", "DAY_TO_DAY", "ACTIVE"]
Side = Literal["yes", "no"]
SignalType = Literal[
    "overreaction-candidate", "divergence-candidate",
    "injury-candidate", "garbage-time-candidate", "game-final",
]


@dataclass
class TeamRef:
    league: LeagueId
    espn_abbr: str
    kalshi_abbr: str | None
    display_name: str


@dataclass
class GameState:
    league: LeagueId
    espn_event_id: str
    status: GameStatus
    home: TeamRef
    away: TeamRef
    home_score: int
    away_score: int
    period: int
    period_half: Literal["top", "bottom"] | None
    clock_seconds: int | None
    win_prob_home: Prob | None
    win_prob_source_ts: datetime | None
    start_time: datetime
    fetched_at: datetime


@dataclass
class InjuryEvent:
    league: LeagueId
    team: TeamRef
    espn_event_id: str | None
    player_id: str
    player_name: str
    position: str
    status: InjuryStatus
    source_ts: datetime | None
    fetched_at: datetime


@dataclass
class MarketRef:
    league: LeagueId
    series_ticker: str
    event_ticker: str
    market_ticker: str
    yes_team_kalshi_abbr: str
    title: str
    close_ts: datetime | None
    settlement_notes: str | None


@dataclass
class DepthLevel:
    price: Cents
    quantity: int


@dataclass
class OrderbookSnapshot:
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


@dataclass
class BookQuote:
    book_name: str
    home_prob: Prob
    fetched_at: datetime
    source_ts: datetime | None


@dataclass
class ConsensusOdds:
    league: LeagueId
    home: TeamRef
    away: TeamRef
    espn_event_id: str | None
    book_count: int
    devigged_home_prob: Prob | None
    max_pairwise_disagreement: float | None
    books: list[BookQuote]
    fetched_at: datetime


@dataclass
class MatchResult:
    espn_event_id: str
    market: MarketRef | None
    method: Literal["alias_exact", "alias_plus_start_time", "none"]
    ambiguous: bool
    candidates_considered: int
    note: str | None = None


@dataclass
class CandidateSignal:
    signal_type: SignalType
    league: LeagueId
    espn_event_id: str
    market_ticker: str | None
    payload: dict
    emitted_at: datetime


@dataclass
class SkillMatch:
    skill_name: str
    score: Prob
    confidence_threshold: Prob
    passed: bool
    reasons: list[str]


@dataclass
class SizingRequest:
    skill_name: str
    market: MarketRef
    side: Side
    entry_price: Cents
    model_prob: Prob
    book_depth_at_entry: int
    signal: CandidateSignal
    espn_event_id: str = ""
    is_live: bool = True


@dataclass
class SizingResult:
    contracts: int
    limit_price: Cents
    kelly_fraction_used: float | None
    capped_by: list[str]
    est_fee_cents_total: int


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


# --- kalshi-client new types ---

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


# --- espn-data new types ---

@dataclass
class SwingEvent:
    espn_event_id: str
    direction: Literal["home", "away"]
    magnitude: float
    window_s: int
    from_prob: Prob
    to_prob: Prob
    tie_prob: Prob
    detected_at: datetime


@dataclass
class DecidedEvent:
    espn_event_id: str
    leader: Literal["home", "away"]
    win_prob: Prob
    rule: str
    detected_at: datetime


@dataclass
class GameDetail:
    state: GameState
    win_prob_series_len: int
    tie_risk: bool
    starting_pitcher_ids: dict | None


# --- league-matching new types ---

@dataclass
class ParsedTicker:
    series_ticker: str
    away_kalshi_abbr: str
    home_kalshi_abbr: str
    start_time: datetime          # UTC, converted from embedded ET
    yes_team_kalshi_abbr: str | None
    game_number: int | None = None  # doubleheader G{n} suffix (verified live)


# --- risk-management new types ---

@dataclass
class ExposureSummary:
    bankroll_cents: int
    open_cost_cents: int
    by_game: dict[str, int]
    by_skill: dict[str, int]
    open_positions: int
    daily_realized_pnl_cents: int
    halted: bool
    halt_reason: str | None


# --- discord-bot new types ---

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


# --- postmortem new types ---

@dataclass
class PostmortemReport:
    league: LeagueId
    espn_event_id: str
    trades_audited: int
    entry_violations: int
    exit_deviations: int
    declined_candidates: int
    counterfactual_pnl_cents: int
    realized_pnl_cents: int
    settlement_status: Literal["settled", "pending", "voided", "mismatch"]
    threshold_flags: list[str]
    note_path: str
