from datetime import datetime, timedelta, timezone

import pytest

from kalshi_bots.skills.odds_api import (
    OddsApi, OddsFormatError, american_to_prob, decimal_to_prob, devig_two_way,
)
from kalshi_bots.skills.vault import Vault
from kalshi_bots.types import TeamRef

NOW = datetime.now(timezone.utc)


class TestConversionMath:
    @pytest.mark.parametrize("odds,expected", [
        (150, 0.400), (-150, 0.600), (100, 0.500), (-100, 0.500),
        (9900, 0.010), (-9900, 0.990), (130, 100 / 230),
    ])
    def test_american_table(self, odds, expected):
        assert american_to_prob(odds) == pytest.approx(expected, abs=1e-9)

    @pytest.mark.parametrize("bad", [0, 50, -50, 99, -99])
    def test_american_invalid_interval(self, bad):
        with pytest.raises(ValueError):
            american_to_prob(bad)

    def test_decimal(self):
        assert decimal_to_prob(2.50) == pytest.approx(0.400)
        with pytest.raises(ValueError):
            decimal_to_prob(1.0)

    def test_decimal_american_equivalence(self):
        # +150 <=> decimal 2.50
        assert american_to_prob(150) == pytest.approx(decimal_to_prob(2.50))


class TestDevig:
    def test_worked_example(self):
        # spec: -150/+130 -> 0.600/0.435 (sum 1.035) -> home 0.580
        p_home = american_to_prob(-150)
        p_away = american_to_prob(130)
        assert p_home + p_away == pytest.approx(1.0348, abs=1e-3)
        h, a = devig_two_way(p_home, p_away)
        assert h == pytest.approx(0.5798, abs=1e-3)
        assert h + a == pytest.approx(1.0)

    def test_symmetric(self):
        h, a = devig_two_way(american_to_prob(-110), american_to_prob(-110))
        assert h == pytest.approx(0.5) and a == pytest.approx(0.5)

    def test_extreme(self):
        h, _ = devig_two_way(american_to_prob(-10000), american_to_prob(2500))
        assert h == pytest.approx(0.9902 / (0.9902 + 0.03846), abs=1e-3)


HOME = TeamRef("mlb", "NYY", "NYY", "New York Yankees")
AWAY = TeamRef("mlb", "BOS", "BOS", "Boston Red Sox")


def make_event(bookmakers, commence=None):
    return {"home_team": "New York Yankees", "away_team": "Boston Red Sox",
            "commence_time": (commence or NOW).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "bookmakers": bookmakers}


def book(key, yankees_price, sox_price, last_update=None):
    return {"key": key,
            "last_update": (last_update or NOW).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "markets": [{"key": "h2h", "outcomes": [
                {"name": "New York Yankees", "price": yankees_price},
                {"name": "Boston Red Sox", "price": sox_price}]}]}


@pytest.fixture
def api():
    return OddsApi(Vault())


class TestConsensus:
    def test_three_books(self, api):
        ev = make_event([book("b1", -150, 130), book("b2", -160, 140),
                         book("b3", -140, 120)])
        c = api.get_consensus("mlb", HOME, AWAY, raw_events=[ev])
        assert c.book_count == 3
        expected = [devig_two_way(american_to_prob(h), american_to_prob(a))[0]
                    for h, a in [(-150, 130), (-160, 140), (-140, 120)]]
        assert c.devigged_home_prob == pytest.approx(sum(expected) / 3)
        assert c.max_pairwise_disagreement == pytest.approx(
            max(expected) - min(expected))

    def test_stale_book_excluded_live(self, api):
        stale = book("frozen", -150, 130, last_update=NOW - timedelta(minutes=10))
        fresh = book("fresh", -150, 130)
        ev = make_event([stale, fresh])
        c = api.get_consensus("mlb", HOME, AWAY,
                              start_time=NOW - timedelta(hours=1),  # live game
                              raw_events=[ev])
        assert c.book_count == 1
        assert c.books[0].book_name == "fresh"

    def test_stale_book_kept_pregame(self, api):
        stale = book("early", -150, 130, last_update=NOW - timedelta(minutes=10))
        ev = make_event([stale], commence=NOW + timedelta(hours=2))
        c = api.get_consensus("mlb", HOME, AWAY,
                              start_time=NOW + timedelta(hours=2), raw_events=[ev])
        assert c.book_count == 1

    def test_three_way_market_excluded(self, api):
        b = book("b1", -150, 130)
        b["markets"][0]["outcomes"].append({"name": "Draw", "price": 900})
        c = api.get_consensus("mlb", HOME, AWAY, raw_events=[make_event([b])])
        assert c.book_count == 0

    def test_decimal_payload_refused(self, api):
        b = {"key": "b1", "last_update": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "markets": [{"key": "h2h", "outcomes": [
                 {"name": "New York Yankees", "price": 1.91},
                 {"name": "Boston Red Sox", "price": 1.95}]}]}
        with pytest.raises(OddsFormatError):
            api.get_consensus("mlb", HOME, AWAY, raw_events=[make_event([b])])

    def test_no_books_is_answer_not_error(self, api):
        c = api.get_consensus("mlb", HOME, AWAY, raw_events=[make_event([])])
        assert c.book_count == 0 and c.devigged_home_prob is None

    def test_unknown_game_returns_empty(self, api):
        ev = make_event([book("b1", -150, 130)])
        other_home = TeamRef("mlb", "LAD", "LAD", "Los Angeles Dodgers")
        c = api.get_consensus("mlb", other_home, AWAY, raw_events=[ev])
        assert c.book_count == 0

    def test_league_scoped_resolution(self, api):
        # 'St. Louis Cardinals' resolves in MLB; an NFL request must not match it
        ev = {"home_team": "St. Louis Cardinals", "away_team": "Boston Red Sox",
              "commence_time": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
              "bookmakers": [book("b1", -150, 130)]}
        c = api.get_consensus("mlb", HOME, AWAY, raw_events=[ev])
        assert c.book_count == 0  # different game, correctly not matched
