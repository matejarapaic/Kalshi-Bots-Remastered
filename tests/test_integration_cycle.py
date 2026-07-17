"""End-to-end pipeline test with synthetic feeds: a decided MLB blowout flows
signal -> matcher -> verification -> sizing -> approval -> paper fill -> trade
note -> game-final -> paper settlement -> postmortem -> skill stats."""
from datetime import date, datetime, timedelta, timezone

import pytest

from kalshi_bots.agents.analyst import Analyst
from kalshi_bots.agents.game_monitor import GameMonitor
from kalshi_bots.agents.trader import Trader
from kalshi_bots.paper import PaperBroker
from kalshi_bots.skills.discord_bot import ConsoleTransport, DiscordBot
from kalshi_bots.skills.risk_management import RiskManager
from kalshi_bots.skills.vault import Vault
from kalshi_bots.types import (
    DepthLevel, GameState, MarketRef, MatchResult, OrderbookSnapshot,
    SwingEvent, TeamRef,
)

NOW = datetime(2026, 7, 17, 18, 0, tzinfo=timezone.utc)
TICKER = "KXMLBGAME-26JUL171735TBBOS-BOS"

GARBAGE_FM = {
    "skill": "garbage-time-mispricing", "sports": ["mlb"],
    "market_conditions": ["live", "blowout", "endgame"],
    "confidence_threshold": 0.6, "risk_profile": "low", "win_rate": None,
    "sample_size": 0, "status": "confirmed", "last_updated": "2026-07-17",
}


def make_market():
    return MarketRef(league="mlb", series_ticker="KXMLBGAME",
                     event_ticker=TICKER.rsplit("-", 1)[0], market_ticker=TICKER,
                     yes_team_kalshi_abbr="BOS", title="TB vs BOS Winner?",
                     close_ts=None, settlement_notes="postponement terms ...")


def make_snapshot(market, yes_ask=93):
    yes_book = [DepthLevel(yes_ask, 2000), DepthLevel(yes_ask + 1, 500)]
    return OrderbookSnapshot(
        market=market, yes_bid=yes_ask - 2, yes_ask=yes_ask, no_bid=100 - yes_ask,
        no_ask=100 - (yes_ask - 2), yes_book=yes_book,
        no_book=[DepthLevel(100 - (yes_ask - 2), 800)],
        devigged_yes_prob=0.93, spread_cents=2, fetched_at=NOW)


class SyntheticKalshi:
    """Market data provider: one market, deep book at 93c."""

    def __init__(self):
        self.market = make_market()

    def get_market(self, ticker, league=""):
        return self.market

    def get_markets(self, series, status="open", league=""):
        return [self.market]

    def get_orderbook(self, market):
        return make_snapshot(self.market)


class SyntheticEspn:
    """9-0 Red Sox blowout in the bottom 9th; one final poll ends the game."""

    def __init__(self):
        self.final = False
        self._pitchers = {}

    def game(self):
        return GameState(
            league="mlb", espn_event_id="e1",
            status="final" if self.final else "in_progress",
            home=TeamRef("mlb", "BOS", "BOS", "Boston Red Sox"),
            away=TeamRef("mlb", "TB", "TB", "Tampa Bay Rays"),
            home_score=9, away_score=0, period=9, period_half="bottom",
            clock_seconds=None, win_prob_home=0.999, win_prob_source_ts=None,
            start_time=NOW - timedelta(hours=2), fetched_at=NOW)

    def get_scoreboard(self, league):
        return [self.game()]

    def get_game_detail(self, league, eid, state):
        return None

    def get_injuries(self, league, eid):
        return []

    def record_poll(self, game):
        pass

    def detect_swing(self, eid):
        return None

    def detect_decided(self, game, outs=None):
        if game.status != "in_progress":
            return None
        from kalshi_bots.types import DecidedEvent
        return DecidedEvent(espn_event_id="e1", leader="home", win_prob=0.999,
                            rule="mlb_lead5_9th", detected_at=NOW)

    def detect_injury_changes(self, league, eid, current):
        return []


class SyntheticMatcher:
    def __init__(self, market):
        self.market = market

    def resolve_slate(self, league, day, games):
        return {g.espn_event_id: MatchResult(
            espn_event_id=g.espn_event_id, market=self.market,
            method="alias_exact", ambiguous=False, candidates_considered=1)
            for g in games}

    def resolve(self, game):
        return MatchResult(game.espn_event_id, self.market, "alias_exact",
                           False, 1)


@pytest.fixture
def system(tmp_path):
    root = tmp_path / "vault"
    for d in ("00-meta", "02-trading-skills", "03-market-context/active-games",
              "03-market-context/daily-slate", "04-trade-history/trades",
              "04-trade-history/postmortems"):
        (root / d).mkdir(parents=True)
    vault = Vault(root=str(root))
    vault.write_note("02-trading-skills/garbage-time-mispricing.md",
                     dict(GARBAGE_FM), "# skill", caller="admin")

    kalshi = SyntheticKalshi()
    espn = SyntheticEspn()
    matcher = SyntheticMatcher(kalshi.market)
    broker = PaperBroker(kalshi, starting_balance_cents=50_000)
    risk = RiskManager(vault, broker)
    discord = DiscordBot(risk, vault, transport=ConsoleTransport(),
                         mode="autonomous")  # test mode: no human to click
    monitor = GameMonitor(vault, espn, matcher, kalshi=kalshi)
    trader = Trader(vault, broker, matcher, risk, discord, env="demo")
    analyst = Analyst(vault, broker, espn, discord=discord, env="demo",
                      paper_broker=broker)
    return dict(vault=vault, espn=espn, monitor=monitor, trader=trader,
                analyst=analyst, broker=broker, risk=risk, discord=discord)


class TestFullPaperCycle:
    def test_signal_to_postmortem(self, system):
        vault, espn = system["vault"], system["espn"]
        monitor, trader = system["monitor"], system["trader"]
        day = date(2026, 7, 17)

        # cycle 1: live blowout -> garbage-time candidate -> paper trade
        signals = monitor.poll_cycle("mlb", day)
        garbage = [s for s in signals if s.signal_type == "garbage-time-candidate"]
        assert garbage, f"expected a garbage-time candidate, got {signals}"
        game = espn.game()
        disposition = trader.handle_signal(garbage[0], game)
        assert disposition.startswith("traded:"), disposition
        assert len(trader.open_trades) == 1
        assert system["broker"].get_positions()[0].side == "yes"  # BOS leading, home-YES

        # real post-fill Discord notification, with actual fill numbers —
        # not the pre-fill "proposal" the old (buggy) implementation sent
        sent = system["discord"].transport.sent
        entry_notifies = [m for m in sent if m["text"].startswith("ENTRY [")]
        assert len(entry_notifies) == 1
        assert "garbage-time-mispricing" in entry_notifies[0]["text"]
        assert f"{system['broker'].get_positions()[0].contracts}x" in entry_notifies[0]["text"]

        # trade note written with full snapshot
        trades = [n for n in vault.query(
            __import__("kalshi_bots.types", fromlist=["VaultQuery"]).VaultQuery(
                directory="04-trade-history/trades"))]
        assert len(trades) == 1
        fm = trades[0].frontmatter
        assert fm["skill"] == "garbage-time-mispricing"
        assert fm["entry_conditions"]["net_fee_edge"] is True
        assert fm["env"] == "demo"

        # cycle 2: game goes final -> game-final signal -> settlement + postmortem
        espn.final = True
        signals2 = monitor.poll_cycle("mlb", day)
        finals = [s for s in signals2 if s.signal_type == "game-final"]
        assert len(finals) == 1
        report = system["analyst"].on_game_final("mlb", "e1")
        assert report.settlement_status == "settled"
        assert report.trades_audited == 1
        assert report.entry_violations == 0
        assert report.realized_pnl_cents > 0  # won at 93c

        # stats updated by the sole writer
        skill_fm = vault.read_note(
            "02-trading-skills/garbage-time-mispricing.md").frontmatter
        assert skill_fm["demo_sample_size"] == 1
        assert skill_fm["demo_win_rate"] == 1.0

        # game-final emitted exactly once
        signals3 = monitor.poll_cycle("mlb", day)
        assert not [s for s in signals3 if s.signal_type == "game-final"]

    def test_comeback_stop_exits(self, system):
        espn, monitor, trader = system["espn"], system["monitor"], system["trader"]
        day = date(2026, 7, 17)
        signals = monitor.poll_cycle("mlb", day)
        garbage = [s for s in signals if s.signal_type == "garbage-time-candidate"]
        trader.handle_signal(garbage[0], espn.game())
        assert trader.open_trades

        # comeback: win prob collapses under the 93% stop
        game = espn.game()
        game.win_prob_home = 0.90
        actions = trader.manage_positions({"e1": game})
        assert actions and "comeback_stop" in actions[0]
        assert not trader.open_trades

    def test_halted_system_declines(self, system):
        espn, monitor, trader = system["espn"], system["monitor"], system["trader"]
        system["risk"].set_halt(True, "test", caller="discord")
        signals = monitor.poll_cycle("mlb", date(2026, 7, 17))
        garbage = [s for s in signals if s.signal_type == "garbage-time-candidate"]
        d = trader.handle_signal(garbage[0], espn.game())
        assert d.startswith("declined:halted")
