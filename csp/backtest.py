"""Two backtests, both honest about what they know.

`ev()` needs no extra data: it replays today's real premium over the symbol's own price history.
`replay()` is the real one: it trades recorded option chains. Nothing here ever prices an option
from a model — the premium is always a quote that existed, entry at a recorded bid and, when a
profit target closes a position early, exit at a recorded ask.
"""

import datetime as dt
from collections import Counter

from .cache import snap_db
from .score import (
    Candidate,
    iv_levels,
    iv_rank,
    levels_upto,
    occ,
    parse_occ,
    realized_vol,
    reject,
    score_contract,
)
from .sources import chain, close_on, known_closes, session_date


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
    """Mean historical P&L of this exact contract, dollars. Memoized: the UI redraws 5x a second.

    `row.ev_n` carries how many windows that mean rests on, so a symbol with no usable price
    history (ev_n == 0) stays distinguishable from one that really does average zero. Sorting
    still gets a number; only the renderer is allowed to care about the difference.
    """
    if row.ev_n is None:
        bt = backtest(row, known_closes(row.sym))
        row.ev, row.ev_n = (bt["mean"], bt["n"]) if bt else (0.0, 0)
    return row.ev


# --------------------------------------------------- recording chains, so a real backtest exists
def snapshot(tickers, max_dte=70, path=None):
    """Append the latest session's put chains to a local SQLite. Returns (rows, session date).

    Free vendors do not cover the names a small account can sell (measured: DoltHub's 2019+
    chain set has MARA but not SOFI/PLUG/NOK, and its SQL endpoint answers in ~45s). We already
    download these chains to score them, so keeping them is the only free way to own real
    option history: one row per (date, sym, expiration, strike), quotes exactly as recorded.

    Rows are stamped with the session the quotes came from, not the calendar day the job ran.
    That distinction is the whole difference between a usable history and a corrupt one: run on
    a market holiday, or before the open, and the vendor serves the previous session — stamping
    it "today" would invent a day in the IV rank series and an entry date the replay would trade
    on. Stamped by session, the same quotes land on the same primary key and the write is an
    idempotent no-op instead.
    """
    chains = []
    for t in tickers:
        try:
            d = chain(t)
        except Exception:
            continue  # a dead ticker must not abort the day's recording
        chains.append((t.upper(), d))

    dates = [d for _, c in chains if (d := session_date(c))]
    if not dates:
        return 0, None  # no vendor timestamp anywhere: a payload change, not a holiday
    # one date for the whole run: replay() treats each recorded date as one decision point, so a
    # snapshot split across sessions would hand it a half-populated universe on both of them.
    session = max(dates)

    rows = []
    for sym, d in chains:
        spot = d["close"] or d["current_price"]
        for o in d["options"]:
            exp, cp, strike = parse_occ(o["option"])
            if cp != "P" or not 0 <= (exp - session).days <= max_dte:
                continue
            rows.append(
                (
                    session.isoformat(),
                    sym,
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
    return len(rows), session


def recorded_symbols(path=None):
    """Every symbol the local chain store has ever seen, alphabetically."""
    with snap_db(path) as con:
        return [r[0] for r in con.execute("select distinct sym from puts order by sym")]


def _settle(pos, trades):
    """Move a position into `trades` once the underlying has a close on its expiration day.

    False means the price series stops short of that expiry, i.e. the position is still open.
    An open position is not a result and is never counted: no lookahead, and no half-trades.
    """
    px = close_on(pos["sym"], pos["exp"])
    if px is None:
        return False
    trades.append(
        {
            **pos,
            "exit": pos["exp"],  # the day the cash came back; for a held contract, expiry
            "settle": px,
            "buyback": None,
            "pl": pos["credit"] * 100 + min(0.0, px - pos["strike"]) * 100,
        }
    )
    return True


def _close_early(pos, date, ask, trades):
    """Buy the put back at a quote that existed, on the day it existed.

    The entry was filled at the recorded bid, so the exit is filled at the recorded ask: the two
    halves of a round turn cost the spread, which is exactly what a profit target gives up in
    exchange for the cash and the tail risk it hands back.
    """
    trades.append(
        {
            **pos,
            "exit": date,
            "settle": None,  # never assigned: the position was gone before expiry
            "buyback": ask,
            "pl": (pos["credit"] - ask) * 100,
        }
    )


def replay(syms, f, path=None, max_per_sym=1, take_profit=None):
    """Replay recorded chains as a portfolio: same filters, same score, real bid, real ask.

    Returns `(trades, still_open)`. Capital is the only thing rationing entries — each day the
    engine settles what expired, recomputes uncommitted cash, and fills the best-scoring
    contracts that fit, at most `max_per_sym` per underlying so one name cannot become the whole
    book. Sold at the recorded BID (the fill you would actually get), settled against the
    underlying close on expiration day. No rolls, no wheel, and no earnings filter (nobody
    records past estimates).

    `take_profit` (0..1, None = hold every contract to expiry) is the one management rule this
    store can actually answer: a position whose recorded ASK has fallen to `1 - take_profit` of
    the credit is bought back that day. It is a real question rather than a preference — closing
    at half the max profit gives up the spread and the rest of the decay, and buys back the
    cash and the tail. Both sides of that trade land in the numbers, since the freed collateral
    is what funds the next entry.

    `syms` takes one symbol, a list, or None for everything recorded. A single name with capital
    for one contract behaves exactly like the one-position-at-a-time engine this replaces.
    ponytail: entries are filled at the same day's recorded quote for every position opened that
    day, so a portfolio that fills 5 contracts assumes 5 fills at those quotes.
    """
    if isinstance(syms, str):
        syms = [syms]
    syms = sorted({s.upper() for s in syms}) if syms else recorded_symbols(path)
    if not syms:
        return [], []
    cols = "date, sym, exp, strike, bid, ask, iv, delta, oi, spot"
    holes = ",".join("?" * len(syms))
    con = snap_db(path)
    rows = con.execute(f"select {cols} from puts where sym in ({holes}) order by date", syms).fetchall()

    days = {}
    for r in rows:
        days.setdefault(r[0], []).append(r)

    levels = {s: iv_levels(s, path) for s in syms}  # read once, then sliced per date below
    trades, open_pos, rv, ivr = [], [], {}, {}
    for date in sorted(days):
        open_pos = [p for p in open_pos if not (p["exp"] <= date and _settle(p, trades))]
        quotes = {(r[1], r[2], r[3]): r[5] for r in days[date]}  # (sym, exp, strike) -> ask
        gone = set()
        if take_profit:
            kept = []
            for p in open_pos:
                ask = quotes.get((p["sym"], p["exp"], p["strike"]), 0.0)
                # an ask of 0 is no offer, not a free buyback: without a quote there is no trade
                if 0 < ask <= p["credit"] * (1 - take_profit):
                    _close_early(p, date, ask, trades)
                    gone.add((p["sym"], p["exp"], p["strike"]))
                else:
                    kept.append(p)
            open_pos = kept
        free = f.capital - sum(p["collat"] for p in open_pos)
        held = Counter(p["sym"] for p in open_pos)
        today = dt.date.fromisoformat(date)

        picks = []
        for _, sym, exp, strike, bid, ask, iv, delta, oi, spot in days[date]:
            dte = (dt.date.fromisoformat(exp) - today).days
            if (sym, date) not in ivr:
                seen = levels_upto(levels[sym], date)  # the rank as it was knowable that day
                today_level = seen[-1][1] if seen and seen[-1][0] == date else None
                ivr[sym, date] = iv_rank(today_level, seen)[0]
            if reject(f, dte, strike, bid, ask, delta, oi, cash=free, iv_rank=ivr[sym, date]):
                continue
            if (sym, date) not in rv:
                rv[sym, date] = realized_vol(sym, before=date)  # only closes up to that day
            sc = score_contract(spot, iv or 0.0, rv[sym, date], strike, dte, bid, ask, oi)[0]
            picks.append((sc, sym, exp, strike, bid, spot, dte, delta))

        for sc, sym, exp, strike, bid, spot, dte, delta in sorted(picks, key=lambda p: -p[0]):
            collat = strike * 100
            if collat > free or held[sym] >= max_per_sym:
                continue
            if (sym, exp, strike) in gone:
                continue  # closing at the ask and re-selling at the bid the same day is a wash
            if any(p["sym"] == sym and p["exp"] == exp and p["strike"] == strike for p in open_pos):
                continue
            open_pos.append(
                dict(
                    sym=sym,
                    entry=date,
                    exp=exp,
                    dte=dte,
                    strike=strike,
                    credit=bid,
                    spot=spot,
                    delta=delta,
                    score=sc,
                    collat=collat,
                )
            )
            free -= collat
            held[sym] += 1

    open_pos = [p for p in open_pos if not _settle(p, trades)]
    trades.sort(key=lambda t: (t["exit"], t["entry"], t["sym"]))
    return trades, open_pos


def peak_committed(trades):
    """Most cash ever committed at once: the denominator a portfolio return has to use.

    Same-day releases are applied before that day's entries, which is the order the engine
    itself fills in — the cash a contract frees is spendable the day it is resolved. That is
    `exit`, not `exp`: a position bought back early hands its collateral back early too.
    """
    events = [(t["entry"], t["collat"]) for t in trades] + [(t["exit"], -t["collat"]) for t in trades]
    run = peak = 0.0
    for _, delta in sorted(events, key=lambda e: (e[0], e[1])):
        run += delta
        peak = max(peak, run)
    return peak


def replay_stats(trades, still_open=()):
    if not trades:
        return None
    pl = [t["pl"] for t in trades]  # trades arrive in settlement order: this is the equity curve
    tied = peak_committed(trades)
    # trades are ordered by resolution, so trades[0] is the first to pay out, not the first to open
    first, last = min(t["entry"] for t in trades), max(t["exit"] for t in trades)
    span = (dt.date.fromisoformat(last) - dt.date.fromisoformat(first)).days
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
        assigned=sum(1 for t in trades if t["settle"] is not None and t["settle"] < t["strike"])
        / len(trades),
        early=sum(1 for t in trades if t["buyback"] is not None),
        # position-days actually held, so an early close counts the days it ran, not its DTE
        deployed=sum(_days(t["entry"], t["exit"]) for t in trades),
        days=max(span, 1),  # calendar days the book was alive, idle stretches included
        tied=tied,
        drawdown=low,
        ann=sum(pl) / tied * 365 / max(span, 1) if tied else 0.0,
        syms=len({t["sym"] for t in trades}),
        first=first,
        last=last,
        open_n=len(still_open),
        open_tied=sum(p["collat"] for p in still_open),
    )


def _days(a, b):
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days


def recorded(path=None):
    """(rows, days, symbols) currently in the local chain store."""
    with snap_db(path) as con:
        return con.execute("select count(*), count(distinct date), count(distinct sym) from puts").fetchone()


__all__ = [
    "backtest",
    "ev",
    "snap_db",
    "snapshot",
    "replay",
    "replay_stats",
    "peak_committed",
    "recorded",
    "recorded_symbols",
    "Candidate",
    "occ",
]
