"""skill-matcher skill. Spec: skills/skill-matcher/SKILL.md.

Deterministic, auditable matching of signals to confirmed trading skills.
Hard gates (status/league/signal-type/tags) then a weighted score whose
components decompose into reasons[]. No LLM calls, no vibes.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from kalshi_bots.skills.vault import Vault
from kalshi_bots.types import (
    CandidateSignal, ConsensusOdds, GameState, OrderbookSnapshot, SkillMatch,
    VaultQuery,
)

log = logging.getLogger(__name__)

# Configuration (Category A tuning weights; sum to 1.0)
W_SIGNAL = 0.40
W_TAGS = 0.25
W_FRESH = 0.20
W_HIST = 0.15
LOW_SAMPLE = 20

SIGNAL_SKILL_MAP = {
    "overreaction-candidate": "live-win-prob-overreaction",
    "divergence-candidate": "sportsbook-kalshi-divergence",
    "injury-candidate": "injury-news-repricing-lag",
    "garbage-time-candidate": "garbage-time-mispricing",
}

STALENESS_BOUND_S = {  # per skill note
    "live-win-prob-overreaction": 90,
    "sportsbook-kalshi-divergence": 60,
    "injury-news-repricing-lag": 60,
    "garbage-time-mispricing": 60,
}

BLOWOUT_MARGIN = {"nba": 15, "nfl": 17, "mlb": 5}


class SkillMatcherError(Exception):
    pass


def derive_condition_tags(game: GameState, signal: CandidateSignal,
                          now: datetime | None = None) -> list[str]:
    now = now or datetime.now(timezone.utc)
    tags = []
    if game.status == "in_progress":
        tags.append("live")
    elif game.status == "scheduled" and (game.start_time - now).total_seconds() <= 3600:
        tags.append("pregame")
    if game.league in ("nfl", "nba"):
        if game.period >= 4 and (game.clock_seconds or 9999) <= 300:
            tags.append("endgame")
    elif game.league == "mlb" and game.period >= 8:
        tags.append("endgame")
    if abs(game.home_score - game.away_score) >= BLOWOUT_MARGIN[game.league]:
        tags.append("blowout")
    swing = signal.payload.get("swing")
    if swing is not None:
        tags.append("momentum-swing")
        magnitude = swing.get("magnitude") if isinstance(swing, dict) else swing.magnitude
        if magnitude >= 0.10:
            tags.append("high-volatility")
    if signal.payload.get("injury") is not None:
        tags.append("news-event")
    return tags


class SkillMatcher:
    def __init__(self, vault: Vault):
        self.vault = vault
        self._last_confirmed_count: int | None = None

    def match(self, signal: CandidateSignal, game: GameState,
              orderbook: OrderbookSnapshot | None = None,
              consensus: ConsensusOdds | None = None,
              now: datetime | None = None) -> list[SkillMatch]:
        now = now or datetime.now(timezone.utc)
        target_skill = SIGNAL_SKILL_MAP.get(signal.signal_type)
        if target_skill is None:  # game-final etc. never matches trading skills
            return []

        try:
            notes = self.vault.query(VaultQuery(
                directory="02-trading-skills",
                frontmatter_filters={"status": "confirmed"}))
        except Exception as e:
            raise SkillMatcherError(f"vault unavailable: {e}") from e

        if self._last_confirmed_count is not None and \
                len(notes) < self._last_confirmed_count:
            log.error("confirmed skill count dropped %d -> %d — a skill vanished "
                      "from the library (incident, not a quiet day)",
                      self._last_confirmed_count, len(notes))
        self._last_confirmed_count = len(notes)

        derived = derive_condition_tags(game, signal, now)
        out: list[SkillMatch] = []
        for note in notes:
            fm = note.frontmatter
            name = fm.get("skill", "")
            reasons = []

            # hard gates
            declared = fm.get("signal_types") or (
                [signal.signal_type] if name == target_skill else [])
            if signal.signal_type not in declared:
                continue
            reasons.append(f"gate:signal_type {signal.signal_type} -> {name}")
            sports = fm.get("sports") or []
            if "all" not in sports and game.league not in sports:
                continue
            reasons.append(f"gate:league {game.league} in {sports}")
            conditions = fm.get("market_conditions") or []
            overlap = sorted(set(conditions) & set(derived))
            if not overlap:
                continue
            reasons.append(f"gate:tags overlap {overlap}")

            ct = fm.get("confidence_threshold")
            if not isinstance(ct, (int, float)) or not 0 <= ct <= 1:
                log.error("skill %s confidence_threshold invalid (%r) — excluded, "
                          "never clamped", name, ct)
                continue

            # scoring components
            s_signal = 1.0
            s_tags = len(overlap) / len(conditions) if conditions else 0.0
            bound = STALENESS_BOUND_S.get(name, 60)
            ages = [(now - game.fetched_at).total_seconds()]
            if orderbook is not None:
                ages.append((now - orderbook.fetched_at).total_seconds())
            if consensus is not None:
                ages.append((now - consensus.fetched_at).total_seconds())
            worst = max(ages)
            if worst <= bound:
                s_fresh = 1.0
            elif worst >= 2 * bound:
                s_fresh = 0.0
            else:
                s_fresh = 1.0 - (worst - bound) / bound
            sample = fm.get("demo_sample_size") or fm.get("sample_size") or 0
            win_rate = fm.get("demo_win_rate") if fm.get("demo_sample_size") \
                else fm.get("win_rate")
            if not sample or sample < LOW_SAMPLE or win_rate is None:
                s_hist = 0.5
            else:
                clamped = min(max(win_rate, 0.2), 0.8)
                s_hist = (clamped - 0.2) / 0.6

            score = (W_SIGNAL * s_signal + W_TAGS * s_tags
                     + W_FRESH * s_fresh + W_HIST * s_hist)
            reasons += [
                f"s_signal=1.00 x {W_SIGNAL}",
                f"s_tags={s_tags:.2f} ({len(overlap)}/{len(conditions)} tags: "
                f"{', '.join(overlap)}) x {W_TAGS}",
                f"s_fresh={s_fresh:.2f} (worst age {worst:.0f}s, bound {bound}s) x {W_FRESH}",
                f"s_hist={s_hist:.2f} (sample={sample}) x {W_HIST}",
            ]
            out.append(SkillMatch(skill_name=name, score=score,
                                  confidence_threshold=float(ct),
                                  passed=score >= ct, reasons=reasons))
        out.sort(key=lambda m: -m.score)
        return out
