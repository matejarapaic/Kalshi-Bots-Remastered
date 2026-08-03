# fair-value-model

**Trigger:** any component needs the model probability that the active window settles up, or the model-vs-market edge.

## What this is for

The system's reference truth. At the 15-minute horizon there is no external "sharper source" — no consensus feed, no model stream about the same question — so the trading edge must be *computed*, not observed. This skill turns (spot, strike, time remaining, realized vol) into a settlement probability and a signed edge against the book. It is a pure function library: no I/O, no state, no network — everything it needs arrives as arguments, which is what makes the trader's decision-time re-verification cheap and the tests exact.

## Model

Log-normal with **drift pinned to zero**:

```
p_up = Phi( ln(S/K) / (sigma * sqrt(tau)) ),   tau = time_remaining_s / 31_536_000
```

**Why drift zero** (future maintainers will second-guess this — don't, without data): over tau = 15 minutes, an annual drift `mu` contributes `mu × 2.85e-5` to the mean log-return. Even a wildly bullish `mu` = 100%/yr shifts the distribution by ~0.003% of spot, while a realistic `sigma` = 50%/yr gives a stddev of `sigma·sqrt(tau)` ≈ 0.27% of spot — **two orders of magnitude larger**. Drift is a rounding error at this horizon; estimating it would add noise, not signal. The model's real failure mode is `sigma` (vol regime breaks) — hence the postmortem vol-was-right check (sprint-4) and the entry-condition sigma band.

The annualization base (31,536,000s) is shared with `crypto-price-feed`'s vol estimator — the two must never diverge or every probability silently rescales.

**Settlement nuance:** KXBTC15M resolves YES on settlement ≥ strike (verified live 2026-07-22), so ties belong to "up." Under the continuous model a tie has measure zero; the degenerate branches resolve it explicitly.

## Interface

```python
def fair_value_prob(spot: float, strike: float, time_remaining_s: float,
                    sigma_annual: float) -> Prob
def evaluate(window: WindowRef, spot: CompositeSpot,
             book: OrderbookSnapshot | None, sigma: float,
             now: datetime | None = None) -> FairValueEstimate
def side_edges(est: FairValueEstimate,
               book: OrderbookSnapshot | None) -> dict[str, float | None]
def moneyness_sigmas(est: FairValueEstimate) -> float
```

Exceptions: `FairValueError` — raised only for genuinely broken inputs (nonpositive spot/strike, missing strike). Degenerate-but-meaningful inputs never raise (Behavior 2).

## Behavior

1. `fair_value_prob` is exact math: `ln(S/K)=0 → 0.5` exactly, multiplicative symmetry `p(S·r,K) + p(S/r,K) = 1`, more time or more vol pulls toward 0.5.
2. **Degenerate inputs collapse, they don't raise:** `tau ≤ 0` or `sigma ≤ 0` is a point mass at spot — returns 1.0 if `spot ≥ strike` (tie → YES) else 0.0. Nonpositive spot/strike is a broken feed and *does* raise.
3. `evaluate` requires `window.strike` (raises without — callers gate upstream) and produces the full `FairValueEstimate` bundle; with no book, `market_ask_cents`/`edge_cents` are `None` — consumers fail closed on `None`, they never substitute a guess.
4. `side_edges` prices each side against **its own ask** (`model_prob_side × 100 − side_ask`): with a spread the two edges are not mirror images — each side pays its own crossing cost; with zero spread they mirror exactly. A missing ask (one-sided book) yields `None` for that side.
5. Who calls what: the window-monitor agent calls `evaluate`+`side_edges` to *flag* candidates (edge ≥ `MIN_EDGE_CENTS`, throttled); the trader recomputes both from fresh inputs at decision time and never trusts the flag's payload; exits recompute again per tick. Same functions, three call sites, zero shared state.
6. `moneyness_sigmas` returns `|ln(spot/strike)| / (sigma·sqrt(tau))` — the magnitude of the z-score that feeds `p_up` (`p_up = Phi(z)`), i.e. how far the strike sits from spot in standard deviations of the settlement distribution. `0.0` == at-the-money (a coin flip); larger == real directional conviction; `+inf` for a degenerate (settled/zero-vol) distribution, which is deterministic rather than a coin flip. The trader gates near-ATM entries on this against `risk-management.ATM_MIN_SIGMA_DISTANCE` (2026-07-24 postmortem: the losers clustered a few dollars from the strike, where the drift-zero model is most fragile). This skill only computes the distance — the threshold and the decision live in `risk-management`.

## Configuration
| Parameter | Value | Notes |
|---|---|---|
| `SECONDS_PER_YEAR` | 31_536_000 | must equal crypto-price-feed's base |

All *trading* thresholds that consume this model (`MIN_EDGE_CENTS`, `EXIT_EDGE_CENTS`, `SIGMA_PLAUSIBLE_*`, `ENTRY_PHASES`, `ATM_MIN_SIGMA_DISTANCE`, depth gates) live in `risk-management`'s table, not here — this skill computes, it never decides.

## Edge cases
- **Sub-second `tau`:** the formula stays finite to arbitrarily small positive tau; probabilities saturate toward 0/1 naturally. The near-close no-entry rule exists because the *book* misbehaves there, not the math.
- **Spot exactly at strike late in the window:** p hovers at 0.5 with huge gamma — edges flap sign tick to tick. The entry-phase gate, the candidate cooldown, and the `moneyness_sigmas` ATM guard keep this from churning signals or trading coin-flips.
- **Sigma from a different window than tau:** callers may pass any sigma window (default 900s); mixing a 60s sigma with a 900s horizon is a modeling *choice* (vol-spike skill territory), not an error — the estimate records `sigma_used` so postmortems can attribute it.

## Dependencies
None (pure; stdlib `math.erf`). Consumed by: window-monitor agent (candidate flags), trader (entry verification + exits), postmortem (model-was-right check, sprint-4).

## Testing requirements
- Hand-computed values: at-the-money = 0.5 exactly; an off-strike case checked against an independently computed `Phi`; multiplicative symmetry to 1e-12.
- Monotonicity: time→0.5, vol→0.5 orderings.
- Degenerate collapses (tau≤0, sigma≤0) including the tie-goes-up branch; broken-feed raises.
- `evaluate` bundle fields incl. `None` edge without a book; missing-strike raise.
- Edge-sign symmetry at zero spread; asymmetry under vig; `None` for empty sides.

## New types
`FairValueEstimate` (CONTRACTS.md, sprint-2 placeholder, live from sprint-3).
