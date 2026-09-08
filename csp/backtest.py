"""Two backtests, both honest about what they know.

`ev()` needs no extra data: it replays today's real premium over the symbol's own price history.
`replay()` is the real one: it trades recorded option chains. Nothing here ever prices an option
from a model — the premium is always a quote that existed.
"""

import datetime as dt
import sqlite3

from .cache import CHAINS
from .score import Candidate, occ, parse_occ, passes, realized_vol, score_contract
from .sources import chain, close_on, known_closes

DDL = """create table if not exists puts (
  date text, sym text, exp text, strike real, bid real, ask real, iv real, delta real,
  oi real, spot real, primary key (date, sym, exp, strike))"""


# ------------------------------------------------------------------ path-based EV, no new data
def backtest(row, px):
    """Today's real premium against this symbol's own history of `dte`-day moves.

    No free source has historical option prices for small caps, so nothing here prices an
    option: the credit is the live mid, only the underlying path is empirical. Settled at
    expiry, `credit - (strike - S_T) * 100`: no early assignment, no roll.
    ponytail: overlapping windows, so the sample is autocorrelated — this bounds, never proves.
    """
    steps = max(1, round(row.dte * 252 / 365))  # DTE is calendar days; the bars are sessions
    if not px or len(px) < steps + 120:
        return None  # under ~6 months of overlap says nothing
    otm = row.strike / row.spot - 1  # negative: how far below spot the strike is
    credit = row.mid * 100
    pl = [
        credit + min(0.0, (px[i + steps] / px[i] - 1) - otm) * row.spot * 100 for i in range(len(px) - steps)
    ]
    n = len(pl)
    return dict(
        n=n,
        years=round(len(px) / 252, 1),
        assign=sum(1 for x in pl if x < credit) / n,
        loss=sum(1 for x in pl if x < 0) / n,
        mean=sum(pl) / n,
        worst=min(pl),
    )


def ev(row):
    """Mean historical P&L of this exact contract, dollars. Memoized: the UI redraws 5x a second."""
    if row.ev is None:
        bt = backtest(row, known_closes(row.sym))
        row.ev = bt["mean"] if bt else 0.0
    return row.ev


# --------------------------------------------------- recording chains, so a real backtest exists
def snap_db(path=None):
    con = sqlite3.connect(path or CHAINS)
    con.execute(DDL)
    return con


def snapshot(tickers, max_dte=70, path=None):
    """Append today's put chains to a local SQLite.

    Free vendors do not cover the names a small account can sell (measured: DoltHub's 2019+
    chain set has MARA but not SOFI/PLUG/NOK, and its SQL endpoint answers in ~45s). We already
    download these chains to score them, so keeping them is the only free way to own real
    option history: one row per (date, sym, expiration, strike), quotes exactly as recorded.
    """
    today = dt.date.today()
    rows = []
    for t in tickers:
        try:
            d = chain(t)
        except Exception:
            continue  # a dead ticker must not abort the day's recording
        spot = d["close"] or d["current_price"]
        for o in d["options"]:
            exp, cp, strike = parse_occ(o["option"])
            if cp != "P" or not 0 <= (exp - today).days <= max_dte:
                continue
            rows.append(
                (
                    today.isoformat(),
                    t.upper(),
                    exp.isoformat(),
                    strike,
                    o["bid"],
                    o["ask"],
                    (o.get("iv") or 0) * 100,
                    o["delta"],
                    o["open_interest"],
                    spot,
                )
            )
    con = snap_db(path)
    with con:
        con.executemany("insert or replace into puts values (?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def replay(sym, f, path=None):
    """Replay recorded chains: same filters, same score, real bid, held to expiry.

    One position at a time — a new one only after the last expires. Sold at the recorded BID
    (the fill you would actually get), settled against the underlying close on expiration day.
    No rolls, no early close, no wheel, and no earnings filter (nobody records past estimates).
    """
    sym = sym.upper()
    con = snap_db(path)
    rows = con.execute(
        "select date, exp, strike, bid, ask, iv, delta, oi, spot from puts where sym = ? order by date",
        (sym,),
    ).fetchall()
    days = {}
    for r in rows:
        days.setdefault(r[0], []).append(r)
    trades, busy_until = [], ""
    for date in sorted(days):
        if date <= busy_until:
            continue
        today, best = dt.date.fromisoformat(date), None
        rv = realized_vol(sym, before=date)  # only closes up to that day: no lookahead
        for _, exp, strike, bid, ask, iv, delta, oi, spot in days[date]:
            dte = (dt.date.fromisoformat(exp) - today).days
            if not passes(f, dte, strike, bid, ask, delta, oi):
                continue
            sc = score_contract(spot, iv or 0.0, rv, strike, dte, bid, ask, oi)[0]
            if not best or sc > best[0]:
                best = (sc, exp, strike, bid, spot, dte, delta)
        if not best:
            continue
        sc, exp, strike, bid, spot, dte, delta = best
        settle = close_on(sym, exp)
        if settle is None:
            break  # expiry still ahead: an open position, not a result
        trades.append(
            dict(
                sym=sym,
                entry=date,
                exp=exp,
                dte=dte,
                strike=strike,
                credit=bid,
                spot=spot,
                delta=delta,
                settle=settle,
                score=sc,
                collat=strike * 100,
                pl=bid * 100 + min(0.0, settle - strike) * 100,
            )
        )
        busy_until = exp
    return trades


def replay_stats(trades):
    if not trades:
        return None
    pl = [t["pl"] for t in trades]
    held = sum(t["dte"] for t in trades) or 1
    tied = max(t["collat"] for t in trades)  # worst single cash commitment: the real denominator
    run = peak = low = 0.0
    for x in pl:  # equity-curve drawdown, in dollars
        run += x
        peak = max(peak, run)
        low = min(low, run - peak)
    return dict(
        n=len(trades),
        total=sum(pl),
        mean=sum(pl) / len(pl),
        worst=min(pl),
        wins=sum(1 for x in pl if x > 0) / len(pl),
        assigned=sum(1 for t in trades if t["settle"] < t["strike"]) / len(trades),
        days=held,
        tied=tied,
        drawdown=low,
        ann=sum(pl) / tied * 365 / held if tied else 0.0,
        first=trades[0]["entry"],
        last=trades[-1]["exp"],
    )


def recorded(path=None):
    """(rows, days, symbols) currently in the local chain store."""
    with snap_db(path) as con:
        return con.execute("select count(*), count(distinct date), count(distinct sym) from puts").fetchone()


__all__ = ["backtest", "ev", "snap_db", "snapshot", "replay", "replay_stats", "recorded", "Candidate", "occ"]
