"""The table adapts to width without losing the columns you decide with; the panel derives its numbers."""

import dataclasses
import datetime as dt

import pytest

from csp.render import COLUMNS, SORTS, columns, ev_cell, explain, print_trades, table, usd
from csp.score import best_per_symbol

FULL = sum(len(h) + 1 for h, _, _ in COLUMNS) - 1
NEVER_DROPS = {"sym", "strike", "dte", "ROC%y", "cush", "score"}


def names(cols):
    return [h.strip() for h, _, _ in cols]


def test_wide_terminal_keeps_everything():
    assert columns(FULL) == COLUMNS
    assert columns(FULL + 40) == COLUMNS


def test_first_column_dropped_is_the_cheapest():
    assert names(columns(FULL - 1)) == [h for h in names(COLUMNS) if h != "yld"]


@pytest.mark.parametrize("width", [FULL, 100, 80, 60, 40, 20])
def test_narrow_terminals_keep_the_decision_columns(width):
    cols = columns(width)
    assert set(names(cols)) >= NEVER_DROPS
    if width >= 45:
        assert sum(len(h) + 1 for h, _, _ in cols) - 1 <= width


def test_row_width_matches_the_header(row):
    hdr, lines = table([row], FULL)
    assert len(lines[0]) == len(hdr)
    assert lines[0].startswith("SOFI ") and lines[0].endswith("    49")


def test_panel_reads_the_contract_in_prose(row):
    ex = explain(row)
    assert "SOFI 17.00 PUT" in ex[0] and "38 gün" in ex[0] and "spot 18.22" in ex[0]
    assert "+$64 prim" in ex[1] and "$1700 nakit" in ex[1]
    assert "~%30" in ex[1]  # delta read as assignment odds
    assert "kazanç 2026-10-27, vadeden sonra" in ex[1]
    assert "başabaş 16.36" in ex[2] and "%10.2 altı" in ex[2]  # strike - mid, measured off spot
    assert "±%15.9" in ex[2]  # IV 49.3 * sqrt(38/365)
    assert "skor 49" in ex[3] and "IV 49.3 vs RV 64.6" in ex[3]


def test_panel_flags_earnings_inside_the_expiry(row):
    assert "VADE İÇİNDE" in explain(dataclasses.replace(row, earn=dt.date(2026, 10, 1)))[1]
    assert "bilinen kazanç tarihi yok" in explain(dataclasses.replace(row, earn=None))[1]


def test_panel_admits_missing_history(row):
    assert "yeterli fiyat geçmişi yok" in explain(row)[4]


def test_panel_reports_the_backtest_when_history_exists(row):
    px = [row.spot] * 400
    assert "%0 atama" in explain(row, px=px)[4]


def test_every_sort_key_orders_the_rows(row):
    rows = [
        dataclasses.replace(row, sym="F", score=30, ev=-5.0),
        dataclasses.replace(row, sym="F", score=55, ev=12.0),
        dataclasses.replace(row, sym="NOK", score=41, ev=3.0),
    ]
    best = best_per_symbol(rows)
    assert sorted(r.sym for r in best) == ["F", "NOK"]
    assert next(r for r in best if r.sym == "F").score == 55  # the best, not the first
    for key, sign in SORTS:
        ordered = sorted(rows, key=lambda r: sign * (getattr(r, key) or 0))
        vals = [sign * (getattr(r, key) or 0) for r in ordered]
        assert vals == sorted(vals), key


def test_usd_always_carries_its_sign():
    assert usd(-3.4) == "-$3"
    assert usd(0) == "+$0"
    assert usd(1234.6) == "+$1235"


# ------------------------------------------------------------------------------ EV$ cell
def test_ev_prints_an_em_dash_when_it_was_never_measured(row, history):
    """A printed 0 claims the contract breaks even historically; no data is not that claim."""
    history("SOFI", [18.0] * 400)
    measured = dataclasses.replace(row, ev_n=None)
    assert ev_cell(measured).strip() not in ("—", "")
    assert measured.ev_n == 400 - 26

    unmeasured = dataclasses.replace(row, sym="NOHIST", ev_n=None)
    assert ev_cell(unmeasured).strip() == "—"
    assert len(ev_cell(unmeasured)) == 6  # still the column's width


# ------------------------------------------------------------------------------ replay table
def trade(sym, entry, exp, pl, **over):
    t = dict(
        sym=sym,
        entry=entry,
        exp=exp,
        dte=32,
        strike=9.0,
        credit=0.25,
        spot=10.0,
        settle=10.0,
        score=44,
        collat=900.0,
        pl=pl,
    )
    return {**t, **over}


def test_the_trade_table_names_the_symbol_of_every_row(capsys):
    from csp.backtest import replay_stats

    trades = [trade("NOK", "2026-01-05", "2026-02-06", 25.0), trade("SOFI", "2026-01-05", "2026-02-06", 64.0)]
    still_open = [trade("NOK", "2026-02-09", "2026-03-13", 0.0)]
    print_trades(trades, replay_stats(trades, still_open), still_open)
    out = capsys.readouterr().out

    assert "NOK" in out and "SOFI" in out
    assert "2 sembol" in out
    assert "hâlâ açık" in out and "sonuçlara girmiyor" in out
    assert "$1800" in out  # both held at once: the peak, not the larger single collateral
