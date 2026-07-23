"""skill-matcher skill. Spec: skills/skill-matcher/SKILL.md.

Deterministic, auditable matching of signals to confirmed trading skills.
Hard gates (status/family/signal-type/tags) then a weighted score whose
components decompose into reasons[]. No LLM calls, no vibes.

Crypto pivot: signals are CryptoSignals; condition tags derive from the
window phase and signal payload (previously per-event state). A skill note's
`status` must be `confirmed` to ever match — draft/retired skills are
structurally invisible here, which is the enforcement mechanism for
"don't trade unconfirmed rules."
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from kalshi_bots.skills.vault import Vault
from kalshi_bots.types import (
    CryptoSignal, OrderbookSnapshot, SkillMatch, VaultQuery,
)

log = logging.getLogger(__name__)

# Configuration (Category A tuning weights; sum to 1.0)
W_SIGNAL = 0.40
W_TAGS = 0.25
W_FRESH = 0.20
W_HIST = 0.15
LOW_SAMPLE = 20

# per skill note: worst acceptable input age at decision time (seconds)
DEFAULT_STALENESS_BOUND_S = 10  # streaming inputs: seconds, not minutes
HIGH_VOL_SIGMA = 1.0            # >=100% annualized tags high-volatility


class SkillMatcherError(Exception):
    pass


def derive_condition_tags(signal: CryptoSignal,
                          now: datetime | None = None) -> list[str]:
    """Tags the matcher intersects with a skill note's market_conditions.
    Crypto is always live (24/7); the window phase is the main condition."""
    tags = ["live"]
    if signal.phase is not None:
        tags.append(signal.phase)
    sigma = signal.payload.get("sigma")
    if isinstance(sigma, (int, float)) and sigma >= HIGH_VOL_SIGMA:
        tags.append("high-volatility")
    if signal.payload.get("thin_book"):
        tags.append("thin-book")
    return tags


class SkillMatcher:
    def __init__(self, vault: Vault):
        self.vault = vault
        self._last_confirmed_count: int | None = None

    def match(self, signal: CryptoSignal,
              orderbook: OrderbookSnapshot | None = None,
              now: datetime | None = None) -> list[SkillMatch]:
        now = now or datetime.now(timezone.utc)
        if signal.signal_type in ("window-open", "window-close", "phase-change"):
            return []  # lifecycle signals never match trading skills

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

        derived = derive_condition_tags(signal, now)
        out: list[SkillMatch] = []
        for note in notes:
            fm = note.frontmatter
            name = fm.get("skill", "")
            reasons = []

            # hard gates
            declared = fm.get("signal_types") or []
            if signal.signal_type not in declared:
                continue
            reasons.append(f"gate:signal_type {signal.signal_type} -> {name}")
            families = fm.get("families") or []
            if "all" not in families and signal.series_ticker not in families:
                continue
            reasons.append(f"gate:family {signal.series_ticker} in {families}")
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
            bound = fm.get("staleness_bound_s") or DEFAULT_STALENESS_BOUND_S
            ages = [(now - signal.emitted_at).total_seconds()]
            if orderbook is not None:
                ages.append((now - orderbook.fetched_at).total_seconds())
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
                f"s_fresh={s_fresh:.2f} (worst age {worst:.1f}s, bound {bound}s) x {W_FRESH}",
                f"s_hist={s_hist:.2f} (sample={sample}) x {W_HIST}",
            ]
            out.append(SkillMatch(skill_name=name, score=score,
                                  confidence_threshold=float(ct),
                                  passed=score >= ct, reasons=reasons))
        out.sort(key=lambda m: -m.score)
        return out
