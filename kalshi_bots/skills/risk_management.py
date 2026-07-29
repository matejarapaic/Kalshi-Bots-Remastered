"""risk-management skill. Spec: skills/risk-management/SKILL.md.

The single place money math lives. Every numeric trading parameter in the
system is named here (risk params below) — no other module may inline one.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from kalshi_bots.skills.kalshi_client import est_fee_cents
from kalshi_bots.types import (
    ExposureSummary, Fill, MarketRef, Settlement, SizingRequest, SizingResult,
)

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# THE PARAMETER TABLE. Statuses: CONFIRMED = owner-approved; PROPOSED = pivot
# defaults awaiting owner sign-off. Changing a CONFIRMED value requires owner
# sign-off by build rule.
# ---------------------------------------------------------------------------
# CONFIRMED 2026-07-17 (carried over from the prior build; the half-Kelly
# discipline is category-agnostic)
BASE_KELLY_FRACTION = 0.5
# CONFIRMED 2026-07-22 (owner override: authorized live without the intended
# 30+ settled-sample validation — demo liquidity was insufficient to gather
# it; values carried over unchanged from the crypto pivot defaults)
SKILL_RISK_MULTIPLIER = {          # (live, non_live); crypto trades 24/7 so
    "btc-15min-fair-value": (1.0, 1.0),        # only the live column applies
    "btc-15min-orderflow-imbalance": (1.0, 1.0),
    "btc-15min-vol-spike": (0.5, 0.5),
}
PER_TRADE_CAP_PCT = {
    "btc-15min-fair-value": 5,
    "btc-15min-orderflow-imbalance": 5,
    "btc-15min-vol-spike": 3,
}
SKILL_MIN_DEPTH = {                # contracts within 2c of entry, per skill note
    "btc-15min-fair-value": 100,
    "btc-15min-orderflow-imbalance": 100,
    "btc-15min-vol-spike": 100,
}
MAX_CONTRACTS_PER_WINDOW = 20      # CONFIRMED 2026-07-22: hard contract cap
                                   # per 15-min window
# btc-15min-fair-value entry/exit parameters (CONFIRMED 2026-07-22; mirrored in
# the vault skill note — the note is the human-readable contract, this is the
# single machine home for the numbers)
MIN_EDGE_CENTS = 4                 # model-vs-ask divergence required to enter
EXIT_EDGE_CENTS = 1                # edge below this = thesis played out, exit
SIGMA_PLAUSIBLE_MIN = 0.20         # outside this band the model is not to be
SIGMA_PLAUSIBLE_MAX = 2.00         # trusted (broken feed or unmodeled regime)
MIN_DEPTH_WITHIN_5C = 100          # contracts, EACH side, entry gate
DEPTH_COLLAPSE_FRACTION = 0.5      # exit when either side falls below
                                   # MIN_DEPTH_WITHIN_5C * this
ENTRY_PHASES = ("midpoint",)       # no entries in opening (strike/book still
                                   # settling) or near_close (gamma dominates)
# PROPOSED 2026-07-24: P1 quality gates from the first live-session postmortem
# (04-trade-history/postmortems/2026-07-24-KXBTC15M.md). That session's losers
# clustered on near-ATM coin-flips whose thin modeled edge did not survive
# round-trip taker fees. Both awaiting owner sign-off — validate over the next
# live sample before promoting to CONFIRMED.
ATM_MIN_SIGMA_DISTANCE = 0.5       # decline entries where the strike is within
                                   # this many settlement-distribution stddevs
                                   # of spot (model is a coin flip at the money)
ENTRY_EDGE_SLIPPAGE_CENTS = 1      # buffer added on top of round-trip taker
                                   # fees when deriving the fee-aware min edge
# PROPOSED 2026-07-24: universal hard risk backstop, independent of any
# skill's own thesis-invalidation exits — applies to every open position
# regardless of skill. Fires when the position's current mark-to-market
# value has fallen to this fraction of entry cost or worse (e.g. 50 -> exit
# once down 50% from entry). Awaiting owner sign-off.
STOP_LOSS_PCT = 50
# PROPOSED 2026-07-24: entry-sizing scale-down when the underlying spot is
# moving unusually fast right now (see CryptoPriceFeed.recent_move_pct) —
# a fast recent move raises the odds a computed edge is chasing a gap rather
# than confirming a stable mispricing. Only entries ever reach sizing at all
# (ENTRY_PHASES == midpoint-only), so this naturally only bites mid-window
# entries, matching the "still at midpoint" case it was written for. Awaiting
# owner sign-off.
VELOCITY_THRESHOLD_PCT = 0.004     # 0.4% move within the feed's short window
VELOCITY_SIZE_SCALE = 0.5          # fraction multiplier once threshold breached
# CONFIRMED 2026-07-17 (Phase 2 checkpoint, "tighter variant"); re-confirmed
# 2026-07-22 for the crypto market family, unchanged
TOTAL_EXPOSURE_CAP_PCT = 15
PER_EVENT_EXPOSURE_CAP_PCT = 5
CORRELATION_SCALE_SAME_EVENT = 0.5
DAILY_LOSS_HALT_PCT = 5
MAX_OPEN_POSITIONS = 6
DEPTH_CONSUMPTION_MAX = 0.25

INTENT_TTL_S = 180  # sizing intent reservation window (documented limitation)
LEDGER_PATH = "03-market-context/exposure-ledger.md"


class RiskError(Exception):
    pass


class RiskUnknownSkill(RiskError):
    pass


def required_edge_cents(entry_price_cents: int) -> int:
    """Fee-aware minimum modeled edge to enter: the larger of the flat
    MIN_EDGE_CENTS floor and round-trip taker fees plus a slippage buffer.

    Round-trip = entry taker fee + exit taker fee; est_fee_cents is per-side
    per the quadratic schedule (highest at the money — exactly where the
    2026-07-24 session's losers clustered), so a 4c 'edge' on a 50c coin flip
    that costs ~5c round-trip is correctly rejected as negative-EV. Uses the
    single-contract fee as the per-contract proxy (a slight, deliberate
    overestimate — the per-order ceil is shared across contracts)."""
    round_trip_fee = 2 * est_fee_cents(1, entry_price_cents)
    return max(MIN_EDGE_CENTS, round_trip_fee + ENTRY_EDGE_SLIPPAGE_CENTS)


def kelly_fraction(p: float, c: int) -> float:
    """Full-Kelly stake fraction for a binary contract at c cents, win prob p."""
    return (p * 100 - c) / (100 - c)


class RiskManager:
    def __init__(self, vault, kalshi_client):
        self.vault = vault
        self.kalshi = kalshi_client
        self._lock = threading.RLock()
        self._positions: dict[str, dict] = {}   # market_ticker -> position dict
        self._daily_pnl: dict[str, int] = {}    # ET date iso -> realized cents
        self._halted = False
        self._halt_reason: str | None = None
        self._intents: dict[str, dict] = {}     # ticker|skill -> intent
        # populated by reconcile(): ticker -> Settlement, for positions that
        # settled while the process was down and were self-healed out of the
        # ledger here (rather than halting) — trader consumes this to close
        # the matching trade notes on restart.
        self.last_reconcile_settled: dict[str, Settlement] = {}
        self._load()

    # --- persistence (via the vault skill; write-through, restart-safe) ---

    def _load(self):
        try:
            note = self.vault.read_note(LEDGER_PATH)
            fm = note.frontmatter
            self._positions = fm.get("positions") or {}
            self._daily_pnl = fm.get("daily_pnl") or {}
            self._halted = bool(fm.get("halted", False))
            self._halt_reason = fm.get("halt_reason")
        except Exception:
            pass  # fresh ledger

    def _persist(self):
        fm = {"positions": self._positions, "daily_pnl": self._daily_pnl,
              "halted": self._halted, "halt_reason": self._halt_reason,
              "updated": datetime.now(timezone.utc).isoformat()}
        lines = ["# Exposure Ledger", "", "| Market | Skill | Side | Contracts | Cost¢ |",
                 "|---|---|---|---|---|"]
        for t, p in self._positions.items():
            lines.append(f"| {t} | {p['skill']} | {p['side']} | {p['contracts']} | {p['cost_cents']} |")
        self.vault.write_note(LEDGER_PATH, fm, "\n".join(lines) + "\n", caller="system")

    # --- helpers ---

    @staticmethod
    def _et_today() -> str:
        return datetime.now(timezone.utc).astimezone(ET).date().isoformat()

    def _prune_intents(self):
        now = time.monotonic()
        self._intents = {k: v for k, v in self._intents.items() if v["expires"] > now}

    def _position_cost(self) -> int:
        """Cost of actual positions only — the bankroll basis. Intents are
        reservations; their cash is still in the balance."""
        return sum(p["cost_cents"] for p in self._positions.values())

    def _open_cost(self) -> int:
        """Committed cost = positions + unexpired sizing intents. The basis for
        all cap headroom checks (a reserved slot is a consumed slot)."""
        self._prune_intents()
        return (self._position_cost()
                + sum(i["cost_cents"] for i in self._intents.values()))

    def _event_cost(self, event_id: str, labels: set[str]) -> int:
        """Exposure already committed to the same event (window), matched by
        event id or overlapping market labels — the correlation basis."""
        total = 0
        for p in self._positions.values():
            if p.get("event_id") == event_id or \
                    (labels and labels & set(p.get("labels") or [])):
                total += p["cost_cents"]
        for i in self._intents.values():
            if i.get("event_id") == event_id or \
                    (labels and labels & set(i.get("labels") or [])):
                total += i["cost_cents"]
        return total

    def _skill_cost(self, skill: str) -> int:
        return (sum(p["cost_cents"] for p in self._positions.values()
                    if p["skill"] == skill)
                + sum(i["cost_cents"] for i in self._intents.values()
                      if i["skill"] == skill))

    def _daily_halted(self, bankroll: int) -> bool:
        pnl = self._daily_pnl.get(self._et_today(), 0)
        return pnl <= -(DAILY_LOSS_HALT_PCT / 100) * bankroll

    # --- public interface ---

    def size(self, req: SizingRequest) -> SizingResult:
        with self._lock:
            return self._size_locked(req)

    def _size_locked(self, req: SizingRequest) -> SizingResult:
        skill = req.skill_name
        if skill not in PER_TRADE_CAP_PCT:
            raise RiskUnknownSkill(f"{skill!r} not in risk parameter table — "
                                   "never a default multiplier")
        capped_by: list[str] = []
        c = req.entry_price
        try:
            balance = self.kalshi.get_balance()
        except Exception as e:
            raise RiskError(f"balance fetch failed: {e}") from e
        bankroll = balance + self._position_cost()  # caps don't loosen as cash -> positions

        def zero(reason: str) -> SizingResult:
            return SizingResult(contracts=0, limit_price=c, kelly_fraction_used=None,
                                capped_by=capped_by + [reason], est_fee_cents_total=0)

        # (1) fee-adjusted edge
        c_f = c + est_fee_cents(1, c)
        if req.model_prob * 100 <= c_f:
            return zero("no_edge")

        # (2)+(3) raw fraction and skill multiplier
        mult = SKILL_RISK_MULTIPLIER[skill]
        kf_used = None
        if mult is None:  # flat sizing (no crypto skill uses it yet; mechanism kept)
            fraction = PER_TRADE_CAP_PCT[skill] / 100
        else:
            f_star = kelly_fraction(req.model_prob, c)
            m = mult[0] if req.is_live else mult[1]
            fraction = f_star * BASE_KELLY_FRACTION * m
            kf_used = fraction

        # (4) per-trade cap
        cap = PER_TRADE_CAP_PCT[skill] / 100
        if mult is not None and fraction > cap:
            fraction = cap
            capped_by.append("per_trade_cap")

        # (5) velocity scaling: spot moving unusually fast right now, any
        # skill — a fast recent move raises the odds an edge is chasing a
        # gap rather than confirming a stable mispricing, so size smaller
        # into it regardless of what the skill's own Kelly math says.
        if req.recent_move_pct is not None and \
                abs(req.recent_move_pct) >= VELOCITY_THRESHOLD_PCT:
            fraction *= VELOCITY_SIZE_SCALE
            capped_by.append("velocity_scale")

        # (6) correlation scaling (same window/event)
        labels = {req.market.market_ticker}
        eid = req.event_id or (req.signal.window.event_ticker
                               if req.signal and req.signal.window else "")
        if self._event_cost(eid, labels) > 0:
            fraction *= CORRELATION_SCALE_SAME_EVENT
            capped_by.append("correlation_same_event")

        budget = int(fraction * bankroll)

        # (7) per-event cap
        event_room = int(PER_EVENT_EXPOSURE_CAP_PCT / 100 * bankroll) - self._event_cost(eid, labels)
        if budget > event_room:
            budget = max(0, event_room)
            capped_by.append("per_event_cap")

        # (8) total exposure cap
        total_room = int(TOTAL_EXPOSURE_CAP_PCT / 100 * bankroll) - self._open_cost()
        if budget > total_room:
            budget = max(0, total_room)
            capped_by.append("total_exposure_cap")

        # (9) daily-loss halt (incl. manual halt)
        if self._halted:
            return zero("halted")
        if self._daily_halted(bankroll):
            return zero("daily_loss_halt")

        # (10) max open positions
        self._prune_intents()
        if len(self._positions) + len(self._intents) >= MAX_OPEN_POSITIONS:
            return zero("max_open_positions")

        # (11) depth gate
        if req.book_depth_at_entry < SKILL_MIN_DEPTH[skill]:
            return zero("depth_min")
        contracts = budget // c_f
        depth_cap = int(DEPTH_CONSUMPTION_MAX * req.book_depth_at_entry)
        if contracts > depth_cap:
            contracts = depth_cap
            capped_by.append("depth_gate")

        # (12) per-window contract cap (draft-skill training wheels)
        if contracts > MAX_CONTRACTS_PER_WINDOW:
            contracts = MAX_CONTRACTS_PER_WINDOW
            capped_by.append("per_window_contract_cap")

        # (13) integer floor
        if contracts < 1:
            return zero(capped_by[-1] if capped_by else "no_room")

        cost = contracts * c + est_fee_cents(contracts, c)
        self._intents[f"{req.market.market_ticker}|{skill}"] = {
            "cost_cents": cost, "event_id": eid, "skill": skill,
            "labels": sorted(labels), "expires": time.monotonic() + INTENT_TTL_S,
        }
        return SizingResult(contracts=int(contracts), limit_price=c,
                            kelly_fraction_used=kf_used, capped_by=capped_by,
                            est_fee_cents_total=est_fee_cents(int(contracts), c))

    def cancel_intent(self, market_ticker: str, skill: str) -> None:
        with self._lock:
            self._intents.pop(f"{market_ticker}|{skill}", None)

    def on_fill(self, fill: Fill, market: MarketRef, skill_name: str,
                event_id: str = "") -> None:
        with self._lock:
            self._intents.pop(f"{market.market_ticker}|{skill_name}", None)
            p = self._positions.setdefault(market.market_ticker, {
                "skill": skill_name, "side": fill.side, "contracts": 0,
                "cost_cents": 0, "event_id": event_id,
                "labels": [market.market_ticker],
                "opened_at": fill.ts.isoformat(),
            })
            p["contracts"] += fill.contracts
            p["cost_cents"] += fill.contracts * fill.price + fill.taker_fee_cents
            self._persist()

    def on_exit(self, fill: Fill, market: MarketRef, skill_name: str) -> int | None:
        """Returns the fee-inclusive realized pnl for this exit fill (basis
        drawn from the ledger's recorded cost, which includes the entry fee),
        or None if there was no matching open position — callers should treat
        that as "nothing to record", not "zero pnl"."""
        with self._lock:
            p = self._positions.get(market.market_ticker)
            if not p or p["contracts"] == 0:
                return None
            portion = min(fill.contracts, p["contracts"])
            basis = round(p["cost_cents"] * portion / p["contracts"])
            proceeds = portion * fill.price - fill.taker_fee_cents
            pnl = proceeds - basis
            p["contracts"] -= portion
            p["cost_cents"] -= basis
            if p["contracts"] <= 0:
                self._positions.pop(market.market_ticker, None)
            day = self._et_today()
            self._daily_pnl[day] = self._daily_pnl.get(day, 0) + pnl
            self._persist()
            return pnl

    def on_settle(self, s: Settlement, market: MarketRef, skill_name: str) -> None:
        with self._lock:
            p = self._positions.pop(market.market_ticker, None)
            if not p:
                return
            pnl = s.revenue_cents - p["cost_cents"]
            day = self._et_today()
            self._daily_pnl[day] = self._daily_pnl.get(day, 0) + pnl
            self._persist()

    def exposure(self) -> ExposureSummary:
        with self._lock:
            try:
                balance = self.kalshi.get_balance()
            except Exception:
                balance = 0
            by_event: dict[str, int] = {}
            by_skill: dict[str, int] = {}
            for p in self._positions.values():
                by_event[p.get("event_id", "?")] = \
                    by_event.get(p.get("event_id", "?"), 0) + p["cost_cents"]
                by_skill[p["skill"]] = by_skill.get(p["skill"], 0) + p["cost_cents"]
            return ExposureSummary(
                bankroll_cents=balance + self._position_cost(),
                open_cost_cents=sum(p["cost_cents"] for p in self._positions.values()),
                by_event=by_event, by_skill=by_skill,
                open_positions=len(self._positions),
                daily_realized_pnl_cents=self._daily_pnl.get(self._et_today(), 0),
                halted=self._halted, halt_reason=self._halt_reason)

    def halted(self) -> tuple[bool, str | None]:
        return self._halted, self._halt_reason

    def set_halt(self, on: bool, reason: str, caller: str) -> None:
        with self._lock:
            self._halted = on
            self._halt_reason = f"{reason} (by {caller})" if on else None
            self._persist()

    def reconcile(self) -> bool:
        """Startup check: live Kalshi positions vs ledger.

        A ledger position missing from live is checked against Kalshi
        settlements first — the common restart case is "it settled while the
        process was down," which is self-healed here (position popped, P&L
        booked from our own recorded cost basis, *not* from the settlement's
        `revenue_cents`, since that field reflects the whole account's
        history on that market, not just this position) rather than halted.
        Only a genuinely unexplained difference — a live position the ledger
        doesn't know about, or a missing one with no settlement record —
        halts for a human to look at.
        """
        with self._lock:
            self.last_reconcile_settled = {}
            live = {p.market_ticker: p.contracts for p in self.kalshi.get_positions()}
            unexplained: dict[str, str] = {}
            for ticker, p in list(self._positions.items()):
                if ticker in live:
                    continue
                try:
                    settlements = self.kalshi.get_settlements(ticker)
                except Exception as e:
                    unexplained[ticker] = f"settlement fetch failed: {e}"
                    continue
                if not settlements:
                    unexplained[ticker] = "no live position, no settlement record"
                    continue
                s = settlements[0]
                won = s.result == p["side"]
                pnl = (p["contracts"] * 100 - p["cost_cents"]) if won else -p["cost_cents"]
                day = self._et_today()
                self._daily_pnl[day] = self._daily_pnl.get(day, 0) + pnl
                self._positions.pop(ticker, None)
                self.last_reconcile_settled[ticker] = s
                log.info("reconcile: %s settled %s while down (pnl=%dc) — ledger updated",
                         ticker, s.result, pnl)
            ours = {t: p["contracts"] for t, p in self._positions.items()}
            if ours != live or unexplained:
                reason = f"ledger/live mismatch: live={live} ledger={ours}"
                if unexplained:
                    reason += f" unexplained={unexplained}"
                self.set_halt(True, reason, caller="reconcile")
                return False
            # Clean: auto-clear a halt only if reconcile itself set it (a prior
            # mismatch that has now resolved) — a manual halt (caller=discord)
            # must survive restart until a human explicitly /resumes it.
            if self._halted and (self._halt_reason or "").endswith("(by reconcile)"):
                self.set_halt(False, "", caller="reconcile")
            else:
                self._persist()  # write back any self-healed positions
            return True
