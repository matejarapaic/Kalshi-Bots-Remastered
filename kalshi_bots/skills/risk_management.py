"""risk-management skill. Spec: skills/risk-management/SKILL.md.

The single place money math lives. Every numeric trading parameter in the
system is named here (risk params below) — no other module may inline one.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from kalshi_bots.skills.kalshi_client import est_fee_cents
from kalshi_bots.types import (
    ExposureSummary, Fill, MarketRef, Settlement, SizingRequest, SizingResult,
)

ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# THE PARAMETER TABLE. Statuses: CONFIRMED = owner-approved; changing one
# requires owner sign-off by build rule.
# ---------------------------------------------------------------------------
# CONFIRMED 2026-07-17 (from the four confirmed skill notes)
BASE_KELLY_FRACTION = 0.5
SKILL_RISK_MULTIPLIER = {          # (live, pregame)
    "live-win-prob-overreaction": (1.0, 1.0),
    "sportsbook-kalshi-divergence": (1.0, 0.5),
    "injury-news-repricing-lag": (0.5, 0.5),
    "garbage-time-mispricing": None,  # FLAT sizing; Kelly forbidden at p->1
}
PER_TRADE_CAP_PCT = {
    "live-win-prob-overreaction": 5,
    "sportsbook-kalshi-divergence": 5,
    "injury-news-repricing-lag": 3,
    "garbage-time-mispricing": 5,
}
GARBAGE_TIME_AGGREGATE_CAP_PCT = 10
SKILL_MIN_DEPTH = {                # contracts within 2c of entry, per skill note
    "live-win-prob-overreaction": 200,
    "sportsbook-kalshi-divergence": 200,
    "injury-news-repricing-lag": 100,
    "garbage-time-mispricing": 300,
}
# CONFIRMED 2026-07-17 (Phase 2 checkpoint, "tighter variant")
TOTAL_EXPOSURE_CAP_PCT = 15
PER_GAME_EXPOSURE_CAP_PCT = 5
CORRELATION_SCALE_SAME_GAME = 0.5
DAILY_LOSS_HALT_PCT = 5
MAX_OPEN_POSITIONS = 6
DEPTH_CONSUMPTION_MAX = 0.25

INTENT_TTL_S = 180  # sizing intent reservation window (documented limitation)
LEDGER_PATH = "03-market-context/exposure-ledger.md"


class RiskError(Exception):
    pass


class RiskUnknownSkill(RiskError):
    pass


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

    def _game_cost(self, espn_event_id: str, teams: set[str]) -> int:
        total = 0
        for p in self._positions.values():
            if p.get("espn_event_id") == espn_event_id or \
                    (teams and teams & set(p.get("teams") or [])):
                total += p["cost_cents"]
        for i in self._intents.values():
            if i.get("espn_event_id") == espn_event_id or \
                    (teams and teams & set(i.get("teams") or [])):
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
        if mult is None:  # flat sizing (garbage-time)
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

        # (5) correlation scaling
        teams = {req.market.yes_team_kalshi_abbr}
        eid = req.espn_event_id or req.signal.espn_event_id
        if self._game_cost(eid, teams) > 0:
            fraction *= CORRELATION_SCALE_SAME_GAME
            capped_by.append("correlation_same_game")

        budget = int(fraction * bankroll)

        # (6) per-game cap
        game_room = int(PER_GAME_EXPOSURE_CAP_PCT / 100 * bankroll) - self._game_cost(eid, teams)
        if budget > game_room:
            budget = max(0, game_room)
            capped_by.append("per_game_cap")

        # (7) skill-aggregate caps
        if skill == "garbage-time-mispricing":
            agg_room = int(GARBAGE_TIME_AGGREGATE_CAP_PCT / 100 * bankroll) - self._skill_cost(skill)
            if budget > agg_room:
                budget = max(0, agg_room)
                capped_by.append("garbage_aggregate_cap")

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

        # (12) integer floor
        if contracts < 1:
            return zero(capped_by[-1] if capped_by else "no_room")

        cost = contracts * c + est_fee_cents(contracts, c)
        self._intents[f"{req.market.market_ticker}|{skill}"] = {
            "cost_cents": cost, "espn_event_id": eid, "skill": skill,
            "teams": sorted(teams), "expires": time.monotonic() + INTENT_TTL_S,
        }
        return SizingResult(contracts=int(contracts), limit_price=c,
                            kelly_fraction_used=kf_used, capped_by=capped_by,
                            est_fee_cents_total=est_fee_cents(int(contracts), c))

    def cancel_intent(self, market_ticker: str, skill: str) -> None:
        with self._lock:
            self._intents.pop(f"{market_ticker}|{skill}", None)

    def on_fill(self, fill: Fill, market: MarketRef, skill_name: str,
                espn_event_id: str = "") -> None:
        with self._lock:
            self._intents.pop(f"{market.market_ticker}|{skill_name}", None)
            p = self._positions.setdefault(market.market_ticker, {
                "skill": skill_name, "side": fill.side, "contracts": 0,
                "cost_cents": 0, "espn_event_id": espn_event_id,
                "teams": [market.yes_team_kalshi_abbr],
                "opened_at": fill.ts.isoformat(),
            })
            p["contracts"] += fill.contracts
            p["cost_cents"] += fill.contracts * fill.price + fill.taker_fee_cents
            self._persist()

    def on_exit(self, fill: Fill, market: MarketRef, skill_name: str) -> None:
        with self._lock:
            p = self._positions.get(market.market_ticker)
            if not p or p["contracts"] == 0:
                return
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
            by_game: dict[str, int] = {}
            by_skill: dict[str, int] = {}
            for p in self._positions.values():
                by_game[p.get("espn_event_id", "?")] = \
                    by_game.get(p.get("espn_event_id", "?"), 0) + p["cost_cents"]
                by_skill[p["skill"]] = by_skill.get(p["skill"], 0) + p["cost_cents"]
            return ExposureSummary(
                bankroll_cents=balance + self._position_cost(),
                open_cost_cents=sum(p["cost_cents"] for p in self._positions.values()),
                by_game=by_game, by_skill=by_skill,
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
        """Startup check: live Kalshi positions vs ledger. Mismatch -> halt."""
        with self._lock:
            live = {p.market_ticker: p.contracts for p in self.kalshi.get_positions()}
            ours = {t: p["contracts"] for t, p in self._positions.items()}
            if live != ours:
                self.set_halt(True, f"ledger/live mismatch: live={live} ledger={ours}",
                              caller="reconcile")
                return False
            return True
