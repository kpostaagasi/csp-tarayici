"""Both backtests: the path-based EV and the replay of recorded chains."""

import dataclasses
import datetime as dt

from csp.backtest import backtest, ev, peak_committed, replay, replay_stats, snap_db, snapshot
from csp.score import Filters, occ


def contract(row, **over):
    return dataclasses.replace(row, **over)


def bt_row(row):
    """A round-numbered contract so every expected P&L is checkable by hand."""
    return contract(row, sym="X", spot=100.0, strike=90.0, mid=0.50, dte=30, delta=-0.25, ev_n=None)


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
    nohist = contract(bt_row(row), sym="NOHIST")
    assert ev(nohist) == 0.0  # no history: 0, so sorting still works...
    assert nohist.ev_n == 0  # ...but the row still knows it was never measured
    assert r.ev_n == 400 - 21


# ------------------------------------------------------------------ replay of recorded chains
ENTRIES = (
    ("2026-01-05", "2026-02-06"),  # the trade
    ("2026-01-06", "2026-02-06"),  # inside it: only opened if there is cash left over
    ("2026-02-09", "2026-03-13"),
)  # expiry still ahead: not a result
QUOTES = (
    (9.0, -0.20, 0.20, 0.21),  # cheap, far, fits $900
    (9.5, -0.30, 0.40, 0.42),  # best score, needs $950
    (12.0, -0.60, 2.00, 2.02),
)  # delta band and capital both reject it


def recorded_db(tmp_path, syms=("T",), entries=ENTRIES, quotes=QUOTES):
    db = tmp_path / "chains.db"
    with snap_db(db) as con:
        for sym in syms:
            for date, exp in entries:
                for strike, delta, bid, ask in quotes:
                    con.execute(
                        "insert or replace into puts values (?,?,?,?,?,?,?,?,?,?)",
                        (date, sym, exp, strike, bid, ask, 50.0, delta, 500.0, 10.0),
                    )
    return db


def dated_history(history, syms=("T",), through="2026-02-06"):
    """Flat at 10.00 until the last day, which closes at 9.20 — so a 9.50 strike is breached."""
    dates = [f"2025-12-{i:02d}" for i in range(1, 32)] + [f"2026-01-{i:02d}" for i in range(1, 32)]
    dates += [d for d in (f"2026-02-{i:02d}" for i in range(1, 29)) if d <= through]
    dates += [d for d in (f"2026-03-{i:02d}" for i in range(1, 32)) if d <= through]
    for sym in syms:
        history(sym, [10.0] * (len(dates) - 1) + [9.2], dates)
    return dates


def test_replay_trades_one_position_at_a_time_when_that_is_all_the_cash_buys(tmp_path, history):
    """The single-position engine this replaces is just the $1000 corner of the portfolio one."""
    dated_history(history)
    trades, still_open = replay("T", Filters(capital=1000), path=recorded_db(tmp_path))
    assert [t["entry"] for t in trades] == ["2026-01-05"]
    # 2026-02-09 does fill: the first contract expired on the 6th and gave its $950 back. The old
    # engine stopped the replay at the first unresolved contract and never saw this entry at all.
    assert [(p["entry"], p["exp"]) for p in still_open] == [("2026-02-09", "2026-03-13")]
    t = trades[0]
    assert (t["sym"], t["strike"], t["credit"], t["exp"], t["dte"]) == ("T", 9.5, 0.40, "2026-02-06", 32)
    assert t["settle"] == 9.2
    assert abs(t["pl"] - (0.40 - 0.30) * 100) < 1e-9  # credit at the bid, assigned at expiry


def test_replay_stats_measure_the_committed_cash(tmp_path, history):
    dated_history(history)
    trades, still_open = replay("T", Filters(capital=1000), path=recorded_db(tmp_path))
    s = replay_stats(trades, still_open)
    assert (s["n"], s["syms"], s["wins"], s["assigned"], s["tied"]) == (1, 1, 1.0, 1.0, 950.0)
    assert abs(s["total"] - 10) < 1e-9 and s["drawdown"] == 0.0
    assert (s["days"], s["deployed"]) == (32, 32)  # one position, no idle stretch: they agree
    assert abs(s["ann"] - 10 / 950 * 365 / 32) < 1e-6


def test_replay_respects_capital(tmp_path, history):
    dated_history(history)
    db = recorded_db(tmp_path)
    cheap, _ = replay("T", Filters(capital=900), path=db)  # $950 collateral no longer affordable
    assert (cheap[0]["strike"], cheap[0]["credit"]) == (9.0, 0.20)
    assert abs(cheap[0]["pl"] - 20.0) < 1e-9  # 9.00 was never breached
    assert replay_stats(cheap)["assigned"] == 0.0
    assert replay("T", Filters(capital=100), path=db) == ([], [])


def test_replay_of_an_unrecorded_symbol_is_empty(tmp_path):
    assert replay("NOPE", Filters(), path=recorded_db(tmp_path)) == ([], [])
    assert replay([], Filters(), path=tmp_path / "empty.db") == ([], [])
    assert replay_stats([]) is None


# --------------------------------------------------------------------------- portfolio behaviour
def test_spare_cash_fills_a_second_position_the_same_day(tmp_path, history):
    """$2000 buys both contracts; the old engine could only ever hold the better one."""
    dated_history(history)
    trades, _ = replay("T", Filters(capital=2000), path=recorded_db(tmp_path), max_per_sym=2)
    assert sorted(t["strike"] for t in trades) == [9.0, 9.5]
    assert all(t["entry"] == "2026-01-05" for t in trades)  # best score first, then what fits
    s = replay_stats(trades)
    assert s["tied"] == 1850.0  # both at once, not the larger of the two
    assert abs(s["total"] - 30.0) < 1e-9  # +$10 assigned on 9.50, +$20 kept on 9.00
    assert s["deployed"] == 64 and s["days"] == 32  # two overlapping positions, one 32-day book


def test_concentration_is_capped_per_symbol(tmp_path, history):
    """Cash alone would let one name become the entire book."""
    dated_history(history)
    trades, _ = replay("T", Filters(capital=2000), path=recorded_db(tmp_path), max_per_sym=1)
    assert [t["strike"] for t in trades] == [9.5]


def test_capital_frees_up_when_a_position_expires(tmp_path, history):
    """The day a contract expires its collateral is spendable again — same day, not the next."""
    entries = (("2026-01-05", "2026-02-06"), ("2026-02-06", "2026-03-13"))
    dated_history(history, through="2026-03-13")
    trades, still_open = replay("T", Filters(capital=1000), path=recorded_db(tmp_path, entries=entries))
    assert [(t["entry"], t["exp"]) for t in trades] == [
        ("2026-01-05", "2026-02-06"),
        ("2026-02-06", "2026-03-13"),
    ]
    assert still_open == []
    s = replay_stats(trades)
    assert s["tied"] == 950.0  # sequential, never doubled up
    assert s["days"] == 67 and s["deployed"] == 32 + 35


def test_a_position_whose_expiry_has_not_passed_is_open_not_a_result(tmp_path, history):
    """No lookahead and no half-trades: an unresolved contract is reported, never counted."""
    dated_history(history)  # price series stops 2026-02-06
    entries = (("2026-02-09", "2026-03-13"),)
    trades, still_open = replay("T", Filters(capital=1000), path=recorded_db(tmp_path, entries=entries))
    assert trades == []
    assert [(p["sym"], p["exp"], p["collat"]) for p in still_open] == [("T", "2026-03-13", 950.0)]
    assert replay_stats(trades, still_open) is None


def test_the_book_spreads_across_symbols(tmp_path, history):
    dated_history(history, syms=("T", "U"))
    db = recorded_db(tmp_path, syms=("T", "U"))
    trades, _ = replay(None, Filters(capital=2000), path=db)  # None = every recorded symbol
    assert sorted(t["sym"] for t in trades) == ["T", "U"]
    assert all(t["strike"] == 9.5 for t in trades)  # one per name, the best-scoring one
    s = replay_stats(trades)
    assert (s["syms"], s["tied"]) == (2, 1900.0)

    one, _ = replay("T", Filters(capital=2000), path=db)
    assert [t["sym"] for t in one] == ["T"]  # naming a symbol still scopes the book to it


def test_trades_come_back_in_settlement_order(tmp_path, history):
    """The drawdown is an equity curve, so it has to follow the cash, not the entries.

    A long-dated contract opened first settles last, so entry order and settlement order cross.
    """
    entries = (("2026-01-05", "2026-03-13"), ("2026-01-06", "2026-02-06"))
    dated_history(history, through="2026-03-13")
    db = recorded_db(tmp_path, entries=entries, quotes=((9.5, -0.30, 0.40, 0.42),))
    trades, still_open = replay("T", Filters(capital=2000, dte_max=70), path=db, max_per_sym=2)

    assert [(t["entry"], t["exp"]) for t in trades] == [
        ("2026-01-06", "2026-02-06"),  # entered second, settles first
        ("2026-01-05", "2026-03-13"),
    ]
    assert still_open == []
    kept, assigned = (t["pl"] for t in trades)  # kept in full, then assigned 0.30 deep
    assert kept == 40.0 and abs(assigned - 10.0) < 1e-9
    s = replay_stats(trades)
    assert s["tied"] == 1900.0 and s["drawdown"] == 0.0
    assert (s["days"], s["deployed"]) == (67, 31 + 67)


def test_peak_committed_counts_overlap_not_the_biggest_single_trade(tmp_path):
    def trade(entry, exp, collat):
        return dict(entry=entry, exp=exp, collat=collat)

    overlapping = [trade("2026-01-05", "2026-02-06", 900), trade("2026-01-06", "2026-02-06", 950)]
    assert peak_committed(overlapping) == 1850

    sequential = [trade("2026-01-05", "2026-02-06", 900), trade("2026-02-06", "2026-03-13", 950)]
    assert peak_committed(sequential) == 950  # the release is applied before the same-day entry


# ------------------------------------------------------------------------ recording a session
EXP = dt.date(2026, 10, 16)  # ~6 weeks after the recorded session, so max_dte keeps it


def payload(last_trade, strikes=(9.0, 9.5), exp=EXP, spot=10.0):
    return {
        "close": spot,
        "current_price": spot,
        "last_trade_time": last_trade,
        "options": [
            {
                "option": occ("T", exp, k),
                "bid": 0.20,
                "ask": 0.22,
                "iv": 0.50,
                "delta": -0.25,
                "open_interest": 500,
            }
            for k in strikes
        ],
    }


def vendor(monkeypatch, by_sym):
    monkeypatch.setattr("csp.backtest.chain", lambda t: by_sym[t.upper()])


def rows_in(db):
    with snap_db(db) as con:
        return con.execute("select date, sym, strike from puts order by date, strike").fetchall()


def test_rows_carry_the_session_the_quotes_came_from_not_the_day_the_job_ran(tmp_path, monkeypatch):
    """Labor Day: the cron fires on the 8th and the vendor is still serving the 4th."""
    vendor(monkeypatch, {"T": payload("2026-09-04T15:59:59")})
    db = tmp_path / "chains.db"
    n, session = snapshot(["T"], path=db)

    assert (n, session) == (2, dt.date(2026, 9, 4))
    assert {r[0] for r in rows_in(db)} == {"2026-09-04"}  # not today, whenever today is


def test_re_recording_a_stale_session_is_a_no_op_not_a_second_day(tmp_path, monkeypatch):
    """The bug this pins: a holiday used to add a fake day to the IV series every time it ran."""
    vendor(monkeypatch, {"T": payload("2026-09-04T15:59:59")})
    db = tmp_path / "chains.db"
    snapshot(["T"], path=db)
    snapshot(["T"], path=db)  # Tuesday's run, vendor still on Friday
    snapshot(["T"], path=db)  # and again

    assert len(rows_in(db)) == 2  # two strikes, one session, however many times it ran
    with snap_db(db) as con:
        assert con.execute("select count(distinct date) from puts").fetchone()[0] == 1


def test_one_run_records_one_session_even_if_a_symbol_lags(tmp_path, monkeypatch):
    """replay() treats a date as one decision point, so a split run would half-populate two."""
    vendor(
        monkeypatch,
        {
            "T": payload("2026-09-04T15:59:59"),
            "U": payload("2026-08-28T15:59:59", strikes=(9.0,)),  # halted for a week
        },
    )
    db = tmp_path / "chains.db"
    n, session = snapshot(["T", "U"], path=db)
    assert (n, session) == (3, dt.date(2026, 9, 4))
    assert {r[0] for r in rows_in(db)} == {"2026-09-04"}


def test_dte_is_measured_from_the_session_not_from_today(tmp_path, monkeypatch):
    far = dt.date(2026, 9, 4) + dt.timedelta(days=80)
    near = dt.date(2026, 9, 4) + dt.timedelta(days=60)
    monkeypatch.setattr(
        "csp.backtest.chain",
        lambda t: {
            "close": 10.0,
            "current_price": 10.0,
            "last_trade_time": "2026-09-04T15:59:59",
            "options": [
                o
                for e in (near, far)
                for o in payload("2026-09-04T15:59:59", strikes=(9.0,), exp=e)["options"]
            ],
        },
    )
    db = tmp_path / "chains.db"
    n, _ = snapshot(["T"], max_dte=70, path=db)
    assert n == 1  # the 80-day expiry is out, the 60-day one is in


def test_a_payload_with_no_timestamp_records_nothing(tmp_path, monkeypatch):
    """Guessing a date here is exactly the failure the session stamp exists to prevent."""
    vendor(monkeypatch, {"T": payload("")})
    db = tmp_path / "chains.db"
    assert snapshot(["T"], path=db) == (0, None)


def test_a_dead_ticker_does_not_abort_the_run(tmp_path, monkeypatch):
    def flaky(t):
        if t.upper() == "DEAD":
            raise RuntimeError("404")
        return payload("2026-09-04T15:59:59")

    monkeypatch.setattr("csp.backtest.chain", flaky)
    db = tmp_path / "chains.db"
    n, session = snapshot(["DEAD", "T"], path=db)
    assert n == 2 and session == dt.date(2026, 9, 4)
