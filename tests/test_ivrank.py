"""IV rank: the only IV history these names have is the one this machine recorded.

Two things matter here. A recorded day and the live chain it came from must produce the same
level — nothing shares the band predicate, so a test has to. And the replay must rank a contract
against what was knowable the day it was entered, never a day later.
"""

import pytest

from csp import score
from csp.backtest import replay
from csp.cache import snap_db
from csp.score import (
    IVR_MIN_DAYS,
    Filters,
    iv_level,
    iv_levels,
    iv_rank,
    levels_upto,
    median,
    reject,
)


@pytest.fixture(autouse=True)
def no_memo():
    score.forget_iv_levels()
    yield
    score.forget_iv_levels()


def quote(dte, iv, delta=-0.30):
    return (dte, iv, delta)


# ------------------------------------------------------------------------------ the daily level
def test_the_level_is_the_median_of_the_band():
    assert iv_level([quote(30, 40.0), quote(30, 50.0), quote(30, 60.0)]) == 50.0
    assert median([1, 2, 3, 4]) == 2.5


def test_quotes_outside_the_band_do_not_move_the_level():
    band = [quote(30, 50.0)]
    assert iv_level(band + [quote(7, 300.0)]) == 50.0  # a weekly, nowhere near what we sell
    assert iv_level(band + [quote(120, 300.0)]) == 50.0  # a LEAP
    assert iv_level(band + [quote(30, 300.0, delta=-0.02)]) == 50.0  # a lottery-ticket wing
    assert iv_level(band + [quote(30, 300.0, delta=-0.90)]) == 50.0  # deep ITM
    assert iv_level(band + [quote(30, 0.0)]) == 50.0  # a quote with no IV at all


def test_no_usable_quote_is_no_level():
    assert iv_level([]) is None
    assert iv_level([quote(7, 50.0)]) is None


# ------------------------------------------------------------------------------ the rank
def series(levels):
    return [(f"2026-01-{i + 1:02d}", lv) for i, lv in enumerate(levels)]


def test_rank_places_today_in_the_recorded_range():
    lv = series([30.0] * (IVR_MIN_DAYS - 1) + [70.0])
    assert iv_rank(70.0, lv) == (1.0, IVR_MIN_DAYS)
    assert iv_rank(30.0, lv) == (0.0, IVR_MIN_DAYS)
    assert iv_rank(50.0, lv) == (0.5, IVR_MIN_DAYS)
    assert iv_rank(200.0, lv)[0] == 1.0  # above everything recorded: clipped, not >1


def test_a_thin_sample_refuses_to_rank():
    """A number computed from four days is not a rank, it is noise wearing one."""
    rank, n = iv_rank(50.0, series([30.0, 40.0, 50.0, 60.0]))
    assert rank is None and n == 4
    assert iv_rank(None, series([30.0] * 40))[0] is None  # nothing to rank today


def test_a_flat_year_ranks_in_the_middle():
    assert iv_rank(30.0, series([30.0] * IVR_MIN_DAYS))[0] == 0.5


def test_the_window_forgets_older_than_a_year():
    old_spike = series([500.0] + [30.0] * score.IVR_WINDOW)
    assert iv_rank(30.0, old_spike)[0] == 0.5  # the spike fell out of the window


# ------------------------------------------------------------------------------ live vs recorded
def recorded_day(db, date, quotes, sym="T", exp="2026-03-06"):
    with snap_db(db) as con:
        for dte, iv, delta in quotes:
            strike = 9.0 + dte / 1000 + iv / 100000  # a distinct row per quote, PK is (…, strike)
            con.execute(
                "insert or replace into puts values (?,?,?,?,?,?,?,?,?,?)",
                (date, sym, exp, strike, 0.2, 0.21, iv, delta, 500.0, 10.0),
            )


def test_a_recorded_day_and_a_live_chain_rank_the_same(tmp_path):
    """The band lives twice — once in SQL, once in Python. This is what keeps them equal."""
    import datetime as dt

    db = tmp_path / "chains.db"
    entry, exp = "2026-01-05", "2026-02-06"
    dte = (dt.date.fromisoformat(exp) - dt.date.fromisoformat(entry)).days
    quotes = [
        quote(dte, 40.0),
        quote(dte, 60.0),
        quote(dte, 900.0, delta=-0.01),  # excluded by both paths, or the medians diverge
        quote(dte, 900.0, delta=-0.99),
    ]
    recorded_day(db, entry, quotes, exp=exp)
    assert iv_levels("T", db) == [(entry, iv_level(quotes))]
    assert iv_level(quotes) == 50.0


def test_levels_read_the_store_once_per_process(tmp_path):
    db = tmp_path / "chains.db"
    recorded_day(db, "2026-01-05", [quote(32, 50.0)], exp="2026-02-06")
    assert len(iv_levels("T", db)) == 1
    recorded_day(db, "2026-01-06", [quote(31, 50.0)], exp="2026-02-06")
    assert len(iv_levels("T", db)) == 1  # the TUI re-scans on every keypress; the store does not
    score.forget_iv_levels()
    assert len(iv_levels("T", db)) == 2


# ------------------------------------------------------------------------------ no lookahead
def test_levels_upto_includes_its_own_day_and_nothing_after():
    lv = series([1.0, 2.0, 3.0])
    assert levels_upto(lv, "2026-01-02") == lv[:2]  # the day's own chain is in hand
    assert levels_upto(lv, "2026-01-01") == lv[:1]
    assert levels_upto(lv, "2025-12-31") == []


# ------------------------------------------------------------------------------ the filter
BASE = dict(dte=30, strike=9.0, bid=0.30, ask=0.32, delta=-0.25, oi=500)


def test_min_iv_rank_is_off_by_default():
    assert reject(Filters(capital=1000), **BASE) is None  # no rank supplied, no rank demanded
    assert reject(Filters(capital=1000), **BASE, iv_rank=0.01) is None


def test_min_iv_rank_cuts_cheap_vol_and_unrankable_symbols():
    f = Filters(capital=1000, min_iv_rank=0.5)
    assert reject(f, **BASE, iv_rank=0.5) is None
    assert reject(f, **BASE, iv_rank=0.49) == "ivr"
    assert reject(f, **BASE, iv_rank=None) == "ivr"  # asking for a floor makes "unknown" a miss


def test_the_replay_applies_the_floor_with_the_rank_of_that_day(tmp_path, history):
    """A contract is ranked against the days before it, never against how the year turned out."""
    from tests.test_backtest import dated_history, recorded_db

    db = recorded_db(tmp_path)
    dated_history(history)
    assert replay("T", Filters(capital=1000), path=db)[0]  # unfiltered, the trade is there
    score.forget_iv_levels()
    kept, _ = replay("T", Filters(capital=1000, min_iv_rank=0.5), path=db)
    assert kept == []  # three recorded days cannot produce a rank, so the floor rejects them all


def test_reading_an_absent_store_does_not_create_one(tmp_path):
    """A scan on a machine that never snapshots must not leave a database behind."""
    db = tmp_path / "chains.db"
    assert iv_levels("T", db) == []
    assert not db.exists()
