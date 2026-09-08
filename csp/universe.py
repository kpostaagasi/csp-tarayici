"""Which symbols are even worth a request."""

import re

from .sources import us_stocks


def universe(capital, limit=60, min_volume=1_000_000, min_cap=3e8, rows=None):
    """Names you can actually cash-secure: cheap enough for the capital, liquid enough to exit.

    Sorted by share volume. With a $2k account the affordable half of the market is ~1500
    tickers and each costs two rate-limited CBOE requests, so the cut happens before the
    network does. `rows` overrides the screener dump (tests, or your own list).
    """
    picks = [
        r
        for r in (us_stocks() if rows is None else rows)
        if re.fullmatch(r"[A-Z]{1,5}", r[0])
        and 1.0 < r[1] <= capital / 100
        and r[2] >= min_volume
        and r[3] >= min_cap
    ]
    picks.sort(key=lambda r: -r[2])
    return [r[0] for r in picks[:limit]]
