"""Both backtests: the path-based EV and the replay of recorded chains."""

import dataclasses

from csp.backtest import backtest, ev, replay, replay_stats, snap_db
from csp.score import Filters


def contract(row, **over):
    return dataclasses.replace(row, **over)


def bt_row(row):
    """A round-numbered contract so every expected P&L is checkable by hand."""
    return contract(row, sym="X", spot=100.0, strike=90.0, mid=0.50, dte=30, delta=-0.25, ev=None)


def test_flat_history_keeps_the_whole_credit(row):
    r = backtest(bt_row(row), [100.0] * 400)
    assert r["n"] == 400 - 21 and r["years"] == 1.6  # 30 calendar days = 21 sessions
    assert r["assign"] == 0.0 and r["loss"] == 0.0
    assert r["mean"] == 50.0 and r["worst"] == 50.0


def test_one_gap_prices_the_tail(row):
    px = [100.0] * 200 + [50.0] * 200  # a single -50% gap
    r = backtest(bt_row(row), px)
    assert r["n"] == 379
    assert abs(r["assign"] - 21 / 379) < 1e-12  # exactly the 21 windows straddling it
    assert r["loss"] == r["assign"]  # a $0.50 credit cannot cover $10
    assert abs(r["worst"] - (50 + (-0.50 + 0.10) * 100 * 100)) < 1e-9  # -3950
    assert abs(r["mean"] - (358 * 50 + 21 * -3950) / 379) < 1e-9


def test_a_strike_below_the_floor_is_never_assigned(row):
    px = [100.0] * 200 + [50.0] * 200
    assert backtest(contract(bt_row(row), strike=40.0), px)["assign"] == 0.0


def test_too_little_history_says_nothing(row):
    assert backtest(bt_row(row), [100.0] * 140) is None
    assert backtest(bt_row(row), None) is None


def test_ev_is_memoized_on_the_row(row, history):
    history("X", [100.0] * 400)
    r = bt_row(row)
    assert ev(r) == 50.0 and r.ev == 50.0
    history("X", [100.0] * 200 + [50.0] * 200)  # a re-read would change the answer
    assert ev(r) == 50.0
    assert ev(contract(bt_row(row), sym="NOHIST")) == 0.0  # no history: 0, so sorting still works


# ------------------------------------------------------------------ replay of recorded chains
ENTRIES = (
    ("2026-01-05", "2026-02-06"),  # the trade
    ("2026-01-06", "2026-02-06"),  # inside it: must be skipped
    ("2026-02-09", "2026-03-13"),
)  # expiry still ahead: not a result
QUOTES = (
    (9.0, -0.20, 0.20, 0.21),  # cheap, far, fits $900
    (9.5, -0.30, 0.40, 0.42),  # best score, needs $950
    (12.0, -0.60, 2.00, 2.02),
)  # delta band and capital both reject it


def recorded_db(tmp_path):
    db = tmp_path / "chains.db"
    with snap_db(db) as con:
        for date, exp in ENTRIES:
            for strike, delta, bid, ask in QUOTES:
                con.execute(
                    "insert or replace into puts values (?,?,?,?,?,?,?,?,?,?)",
                    (date, "T", exp, strike, bid, ask, 50.0, delta, 500.0, 10.0),
                )
    return db


def dated_history(history):
    dates = (
        [f"2025-12-{i:02d}" for i in range(1, 32)]
        + [f"2026-01-{i:02d}" for i in range(1, 32)]
        + [f"2026-02-{i:02d}" for i in range(1, 7)]
    )
    history("T", [10.0] * (len(dates) - 1) + [9.2], dates)  # expiry close 9.2: 9.5 breached
    return dates


def test_replay_trades_one_position_at_a_time(tmp_path, history):
    dated_history(history)
    trades = replay("T", Filters(capital=1000), path=recorded_db(tmp_path))
    assert [t["entry"] for t in trades] == ["2026-01-05"]
    t = trades[0]
    assert (t["strike"], t["credit"], t["exp"], t["dte"]) == (9.5, 0.40, "2026-02-06", 32)
    assert t["settle"] == 9.2
    assert abs(t["pl"] - (0.40 - 0.30) * 100) < 1e-9  # credit at the bid, assigned at expiry


def test_replay_stats_measure_the_committed_cash(tmp_path, history):
    dated_history(history)
    s = replay_stats(replay("T", Filters(capital=1000), path=recorded_db(tmp_path)))
    assert (s["n"], s["wins"], s["assigned"], s["tied"]) == (1, 1.0, 1.0, 950.0)
    assert abs(s["total"] - 10) < 1e-9 and s["drawdown"] == 0.0
    assert abs(s["ann"] - 10 / 950 * 365 / 32) < 1e-6


def test_replay_respects_capital(tmp_path, history):
    dated_history(history)
    db = recorded_db(tmp_path)
    cheap = replay("T", Filters(capital=900), path=db)  # $950 collateral no longer affordable
    assert (cheap[0]["strike"], cheap[0]["credit"]) == (9.0, 0.20)
    assert abs(cheap[0]["pl"] - 20.0) < 1e-9  # 9.00 was never breached
    assert replay_stats(cheap)["assigned"] == 0.0
    assert replay("T", Filters(capital=100), path=db) == []


def test_replay_of_an_unrecorded_symbol_is_empty(tmp_path):
    assert replay("NOPE", Filters(), path=recorded_db(tmp_path)) == []
    assert replay_stats([]) is None
