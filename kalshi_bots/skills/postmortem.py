"""postmortem skill. Spec: skills/postmortem/SKILL.md.

Triggered on game-final. Audits trades, computes declined-candidate
counterfactuals, updates skill stats (sole writer), writes the postmortem note.
Batch context: reads may bypass the cache, writes go through the vault skill.

Spec deviation (flagged 2026-07-17): v1 counterfactuals hold declined entries
to settlement instead of replaying each skill's invalidation rules against the
intra-game price log — the log granularity recorded by game-monitor v1 is not
sufficient for a faithful replay. The assumption is stated in each note.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from kalshi_bots.skills.kalshi_client import est_fee_cents
from kalshi_bots.types import PostmortemReport, VaultQuery

log = logging.getLogger(__name__)

SETTLEMENT_WAIT_MAX_MIN = 60
REVIEW_MIN_SAMPLE = 20
REVIEW_DELTA = 0.10


class PostmortemError(Exception):
    pass


class SettlementMismatch(PostmortemError):
    pass


class Postmortem:
    def __init__(self, vault, kalshi_client, discord_bot=None, env: str = "demo"):
        self.vault = vault
        self.kalshi = kalshi_client
        self.discord = discord_bot
        self.env = env

    def _note_path(self, league: str, eid: str) -> str:
        day = datetime.now(timezone.utc).date().isoformat()
        return f"04-trade-history/postmortems/{day}-{league}-{eid}.md"

    def run(self, league: str, espn_event_id: str) -> PostmortemReport:
        note_path = self._note_path(league, espn_event_id)
        # idempotency
        try:
            existing = self.vault.read_note(note_path)
            if existing.frontmatter.get("settlement_status") == "settled":
                return self._report_from_note(existing, note_path)
        except Exception:
            pass

        game_note = None
        try:
            game_note = self.vault.read_note(
                f"03-market-context/active-games/{league}-{espn_event_id}.md")
        except Exception:
            log.warning("no active-game note for %s-%s (RECORD GAP)",
                        league, espn_event_id)

        trades = self.vault.query(VaultQuery(
            directory="04-trade-history/trades",
            frontmatter_filters={"espn_event_id": espn_event_id}))

        # --- settlement truth + cross-check ---
        settlement_status = "settled"
        mismatch_details = []
        settled_results: dict[str, str] = {}
        tickers = {t.frontmatter.get("market_ticker") for t in trades}
        if game_note and game_note.frontmatter.get("market_ticker"):
            tickers.add(game_note.frontmatter["market_ticker"])
        tickers.discard(None)
        for ticker in tickers:
            try:
                settlements = self.kalshi.get_settlements(ticker)
            except Exception:
                settlements = []
            if not settlements:
                if any(t.frontmatter.get("status") != "closed" for t in trades
                       if t.frontmatter.get("market_ticker") == ticker):
                    settlement_status = "pending"
                continue
            s = settlements[0]
            settled_results[ticker] = s.result
            if s.result == "void":
                settlement_status = "voided"
            elif game_note is not None:
                fm = game_note.frontmatter
                yes_team = fm.get("yes_team_kalshi_abbr")
                winner = fm.get("winner_kalshi_abbr")
                if yes_team and winner and ticker == fm.get("market_ticker"):
                    expected = "yes" if winner == yes_team else "no"
                    if s.result != expected:
                        settlement_status = "mismatch"
                        mismatch_details.append(
                            f"{ticker}: Kalshi settled {s.result}, ESPN implies {expected}")

        # --- per-trade audit ---
        entry_violations = 0
        exit_deviations = 0
        realized = 0
        audits = []
        for t in trades:
            fm = t.frontmatter
            conditions = fm.get("entry_conditions") or {}
            failed = sorted(k for k, v in conditions.items() if v is False)
            if failed:
                entry_violations += 1
            if fm.get("exit_deviation"):
                exit_deviations += 1
            pnl = fm.get("realized_pnl_cents")
            if pnl is None and fm.get("market_ticker") in settled_results:
                res = settled_results[fm["market_ticker"]]
                contracts = fm.get("contracts", 0)
                cost = contracts * fm.get("entry_price_cents", 0) + fm.get("fee_cents", 0)
                won = (res == fm.get("side"))
                pnl = (contracts * 100 - cost) if won else -cost
            realized += pnl or 0
            slippage = None
            if fm.get("signal_price_cents") is not None and fm.get("entry_price_cents") is not None:
                slippage = fm["entry_price_cents"] - fm["signal_price_cents"]
            audits.append({
                "trade": t.path, "skill": fm.get("skill"),
                "entry_conditions_failed": failed,
                "exit_deviation": bool(fm.get("exit_deviation")),
                "slippage_cents": slippage, "pnl_cents": pnl,
                "env": fm.get("env", self.env),
            })

        # --- declined-candidate counterfactuals ---
        declined = []
        cf_pnl = 0
        if game_note:
            traded_signals = {t.frontmatter.get("signal_id") for t in trades}
            for line in game_note.body.splitlines():
                if not line.startswith("- SIGNAL "):
                    continue
                try:
                    sig = json.loads(line[len("- SIGNAL "):])
                except json.JSONDecodeError:
                    continue
                if sig.get("id") in traded_signals or sig.get("type") == "game-final":
                    continue
                price = sig.get("entry_price_cents")
                side = sig.get("side", "yes")
                ticker = sig.get("market_ticker")
                result = settled_results.get(ticker)
                cf = None
                if price is not None and result in ("yes", "no"):
                    contracts = 100  # normalized counterfactual size
                    cost = contracts * price + est_fee_cents(contracts, price)
                    cf = (contracts * 100 - cost) if result == side else -cost
                    cf_pnl += cf
                declined.append({"signal": sig, "declined_reason": sig.get("declined_reason"),
                                 "counterfactual_pnl_cents": cf,
                                 "assumption": "held to settlement, fill at recorded "
                                               "price, 100 contracts, no market impact"})

        # --- skill stats update (sole writer; env-labeled fields) ---
        threshold_flags = []
        settled_by_skill: dict[str, list] = {}
        for a in audits:
            if a["pnl_cents"] is not None and a["skill"]:
                settled_by_skill.setdefault(a["skill"], []).append(a)
        for skill, rows in settled_by_skill.items():
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
                entry_prices = [t.frontmatter.get("entry_price_cents") for t in trades
                                if t.frontmatter.get("skill") == skill
                                and t.frontmatter.get("entry_price_cents")]
                if entry_prices:
                    breakeven = sum(entry_prices) / len(entry_prices) / 100
                    if abs(new_wr - breakeven) >= REVIEW_DELTA:
                        threshold_flags.append(
                            f"⚠ THRESHOLD REVIEW {skill}: win_rate {new_wr:.2f} vs "
                            f"breakeven ~{breakeven:.2f} at n={new_n}")

        # --- write the note ---
        fm = {"date": datetime.now(timezone.utc).date().isoformat(),
              "league": league, "espn_event_id": espn_event_id,
              "settlement_status": settlement_status,
              "realized_pnl_cents": realized,
              "counterfactual_pnl_cents": cf_pnl,
              "trades": len(trades), "declined": len(declined), "env": self.env}
        body_lines = [f"# Postmortem {league} {espn_event_id}", ""]
        if mismatch_details:
            body_lines += ["## ⚠ SETTLEMENT MISMATCH"] + mismatch_details + [""]
        if threshold_flags:
            body_lines += ["## Threshold review"] + threshold_flags + [""]
        body_lines.append("## Trades")
        for a in audits:
            flag = " ⚠ ENTRY VIOLATION" if a["entry_conditions_failed"] else ""
            body_lines.append(f"- {a['trade']} [{a['skill']}] pnl={a['pnl_cents']}c "
                              f"slippage={a['slippage_cents']}c{flag}")
        if not audits:
            body_lines.append("- none (watched, nothing traded — that is data)")
        body_lines.append("")
        body_lines.append("## Declined candidates")
        for d in declined:
            body_lines.append(f"- {d['signal'].get('type')} cf={d['counterfactual_pnl_cents']}c "
                              f"({d['assumption']})")
        if not declined:
            body_lines.append("- none")
        try:
            existing = self.vault.read_note(note_path)
            merged = dict(existing.frontmatter)
            merged.update(fm)
            fm = merged
        except Exception:
            pass
        self.vault.write_note(note_path, fm, "\n".join(body_lines) + "\n",
                              caller="analyst")

        if settlement_status == "mismatch":
            if self.discord:
                self.discord.notify("SETTLEMENT MISMATCH: " + "; ".join(mismatch_details),
                                    level="critical")
            raise SettlementMismatch("; ".join(mismatch_details))

        return PostmortemReport(
            league=league, espn_event_id=espn_event_id, trades_audited=len(trades),
            entry_violations=entry_violations, exit_deviations=exit_deviations,
            declined_candidates=len(declined), counterfactual_pnl_cents=cf_pnl,
            realized_pnl_cents=realized, settlement_status=settlement_status,
            threshold_flags=threshold_flags, note_path=note_path)

    @staticmethod
    def _report_from_note(note, note_path) -> PostmortemReport:
        fm = note.frontmatter
        return PostmortemReport(
            league=fm.get("league"), espn_event_id=fm.get("espn_event_id"),
            trades_audited=fm.get("trades", 0), entry_violations=0,
            exit_deviations=0, declined_candidates=fm.get("declined", 0),
            counterfactual_pnl_cents=fm.get("counterfactual_pnl_cents", 0),
            realized_pnl_cents=fm.get("realized_pnl_cents", 0),
            settlement_status=fm.get("settlement_status", "settled"),
            threshold_flags=[], note_path=note_path)
