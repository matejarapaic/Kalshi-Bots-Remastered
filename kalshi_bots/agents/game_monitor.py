"""game-monitor agent. Prompt: vault 01-agents/game-monitor/system-prompt.md.

Watches live games and market state; flags candidates; never trades. Writes
active-game notes (frontmatter = machine state, Signals section = log) via
the vault skill.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict
from datetime import date, datetime, timezone

from kalshi_bots.timefmt import fmt_et
from kalshi_bots.types import CandidateSignal, GameState, MatchResult

log = logging.getLogger(__name__)


class GameMonitor:
    def __init__(self, vault, espn, matcher, kalshi=None, odds=None):
        self.vault = vault
        self.espn = espn
        self.matcher = matcher
        self.kalshi = kalshi   # read-only market data (never order endpoints)
        self.odds = odds       # optional; divergence candidates need it
        self._final_emitted: set[str] = set()

    # --- slate ---

    def build_slate(self, league: str, day: date) -> dict[str, MatchResult]:
        games = self.espn.get_scoreboard(league)
        results = self.matcher.resolve_slate(league, day, games)
        lines = [f"# Daily slate {day.isoformat()} — {league.upper()}", ""]
        for g in games:
            r = results[g.espn_event_id]
            status = (r.market.market_ticker if r.market
                      else f"UNMATCHED ({r.note or ('ambiguous' if r.ambiguous else 'no market')})")
            lines.append(f"- {g.away.espn_abbr} @ {g.home.espn_abbr} "
                         f"{fmt_et(g.start_time)} [{g.status}] -> {status}")
        self.vault.write_note(
            f"03-market-context/daily-slate/{day.isoformat()}-{league}.md",
            {"league": league, "date": day.isoformat(), "games": len(games),
             "matched": sum(1 for r in results.values() if r.market)},
            "\n".join(lines) + "\n", caller="game-monitor")
        return results

    # --- live cycle ---

    def poll_cycle(self, league: str, day: date) -> list[CandidateSignal]:
        """One polling pass over the league's slate. Returns candidate signals
        (the trader verifies; the monitor only flags)."""
        signals: list[CandidateSignal] = []
        try:
            games = self.espn.get_scoreboard(league)
        except Exception as e:
            log.error("scoreboard fetch failed (%s) — feed-stale cycle", e)
            return signals
        matches = self.matcher.resolve_slate(league, day, games)

        for game in games:
            match = matches.get(game.espn_event_id)
            if game.status == "final":
                if game.espn_event_id not in self._final_emitted:
                    self._final_emitted.add(game.espn_event_id)
                    self._write_game_note(game, match, final=True)
                    signals.append(self._signal("game-final", game, match, {}))
                continue
            if game.status != "in_progress":
                continue

            # enrich with win prob (summary endpoint)
            try:
                self.espn.get_game_detail(league, game.espn_event_id, game)
            except Exception as e:
                log.warning("summary fetch failed for %s: %s", game.espn_event_id, e)
            self.espn.record_poll(game)

            snapshot = None
            if self.kalshi and match and match.market:
                try:
                    snapshot = self.kalshi.get_orderbook(match.market)
                except Exception as e:
                    log.warning("orderbook fetch failed: %s", e)

            self._write_game_note(game, match, snapshot=snapshot)
            if match is None or match.market is None:
                continue  # unmatched games are watched, never signaled for trading

            signals.extend(self._detect(game, match, snapshot))
        return signals

    def _detect(self, game: GameState, match: MatchResult,
                snapshot) -> list[CandidateSignal]:
        out = []
        swing = self.espn.detect_swing(game.espn_event_id)
        if swing:
            payload = {"swing": asdict(swing)}
            if snapshot and snapshot.devigged_yes_prob is not None:
                payload["kalshi_devig_home"] = self._devig_home(snapshot, match)
            out.append(self._signal("overreaction-candidate", game, match, payload))

        decided = self.espn.detect_decided(game)
        if decided and snapshot is not None:
            leader_is_home = decided.leader == "home"
            # ask for the leader's side of the canonical (home-YES) market
            side = "yes" if leader_is_home else "no"
            ask = snapshot.yes_ask if side == "yes" else snapshot.no_ask
            if ask is not None and ask <= 95:
                out.append(self._signal("garbage-time-candidate", game, match, {
                    "decided": asdict(decided), "side": side,
                    "entry_price_cents": ask,
                }))

        try:
            injuries = self.espn.get_injuries(game.league, game.espn_event_id)
        except Exception:
            injuries = []
        for inj in self.espn.detect_injury_changes(game.league,
                                                   game.espn_event_id, injuries):
            if inj.status == "OUT":
                out.append(self._signal("injury-candidate", game, match,
                                        {"injury": asdict(inj)}))

        if self.odds is not None and snapshot is not None \
                and snapshot.devigged_yes_prob is not None:
            try:
                consensus = self.odds.get_consensus(
                    game.league, game.home, game.away, start_time=game.start_time)
                if consensus.book_count >= 3 and consensus.devigged_home_prob is not None:
                    gap = abs(consensus.devigged_home_prob
                              - self._devig_home(snapshot, match))
                    if gap >= 0.05:
                        out.append(self._signal("divergence-candidate", game, match, {
                            "consensus_home_prob": consensus.devigged_home_prob,
                            "book_count": consensus.book_count,
                            "max_pairwise_disagreement": consensus.max_pairwise_disagreement,
                        }))
            except Exception as e:
                log.warning("odds consensus failed: %s", e)
        return out

    @staticmethod
    def _devig_home(snapshot, match) -> float:
        """Canonical market is home-YES; devigged_yes_prob IS the home prob."""
        return snapshot.devigged_yes_prob

    def _signal(self, sig_type: str, game: GameState, match: MatchResult | None,
                payload: dict) -> CandidateSignal:
        sig = CandidateSignal(
            signal_type=sig_type, league=game.league,
            espn_event_id=game.espn_event_id,
            market_ticker=match.market.market_ticker if match and match.market else None,
            payload={**payload, "id": str(uuid.uuid4())[:8]},
            emitted_at=datetime.now(timezone.utc))
        self._log_signal(game, sig)
        return sig

    def _write_game_note(self, game: GameState, match: MatchResult | None,
                         snapshot=None, final: bool = False):
        path = f"03-market-context/active-games/{game.league}-{game.espn_event_id}.md"
        winner = None
        if final:
            winner_team = game.home if game.home_score > game.away_score else game.away
            cfg_row = None
            try:
                from kalshi_bots.league_config import parse_league_config
                cfg_row = parse_league_config(self.vault)[game.league].by_espn(
                    winner_team.espn_abbr)
            except Exception:
                pass
            winner = cfg_row.kalshi_abbr if cfg_row else winner_team.espn_abbr
        fm = {
            "league": game.league, "espn_event_id": game.espn_event_id,
            "status": game.status,
            "home_espn": game.home.espn_abbr, "away_espn": game.away.espn_abbr,
            "home_score": game.home_score, "away_score": game.away_score,
            "period": game.period, "win_prob_home": game.win_prob_home,
            "market_ticker": match.market.market_ticker if match and match.market else None,
            "yes_team_kalshi_abbr": match.market.yes_team_kalshi_abbr if match and match.market else None,
            "winner_kalshi_abbr": winner,
            "kalshi_devig": snapshot.devigged_yes_prob if snapshot else None,
            "spread_cents": snapshot.spread_cents if snapshot else None,
            "feed_stale": False,
            "updated": game.fetched_at.isoformat(),
        }
        try:
            existing = self.vault.read_note(path)
            body = existing.body
            merged = dict(existing.frontmatter)
            merged.update(fm)
            fm = merged
        except Exception:
            body = (f"# {game.away.espn_abbr} @ {game.home.espn_abbr} "
                    f"({game.league} {game.espn_event_id})\n\n## Signals\n")
        self.vault.write_note(path, fm, body, caller="game-monitor")

    def _log_signal(self, game: GameState, sig: CandidateSignal):
        path = f"03-market-context/active-games/{game.league}-{game.espn_event_id}.md"
        entry = {"id": sig.payload.get("id"), "type": sig.signal_type,
                 "market_ticker": sig.market_ticker,
                 "ts": sig.emitted_at.isoformat(),
                 "entry_price_cents": sig.payload.get("entry_price_cents"),
                 "side": sig.payload.get("side", "yes")}
        try:
            note = self.vault.read_note(path)
            body = note.body.rstrip("\n") + f"\n- SIGNAL {json.dumps(entry)}\n"
            self.vault.write_note(path, note.frontmatter, body, caller="game-monitor")
        except Exception:
            pass
