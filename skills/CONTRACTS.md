# Shared Type Contract

Every `SKILL.md` in this directory expresses its inputs and outputs in terms of the
types below. Phase 3 implements these as Python dataclasses in a single shared
module (`kalshi_bots/types.py`). A spec that needs a new type defines it in its own
"New types" section using the same conventions; a spec may never redefine a type
listed here.

## Global conventions

- **Probabilities** are `float` in `[0.0, 1.0]`. Never percentages, never cents.
- **Prices** are integer cents `1–99` (`Cents = int`). Conversion between price and
  probability happens ONLY in `kalshi-client` (de-vig) and `odds-api` (odds math).
- **Timestamps** are timezone-aware UTC `datetime`. Every externally-fetched datum
  carries `fetched_at` (when we polled) and, where the source provides one,
  `source_ts` (the source's own timestamp). Staleness checks always use `fetched_at`.
- **Money** is integer cents (`bankroll_cents: int`), never floats.
- **Errors**: each skill raises its own typed exceptions (`KalshiClientError`,
  `VaultError`, …) — no bare exceptions cross a skill boundary. Every skill's spec
  lists its exception types.
- **League IDs**: `LeagueId = Literal["nfl", "nba", "mlb"]` — matches
  `league-config.md` keys.

## Core types

```python
Prob = float          # 0.0–1.0
Cents = int           # 1–99
LeagueId = Literal["nfl", "nba", "mlb"]
GameStatus = Literal["scheduled", "in_progress", "final", "postponed", "suspended"]
InjuryStatus = Literal["OUT", "DOUBTFUL", "QUESTIONABLE", "PROBABLE", "DAY_TO_DAY", "ACTIVE"]
Side = Literal["yes", "no"]

@dataclass
class TeamRef:
    league: LeagueId
    espn_abbr: str          # authoritative key into league-config alias map
    kalshi_abbr: str | None # None until verified against a live ticker
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
    period: int                    # quarter / inning number; 0 if not started
    period_half: Literal["top", "bottom"] | None  # MLB only, else None
    clock_seconds: int | None      # NFL/NBA game clock; None for MLB
    win_prob_home: Prob | None     # ESPN model; None if feed absent
    win_prob_source_ts: datetime | None
    start_time: datetime
    fetched_at: datetime

@dataclass
class InjuryEvent:
    league: LeagueId
    team: TeamRef
    espn_event_id: str | None   # None for pregame/team-level news
    player_id: str
    player_name: str
    position: str
    status: InjuryStatus
    source_ts: datetime | None
    fetched_at: datetime

@dataclass
class MarketRef:
    league: LeagueId
    series_ticker: str          # e.g. KXMLBGAME (verify at runtime)
    event_ticker: str
    market_ticker: str
    yes_team_kalshi_abbr: str   # which team YES resolves for
    title: str
    close_ts: datetime | None
    settlement_notes: str | None  # populated when terms were checked

@dataclass
class DepthLevel:
    price: Cents
    quantity: int

@dataclass
class OrderbookSnapshot:
    market: MarketRef
    yes_bid: Cents | None
    yes_ask: Cents | None       # DERIVED as 100 - no_bid when YES ask side empty
    no_bid: Cents | None
    no_ask: Cents | None
    yes_book: list[DepthLevel]  # asks available to a YES buyer (derived from NO bids too)
    no_book: list[DepthLevel]
    devigged_yes_prob: Prob | None  # None when book too empty to de-vig
    spread_cents: int | None
    fetched_at: datetime

@dataclass
class BookQuote:
    book_name: str
    home_prob: Prob             # de-vigged, this book alone
    fetched_at: datetime
    source_ts: datetime | None

@dataclass
class ConsensusOdds:
    league: LeagueId
    home: TeamRef
    away: TeamRef
    espn_event_id: str | None   # set once league-matching has resolved it
    book_count: int
    devigged_home_prob: Prob    # consensus (mean of de-vigged book probs)
    max_pairwise_disagreement: float  # max |prob_i - prob_j| across books
    books: list[BookQuote]
    fetched_at: datetime

@dataclass
class MatchResult:
    espn_event_id: str
    market: MarketRef | None    # None = no unambiguous match; NEVER a guess
    method: Literal["alias_exact", "alias_plus_start_time", "none"]
    ambiguous: bool             # True when >1 candidate survived tie-breaking
    candidates_considered: int

SignalType = Literal[
    "overreaction-candidate", "divergence-candidate",
    "injury-candidate", "garbage-time-candidate", "game-final",
]

@dataclass
class CandidateSignal:
    signal_type: SignalType
    league: LeagueId
    espn_event_id: str
    market_ticker: str | None
    payload: dict               # per-type shape documented in espn-data / skill specs
    emitted_at: datetime

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
    signal: CandidateSignal

@dataclass
class SizingResult:
    contracts: int              # 0 is a valid, final answer
    limit_price: Cents
    kelly_fraction_used: float | None   # None for flat-sized skills
    capped_by: list[str]        # names of every cap that bound, e.g. ["per_trade_cap"]
    est_fee_cents_total: int

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
class VaultNote:
    path: str                   # vault-relative, e.g. "02-trading-skills/x.md"
    frontmatter: dict
    body: str
    mtime: datetime

@dataclass
class VaultQuery:
    directory: str                       # vault-relative prefix
    frontmatter_filters: dict            # exact-match, e.g. {"status": "confirmed"}
    tag_filters: list[str]               # matches list-membership, e.g. market_conditions
```

## Cross-cutting rules restated (specs must not contradict these)

1. Orderbook truth only: prices come from the orderbook endpoint; Kalshi summary
   fields (`yes_ask` etc. on the market object) are frequently null/stale — never
   read them for trading decisions.
2. `best_yes_ask = 100 - best_no_bid` when the YES ask side is empty (and
   symmetrically for NO). The derivation lives in `kalshi-client` only.
3. No skill reads vault files from disk on a live trading cycle — all vault access
   through the `vault` skill's TTL cache.
4. Ambiguity → `None`, never a guess: league-matching, and any skill consuming it,
   must treat `market=None` / `ambiguous=True` as "do not trade this game."
5. Every numeric trading parameter lives in `risk-management`'s named parameter
   table or a skill note's frontmatter — never inline in another skill's logic.
6. All trading on `KALSHI_ENV=demo` until the Phase 3 final checkpoint explicitly
   changes it.
