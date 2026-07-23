"""postmortem skill. Spec: skills/postmortem/SKILL.md.

Triggered per settled 15-minute window (~96/day per family). Audits trades
against their recorded entry-condition snapshots, computes declined-candidate
counterfactuals plus the crypto counterfactual dimensions (model-was-right,
vol-was-right, constituent-drift), and appends to a DAILY aggregate note —
one file per family per UTC day, never one per window (Obsidian degrades at
~10K files/folder; 96/day reaches that in weeks).

Stats discipline: this module remains the sole writer of skill-note
win_rate/sample_size (env-labeled demo_* vs prod fields), but run() no longer
writes them — it RETURNS per-skill outcomes and the analyst flushes them in
batches via update_skill_stats() (write amplification at 96 windows/day).
Windows with constituent drift are excluded from aggregate learning.

Counterfactual caveat (carried from v1, still true): declined entries are held
to settlement rather than replaying invalidation rules against the intra-window
log — the 30s log granularity cannot support a faithful replay. Stated in the
note lines.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone

from kalshi_bots.skills.kalshi_client import est_fee_cents
from kalshi_bots.types import PostmortemReport, VaultQuery

log = logging.getLogger(__name__)

REVIEW_MIN_SAMPLE = 20   # CONFIRMED 2026-07-17: threshold-review flag floor
REVIEW_DELTA = 0.10      # CONFIRMED 2026-07-17
VOL_RATIO_BAND = (0.5, 2.0)  # PROPOSED 2026-07-22: outside -> vol-was-wrong flag
SECONDS_PER_YEAR = 31_536_000
CF_CONTRACTS = 100       # normalized counterfactual size (carried from v1)


class PostmortemError(Exception):
    pass


class SettlementMismatch(PostmortemError):
    pass


def _parse_log_lines(body: str) -> list[dict]:
    out = []
    for line in body.splitlines():
        if not line.startswith("- LOG "):
            continue
        try:
            out.append(json.loads(line[len("- LOG "):]))
        except json.JSONDecodeError:
            continue
    return out


def window_realized_vol(log_lines: list[dict]) -> float | None:
    """Annualized realized vol from the window's spot sample log (~30s
    cadence, so noisy — indicative, not authoritative). Same sqrt(dt)
    normalization and annual base as the live estimator."""
    pts = []
    for entry in log_lines:
        spot, ts = entry.get("spot"), entry.get("ts")
        if spot is None or ts is None:
            continue
        try:
            pts.append((datetime.fromisoformat(ts).timestamp(), float(spot)))
        except (ValueError, TypeError):
            continue
    if len(pts) < 5:
        return None
    rets = []
    for (t0, p0), (t1, p1) in zip(pts, pts[1:]):
        dt = t1 - t0
        if dt <= 0 or p0 <= 0 or p1 <= 0:
            continue
        rets.append(math.log(p1 / p0) / math.sqrt(dt))
    if len(rets) < 4:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    return math.sqrt(var) * math.sqrt(SECONDS_PER_YEAR)


def _cell(v) -> str:
    return "—" if v is None else str(v)


def _hit_mark(model_right: bool | None) -> str:
    return "—" if model_right is None else ("✓" if model_right else "✗")


def _orders_table(audits: list[dict], declined: list[dict]) -> str:
    """One row per order this window produced — a real fill or a declined
    candidate — so a window's activity is scannable without reading prose.
    Real trades and held-to-settlement counterfactuals share one table; the
    Type column is the only thing distinguishing real P&L from hypothetical."""
    header = ("| Type | Order | Skill | Side | Entry¢ | Contracts | Result "
              "| P&L¢ | Slippage¢ | Model | Flags |")
    sep = "|---|---|---|---|---|---|---|---|---|---|---|"
    rows = [header, sep]
    for a in audits:
        name = a["trade"].rsplit("/", 1)[-1].removesuffix(".md")
        flag = "⚠ ENTRY VIOLATION" if a["entry_conditions_failed"] else ""
        rows.append(
            f"| trade | [[{name}]] | {_cell(a['skill'])} | {_cell(a['side'])} "
            f"| {_cell(a['entry_price_cents'])} | {_cell(a['contracts'])} "
            f"| {_cell(a['exit_reason'])} | {_cell(a['pnl_cents'])} "
            f"| {_cell(a['slippage_cents'])} | {_hit_mark(a['model_right'])} "
            f"| {flag} |")
    for d in declined:
        sig = d["signal"]
        rows.append(
            f"| declined | {_cell(sig.get('id'))} | — | {_cell(sig.get('side'))} "
            f"| {_cell(sig.get('entry_price_cents'))} | {CF_CONTRACTS} (cf) "
            f"| held to settlement | {_cell(d['counterfactual_pnl_cents'])} "
            f"| — | {_hit_mark(d['model_right'])} | |")
    return "\n".join(rows)


class Postmortem:
    def __init__(self, vault, kalshi_client, discord_bot=None, env: str = "demo"):
        self.vault = vault
        self.kalshi = kalshi_client
        self.discord = discord_bot
        self.env = env

    @staticmethod
    def _daily_path(family: str, closes_at: datetime | None = None) -> str:
        day = (closes_at or datetime.now(timezone.utc)).date().isoformat()
        return f"04-trade-history/postmortems/{day}-{family}.md"

    @staticmethod
    def _market_ticker(event_id: str) -> str:
        return f"{event_id}-{event_id[-2:]}"  # grammar: MM duplicates close minute

    # --- the per-window audit ---

    def run(self, family: str, event_id: str,
            market_result: str | None = None,
            expiration_value: float | None = None,
            closes_at: datetime | None = None,
            ) -> tuple[PostmortemReport, dict[str, list[dict]]]:
        """Audit one settled window. `market_result` ("yes"/"no"/"void") and
        `expiration_value` come from the analyst's settlement poll; None means
        the market hasn't finalized (report goes out as pending).

        Returns (report, outcomes_by_skill) — the caller batches the outcomes
        into update_skill_stats(); run() itself never touches skill stats.
        """
        note_path = self._daily_path(family, closes_at)
        market_ticker = self._market_ticker(event_id)

        # idempotency: a window already in the daily aggregate is done
        daily_fm, daily_body = {}, None
        try:
            existing = self.vault.read_note(note_path)
            daily_fm, daily_body = dict(existing.frontmatter), existing.body
            if event_id in (daily_fm.get("settled_events") or []):
                return (self._replay_report(family, event_id, note_path), {})
        except Exception:
            pass

        window_note = None
        try:
            window_note = self.vault.read_note(
                f"03-market-context/active-windows/{market_ticker}.md")
        except Exception:
            log.warning("no active-window note for %s (RECORD GAP)", market_ticker)

        trades = self.vault.query(VaultQuery(
            directory="04-trade-history/trades",
            frontmatter_filters={"event_id": event_id}))

        settlement_status = "settled"
        if market_result == "void":
            settlement_status = "voided"
        elif market_result not in ("yes", "no"):
            settlement_status = "pending"

        # cross-check: settlement direction implied by expiration vs strike
        # must agree with Kalshi's result (internal-consistency guard; a
        # BRTI-stream cross-check is future work)
        mismatch_details = []
        strike = window_note.frontmatter.get("strike") if window_note else None
        if (settlement_status == "settled" and strike is not None
                and expiration_value is not None):
            implied = "yes" if expiration_value >= strike else "no"
            if implied != market_result:
                settlement_status = "mismatch"
                mismatch_details.append(
                    f"{market_ticker}: Kalshi settled {market_result}, but "
                    f"expiration {expiration_value} vs strike {strike} "
                    f"implies {implied}")

        # --- crypto counterfactual inputs from the window log ---
        log_lines = _parse_log_lines(window_note.body) if window_note else []
        realized_vol = window_realized_vol(log_lines)
        constituent_drift = any(
            e.get("healthy") is not None and e.get("total")
            and e["healthy"] < e["total"] for e in log_lines)

        # --- per-trade audit ---
        entry_violations = exit_deviations = realized = 0
        model_hits = 0
        sigmas_used = []
        audits = []
        outcomes_by_skill: dict[str, list[dict]] = {}
        for t in trades:
            fm = t.frontmatter
            conditions = fm.get("entry_conditions") or {}
            failed = sorted(k for k, v in conditions.items() if v is False)
            if failed:
                entry_violations += 1
            if fm.get("exit_deviation"):
                exit_deviations += 1
            pnl = fm.get("realized_pnl_cents")
            if pnl is None and settlement_status in ("settled", "mismatch"):
                contracts = fm.get("contracts", 0)
                cost = contracts * fm.get("entry_price_cents", 0) + fm.get("fee_cents", 0)
                won = market_result == fm.get("side")
                pnl = (contracts * 100 - cost) if won else -cost
                self.vault.update_frontmatter(t.path, {
                    "status": "closed", "exit_reason": "held_to_settlement",
                    "realized_pnl_cents": pnl}, caller="analyst")
                # keep the in-memory copy in sync — audits.append below reads
                # fm directly, and update_frontmatter only wrote to disk
                fm["status"] = "closed"
                fm["exit_reason"] = "held_to_settlement"
                fm["realized_pnl_cents"] = pnl
            # model-was-right: did the entered side match the settled direction?
            model_right = None
            if market_result in ("yes", "no"):
                model_right = fm.get("side") == market_result
                if model_right:
                    model_hits += 1
            if fm.get("sigma") is not None:
                sigmas_used.append(fm["sigma"])
            slippage = None
            if fm.get("signal_price_cents") is not None and fm.get("entry_price_cents") is not None:
                slippage = fm["entry_price_cents"] - fm["signal_price_cents"]
            realized += pnl or 0
            audits.append({
                "trade": t.path, "skill": fm.get("skill"),
                "side": fm.get("side"), "contracts": fm.get("contracts"),
                "entry_price_cents": fm.get("entry_price_cents"),
                "exit_reason": fm.get("exit_reason"),
                "entry_conditions_failed": failed,
                "exit_deviation": bool(fm.get("exit_deviation")),
                "slippage_cents": slippage, "pnl_cents": pnl,
                "model_right": model_right,
                "env": fm.get("env", self.env),
            })
            if pnl is not None and fm.get("skill"):
                outcomes_by_skill.setdefault(fm["skill"], []).append({
                    "pnl_cents": pnl, "entry_price_cents": fm.get("entry_price_cents"),
                    "excluded": constituent_drift,
                    "event_id": event_id,
                })

        # vol-was-right: window realized vol vs the sigma the trades used
        vol_ratio = None
        vol_flag = ""
        if realized_vol and sigmas_used:
            mean_sigma = sum(sigmas_used) / len(sigmas_used)
            if mean_sigma > 0:
                vol_ratio = realized_vol / mean_sigma
                if not (VOL_RATIO_BAND[0] <= vol_ratio <= VOL_RATIO_BAND[1]):
                    vol_flag = (f"⚠ VOL-WAS-WRONG: realized {realized_vol:.2f} vs "
                                f"sigma_used {mean_sigma:.2f} (ratio {vol_ratio:.2f})")

        # --- declined-candidate counterfactuals (held to settlement) ---
        declined = []
        cf_pnl = 0
        if window_note:
            traded_signals = {t.frontmatter.get("signal_id") for t in trades}
            for line in window_note.body.splitlines():
                if not line.startswith("- SIGNAL "):
                    continue
                try:
                    sig = json.loads(line[len("- SIGNAL "):])
                except json.JSONDecodeError:
                    continue
                if sig.get("id") in traded_signals or \
                        sig.get("type") != "fair-value-candidate":
                    continue
                price = sig.get("entry_price_cents")
                side = sig.get("side", "yes")
                cf = None
                model_right = market_result == side if market_result in ("yes", "no") else None
                if price is not None and market_result in ("yes", "no"):
                    cost = CF_CONTRACTS * price + est_fee_cents(CF_CONTRACTS, price)
                    cf = (CF_CONTRACTS * 100 - cost) if market_result == side else -cost
                    cf_pnl += cf
                declined.append({"signal": sig, "model_right": model_right,
                                 "counterfactual_pnl_cents": cf})

        # --- append to the daily aggregate note ---
        threshold_flags = [vol_flag] if vol_flag else []
        section = [f"## {event_id} — {market_result or 'pending'}"]
        narrative = self._narrative(market_result, len(trades), model_hits,
                                    realized_vol, sigmas_used, vol_ratio,
                                    constituent_drift, realized, cf_pnl,
                                    len(declined))
        if narrative:
            section.append(narrative)
        if mismatch_details:
            section += ["**⚠ SETTLEMENT MISMATCH**"] + mismatch_details
        if vol_flag:
            section.append(vol_flag)
        if constituent_drift:
            section.append("constituent drift in window — excluded from "
                           "aggregate learning")
        meta = (f"strike={strike} expiration={expiration_value} "
                f"realized_vol={realized_vol and round(realized_vol, 3)} "
                f"trades={len(trades)} pnl={realized}c declined={len(declined)} "
                f"cf={cf_pnl}c")
        section.append(meta)
        if audits or declined:
            section.append(_orders_table(audits, declined))
        else:
            section.append("_watched, nothing traded — that is data_")

        if daily_body is None:
            daily_body = f"# Postmortems {family}\n"
            daily_fm = {"family": family, "env": self.env,
                        "date": (closes_at or datetime.now(timezone.utc)).date().isoformat(),
                        "windows": 0, "trades": 0, "realized_pnl_cents": 0,
                        "counterfactual_pnl_cents": 0, "settled_events": []}
        daily_fm["windows"] = (daily_fm.get("windows") or 0) + 1
        daily_fm["trades"] = (daily_fm.get("trades") or 0) + len(trades)
        daily_fm["realized_pnl_cents"] = (daily_fm.get("realized_pnl_cents") or 0) + realized
        daily_fm["counterfactual_pnl_cents"] = \
            (daily_fm.get("counterfactual_pnl_cents") or 0) + cf_pnl
        daily_fm.setdefault("settled_events", []).append(event_id)
        daily_body = daily_body.rstrip("\n") + "\n\n" + "\n".join(section) + "\n"
        self.vault.write_note(note_path, daily_fm, daily_body, caller="analyst")

        if settlement_status == "mismatch":
            if self.discord:
                self.discord.notify("SETTLEMENT MISMATCH: " + "; ".join(mismatch_details),
                                    level="critical")
            raise SettlementMismatch("; ".join(mismatch_details))

        report = PostmortemReport(
            family=family, event_id=event_id, trades_audited=len(trades),
            entry_violations=entry_violations, exit_deviations=exit_deviations,
            declined_candidates=len(declined), counterfactual_pnl_cents=cf_pnl,
            realized_pnl_cents=realized, settlement_status=settlement_status,
            threshold_flags=threshold_flags, note_path=note_path,
            model_direction_hits=model_hits, vol_ratio=vol_ratio,
            constituent_drift=constituent_drift)
        return report, outcomes_by_skill

    @staticmethod
    def _narrative(result, n_trades, model_hits, realized_vol, sigmas_used,
                   vol_ratio, drift, realized, cf_pnl, n_declined) -> str:
        """Deterministic 2-4 sentence commentary, heavy on model-vs-market and
        vol regime (templated — no LLM in the trading loop, ever)."""
        parts = []
        if n_trades:
            parts.append(f"Traded {n_trades} position(s); the model's entry "
                         f"direction matched settlement on {model_hits}/{n_trades} "
                         f"(per-window this is coin-flippy — judge it in the "
                         f"aggregate). Net {realized}c realized.")
        elif n_declined:
            parts.append(f"No entries; {n_declined} candidate(s) declined for "
                         f"a held-to-settlement counterfactual of {cf_pnl}c.")
        else:
            parts.append("Watched, nothing flagged — divergence never reached "
                         "the entry threshold.")
        if vol_ratio is not None:
            mean_sigma = sum(sigmas_used) / len(sigmas_used)
            regime = ("in line with" if 0.5 <= vol_ratio <= 2.0 else
                      "far above" if vol_ratio > 2.0 else "far below")
            parts.append(f"Window realized vol {realized_vol:.0%} came in "
                         f"{regime} the {mean_sigma:.0%} the model priced — "
                         f"vol input quality is this skill's #1 failure mode.")
        if drift:
            parts.append("A spot-feed constituent degraded mid-window; this "
                         "window is excluded from aggregate learning.")
        return " ".join(parts)

    # --- batched stats (sole writer of skill win_rate/sample_size) ---

    def update_skill_stats(self, outcomes_by_skill: dict[str, list[dict]]) -> list[str]:
        """Apply a BATCH of trade outcomes to skill-note frontmatter — called
        by the analyst every rollup, never per window (write amplification).
        Outcomes flagged excluded (constituent drift) don't count. Returns
        threshold-review flags."""
        flags = []
        for skill, rows in outcomes_by_skill.items():
            rows = [r for r in rows if not r.get("excluded")]
            if not rows:
                continue
            path = f"02-trading-skills/{skill}.md"
            try:
                note = self.vault.read_note(path)
            except Exception:
                continue
            prefix = "demo_" if self.env == "demo" else ""
            old_n = note.frontmatter.get(f"{prefix}sample_size") or 0
            old_wr = note.frontmatter.get(f"{prefix}win_rate") or 0.0
            wins = sum(1 for r in rows if (r["pnl_cents"] or 0) > 0)
            new_n = old_n + len(rows)
            new_wr = round((old_wr * old_n + wins) / new_n, 4) if new_n else 0.0
            self.vault.update_frontmatter(
                path, {f"{prefix}sample_size": new_n, f"{prefix}win_rate": new_wr},
                caller="analyst")
            if new_n >= REVIEW_MIN_SAMPLE:
                entries = [r.get("entry_price_cents") for r in rows
                           if r.get("entry_price_cents")]
                if entries:
                    breakeven = sum(entries) / len(entries) / 100
                    if abs(new_wr - breakeven) >= REVIEW_DELTA:
                        flags.append(
                            f"⚠ THRESHOLD REVIEW {skill}: win_rate {new_wr:.2f} vs "
                            f"breakeven ~{breakeven:.2f} at n={new_n}")
        return flags

    @staticmethod
    def _replay_report(family, event_id, note_path) -> PostmortemReport:
        return PostmortemReport(
            family=family, event_id=event_id, trades_audited=0,
            entry_violations=0, exit_deviations=0, declined_candidates=0,
            counterfactual_pnl_cents=0, realized_pnl_cents=0,
            settlement_status="settled", threshold_flags=[],
            note_path=note_path)
