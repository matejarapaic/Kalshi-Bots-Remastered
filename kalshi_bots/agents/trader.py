"""trader agent. Prompt: vault 01-agents/trader/system-prompt.md.

The only component that places orders. Re-verifies every candidate with fresh
data (the monitor flags, it does not vouch), matches via skill-matcher, sizes
via risk-management, routes through Discord approval (manual_approve mode
until the owner answers the execution-mode question), executes limit orders,
records trade notes in the same cycle as the fill.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from kalshi_bots.skills.kalshi_client import depth_within, est_fee_cents
from kalshi_bots.skills.skill_matcher import SkillMatcher
from kalshi_bots.types import (
    CandidateSignal, GameState, OrderRequest, Settlement, SizingRequest,
    TradeCard, VaultQuery,
)

log = logging.getLogger(__name__)

ENDGAME_CUTOFF_NFL_NBA_S = 300   # skill notes: exit/no-entry inside 5:00
ENDGAME_CUTOFF_MLB_INNING = 8    # top of 8th onward
OVERREACTION_GAP_ENTRY = 0.08
OVERREACTION_GAP_EXIT = 0.03
OVERREACTION_GAP_STOP = 0.15
GARBAGE_MAX_ASK = 95
GARBAGE_MIN_NET_EDGE_CENTS = 1.5
MAX_SPREAD_OVERREACTION = 4
PERSISTENCE_CYCLES = 2


class Trader:
    def __init__(self, vault, broker, matcher, risk, discord, env: str = "demo"):
        self.vault = vault
        self.broker = broker           # KalshiClient or PaperBroker
        self.matcher = matcher
        self.skill_matcher = SkillMatcher(vault)
        self.risk = risk
        self.discord = discord
        self.env = env
        self._gap_history: dict[str, list] = {}   # persistence tracking
        self.open_trades: dict[str, dict] = {}    # market_ticker -> trade meta

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
                market = self.broker.get_market(ticker, league=fm.get("league", ""))
            except Exception as e:
                log.error("reload_open_trades: market fetch failed for %s: %s", ticker, e)
                continue
            opened_at = fm.get("opened_at")
            self.open_trades[coid] = {
                "market_ticker": ticker, "skill": fm.get("skill"), "side": fm.get("side"),
                "contracts": fm.get("contracts", 0), "entry_price": fm.get("entry_price_cents"),
                "espn_event_id": fm.get("espn_event_id"), "league": fm.get("league"),
                "opened_at": datetime.fromisoformat(opened_at) if opened_at
                            else datetime.now(timezone.utc),
                "note": note.path, "market": market,
            }
            restored += 1
        return {"restored": restored, "closed": closed}

    # --- signal handling ---

    def handle_signal(self, signal: CandidateSignal, game: GameState) -> str:
        """Returns a disposition string (for logs/postmortem declined reasons)."""
        if signal.signal_type == "game-final":
            return "not-a-trade-signal"
        if signal.market_ticker is None:
            return "declined:unmatched"
        halted, reason = self.risk.halted()
        if halted:
            return f"declined:halted({reason})"

        try:
            market = self.broker.get_market(signal.market_ticker, league=signal.league)
        except Exception as e:
            return f"declined:market_fetch_failed({e})"
        try:
            snapshot = self.broker.get_orderbook(market)
        except Exception as e:
            return f"declined:orderbook_failed({e})"

        matches = self.skill_matcher.match(signal, game, orderbook=snapshot)
        passing = [m for m in matches if m.passed]
        if not passing:
            return "declined:matcher_below_threshold"
        best = passing[0]

        if any(t["market_ticker"] == market.market_ticker
               and t["skill"] == best.skill_name
               for t in self.open_trades.values()):
            return "declined:position_exists"  # one position per market per skill

        verified, side, price, model_prob, conditions = self._verify_entry(
            best.skill_name, signal, game, snapshot)
        if not verified:
            failed = sorted(k for k, v in conditions.items() if v is False)
            return f"declined:entry_verification_failed({','.join(failed)})"

        depth = depth_within(snapshot, side, 2)
        sizing = self.risk.size(SizingRequest(
            skill_name=best.skill_name, market=market, side=side,
            entry_price=price, model_prob=model_prob,
            book_depth_at_entry=depth, signal=signal,
            espn_event_id=game.espn_event_id,
            is_live=(game.status == "in_progress")))
        if sizing.contracts == 0:
            return f"declined:sized_zero({','.join(sizing.capped_by)})"

        coid = f"kb-{uuid.uuid4().hex[:12]}"
        card = TradeCard(client_order_id=coid, skill_name=best.skill_name,
                         market=market, side=side, action="buy", sizing=sizing,
                         snapshot={**{k: str(v) for k, v in conditions.items()},
                                   "model_prob": model_prob,
                                   "matcher_score": round(best.score, 3),
                                   "signal_id": signal.payload.get("id")},
                         is_live=(game.status == "in_progress"))
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
            self.risk.on_fill(fill, market, best.skill_name, game.espn_event_id)
        fill_price = order.avg_fill_price or price
        edge_cents = model_prob * 100 - fill_price
        partial = " (partial)" if order.filled_contracts < sizing.contracts else ""
        self.discord.notify(
            f"ENTRY [{best.skill_name}] {side.upper()} {order.filled_contracts}x"
            f"{partial} {market.market_ticker} @ {fill_price}c "
            f"(fee {order.fee_cents}c, model_prob={model_prob:.2f}, "
            f"edge≈{edge_cents:.1f}c)", level="info")
        self._write_trade_note(coid, best.skill_name, market, side, order,
                               price, model_prob, conditions, signal, game)
        self.open_trades[coid] = {
            "market_ticker": market.market_ticker, "skill": best.skill_name,
            "side": side, "contracts": order.filled_contracts,
            "entry_price": order.avg_fill_price or price,
            "espn_event_id": game.espn_event_id, "league": game.league,
            "opened_at": datetime.now(timezone.utc), "note": self._note_path(coid),
            "market": market,
        }
        return f"traded:{order.filled_contracts}x@{order.avg_fill_price or price}"

    # --- entry verification (mechanical re-check of the skill note rules) ---

    def _verify_entry(self, skill: str, signal: CandidateSignal, game: GameState,
                      snapshot):
        c: dict[str, bool] = {}
        payload = signal.payload
        devig = snapshot.devigged_yes_prob

        if skill == "garbage-time-mispricing":
            side = payload.get("side", "yes")
            ask = snapshot.yes_ask if side == "yes" else snapshot.no_ask
            wp_home = game.win_prob_home
            model_prob = None
            if wp_home is not None:
                model_prob = wp_home if side == "yes" else 1 - wp_home
            c["decided_signal"] = payload.get("decided") is not None
            c["win_prob_98"] = model_prob is not None and model_prob >= 0.98
            c["ask_le_95"] = ask is not None and ask <= GARBAGE_MAX_ASK
            if ask is not None and model_prob is not None:
                net = model_prob * 100 - (ask + est_fee_cents(1, ask))
                c["net_fee_edge"] = net >= GARBAGE_MIN_NET_EDGE_CENTS
            else:
                c["net_fee_edge"] = False
            c["settlement_terms_known"] = bool(snapshot.market.settlement_notes)
            ok = all(c.values())
            return ok, side, ask or 0, model_prob or 0.0, c

        if skill == "live-win-prob-overreaction":
            wp_home = game.win_prob_home
            c["swing_present"] = payload.get("swing") is not None
            c["win_prob_available"] = wp_home is not None
            c["book_two_sided"] = devig is not None
            c["not_endgame"] = not self._is_endgame(game)
            c["spread_ok"] = (snapshot.spread_cents is not None
                              and 0 <= snapshot.spread_cents <= MAX_SPREAD_OVERREACTION)
            c["no_injury_news"] = payload.get("injury") is None
            gap_ok = False
            side, price, model_prob = "yes", 0, 0.0
            if wp_home is not None and devig is not None:
                gap = devig - wp_home  # canonical market is home-YES
                c["gap_ge_8"] = abs(gap) >= OVERREACTION_GAP_ENTRY
                hist = self._gap_history.setdefault(signal.market_ticker, [])
                hist.append(abs(gap) >= OVERREACTION_GAP_ENTRY)
                del hist[:-PERSISTENCE_CYCLES]
                c["persistence_2_cycles"] = (len(hist) >= PERSISTENCE_CYCLES
                                             and all(hist))
                # fade the overshoot: market over-prices home -> buy NO, and v.v.
                if gap > 0:
                    side, price, model_prob = "no", snapshot.no_ask or 0, 1 - wp_home
                else:
                    side, price, model_prob = "yes", snapshot.yes_ask or 0, wp_home
                gap_ok = c.get("gap_ge_8", False)
            else:
                c["gap_ge_8"] = False
                c["persistence_2_cycles"] = False
            ok = all(c.values()) and gap_ok and price > 0
            return ok, side, price, model_prob, c

        if skill == "sportsbook-kalshi-divergence":
            cons = payload.get("consensus_home_prob")
            c["consensus_present"] = cons is not None
            c["book_count_3"] = (payload.get("book_count") or 0) >= 3
            c["books_agree"] = (payload.get("max_pairwise_disagreement") or 1.0) <= 0.03
            c["book_two_sided"] = devig is not None
            c["spread_le_3"] = (snapshot.spread_cents is not None
                                and 0 <= snapshot.spread_cents <= 3)
            side, price, model_prob = "yes", 0, 0.0
            if cons is not None and devig is not None:
                gap = cons - devig
                c["gap_ge_5"] = abs(gap) >= 0.05
                hist = self._gap_history.setdefault("dvg:" + signal.market_ticker, [])
                hist.append(abs(gap) >= 0.05)
                del hist[:-PERSISTENCE_CYCLES]
                c["persistence_2_cycles"] = (len(hist) >= PERSISTENCE_CYCLES
                                             and all(hist))
                if gap > 0:   # books say home is worth more -> buy YES (home)
                    side, price, model_prob = "yes", snapshot.yes_ask or 0, cons
                else:
                    side, price, model_prob = "no", snapshot.no_ask or 0, 1 - cons
            else:
                c["gap_ge_5"] = False
                c["persistence_2_cycles"] = False
            ok = all(c.values()) and price > 0
            return ok, side, price, model_prob, c

        if skill == "injury-news-repricing-lag":
            inj = payload.get("injury") or {}
            c["status_out"] = inj.get("status") == "OUT"
            c["material"] = self._is_material(game.league, inj)
            age_ok = False
            if signal.emitted_at:
                age_ok = (datetime.now(timezone.utc)
                          - signal.emitted_at).total_seconds() <= 600
            c["within_10min"] = age_ok
            c["books_moved_kalshi_lagged"] = bool(payload.get("books_moved"))
            c["book_two_sided"] = devig is not None
            # direction: fade the injured player's team
            injured_home = inj.get("team", {}).get("espn_abbr") == game.home.espn_abbr
            side = "no" if injured_home else "yes"
            price = snapshot.no_ask if side == "no" else snapshot.yes_ask
            model_prob = payload.get("post_news_consensus") or 0.0
            c["target_prob_known"] = model_prob > 0
            ok = all(c.values()) and (price or 0) > 0
            return ok, side, price or 0, model_prob, c

        return False, "yes", 0, 0.0, {"unknown_skill": False}

    @staticmethod
    def _is_material(league: str, inj: dict) -> bool:
        pos = (inj.get("position") or "").upper()
        if league == "nfl":
            return pos == "QB"
        if league == "mlb":
            return pos == "SP"
        if league == "nba":
            return bool(inj.get("top2_minutes"))  # monitor-provided flag
        return False

    @staticmethod
    def _is_endgame(game: GameState) -> bool:
        if game.league in ("nfl", "nba"):
            return game.period >= 4 and (game.clock_seconds or 9999) <= ENDGAME_CUTOFF_NFL_NBA_S
        return game.period >= ENDGAME_CUTOFF_MLB_INNING

    # --- exit management ---

    def manage_positions(self, games: dict[str, GameState]) -> list[str]:
        """Evaluate invalidation rules for every open trade. Exits are
        mechanical and never approval-gated."""
        actions = []
        for coid, t in list(self.open_trades.items()):
            game = games.get(t["espn_event_id"])
            reason = self._exit_reason(t, game)
            if reason is None:
                continue
            market = t["market"]
            try:
                snapshot = self.broker.get_orderbook(market)
            except Exception as e:
                log.error("exit orderbook fetch failed: %s", e)
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

    def _exit_reason(self, t: dict, game: GameState | None) -> str | None:
        skill = t["skill"]
        if game is None:
            return None
        if skill == "garbage-time-mispricing":
            wp_home = game.win_prob_home
            if wp_home is not None:
                held_prob = wp_home if t["side"] == "yes" else 1 - wp_home
                if held_prob < 0.93:
                    return "comeback_stop"
            if game.status == "final":
                return None  # hold to settlement (default exit is the game ending)
            return None
        if skill == "live-win-prob-overreaction":
            wp_home = game.win_prob_home
            if wp_home is None:
                return "feed_loss"
            if self._is_endgame(game):
                return "endgame_cutoff"
            try:
                snapshot = self.broker.get_orderbook(t["market"])
            except Exception:
                return None
            devig = snapshot.devigged_yes_prob
            if devig is None:
                return None
            gap = abs(devig - wp_home)
            if gap <= OVERREACTION_GAP_EXIT:
                return "convergence_take_profit"
            if gap >= OVERREACTION_GAP_STOP:
                return "gap_widened_stop"
            age = (datetime.now(timezone.utc) - t["opened_at"]).total_seconds()
            if age > 1200:
                return "time_stop"
            return None
        if skill == "sportsbook-kalshi-divergence":
            if self._is_endgame(game) and game.status == "in_progress":
                return "endgame_cutoff"
            return None  # convergence checks need consensus; orchestrator supplies
        if skill == "injury-news-repricing-lag":
            age = (datetime.now(timezone.utc) - t["opened_at"]).total_seconds()
            if age > 1800:
                return "hard_time_stop"
            return None
        return None

    # --- trade notes ---

    def _note_path(self, coid: str) -> str:
        day = datetime.now(timezone.utc).date().isoformat()
        return f"04-trade-history/trades/{day}-{coid}.md"

    def _write_trade_note(self, coid, skill, market, side, order, signal_price,
                          model_prob, conditions, signal, game):
        path = self._note_path(coid)
        fm = {
            "client_order_id": coid, "espn_event_id": game.espn_event_id,
            "league": game.league, "market_ticker": market.market_ticker,
            "skill": skill, "side": side,
            "contracts": order.filled_contracts,
            "entry_price_cents": order.avg_fill_price or signal_price,
            "signal_price_cents": signal_price,
            "fee_cents": order.fee_cents,
            "model_prob": model_prob,
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
