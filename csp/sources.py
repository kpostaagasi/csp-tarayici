"""Where the numbers come from: CBOE delayed chains and daily bars, Nasdaq earnings and screener."""

import bisect
import datetime as dt
import pathlib
import re

from .cache import CHAINS, EARNINGS, HIST, UNIV, cached, snap_db, store
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


ERR_TTL_DAYS = 1  # a vendor outage must not be remembered as long as a real answer


def earnings_date(sym, ttl_days=7):
    """Estimated next report date. Cached: it moves once a quarter, not once a scan.

    Failures are cached too, for a shorter day: Nasdaq refusing one symbol used to cost a fresh
    request on every scan, and every request spends the CBOE rate gate that the scan is queued
    behind. A cached failure still reads as "no known event" — it never invents a date.
    """
    sym = sym.upper()
    today = dt.date.today()
    hit = cached(EARNINGS).get(sym)
    if hit:
        ttl = ERR_TTL_DAYS if hit.get("err") else ttl_days
        if (today - dt.date.fromisoformat(hit["at"])).days < ttl:
            return dt.date.fromisoformat(hit["d"]) if hit["d"] else None
    try:
        txt = get(f"https://api.nasdaq.com/api/analyst/{sym}/earnings-date")["data"]["reportText"]
        m = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", txt)
        d = dt.datetime.strptime(m.group(1), "%m/%d/%Y").date() if m else None
    except Exception:
        store(EARNINGS, sym, {"d": None, "at": today.isoformat(), "err": True})
        return None  # unknown date: treated as "no known event"
    store(EARNINGS, sym, {"d": d.isoformat() if d else None, "at": today.isoformat()})
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


def recorded_ivs(sym, path=None, dte=None, delta=None):
    """(date, iv) for every recorded put of `sym` inside the given DTE and |delta| bands.

    The chain store is the only IV history that exists for these names — no free vendor sells
    one — so it is read here alongside the vendors rather than pretended to be a live feed. The
    bands are filtered in SQL because a year of one symbol's chains is tens of thousands of rows
    and the TUI re-scans on every keypress; the caller owns what the bands are.
    """
    store_path = pathlib.Path(path or CHAINS)
    if not store_path.exists():
        return []  # reading must not conjure a database on a machine that never snapshots
    where = ["sym = ?", "iv > 0"]
    args = [sym.upper()]
    if dte is not None:
        where.append("julianday(exp) - julianday(date) between ? and ?")
        args += list(dte)
    if delta is not None:
        where.append("abs(delta) between ? and ?")
        args += list(delta)
    with snap_db(path) as con:
        sql = f"select date, iv from puts where {' and '.join(where)} order by date"
        return con.execute(sql, args).fetchall()
