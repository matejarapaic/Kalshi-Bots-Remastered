"""analyst agent. Prompt: vault 01-agents/analyst/system-prompt.md.

Closes the feedback loop at 15-minute cadence without drowning the operator:
polls settlement for closed windows, settles the paper broker, runs the
mechanical postmortem, and batches everything human-facing — one Discord
rollup per ROLLUP_WINDOWS settled windows (hourly at 4), except windows that
actually traded, which get their own card immediately. Skill-note stats flush
in the same batches (write amplification at 96 windows/day). Never on the
live trading path; never sizes or trades.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from datetime import datetime, timezone

from kalshi_bots.skills.postmortem import Postmortem, SettlementMismatch
from kalshi_bots.types import PostmortemReport, WindowRef

log = logging.getLogger(__name__)

ROLLUP_WINDOWS = 4            # one quiet-hours Discord message per hour
SETTLE_POLL_INTERVAL_S = 5.0  # REST check cadence per pending window
SETTLE_GIVE_UP_S = 900.0      # a window unfinalized 15min after close is an
                              # incident: report pending, alert, stop polling
FINAL_STATUSES = {"finalized", "settled"}


class Analyst:
    def __init__(self, vault, broker, discord=None, env: str = "demo",
                 paper_broker=None):
        self.vault = vault
        self.broker = broker
        self.discord = discord
        self.paper = paper_broker
        self.postmortem = Postmortem(vault, broker, discord_bot=discord, env=env)
        # event_id -> {window, next_check_mono, closed_mono}; bounded: at most
        # a handful of windows can be pending settlement at once
        self._pending: dict[str, dict] = {}
        self._rollup: list[PostmortemReport] = []
        self._stats_batch: dict[str, list[dict]] = {}
        self.recent_reports: deque[PostmortemReport] = deque(maxlen=8)  # dashboard

    # --- orchestrator hooks ---

    def on_window_close(self, window: WindowRef) -> None:
        """Queue a closed window for settlement polling (window-close fires at
        the boundary; Kalshi finalizes within seconds to a few minutes)."""
        if window is None:
            return
        mono = time.monotonic()
        self._pending[window.event_ticker] = {
            "window": window, "next_check_mono": mono + 2.0,
            "closed_mono": mono,
        }

    def poll_pending(self, now: datetime | None = None) -> list[PostmortemReport]:
        """Called every orchestrator tick. Checks pending windows for
        finalization (throttled per window), runs postmortems, batches
        rollups/stats. Returns reports completed this tick."""
        mono = time.monotonic()
        done: list[PostmortemReport] = []
        for event_id, p in list(self._pending.items()):
            if mono < p["next_check_mono"]:
                continue
            p["next_check_mono"] = mono + SETTLE_POLL_INTERVAL_S
            w: WindowRef = p["window"]
            try:
                raw = self.broker.get_market_raw(w.market_ticker)
            except Exception as e:
                log.warning("settlement poll failed for %s: %s", w.market_ticker, e)
                raw = None
            result = (raw or {}).get("result") or None
            status = (raw or {}).get("status")
            if raw is not None and (result in ("yes", "no", "void")
                                    or status in FINAL_STATUSES):
                expiration = raw.get("expiration_value")
                try:
                    expiration = float(expiration) if expiration is not None else None
                except (TypeError, ValueError):
                    expiration = None
                self._pending.pop(event_id, None)
                done.append(self._settle(w, result, expiration))
            elif mono - p["closed_mono"] > SETTLE_GIVE_UP_S:
                self._pending.pop(event_id, None)
                log.error("window %s unfinalized %.0fs after close — reporting "
                          "pending", w.market_ticker, mono - p["closed_mono"])
                if self.discord:
                    self.discord.notify(
                        f"⚠ settlement never finalized for {w.market_ticker} — "
                        f"postmortem recorded as pending", level="warning")
                done.append(self._settle(w, None, None))
        return done

    # --- internals ---

    def _settle(self, w: WindowRef, result: str | None,
                expiration: float | None) -> PostmortemReport | None:
        # paper settlement first, from the real market result — the broker's
        # simulated positions must resolve before P&L is audited
        if self.paper is not None and result in ("yes", "no"):
            try:
                self.paper.settle(w.market_ticker, result)
            except Exception as e:
                log.warning("paper settlement failed for %s: %s", w.market_ticker, e)
        # record the settled direction on the window note (postmortem input +
        # the next window's context)
        if result in ("yes", "no") or expiration is not None:
            try:
                self.vault.update_frontmatter(
                    f"03-market-context/active-windows/{w.market_ticker}.md",
                    {"settled_result": result, "expiration_value": expiration,
                     "settled_direction": ("up" if result == "yes" else
                                           "down" if result == "no" else None)},
                    caller="analyst")
            except Exception:
                pass  # no note = record gap; postmortem logs it
        try:
            report, outcomes = self.postmortem.run(
                w.series_ticker, w.event_ticker, market_result=result,
                expiration_value=expiration, closes_at=w.closes_at)
        except SettlementMismatch:
            raise
        except Exception as e:
            log.error("postmortem failed for %s: %s", w.event_ticker, e)
            return None
        for skill, rows in outcomes.items():
            self._stats_batch.setdefault(skill, []).extend(rows)
        self._rollup.append(report)
        self.recent_reports.append(report)
        # traded windows are worth a message of their own, immediately
        if report.trades_audited > 0 and self.discord:
            self.discord.notify(
                f"POSTMORTEM {report.event_id}: {report.trades_audited} trade(s) "
                f"pnl={report.realized_pnl_cents}c "
                f"model_hits={report.model_direction_hits} "
                f"vol_ratio={report.vol_ratio and round(report.vol_ratio, 2)}"
                f"{' ⚠drift' if report.constituent_drift else ''}", level="info")
        if len(self._rollup) >= ROLLUP_WINDOWS:
            self._flush_rollup()
        return report

    def _flush_rollup(self) -> None:
        batch, self._rollup = self._rollup, []
        stats, self._stats_batch = self._stats_batch, {}
        flags = []
        try:
            flags = self.postmortem.update_skill_stats(stats)
        except Exception as e:
            log.error("skill stats flush failed: %s", e)
        if not self.discord:
            return
        pnl = sum(r.realized_pnl_cents for r in batch)
        cf = sum(r.counterfactual_pnl_cents for r in batch)
        traded = sum(r.trades_audited for r in batch)
        drift = sum(1 for r in batch if r.constituent_drift)
        lines = [f"ROLLUP {len(batch)} windows: trades={traded} pnl={pnl}c "
                 f"declined_cf={cf}c drift_windows={drift}"]
        lines += [f"  {r.event_id}: {r.settlement_status} pnl={r.realized_pnl_cents}c"
                  for r in batch]
        lines += flags
        self.discord.notify("\n".join(lines), level="info")
