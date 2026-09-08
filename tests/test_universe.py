"""The universe cut happens before the network, so every rejection has to be deliberate."""

from csp.universe import universe

LISTED = [
    ["SOFI", 18.22, 40_000_000, 2.2e10],  # affordable, busy, real
    ["NOK", 10.03, 20_000_000, 5.5e10],
    ["NVDA", 230.36, 200_000_000, 5.5e12],  # $23k of collateral: not with $2k
    ["PENNY", 0.42, 90_000_000, 4e8],  # sub-$1: no options worth selling
    ["THIN", 12.0, 900, 4e8],  # nobody trades it
    ["SMALL", 9.0, 5_000_000, 1e8],  # micro cap
    ["BRK.A", 12.0, 9_000_000, 4e8],
]  # dotted root: its chain lives elsewhere


def test_busiest_affordable_names_first():
    assert universe(2000, rows=LISTED) == ["SOFI", "NOK"]


def test_limit_truncates_after_sorting():
    assert universe(2000, limit=1, rows=LISTED) == ["SOFI"]


def test_more_cash_reaches_more_of_the_tape():
    assert universe(25000, rows=LISTED) == ["NVDA", "SOFI", "NOK"]


def test_thresholds_can_empty_the_list():
    assert universe(2000, min_volume=10**9, rows=LISTED) == []
    assert universe(50, rows=LISTED) == []  # $50 buys no 100-share commitment
