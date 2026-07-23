"""trader agent. Prompt: vault 01-agents/trader/system-prompt.md.

The only component that places orders. Re-verifies every candidate with fresh
data (the monitor flags, it does not vouch), matches via skill-matcher, sizes
via risk-management, routes through Discord approval, executes limit orders,
records trade notes in the same cycle as the fill.

Re-verification is the pattern that survived the pivot: the sports trader
re-checked odds at decision time; this trader recomputes the FairValueEstimate
from fresh spot/sigma/book at decision time and never trusts a signal payload.
Every gate that fails is recorded per-condition so postmortems can audit it.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from kalshi_bots.skills.fair_value_model import evaluate, side_edges
from kalshi_bots.skills.kalshi_client import depth_within
from kalshi_bots.skills.risk_management import (
    DEPTH_COLLAPSE_FRACTION, ENTRY_PHASES, EXIT_EDGE_CENTS, MIN_DEPTH_WITHIN_5C,
    MIN_EDGE_CENTS, SIGMA_PLAUSIBLE_MAX, SIGMA_PLAUSIBLE_MIN,
)
from kalshi_bots.skills.skill_matcher import SkillMatcher
from kalshi_bots.skills.window_monitor import (
    WINDOW_S, parse_market_ticker, window_phase,
)
from kalshi_bots.types import (
    CryptoSignal, OrderbookSnapshot, OrderRequest, Settlement, SizingRequest,
    TradeCard, VaultQuery, WindowRef,
)

log = logging.getLogger(__name__)


class Trader:
    def __init__(self, vault, broker, risk, discord, env: str = "demo",
                 feed=None, book=None):
        self.vault = vault
        self.broker = broker           # KalshiClient or PaperBroker
        # demo/paper: draft skills may trade to accumulate demo_* stats toward
        # confirmation; live keeps confirmed-only (and sprint-5 re-guards it)
        self.skill_matcher = SkillMatcher(
            vault, allowed_statuses=("confirmed", "draft") if env == "demo"
            else ("confirmed",))
        self.risk = risk
        self.discord = discord
        self.env = env
        self.feed = feed               # CryptoPriceFeed | None
        self.book = book               # KalshiOrderBook | None
        self.open_trades: dict[str, dict] = {}    # client_order_id -> trade meta

    # --- restart recovery ---

    def reload_open_trades(self, settled: dict[str, Settlement] | None = None) -> dict[str, int]:
        """Repopulate open_trades from vault trade notes still marked
        status=open, so exit management resumes after an abrupt restart
        instead of silently going dark (open_trades is otherwise pure
        in-memory). `settled` is RiskManager.reconcile()'s
        last_reconcile_settled — tickers it discovered had settled while the
        process was down; those trade notes are closed out here (using the
        note's own recorded contracts/entry price, the same math postmortem
        uses) instead of being restored for exit management."""
        settled = settled or {}
        notes = self.vault.query(VaultQuery(directory="04-trade-history/trades",
                                            frontmatter_filters={"status": "open"}))
        restored = closed = 0
        for note in notes:
            fm = note.frontmatter
            coid = fm.get("client_order_id")
            ticker = fm.get("market_ticker")
            if not coid or not ticker:
                continue
            s = settled.get(ticker)
            if s is not None:
                contracts = fm.get("contracts", 0)
                cost = contracts * fm.get("entry_price_cents", 0) + fm.get("fee_cents", 0)
                won = s.result == fm.get("side")
                pnl = (contracts * 100 - cost) if won else -cost
                self._close_trade_note(note.path, "settled_while_down", None, pnl)
                closed += 1
                continue
            try:
                market = self.broker.get_market(ticker, family=fm.get("family", ""))
            except Exception as e:
                log.error("reload_open_trades: market fetch failed for %s: %s", ticker, e)
                continue
            opened_at = fm.get("opened_at")
            # rebuild the WindowRef from the ticker grammar so the near-close
            # exit sweep covers restored positions too (otherwise a restart
            # would orphan them until settlement)
            window = None
            try:
                series, closes_at = parse_market_ticker(ticker)
                window = WindowRef(
                    series_ticker=series,
                    event_ticker=ticker.rsplit("-", 1)[0], market_ticker=ticker,
                    opens_at=closes_at - timedelta(seconds=WINDOW_S),
                    closes_at=closes_at, strike=fm.get("strike"))
            except Exception as e:
                log.warning("reload: no window for %s (%s) — near-close exit "
                            "sweep will not cover it", ticker, e)
            self.open_trades[coid] = {
                "market_ticker": ticker, "skill": fm.get("skill"), "side": fm.get("side"),
                "contracts": fm.get("contracts", 0), "entry_price": fm.get("entry_price_cents"),
                "event_id": fm.get("event_id"), "family": fm.get("family"),
                "opened_at": datetime.fromisoformat(opened_at) if opened_at
                            else datetime.now(timezone.utc),
                "note": note.path, "market": market, "window": window,
            }
            restored += 1
        return {"restored": restored, "closed": closed}

    # --- decision-time data (fresh, never from the signal payload) ---

    def _book_snapshot(self, market_ticker: str) -> OrderbookSnapshot | None:
        """Freshest trustworthy book: the WS ladder when healthy, else a REST
        fetch. None when neither can be trusted — the caller declines."""
        if self.book is not None:
            snap = self.book.snapshot(market_ticker)
            if snap is not None:
                return snap
        try:
            market = self.broker.get_market(market_ticker, family="crypto")
            return self.broker.get_orderbook(market)
        except Exception as e:
            log.warning("orderbook unavailable for %s: %s", market_ticker, e)
            return None

    # --- signal handling ---

    def handle_signal(self, signal: CryptoSignal,
                      now: datetime | None = None) -> str:
        """Returns a disposition string (for logs/postmortem declined reasons)."""
        now = now or datetime.now(timezone.utc)
        if signal.signal_type != "fair-value-candidate":
            return "not-a-trade-signal"
        if signal.market_ticker is None:
            return "declined:unresolved_window"
        halted, reason = self.risk.halted()
        if halted:
            return f"declined:halted({reason})"
        window = signal.window
        if window is None or window.strike is None:
            return "declined:no_strike"

        # fresh inputs (fail closed on any gap)
        if self.feed is None:
            return "declined:no_feed"
        spot = self.feed.current_composite()
        if spot is None:
            return "declined:spot_unavailable"
        sigma = self.feed.realized_vol()
        if sigma is None:
            return "declined:sigma_unavailable"
        snapshot = self._book_snapshot(signal.market_ticker)
        if snapshot is None:
            return "declined:book_unavailable"
        market = snapshot.market

        matches = self.skill_matcher.match(signal, orderbook=snapshot)
        passing = [m for m in matches if m.passed]
        if not passing:
            return "declined:matcher_below_threshold"
        best = passing[0]

        if any(t["market_ticker"] == market.market_ticker
               and t["skill"] == best.skill_name
               for t in self.open_trades.values()):
            return "declined:position_exists"  # one position per market per skill

        verified, side, price, model_prob, conditions = self._verify_entry(
            best.skill_name, window, spot, sigma, snapshot, now)
        if not verified:
            failed = sorted(k for k, v in conditions.items() if v is False)
            return f"declined:entry_verification_failed({','.join(failed)})"

        depth = depth_within(snapshot, side, 2)
        sizing = self.risk.size(SizingRequest(
            skill_name=best.skill_name, market=market, side=side,
            entry_price=price, model_prob=model_prob,
            book_depth_at_entry=depth, signal=signal,
            event_id=window.event_ticker, is_live=True))
        if sizing.contracts == 0:
            return f"declined:sized_zero({','.join(sizing.capped_by)})"

        coid = f"kb-{uuid.uuid4().hex[:12]}"
        card = TradeCard(client_order_id=coid, skill_name=best.skill_name,
                         market=market, side=side, action="buy", sizing=sizing,
                         snapshot={**{k: str(v) for k, v in conditions.items()},
                                   "model_prob": model_prob,
                                   "sigma": sigma, "spot": spot.mid,
                                   "strike": window.strike,
                                   "matcher_score": round(best.score, 3),
                                   "signal_id": signal.payload.get("id")},
                         is_live=True)
        outcome = self.discord.send_trade_card(card)
        if outcome.decision != "approved":
            self.risk.cancel_intent(market.market_ticker, best.skill_name)
            return f"declined:approval_{outcome.decision}"

        try:
            order = self.broker.place_order(OrderRequest(
                market_ticker=market.market_ticker, side=side, action="buy",
                contracts=sizing.contracts, limit_price=price,
                client_order_id=coid))
        except Exception as e:
            self.risk.cancel_intent(market.market_ticker, best.skill_name)
            return f"declined:order_rejected({e})"
        if order.filled_contracts == 0:
            self.risk.cancel_intent(market.market_ticker, best.skill_name)
            return f"declined:unfilled({order.status})"

        fills = [f for f in self.broker.get_fills(market.market_ticker)
                 if f.order_id == order.order_id]
        fill = fills[-1] if fills else None
        if fill:
            self.risk.on_fill(fill, market, best.skill_name, window.event_ticker)
        fill_price = order.avg_fill_price or price
        edge_cents = model_prob * 100 - fill_price
        partial = " (partial)" if order.filled_contracts < sizing.contracts else ""
        self.discord.notify(
            f"ENTRY [{best.skill_name}] {side.upper()} {order.filled_contracts}x"
            f"{partial} {market.market_ticker} @ {fill_price}c "
            f"(fee {order.fee_cents}c, model_prob={model_prob:.2f}, "
            f"edge≈{edge_cents:.1f}c)", level="info")
        self._write_trade_note(coid, best.skill_name, market, side, order,
                               price, model_prob, conditions, signal, window,
                               sigma=sigma, spot=spot.mid)
        self.open_trades[coid] = {
            "market_ticker": market.market_ticker, "skill": best.skill_name,
            "side": side, "contracts": order.filled_contracts,
            "entry_price": order.avg_fill_price or price,
            "event_id": window.event_ticker, "family": signal.series_ticker,
            "opened_at": datetime.now(timezone.utc), "note": self._note_path(coid),
            "market": market, "window": window,
        }
        return f"traded:{order.filled_contracts}x@{order.avg_fill_price or price}"

    # --- entry verification (mechanical re-check of the skill note rules) ---

    def _verify_entry(self, skill: str, window: WindowRef, spot, sigma: float,
                      snapshot: OrderbookSnapshot, now: datetime):
        c: dict[str, bool] = {}

        if skill == "btc-15min-fair-value":
            est = evaluate(window, spot, snapshot, sigma, now=now)
            edges = side_edges(est, snapshot)
            # trade the side the model says is cheap; None edges lose the vote
            side = "yes" if (edges["yes"] if edges["yes"] is not None else -999) \
                >= (edges["no"] if edges["no"] is not None else -999) else "no"
            edge = edges[side]
            c["edge_ge_min"] = edge is not None and edge >= MIN_EDGE_CENTS
            c["phase_allowed"] = window_phase(now, window) in ENTRY_PHASES
            c["depth_both_sides"] = (
                depth_within(snapshot, "yes", 5) >= MIN_DEPTH_WITHIN_5C
                and depth_within(snapshot, "no", 5) >= MIN_DEPTH_WITHIN_5C)
            c["spot_healthy"] = spot.constituents_healthy >= 2
            c["sigma_plausible"] = SIGMA_PLAUSIBLE_MIN <= sigma <= SIGMA_PLAUSIBLE_MAX
            price = snapshot.yes_ask if side == "yes" else snapshot.no_ask
            c["ask_available"] = price is not None
            model_prob = (est.model_prob_up if side == "yes"
                          else est.model_prob_down)
            ok = all(c.values())
            return ok, side, price or 0, model_prob, c

        return False, "yes", 0, 0.0, {"unknown_skill": False}

    # --- exit management ---

    def manage_positions(self, now: datetime | None = None) -> list[str]:
        """Evaluate exit rules for every open trade. Exits are mechanical and
        never approval-gated."""
        now = now or datetime.now(timezone.utc)
        actions = []
        for coid, t in list(self.open_trades.items()):
            reason = self._exit_reason(t, now)
            if reason is None:
                continue
            market = t["market"]
            snapshot = self._book_snapshot(market.market_ticker)
            if snapshot is None:
                log.error("exit book unavailable for %s — will retry", coid)
                continue
            bid = snapshot.yes_bid if t["side"] == "yes" else snapshot.no_bid
            if bid is None:
                log.error("no bid to exit into for %s — will retry", coid)
                continue
            try:
                order = self.broker.place_order(OrderRequest(
                    market_ticker=market.market_ticker, side=t["side"],
                    action="sell", contracts=t["contracts"], limit_price=max(1, bid - 1),
                    # unique per attempt: a fixed f"{coid}-exit" id collided
                    # ("order_already_exists") on retry after an attempt that
                    # didn't fully fill, killing the orchestrator loop
                    client_order_id=f"{coid}-exit-{uuid.uuid4().hex[:8]}"))
            except Exception as e:
                log.error("exit order failed for %s: %s — will retry", coid, e)
                continue
            if order.filled_contracts > 0:
                fills = [f for f in self.broker.get_fills(market.market_ticker)
                         if f.order_id == order.order_id]
                if fills:
                    self.risk.on_exit(fills[-1], market, t["skill"])
                pnl = (order.avg_fill_price - t["entry_price"]) * order.filled_contracts
                self._close_trade_note(t["note"], reason, order.avg_fill_price, pnl)
                self.discord.notify(
                    f"EXIT [{t['skill']}] {market.market_ticker} {reason}: "
                    f"{order.filled_contracts}x @ {order.avg_fill_price}c", "info")
                self.open_trades.pop(coid, None)
                actions.append(f"exited:{coid}:{reason}")
        return actions

    def _exit_reason(self, t: dict, now: datetime) -> str | None:
        """Skill-note invalidation/exit rules, evaluated on fresh data.
        Ordering matters: the universal near-close rule fires first (no skill
        holds into settlement noise), then thesis/health rules."""
        window: WindowRef | None = t.get("window")
        if window is not None and window_phase(now, window) in ("near_close", "settled"):
            return "near_close_exit"
        if t.get("skill") != "btc-15min-fair-value" or window is None \
                or window.strike is None:
            return None  # only the near-close rule applies
        if self.feed is None:
            return None  # no model inputs: near-close will still catch it
        spot = self.feed.current_composite()
        sigma = self.feed.realized_vol()
        if spot is None or sigma is None:
            # skill note invalidation: constituents dropped / vol unavailable
            # -> never hold a model-driven position blind
            return "feed_loss"
        snapshot = self._book_snapshot(t["market_ticker"])
        if snapshot is None:
            return None  # nothing to exit into anyway; retry next tick
        yes_d = depth_within(snapshot, "yes", 5)
        no_d = depth_within(snapshot, "no", 5)
        if min(yes_d, no_d) < MIN_DEPTH_WITHIN_5C * DEPTH_COLLAPSE_FRACTION:
            return "depth_collapse"
        est = evaluate(window, spot, snapshot, sigma, now=now)
        edges = side_edges(est, snapshot)
        held, other = t["side"], ("no" if t["side"] == "yes" else "yes")
        if edges[other] is not None and edges[other] > 0:
            return "edge_inverted"   # model now on the market's other side
        if edges[held] is not None and edges[held] <= EXIT_EDGE_CENTS:
            return "edge_converged"  # thesis played out
        return None

    # --- trade notes ---

    def _note_path(self, coid: str) -> str:
        day = datetime.now(timezone.utc).date().isoformat()
        return f"04-trade-history/trades/{day}-{coid}.md"

    def _write_trade_note(self, coid, skill, market, side, order, signal_price,
                          model_prob, conditions, signal, window: WindowRef,
                          sigma: float | None = None, spot: float | None = None):
        path = self._note_path(coid)
        fm = {
            "client_order_id": coid,
            "event_id": window.event_ticker,
            "family": signal.series_ticker,
            "market_ticker": market.market_ticker,
            "skill": skill, "side": side,
            "contracts": order.filled_contracts,
            "entry_price_cents": order.avg_fill_price or signal_price,
            "signal_price_cents": signal_price,
            "fee_cents": order.fee_cents,
            "model_prob": model_prob,
            "sigma": sigma, "spot": spot,
            "strike": window.strike,
            "entry_conditions": {k: bool(v) for k, v in conditions.items()},
            "signal_id": signal.payload.get("id"),
            "status": "open", "realized_pnl_cents": None,
            "exit_deviation": False,
            "env": self.env,
            "opened_at": datetime.now(timezone.utc).isoformat(),
        }
        body = (f"# Trade {coid}\n\n{skill} {side} {order.filled_contracts}x "
                f"{market.market_ticker} @ {order.avg_fill_price}c "
                f"(signal price {signal_price}c)\n")
        self.vault.write_note(path, fm, body, caller="trader")

    def _close_trade_note(self, path, reason, exit_price, pnl):
        try:
            self.vault.update_frontmatter(path, {
                "status": "closed", "exit_reason": reason,
                "exit_price_cents": exit_price, "realized_pnl_cents": pnl,
            }, caller="trader")
        except Exception as e:
            log.error("failed to close trade note %s: %s", path, e)
