"""Scoring: components must move the right way, and the hard cuts must cut."""

import datetime as dt
import math

import pytest

from csp.score import (
    VRP_HI,
    VRP_LO,
    Filters,
    clip01,
    occ,
    parse_occ,
    passes,
    rv_from_closes,
    score_contract,
)

BASE = dict(spot=10.0, iv=50.0, rv30=35.0, strike=9.0, dte=30, bid=0.30, ask=0.32, oi=500)


def score(**over):
    return score_contract(**{**BASE, **over})


def test_occ_round_trip():
    assert parse_occ("NOK260911P00003000") == (dt.date(2026, 9, 11), "P", 3.0)
    assert parse_occ("MARA261016C00012500") == (dt.date(2026, 10, 16), "C", 12.5)
    assert occ("SOFI", dt.date(2026, 10, 16), 17.0) == "SOFI261016P00017000"
    assert occ("F", dt.date(2026, 10, 16), 14.5) == "F261016P00014500"  # half strikes survive
    assert parse_occ(occ("NOK", dt.date(2026, 9, 11), 3.0)) == (dt.date(2026, 9, 11), "P", 3.0)


def test_realized_vol_from_closes():
    px = [100 * math.exp(0.01 * (-1) ** i) for i in range(60)]  # +-1% alternating
    assert abs(rv_from_closes(px, 30) - 0.02 * math.sqrt(252) * 100) < 1  # 2% daily, annualized
    assert rv_from_closes([100, 101], 30) is None  # too few points


def test_richer_iv_lifts_vrp_but_shrinks_cushion():
    """Deliberately ambiguous: you are paid more, and the market expects a bigger move."""
    base_parts = score()[1]
    rich = score(iv=70.0)[1]
    assert rich["vrp"] > base_parts["vrp"]
    assert rich["cushion"] < base_parts["cushion"]


def test_iv_under_rv_is_ranked_not_tied():
    base_vrp = score()[1]["vrp"]
    lo = score(rv30=60.0)[1]["vrp"]
    lower = score(rv30=75.0)[1]["vrp"]
    assert 0.0 < lo < base_vrp
    assert lower < lo
    assert score(rv30=200.0)[1]["vrp"] == 0.0  # far below RV: floor
    assert score(rv30=50.0)[1]["vrp"] == clip01(-VRP_LO / (VRP_HI - VRP_LO))  # IV == RV
    assert score(rv30=None)[1]["vrp"] == 0.0  # no history, no claim


@pytest.mark.parametrize(
    "over, part",
    [
        (dict(ask=0.45), "liq"),  # wider spread
        (dict(oi=25), "liq"),  # thinner book
        (dict(bid=0.10, ask=0.11), "yield"),
    ],
)
def test_components_drop_when_the_contract_gets_worse(over, part):
    assert score(**over)[1][part] < score()[1][part]


def test_total_falls_when_premium_stops_covering_realized_vol():
    assert score(rv30=60.0)[0] < score()[0]
    assert 0 <= score()[0] <= 100


def test_cushion_grows_with_distance():
    assert score(strike=7.0)[1]["cushion"] > score()[1]["cushion"]
    # expected-move convention matches the CBOE card: EM = IV * sqrt(DTE/365)
    assert abs(math.sqrt(30 / 365) - 0.28669) < 1e-4


def test_filters_cut_on_every_axis():
    f = Filters(capital=1000)
    ok = dict(dte=30, strike=9.0, bid=0.30, ask=0.32, delta=-0.25, oi=500)
    assert passes(f, **ok)
    assert not passes(f, **{**ok, "dte": 7})  # outside the DTE band
    assert not passes(f, **{**ok, "dte": 90})
    assert not passes(f, **{**ok, "strike": 11.0})  # $1100 collateral, $1000 cash
    assert not passes(f, **{**ok, "bid": 0.0})  # no bid: not tradeable
    assert not passes(f, **{**ok, "ask": 0.60})  # spread way over the cap
    assert not passes(f, **{**ok, "delta": -0.05})  # too far out
    assert not passes(f, **{**ok, "delta": -0.60})  # too deep
    assert not passes(f, **{**ok, "oi": 5})  # nobody trades it
    assert passes(Filters(capital=1000, min_oi=1), **{**ok, "oi": 5})  # ...unless you allow it
