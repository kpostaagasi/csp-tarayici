"""What a short put is worth, and which ones survive the filters.

Two separate ideas, deliberately not shared:
  * `Filters` decides what may enter the list (hard cuts, tuned per account).
  * The scoring scales below decide how good an entrant is. They are fixed, so widening a
    filter no longer silently re-scales the score.
"""

import bisect
import concurrent.futures as cf
import datetime as dt
import math
import re
from dataclasses import dataclass, field

from .sources import chain, earnings_date, history, recorded_ivs

W = {"vrp": 0.35, "liq": 0.25, "yield": 0.20, "cushion": 0.20}  # must sum to 1
VRP_LO, VRP_HI = -0.20, 0.40  # log(IV/RV) mapped onto 0..1; IV under RV is ranked, not tied at 0
SPREAD_SCALE = 0.10  # relative spread that scores 0 on the liquidity component
OI_SCALE, OI_DEEP = 25, 500  # open interest that scores 0 and 1
YIELD_LO, YIELD_HI = 0.08, 0.40  # annualized return on collateral mapped onto 0..1
WORKERS = 8  # the 0.35s request gate is the limit, not the CPU

# IV rank reads the same band every day, on purpose: if it followed `Filters` then widening the
# DTE knob would silently re-rank every symbol, which is the trap the scoring scales above avoid.
IVR_DTE_LO, IVR_DTE_HI = 21, 45
IVR_DELTA_LO, IVR_DELTA_HI = 0.10, 0.50  # the strikes worth selling; the wings move on their own
IVR_MIN_DAYS = 20  # fewer recorded days than this and a "rank" is just noise with a number on it
IVR_WINDOW = 252  # a year of recorded days, the convention every other IV rank uses


@dataclass
class Filters:
    """Hard cuts. One object flows from the CLI through the scan and the backtest."""

    capital: float = 2000.0
    dte_min: int = 21
    dte_max: int = 45
    delta_lo: float = 0.15
    delta_hi: float = 0.35
    max_spread: float = 0.10
    min_oi: int = 25
    allow_earnings: bool = False
    min_iv_rank: float | None = None  # 0..1; None = off, which is what a fresh install has


@dataclass
class Candidate:
    """One short put worth looking at, plus everything the UI needs to explain it."""

    sym: str
    spot: float
    strike: float
    exp: dt.date
    dte: int
    delta: float
    mid: float
    iv: float  # the contract's own implied vol, %
    iv30: float  # the underlying's ATM 30-day IV, %, for context only
    rv30: float | None
    spread: float
    oi: float
    roc: float
    cushion: float
    collat: float
    score: int
    parts: dict
    earn: dt.date | None = None
    iv_rank: float | None = None  # 0..1 against this machine's own recorded IV; None = not enough
    iv_rank_n: int = 0  # recorded days behind it, so the panel can show how thin the sample is
    ev: float = 0.0  # memoized mean historical P&L, dollars; only meaningful once ev_n is set
    ev_n: int | None = field(default=None)  # windows behind ev. None = not computed, 0 = unmeasurable


def parse_occ(s):
    """NOK260911P00003000 -> (date(2026, 9, 11), 'P', 3.0)"""
    m = re.fullmatch(r"([A-Z0-9]+?)(\d{6})([CP])(\d{8})", s)
    _, ymd, cp, k = m.groups()
    return dt.date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6])), cp, int(k) / 1000


def occ(sym, exp, strike):
    """The inverse: ('SOFI', 2026-10-16, 17.0) -> SOFI261016P00017000, as CBOE spells it."""
    # ponytail: plain equity roots only; an adjusted root (post-split "F1") will not match
    return f"{sym}{exp:%y%m%d}P{round(strike * 1000):08d}"


def clip01(x):
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def median(xs):
    xs = sorted(xs)
    mid = len(xs) // 2
    return xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2


def iv_level(quotes):
    """One number for "how expensive is this symbol's vol today", or None.

    `quotes` is (dte, iv, delta) for puts, from a live chain or a recorded one — the same call
    on both sides, for the same reason `passes()` is shared: a rank is only meaningful if today
    and every day it is compared against were measured the same way. Median rather than ATM,
    because which strikes are listed changes and a median survives that.
    """
    band = [
        iv
        for dte, iv, delta in quotes
        if IVR_DTE_LO <= dte <= IVR_DTE_HI and iv > 0 and IVR_DELTA_LO <= abs(delta) <= IVR_DELTA_HI
    ]
    return median(band) if band else None


_levels = {}  # (sym, path) -> [(date, level)]; the store changes once a day, not once a keypress


def iv_levels(sym, path=None):
    """Recorded daily IV levels as (date, level), oldest first. Memoized per process.

    Same band and same median as `iv_level()` above — one applied in SQL for the year of rows
    the store holds, one in Python for the chain in hand. `test_a_recorded_day_and_a_live_chain
    _rank_the_same` is what keeps the two honest; there is no way to share the predicate itself
    without reading every row back into Python.
    """
    key = (sym.upper(), str(path) if path else None)
    if key not in _levels:
        days = {}
        for date, iv in recorded_ivs(
            sym, path, dte=(IVR_DTE_LO, IVR_DTE_HI), delta=(IVR_DELTA_LO, IVR_DELTA_HI)
        ):
            days.setdefault(date, []).append(iv)
        _levels[key] = [(d, median(ivs)) for d, ivs in sorted(days.items())]
    return _levels[key]


def forget_iv_levels():
    _levels.clear()


def levels_upto(levels, date):
    """The part of a level series knowable on `date`, that day included.

    The day's own chain is in hand when an entry is decided — the quotes being filled come out of
    it — so dropping it would be its own kind of wrong. Everything after `date` is the lookahead.
    """
    return levels[: bisect.bisect_right([d for d, _ in levels], date)]


def iv_rank(level, levels):
    """(rank 0..1, sample size). Where today's IV sits in the recorded year's own range.

    This is deliberately *not* a score component. Its input is whatever this machine happened to
    record, so a symbol you have tracked for a year and one you added yesterday would be scored
    on different evidence — and the score's whole job is to compare them in one table.
    """
    window = [lv for _, lv in levels[-IVR_WINDOW:]]
    if level is None or len(window) < IVR_MIN_DAYS:
        return None, len(window)
    lo, hi = min(window), max(window)
    return (clip01((level - lo) / (hi - lo)) if hi > lo else 0.5), len(window)


def rv_from_closes(px, window=30):
    rets = [math.log(px[i] / px[i - 1]) for i in range(1, len(px))][-window:]
    if len(rets) < 10:
        return None
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var * 252) * 100


def realized_vol(sym, window=30, before=None):
    """Annualized close-to-close realized vol, %. `before` walks it back to a historical date."""
    px, ds = history(sym)
    end = bisect.bisect_left(ds, before) if before else len(px)
    return rv_from_closes(px[max(0, end - window - 10) : end], window)


def score_contract(spot, iv, rv30, strike, dte, bid, ask, oi):
    """0-100 with transparent components. Every map here is a knob, not a fitted truth.

    `iv` is the short put's own implied vol, not ATM iv30: skew is what you actually sell, and
    it is also the market's own distribution for that strike, so cushion uses it too.
    """
    mid = (bid + ask) / 2
    rel_spread = (ask - bid) / mid
    roc_ann = (mid / strike) * 365 / dte  # return on cash collateral
    em = iv / 100 * math.sqrt(dte / 365)  # expected move to expiry, fraction
    cushion = (spot - strike) / (spot * em)  # OTM distance in expected moves
    c = {
        "vrp": clip01((math.log(iv / rv30) - VRP_LO) / (VRP_HI - VRP_LO)) if rv30 and rv30 > 0 else 0.0,
        "liq": 0.6 * clip01((SPREAD_SCALE - rel_spread) / (SPREAD_SCALE - 0.005))
        + 0.4 * clip01(math.log10(max(oi, 1) / OI_SCALE) / math.log10(OI_DEEP / OI_SCALE)),
        "yield": clip01((roc_ann - YIELD_LO) / (YIELD_HI - YIELD_LO)),
        "cushion": clip01(cushion / 1.0),
    }
    return round(100 * sum(W[k] * v for k, v in c.items())), c, mid, rel_spread, roc_ann, cushion


def passes(f, dte, strike, bid, ask, delta, oi, cash=None, iv_rank=None):
    """The hard cuts, in one place: the scan and the replay must agree on what is tradeable.

    `cash` is the collateral actually available right now, which is the whole account for a live
    scan but only the uncommitted part of it once the replay holds positions. It defaults to
    `f.capital`, so a caller that does not think in portfolios cannot get this wrong.
    """
    cash = f.capital if cash is None else cash
    if not f.dte_min <= dte <= f.dte_max or strike * 100 > cash:
        return False
    if f.min_iv_rank is not None and (iv_rank is None or iv_rank < f.min_iv_rank):
        return False  # asking for a rank floor makes an unrankable symbol a miss, not a pass
    if bid <= 0 or ask <= 0 or (ask - bid) / ((ask + bid) / 2) > f.max_spread:
        return False
    return f.delta_lo <= abs(delta) <= f.delta_hi and oi >= f.min_oi


def scan_symbol(sym, f, today=None):
    """Every contract of one symbol that survives `f`, scored."""
    sym, today = sym.upper(), today or dt.date.today()
    d = chain(sym)
    spot, iv30 = d["close"] or d["current_price"], d["iv30"]
    rv30 = realized_vol(sym)
    earn = earnings_date(sym)
    puts = []
    for o in d["options"]:
        exp, cp, strike = parse_occ(o["option"])
        if cp == "P":
            puts.append((exp, strike, o))
    quotes = [((exp - today).days, (o.get("iv") or 0) * 100, o["delta"]) for exp, _, o in puts]
    ivr, ivr_n = iv_rank(iv_level(quotes), iv_levels(sym))
    out = []
    for exp, strike, o in puts:
        dte = (exp - today).days
        args = (o["bid"], o["ask"], o["delta"], o["open_interest"])
        if not passes(f, dte, strike, *args, iv_rank=ivr):
            continue
        if earn and not f.allow_earnings and today <= earn <= exp:
            continue
        iv = (o.get("iv") or 0) * 100 or iv30  # chain iv is a fraction; fall back to ATM
        sc, parts, mid, spread, roc, cushion = score_contract(
            spot, iv, rv30, strike, dte, o["bid"], o["ask"], o["open_interest"]
        )
        out.append(
            Candidate(
                sym=sym,
                spot=spot,
                strike=strike,
                exp=exp,
                dte=dte,
                delta=o["delta"],
                mid=mid,
                iv=iv,
                iv30=iv30,
                rv30=rv30,
                spread=spread,
                oi=o["open_interest"],
                roc=roc,
                cushion=cushion,
                collat=strike * 100,
                score=sc,
                parts=parts,
                earn=earn,
                iv_rank=ivr,
                iv_rank_n=ivr_n,
            )
        )
    return out


def scan_all(tickers, f, today=None, on_done=None):
    """Scan symbols in parallel. `on_done(n, sym, rows, err)` fires as each lands, in any order."""
    rows, errs = [], []
    with cf.ThreadPoolExecutor(WORKERS) as ex:
        jobs = {ex.submit(scan_symbol, t, f, today): t for t in tickers}
        try:
            for n, fut in enumerate(cf.as_completed(jobs), 1):
                sym, got, err = jobs[fut], [], None
                try:
                    got = fut.result()
                except Exception as e:
                    err = f"{sym}: {type(e).__name__} {e}"
                    errs.append(err)
                rows += got
                if on_done:
                    on_done(n, sym, got, err)
        except BaseException:  # Ctrl-C: drop the queue, or the executor drains it first
            for fut in jobs:
                fut.cancel()
            raise
    return rows, errs


def best_per_symbol(rows):
    """One contract per underlying: the highest-scoring one."""
    best = {}
    for r in sorted(rows, key=lambda r: -r.score):
        best.setdefault(r.sym, r)
    return list(best.values())
