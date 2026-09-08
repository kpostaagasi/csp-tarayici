"""Where the numbers come from: CBOE delayed chains and daily bars, Nasdaq earnings and screener."""

import bisect
import datetime as dt
import re

from .cache import EARNINGS, HIST, UNIV, cached, store
from .http import get

CHAIN_TTL = 600  # seconds; changing a filter must not re-download a chain
_chains = {}  # sym -> (monotonic, data)
_px, _dates = {}, {}  # sym -> closes / their ISO dates; the UI must never fetch


def chain(sym):
    """Spot, iv30 and the full option chain with greeks.

    Held for CHAIN_TTL: quotes are delayed anyway, a 60-name scan costs minutes, and every
    filter knob re-reads the same chain. Clearing `_chains` forces a refetch.
    """
    import time

    sym = sym.upper()
    hit = _chains.get(sym)
    if hit and time.monotonic() - hit[0] < CHAIN_TTL:
        return hit[1]
    d = get(f"https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json")["data"]
    _chains[sym] = (time.monotonic(), d)
    return d


def forget_chains():
    _chains.clear()


def history(sym):
    """(closes, dates) — every daily bar CBOE has, 1.5 to 22 years depending on the listing."""
    sym = sym.upper()
    if sym in _px:
        return _px[sym], _dates[sym]
    hit = cached(HIST).get(sym)
    if hit and hit["at"] == dt.date.today().isoformat() and "d" in hit:
        px, ds = hit["px"], hit["d"]
    else:
        url = f"https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/{sym}.json"
        bars = [(b["date"], b["close"]) for b in get(url)["data"] if b["close"] > 0]
        px, ds = [c for _, c in bars], [d for d, _ in bars]  # pre-listing rows quote 0.0
        store(HIST, sym, {"px": px, "d": ds, "at": dt.date.today().isoformat()})
    _px[sym], _dates[sym] = px, ds
    return px, ds


def closes(sym):
    return history(sym)[0]


def known_closes(sym):
    """Closes already in memory from this session. Never fetches: the UI loop must not block."""
    return _px.get(sym.upper())


def close_on(sym, iso):
    """Last close at or before `iso`, or None if the series ends before it (expiry still ahead)."""
    px, ds = history(sym)
    i = bisect.bisect_right(ds, iso)
    return px[i - 1] if i and ds[-1] >= iso else None


def earnings_date(sym, ttl_days=7):
    """Estimated next report date. Cached: it moves once a quarter, not once a scan."""
    sym = sym.upper()
    hit = cached(EARNINGS).get(sym)
    if hit and (dt.date.today() - dt.date.fromisoformat(hit["at"])).days < ttl_days:
        return dt.date.fromisoformat(hit["d"]) if hit["d"] else None
    try:
        txt = get(f"https://api.nasdaq.com/api/analyst/{sym}/earnings-date")["data"]["reportText"]
        m = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", txt)
        d = dt.datetime.strptime(m.group(1), "%m/%d/%Y").date() if m else None
    except Exception:
        return None  # unknown date: treated as "no known event"
    store(EARNINGS, sym, {"d": d.isoformat() if d else None, "at": dt.date.today().isoformat()})
    return d


def us_stocks():
    """Every US-listed stock with price/volume/market cap. One screener request, cached daily."""
    c = cached(UNIV)
    if c.get("at") == dt.date.today().isoformat():
        return c["rows"]
    raw = get("https://api.nasdaq.com/api/screener/stocks?tableonly=false&limit=25000&download=true")
    rows = []
    for r in raw["data"]["rows"]:
        try:
            rows.append(
                [
                    r["symbol"],
                    float(r["lastsale"].lstrip("$").replace(",", "")),
                    int(r["volume"] or 0),
                    float(r["marketCap"] or 0),
                ]
            )
        except ValueError:
            continue  # units, warrants and fresh listings quote '' or 'NA'
    store(UNIV, "at", dt.date.today().isoformat())
    store(UNIV, "rows", rows)
    return rows
