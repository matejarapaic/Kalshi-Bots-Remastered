"""window-monitor agent. Prompt: vault 01-agents/window-monitor/system-prompt.md.

Watches the active 15-minute window and market state; emits lifecycle signals
(window-open, phase-change, window-close) and fair-value candidates; never
places orders. A candidate is a flag, not a vouch — the trader recomputes
everything from fresh data at decision time. Writes active-window notes
(frontmatter = machine state, Signals section = log) via the vault skill.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone

from kalshi_bots.skills.fair_value_model import evaluate, side_edges
from kalshi_bots.skills.risk_management import ENTRY_PHASES, MIN_EDGE_CENTS
from kalshi_bots.skills.window_monitor import window_phase
from kalshi_bots.types import CryptoSignal, MarketRef, Phase, WindowRef

log = logging.getLogger(__name__)

NOTE_UPDATE_S = 30  # steady-state note refresh cadence (writes are disk I/O)
CANDIDATE_COOLDOWN_S = 30  # min gap between candidate flags per window (the
                           # trader's position-exists gate dedupes after a
                           # fill; this throttles unfilled/declined retries)


class WindowMonitor:
    def __init__(self, vault, resolver, book=None, feed=None):
        self.vault = vault
        self.resolver = resolver     # skills.window_monitor.WindowResolver
        self.book = book             # KalshiOrderBook | None (read-only data)
        self.feed = feed             # CryptoPriceFeed | None
        self.current_window: WindowRef | None = None
        self.current_phase: Phase | None = None
        self._last_note_write: float = 0.0
        self._last_candidate_mono: float | None = None  # reset per window

    @staticmethod
    def _market_ref(w: WindowRef) -> MarketRef:
        """Identifier bundle for the WS subscription — built locally from the
        verified WindowRef; no extra REST round-trip."""
        return MarketRef(family="crypto", series_ticker=w.series_ticker,
                         event_ticker=w.event_ticker,
                         market_ticker=w.market_ticker, yes_label="up",
                         title="", close_ts=w.closes_at, settlement_notes=None)

    async def tick(self, now: datetime | None = None) -> list[CryptoSignal]:
        """One evaluation pass. Returns lifecycle signals; the trader
        re-verifies anything tradeable (the monitor flags, it never vouches)."""
        now = now or datetime.now(timezone.utc)
        signals: list[CryptoSignal] = []
        w = self.resolver.resolve_active(now)

        cur = self.current_window
        if (w.market_ticker if w else None) != (cur.market_ticker if cur else None):
            if cur is not None:
                signals.append(self._signal("window-close", cur, "settled"))
                self._write_window_note(cur, "settled", now, force=True)
                if self.book is not None:
                    try:
                        await self.book.unsubscribe(cur.market_ticker)
                    except Exception as e:
                        log.warning("unsubscribe %s failed: %s", cur.market_ticker, e)
            if w is not None:
                ph = window_phase(now, w)
                if self.book is not None:
                    try:
                        await self.book.subscribe(self._market_ref(w))
                    except Exception as e:
                        log.warning("subscribe %s failed: %s", w.market_ticker, e)
                # note first, then signal: _log_signal appends to the note and
                # would silently drop the entry if the note doesn't exist yet
                self._write_window_note(w, ph, now, force=True)
                signals.append(self._signal("window-open", w, ph))
                self.current_phase = ph
            else:
                self.current_phase = None
            self.current_window = w
            self._last_candidate_mono = None
            return signals

        if w is None:
            return signals  # unresolved (API mismatch/outage): watched, never traded

        if cur is not None and cur.strike is None and w.strike is not None:
            cur.strike = w.strike  # strike stamped shortly after open

        ph = window_phase(now, w)
        if ph != self.current_phase:
            self.current_phase = ph
            signals.append(self._signal("phase-change", self.current_window, ph))
            self._write_window_note(self.current_window, ph, now, force=True)
        else:
            self._write_window_note(self.current_window, ph, now)

        candidate = self._detect_candidate(self.current_window, ph, now)
        if candidate is not None:
            signals.append(candidate)
        return signals

    # --- candidate detection (flags only; the trader re-verifies) ---

    def _detect_candidate(self, w: WindowRef, ph: Phase,
                          now: datetime) -> CryptoSignal | None:
        if ph not in ENTRY_PHASES or w.strike is None or self.feed is None:
            return None
        mono = time.monotonic()
        if (self._last_candidate_mono is not None
                and mono - self._last_candidate_mono < CANDIDATE_COOLDOWN_S):
            return None
        spot = self.feed.current_composite()
        sigma = self.feed.realized_vol()
        if spot is None or sigma is None:
            return None  # fail closed: no flag without healthy model inputs
        snapshot = self.book.snapshot(w.market_ticker) if self.book else None
        if snapshot is None:
            return None  # no trustworthy book: nothing to diverge from
        try:
            est = evaluate(w, spot, snapshot, sigma, now=now)
        except Exception as e:
            log.warning("fair-value evaluate failed for %s: %s", w.market_ticker, e)
            return None
        edges = side_edges(est, snapshot)
        best_side = max((s for s in ("yes", "no") if edges[s] is not None),
                        key=lambda s: edges[s], default=None)
        if best_side is None or edges[best_side] < MIN_EDGE_CENTS:
            return None
        self._last_candidate_mono = mono
        sig = CryptoSignal(
            signal_type="fair-value-candidate", series_ticker=w.series_ticker,
            market_ticker=w.market_ticker, window=w, phase=ph,
            payload={
                "id": str(uuid.uuid4())[:8],
                "side": best_side,
                "edge_cents": round(edges[best_side], 2),
                "model_prob_up": round(est.model_prob_up, 4),
                "entry_price_cents": (snapshot.yes_ask if best_side == "yes"
                                      else snapshot.no_ask),
                "sigma": sigma, "spot": spot.mid, "strike": w.strike,
            },
            emitted_at=datetime.now(timezone.utc))
        self._log_signal(w, sig)
        return sig

    # --- helpers ---

    def _market_context(self) -> dict:
        ctx: dict = {}
        if self.feed is not None:
            spot = self.feed.current_composite()
            ctx["spot"] = spot.mid if spot else None
            ctx["spot_healthy"] = spot.constituents_healthy if spot else 0
            ctx["sigma"] = self.feed.realized_vol()
        return ctx

    def _signal(self, sig_type: str, w: WindowRef, phase: Phase) -> CryptoSignal:
        sig = CryptoSignal(
            signal_type=sig_type, series_ticker=w.series_ticker,
            market_ticker=w.market_ticker, window=w, phase=phase,
            payload={**self._market_context(), "strike": w.strike,
                     "id": str(uuid.uuid4())[:8]},
            emitted_at=datetime.now(timezone.utc))
        self._log_signal(w, sig)
        return sig

    def _note_path(self, w: WindowRef) -> str:
        return f"03-market-context/active-windows/{w.market_ticker}.md"

    def _write_window_note(self, w: WindowRef, phase: Phase, now: datetime,
                           force: bool = False):
        import time as _time
        mono = _time.monotonic()
        if not force and mono - self._last_note_write < NOTE_UPDATE_S:
            return
        self._last_note_write = mono
        ctx = self._market_context()
        fm = {
            "series_ticker": w.series_ticker, "event_id": w.event_ticker,
            "market_ticker": w.market_ticker,
            "opens_at": w.opens_at.isoformat(), "closes_at": w.closes_at.isoformat(),
            "strike": w.strike, "phase": phase,
            "spot": ctx.get("spot"), "sigma": ctx.get("sigma"),
            "updated": now.isoformat(),
        }
        try:
            existing = self.vault.read_note(self._note_path(w))
            body = existing.body
            merged = dict(existing.frontmatter)
            merged.update(fm)
            fm = merged
        except Exception:
            body = f"# Window {w.market_ticker}\n\n## Signals\n"
        try:
            self.vault.write_note(self._note_path(w), fm, body,
                                  caller="window-monitor")
        except Exception as e:
            log.error("window note write failed: %s", e)

    def _log_signal(self, w: WindowRef, sig: CryptoSignal):
        entry = {"id": sig.payload.get("id"), "type": sig.signal_type,
                 "market_ticker": sig.market_ticker,
                 "ts": sig.emitted_at.isoformat(),
                 "phase": sig.phase,
                 "entry_price_cents": sig.payload.get("entry_price_cents"),
                 "side": sig.payload.get("side")}
        try:
            note = self.vault.read_note(self._note_path(w))
            body = note.body.rstrip("\n") + f"\n- SIGNAL {json.dumps(entry)}\n"
            self.vault.write_note(self._note_path(w), note.frontmatter, body,
                                  caller="window-monitor")
        except Exception:
            pass
