"""What a short put is worth, and which ones survive the filters.

Two separate ideas, deliberately not shared:
  * `Filters` decides what may enter the list (hard cuts, tuned per account).
  * The scoring scales below decide how good an entrant is. They are fixed, so widening a
    filter no longer silently re-scales the score.
"""

import concurrent.futures as cf
import datetime as dt
import math
import re
from dataclasses import dataclass, field

from .sources import chain, earnings_date, history

W = {"vrp": 0.35, "liq": 0.25, "yield": 0.20, "cushion": 0.20}  # must sum to 1
VRP_LO, VRP_HI = -0.20, 0.40  # log(IV/RV) mapped onto 0..1; IV under RV is ranked, not tied at 0
SPREAD_SCALE = 0.10  # relative spread that scores 0 on the liquidity component
OI_SCALE, OI_DEEP = 25, 500  # open interest that scores 0 and 1
YIELD_LO, YIELD_HI = 0.08, 0.40  # annualized return on collateral mapped onto 0..1
WORKERS = 8  # the 0.35s request gate is the limit, not the CPU


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
    ev: float | None = field(default=None)  # memoized by backtest.ev(); None = not computed yet


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


def rv_from_closes(px, window=30):
    rets = [math.log(px[i] / px[i - 1]) for i in range(1, len(px))][-window:]
    if len(rets) < 10:
        return None
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var * 252) * 100


def realized_vol(sym, window=30, before=None):
    """Annualized close-to-close realized vol, %. `before` walks it back to a historical date."""
    import bisect

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


def passes(f, dte, strike, bid, ask, delta, oi):
    """The hard cuts, in one place: the scan and the replay must agree on what is tradeable."""
    if not f.dte_min <= dte <= f.dte_max or strike * 100 > f.capital:
        return False
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
    out = []
    for o in d["options"]:
        exp, cp, strike = parse_occ(o["option"])
        dte = (exp - today).days
        if cp != "P" or not passes(f, dte, strike, o["bid"], o["ask"], o["delta"], o["open_interest"]):
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
        except BaseException:            # Ctrl-C: drop the queue, or the executor drains it first
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
