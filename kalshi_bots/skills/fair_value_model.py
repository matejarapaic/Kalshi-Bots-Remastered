"""fair-value-model skill. Spec: skills/fair-value-model/SKILL.md.

Pure functions, no I/O: model probability that spot settles at/above the
strike from (spot, strike, time remaining, realized vol), and the evaluation
bundle the trader re-verifies against.

Model: log-normal with drift pinned to zero. p_up = Phi(ln(S/K) / (sigma*sqrt(tau)))
with tau in years (same 31,536,000s base as the vol estimator's annualization).

Why drift zero (future maintainers will second-guess this — don't, without
data): over tau = 15 minutes, any plausible annual drift mu contributes
mu*tau ≈ mu * 2.85e-5 to the log-return mean. Even a wildly bullish mu = 100%/yr
shifts the distribution by ~0.003% of spot, while a realistic sigma = 50%/yr
gives a stddev of sigma*sqrt(tau) ≈ 0.27% of spot — two orders of magnitude
larger. Drift is a rounding error at this horizon; estimating it would add
noise, not signal. The model's real failure mode is sigma (vol regime breaks),
which is why postmortems run the vol-was-right check (sprint-4) and entry
conditions gate sigma to a plausible band.

Settlement nuance: KXBTC15M resolves YES when the settlement value is
greater than OR EQUAL to the strike (verified live 2026-07-22), so the tie
belongs to "up" — with a continuous model the tie has measure zero, and the
degenerate branches below resolve ties as YES accordingly.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from kalshi_bots.types import (
    CompositeSpot, FairValueEstimate, OrderbookSnapshot, Prob, WindowRef,
)

SECONDS_PER_YEAR = 31_536_000  # must match crypto_price_feed's annualization


class FairValueError(Exception):
    pass


def _phi(x: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def fair_value_prob(spot: float, strike: float, time_remaining_s: float,
                    sigma_annual: float) -> Prob:
    """P(settlement >= strike) under drift-zero log-normal.

    Degenerate inputs collapse to the deterministic answer rather than
    raising: at tau<=0 or sigma<=0 the distribution is a point mass at spot
    (ties resolve YES per contract terms). Nonpositive spot/strike is a
    broken feed — that IS an error.
    """
    if spot <= 0 or strike <= 0:
        raise FairValueError(f"nonpositive spot/strike: {spot}/{strike}")
    if time_remaining_s <= 0 or sigma_annual <= 0:
        return 1.0 if spot >= strike else 0.0
    tau = time_remaining_s / SECONDS_PER_YEAR
    return _phi(math.log(spot / strike) / (sigma_annual * math.sqrt(tau)))


def evaluate(window: WindowRef, spot: CompositeSpot,
             book: OrderbookSnapshot | None, sigma: float,
             now: datetime | None = None) -> FairValueEstimate:
    """Model-vs-market bundle at `now`. Requires a strike (gate upstream);
    market fields are None when the book side needed is empty — consumers
    fail closed on None, they never substitute."""
    if window.strike is None:
        raise FairValueError(f"window {window.market_ticker} has no strike yet")
    now = now or datetime.now(timezone.utc)
    time_remaining_s = (window.closes_at - now).total_seconds()
    p_up = fair_value_prob(spot.mid, window.strike, time_remaining_s, sigma)
    ask = book.yes_ask if book is not None else None
    return FairValueEstimate(
        model_prob_up=p_up,
        model_prob_down=1.0 - p_up,
        market_ask_cents=ask,
        edge_cents=(p_up * 100 - ask) if ask is not None else None,
        sigma_used=sigma,
        spot_used=spot.mid,
        strike=window.strike,
        time_remaining_s=time_remaining_s,
        computed_at=now,
    )


def side_edges(est: FairValueEstimate,
               book: OrderbookSnapshot | None) -> dict[str, float | None]:
    """Signed edge (model cents minus that side's ask) for both sides.
    With a spread the two are not mirror images — each side pays its own
    crossing cost. None where the side has no ask to buy."""
    yes_ask = book.yes_ask if book is not None else None
    no_ask = book.no_ask if book is not None else None
    return {
        "yes": (est.model_prob_up * 100 - yes_ask) if yes_ask is not None else None,
        "no": (est.model_prob_down * 100 - no_ask) if no_ask is not None else None,
    }
