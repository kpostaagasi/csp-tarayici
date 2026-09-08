import datetime as dt

import pytest

from csp import sources
from csp.score import Candidate


@pytest.fixture
def row():
    """The SOFI 17 put as it actually printed on 2026-09-08, for renderers and backtests."""
    return Candidate(
        sym="SOFI",
        spot=18.22,
        strike=17.0,
        exp=dt.date(2026, 10, 16),
        dte=38,
        delta=-0.30,
        mid=0.64,
        iv=49.3,
        iv30=47.0,
        rv30=64.6,
        spread=0.016,
        oi=7175,
        roc=0.359,
        cushion=0.42,
        collat=1700.0,
        score=49,
        parts={"vrp": 0.0, "liq": 0.93, "yield": 0.87, "cushion": 0.42},
        earn=dt.date(2026, 10, 27),
    )


@pytest.fixture
def history(monkeypatch):
    """Install a fake price series for a symbol without touching the network or the disk cache."""

    def install(sym, px, dates=None):
        dates = dates or [f"2020-01-{i % 28 + 1:02d}" for i in range(len(px))]
        monkeypatch.setitem(sources._px, sym, px)
        monkeypatch.setitem(sources._dates, sym, dates)

    return install
