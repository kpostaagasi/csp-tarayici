#!/usr/bin/env python3
"""Cash-secured put scanner. CBOE chain + CBOE history + Nasdaq earnings. Stdlib only, no API key.

  csp --universe --tui                 en işlek, sermayeyle alınabilen ABD hisseleri
  csp NOK F MARA --capital 2000        tek seferlik tablo
  csp --universe --snapshot            gerçek backtest için zincir kaydı (günlük)
  csp --replay NOK                     kayıtlı zincirlerle backtest
  csp --selftest
"""
import argparse, bisect, concurrent.futures as cf, curses, datetime as dt, gzip, json, locale, math, pathlib, re, sqlite3, sys, tempfile, threading, time, urllib.error, urllib.request

VERSION = "0.1.0"

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36",
      "Accept": "application/json,text/plain,*/*", "Accept-Encoding": "gzip"}

# --- tuning knobs: owning these is the whole point of running your own scanner ---
W = {"vrp": 0.35, "liq": 0.25, "yield": 0.20, "cushion": 0.20}   # must sum to 1
DTE_MIN, DTE_MAX = 21, 45
DELTA_LO, DELTA_HI = 0.15, 0.35      # |delta| band of the short put
MAX_REL_SPREAD = 0.10                # (ask-bid)/mid
MIN_OI = 25

_last = [0.0]                        # CBOE sits behind Cloudflare burst protection
MIN_INTERVAL = 0.35
_gate = threading.Lock()             # paces request *starts*; the downloads themselves overlap
_disk = threading.Lock()             # cache files are read-modify-write, so writers must queue


def cached(path):
    try:
        with _disk:
            return json.loads(path.read_text()) if path.exists() else {}
    except ValueError:
        return {}                        # a half-written cache is not worth a traceback


def store(path, key, value):
    """Merge one key into a cache file under the lock: a parallel scan writes the same file."""
    with _disk:
        try:
            c = json.loads(path.read_text()) if path.exists() else {}
        except ValueError:
            c = {}
        c[key] = value
        path.write_text(json.dumps(c))


def get(url, tries=6):
    for i in range(tries):
        with _gate:
            time.sleep(max(0, _last[0] + MIN_INTERVAL - time.monotonic()))
            _last[0] = time.monotonic()
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30)
            b = r.read()
            return json.loads(gzip.decompress(b) if r.headers.get("Content-Encoding") == "gzip" else b)
        except urllib.error.HTTPError as e:
            if e.code != 429 or i == tries - 1:
                raise
            time.sleep(float(e.headers.get("Retry-After", 1)) + 2 ** i)


# ---------------------------------------------------------------- data sources
_chains = {}                         # sym -> (monotonic, data). Changing a filter must not re-download.
CHAIN_TTL = 600


def chain(sym):
    """CBOE delayed quotes: spot, iv30, full option chain with greeks.

    Held for CHAIN_TTL seconds: the quotes are delayed anyway, a 60-name scan costs minutes
    (CBOE answers ~0.4 req/s before it 429s), and every filter knob re-reads the same chain.
    'r' in the TUI clears this and refetches.
    """
    sym = sym.upper()
    hit = _chains.get(sym)
    if hit and time.monotonic() - hit[0] < CHAIN_TTL:
        return hit[1]
    d = get(f"https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json")["data"]
    _chains[sym] = (time.monotonic(), d)
    return d


HIST = pathlib.Path.home() / ".csp_hist.json"


_px, _dates = {}, {}                 # sym -> closes / their ISO dates; the UI must never fetch


def history(sym):
    """(closes, dates) — every daily bar CBOE has. Cached per calendar day, memoized per process."""
    sym = sym.upper()
    if sym in _px:
        return _px[sym], _dates[sym]
    hit = cached(HIST).get(sym)
    if hit and hit["at"] == dt.date.today().isoformat() and "d" in hit:
        px, ds = hit["px"], hit["d"]
    else:
        raw = get(f"https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/{sym}.json")["data"]
        bars = [(b["date"], b["close"]) for b in raw if b["close"] > 0]   # pre-listing rows are 0.0
        px, ds = [c for _, c in bars], [d for d, _ in bars]
        store(HIST, sym, {"px": px, "d": ds, "at": dt.date.today().isoformat()})
    _px[sym], _dates[sym] = px, ds
    return px, ds


def closes(sym):
    return history(sym)[0]


def close_on(sym, iso):
    """Last close at or before `iso`, or None if the series ends before it (expiry still ahead)."""
    px, ds = history(sym)
    i = bisect.bisect_right(ds, iso)
    return px[i - 1] if i and ds[-1] >= iso else None


def realized_vol(sym, window=30, before=None):
    """Annualized close-to-close realized vol, %. `before` walks it back to a historical date."""
    px, ds = history(sym)
    end = bisect.bisect_left(ds, before) if before else len(px)
    return rv_from_closes(px[max(0, end - window - 10):end], window)


def rv_from_closes(px, window=30):
    rets = [math.log(px[i] / px[i - 1]) for i in range(1, len(px))][-window:]
    if len(rets) < 10:
        return None
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var * 252) * 100


CACHE = pathlib.Path.home() / ".csp_earnings.json"


def earnings_date(sym, ttl_days=7):
    """Estimated next report date. Cached on disk: it moves once a quarter, not once a scan."""
    sym = sym.upper()
    hit = cached(CACHE).get(sym)
    if hit and (dt.date.today() - dt.date.fromisoformat(hit["at"])).days < ttl_days:
        return dt.date.fromisoformat(hit["d"]) if hit["d"] else None
    try:
        txt = get(f"https://api.nasdaq.com/api/analyst/{sym}/earnings-date")["data"]["reportText"]
        m = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", txt)
        d = dt.datetime.strptime(m.group(1), "%m/%d/%Y").date() if m else None
    except Exception:
        return None                      # unknown date: treated as "no known event"
    store(CACHE, sym, {"d": d.isoformat() if d else None, "at": dt.date.today().isoformat()})
    return d


UNIV = pathlib.Path.home() / ".csp_universe.json"


def us_stocks():
    """Every US-listed stock with price/volume/market cap. One Nasdaq screener request, cached daily."""
    c = cached(UNIV)
    if c.get("at") == dt.date.today().isoformat():
        return c["rows"]
    raw = get("https://api.nasdaq.com/api/screener/stocks?tableonly=false&limit=25000&download=true")
    rows = []
    for r in raw["data"]["rows"]:
        try:
            rows.append([r["symbol"], float(r["lastsale"].lstrip("$").replace(",", "")),
                         int(r["volume"] or 0), float(r["marketCap"] or 0)])
        except ValueError:
            continue                     # units, warrants and fresh listings quote '' or 'NA'
    store(UNIV, "at", dt.date.today().isoformat()); store(UNIV, "rows", rows)
    return rows


def universe(capital, limit=60, min_volume=1_000_000, min_cap=3e8, rows=None):
    """Names you can actually cash-secure: cheap enough for the capital, liquid enough to exit.

    Sorted by share volume — with a $2k account the affordable half of the market is ~1500 tickers
    and a full scan is two CBOE requests each, so the cut has to happen before the network does.
    """
    picks = [r for r in (us_stocks() if rows is None else rows)
             if re.fullmatch(r"[A-Z]{1,5}", r[0]) and 1.0 < r[1] <= capital / 100
             and r[2] >= min_volume and r[3] >= min_cap]
    picks.sort(key=lambda r: -r[2])
    return [r[0] for r in picks[:limit]]


# ------------------------------------------------------------------- scoring
def parse_occ(s):
    """NOK260911P00003000 -> (date(2026,9,11), 'P', 3.0)"""
    m = re.fullmatch(r"([A-Z0-9]+?)(\d{6})([CP])(\d{8})", s)
    _, ymd, cp, k = m.groups()
    return dt.date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6])), cp, int(k) / 1000


def clip01(x):
    return 0.0 if x < 0 else 1.0 if x > 1 else x


VRP_LO, VRP_HI = -0.20, 0.40   # log(IV/RV) mapped onto 0..1; IV under RV must still be ranked, not tied at 0


def score_contract(spot, iv, rv30, strike, dte, bid, ask, oi):
    """0-100, transparent components. Every map below is a knob, not a fitted truth.

    iv is the short put's own implied vol, not ATM iv30: skew is what you actually sell,
    and it is also the market's own distribution for that strike, so cushion uses it too.
    """
    mid = (bid + ask) / 2
    rel_spread = (ask - bid) / mid
    roc_ann = (mid / strike) * 365 / dte                     # return on cash collateral
    em = iv / 100 * math.sqrt(dte / 365)                     # expected move to expiry, fraction
    cushion = (spot - strike) / (spot * em)                  # OTM distance in expected moves
    c = {
        "vrp":     clip01((math.log(iv / rv30) - VRP_LO) / (VRP_HI - VRP_LO)) if rv30 and rv30 > 0 else 0.0,
        "liq":     0.6 * clip01((MAX_REL_SPREAD - rel_spread) / (MAX_REL_SPREAD - 0.005))
                   + 0.4 * clip01(math.log10(max(oi, 1) / 25) / math.log10(500 / 25)),
        "yield":   clip01((roc_ann - 0.08) / (0.40 - 0.08)),
        "cushion": clip01(cushion / 1.0),
    }
    return round(100 * sum(W[k] * v for k, v in c.items())), c, mid, rel_spread, roc_ann, cushion


def scan_symbol(sym, capital, today, allow_earnings):
    sym = sym.upper()
    d = chain(sym)
    spot, iv30 = d["close"] or d["current_price"], d["iv30"]
    rv30 = realized_vol(sym)
    ed = earnings_date(sym)
    out = []
    for o in d["options"]:
        exp, cp, strike = parse_occ(o["option"])
        dte = (exp - today).days
        if cp != "P" or not DTE_MIN <= dte <= DTE_MAX:
            continue
        if strike * 100 > capital or o["bid"] <= 0 or o["ask"] <= 0:
            continue
        if not DELTA_LO <= abs(o["delta"]) <= DELTA_HI or o["open_interest"] < MIN_OI:
            continue
        if (o["ask"] - o["bid"]) / ((o["ask"] + o["bid"]) / 2) > MAX_REL_SPREAD:
            continue
        if ed and not allow_earnings and today <= ed <= exp:
            continue
        civ = (o.get("iv") or 0) * 100 or iv30              # chain iv is a fraction; fall back to ATM
        sc, c, mid, rs, roc, cush = score_contract(
            spot, civ, rv30, strike, dte, o["bid"], o["ask"], o["open_interest"])
        out.append(dict(sym=sym, spot=spot, iv=civ, iv30=iv30, rv30=rv30, exp=exp, dte=dte, strike=strike,
                        delta=o["delta"], mid=mid, spread=rs, oi=o["open_interest"], roc=roc,
                        cushion=cush, collat=strike * 100, earn=ed, score=sc, parts=c))
    return out


WORKERS = 8                          # 60 symbols x 2 requests: the 0.35s gate, not the CPU, is the limit


def scan_all(tickers, capital, today, allow_earnings, on_done=None):
    """Scan symbols in parallel. `on_done(n, sym, rows, err)` fires as each one lands, in any order."""
    rows, errs = [], []
    with cf.ThreadPoolExecutor(WORKERS) as ex:
        jobs = {ex.submit(scan_symbol, t, capital, today, allow_earnings): t for t in tickers}
        for n, f in enumerate(cf.as_completed(jobs), 1):
            sym, got, err = jobs[f], [], None
            try:
                got = f.result()
            except Exception as e:
                err = f"{sym}: {type(e).__name__} {e}"
                errs.append(err)
            rows += got
            if on_done:
                on_done(n, sym, got, err)
    return rows, errs


def best_per_symbol(rows):
    """One contract per underlying: the highest-scoring one."""
    best = {}
    for r in sorted(rows, key=lambda r: -r["score"]):
        best.setdefault(r["sym"], r)
    return list(best.values())


# (header, cell, drop priority) — the header carries its own width. Priority 0 never drops;
# when the terminal is too narrow the biggest number leaves first, so score/strike/DTE stay.
COLUMNS = [
    (f"{'sym':<5}",    lambda r: f"{r['sym']:<5}",                0),
    (f"{'spot':>7}",   lambda r: f"{r['spot']:>7.2f}",            10),
    (f"{'strike':>7}", lambda r: f"{r['strike']:>7.2f}",          0),
    (f"{'exp':>10}",   lambda r: f"{r['exp']!s:>10}",             9),
    (f"{'dte':>4}",    lambda r: f"{r['dte']:>4}",                0),
    (f"{'delta':>6}",  lambda r: f"{r['delta']:>6.2f}",           4),
    (f"{'mid':>6}",    lambda r: f"{r['mid']:>6.2f}",             3),
    (f"{'ROC%y':>6}",  lambda r: f"{r['roc'] * 100:>6.1f}",       0),
    (f"{'IV':>6}",     lambda r: f"{r['iv']:>6.1f}",              5),
    (f"{'RV30':>6}",   lambda r: f"{(r['rv30'] or 0):>6.1f}",     7),
    (f"{'cush':>5}",   lambda r: f"{r['cushion']:>5.2f}",         0),
    (f"{'sprd%':>6}",  lambda r: f"{r['spread'] * 100:>6.1f}",    6),
    (f"{'OI':>7}",     lambda r: f"{r['oi']:>7.0f}",              8),
    (f"{'$col':>6}",   lambda r: f"{r['collat']:>6.0f}",          2),
    (f"{'EV$':>6}",    lambda r: f"{ev(r):>6.0f}",                1),
    (f"{'vrp':>4}",    lambda r: f"{r['parts']['vrp']:>4.2f}",    11),
    (f"{'liq':>4}",    lambda r: f"{r['parts']['liq']:>4.2f}",    12),
    (f"{'yld':>4}",    lambda r: f"{r['parts']['yield']:>4.2f}",  13),
    (f"{'score':>6}",  lambda r: f"{r['score']:>6}",              0),
]


def columns(width):
    """Widest column set that fits: a cut-off score column is worse than a missing spot column."""
    cols = list(COLUMNS)
    while sum(len(h) + 1 for h, _, _ in cols) - 1 > width:
        worst = max(cols, key=lambda c: c[2])
        if worst[2] == 0:
            return cols                # nothing left to sacrifice: let curses clip it
        cols.remove(worst)
    return cols


def table(rows, width):
    cols = columns(width)
    return (" ".join(h for h, _, _ in cols),
            [" ".join(cell(r) for _, cell, _ in cols) for r in rows])


def backtest(r, px):
    """Today's real premium against this symbol's own history of `dte`-day moves.

    No free source has historical option prices, so nothing here prices an option: the credit is
    the live mid, only the underlying path is empirical. Assignment P&L is settled at expiry,
    `credit - (strike - S_T) * 100`, i.e. no early assignment and no roll.
    ponytail: overlapping windows, so the sample is autocorrelated — this bounds, never proves.
    """
    steps = max(1, round(r["dte"] * 252 / 365))              # DTE is calendar days; the bars are sessions
    if not px or len(px) < steps + 120:
        return None                                          # under ~6 months of overlap says nothing
    otm = r["strike"] / r["spot"] - 1                        # negative: how far below spot the strike is
    credit = r["mid"] * 100
    pl = [credit + min(0.0, (px[i + steps] / px[i] - 1) - otm) * r["spot"] * 100
          for i in range(len(px) - steps)]
    n = len(pl)
    return dict(n=n, years=round(len(px) / 252, 1),
                assign=sum(1 for x in pl if x < credit) / n,
                loss=sum(1 for x in pl if x < 0) / n,
                mean=sum(pl) / n, worst=min(pl))


# ------------------------------------------------- real backtest: recorded chains, replayed
SNAP = pathlib.Path.home() / ".csp_chains.db"
DDL = """create table if not exists puts (
  date text, sym text, exp text, strike real, bid real, ask real, iv real, delta real,
  oi real, spot real, primary key (date, sym, exp, strike))"""


def snap_db(path=None):
    con = sqlite3.connect(path or SNAP)
    con.execute(DDL)
    return con


def snapshot(tickers, max_dte=70, path=None):
    """Append today's put chains to a local SQLite.

    Free vendors do not cover the names a $2k account can actually sell (measured: DoltHub's
    2019+ chain set has MARA but not SOFI/PLUG/NOK, and its SQL endpoint answers in ~45s).
    We already download these chains to score them, so keeping them is the only free way to
    own real option history: one row per (date, sym, expiration, strike), quotes as recorded.
    """
    today = dt.date.today()
    rows = []
    for t in tickers:
        try:
            d = chain(t)
        except Exception:
            continue                     # a dead ticker must not abort the day's recording
        spot = d["close"] or d["current_price"]
        for o in d["options"]:
            exp, cp, strike = parse_occ(o["option"])
            if cp != "P" or not 0 <= (exp - today).days <= max_dte:
                continue
            rows.append((today.isoformat(), t.upper(), exp.isoformat(), strike, o["bid"], o["ask"],
                         (o.get("iv") or 0) * 100, o["delta"], o["open_interest"], spot))
    con = snap_db(path)
    with con:
        con.executemany("insert or replace into puts values (?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def replay(sym, capital, path=None):
    """Replay recorded chains: same filters, same score, real bid, held to expiry.

    One position at a time — a new one only after the last expires. Sold at the recorded BID
    (the fill you would actually get), settled against the underlying close on expiration day.
    No rolls, no early close, no wheel, and no earnings filter (nobody records past estimates).
    """
    sym = sym.upper()
    con = snap_db(path)
    rows = con.execute("select date, exp, strike, bid, ask, iv, delta, oi, spot from puts "
                       "where sym = ? order by date", (sym,)).fetchall()
    days = {}
    for r in rows:
        days.setdefault(r[0], []).append(r)
    trades, busy_until = [], ""
    for date in sorted(days):
        if date <= busy_until:
            continue
        today, best = dt.date.fromisoformat(date), None
        rv = realized_vol(sym, before=date)
        for _, exp, strike, bid, ask, iv, delta, oi, spot in days[date]:
            dte = (dt.date.fromisoformat(exp) - today).days
            if not DTE_MIN <= dte <= DTE_MAX or strike * 100 > capital:
                continue
            if bid <= 0 or ask <= 0 or (ask - bid) / ((ask + bid) / 2) > MAX_REL_SPREAD:
                continue
            if not DELTA_LO <= abs(delta) <= DELTA_HI or oi < MIN_OI:
                continue
            sc = score_contract(spot, iv or 0.0, rv, strike, dte, bid, ask, oi)[0]
            if not best or sc > best[0]:
                best = (sc, exp, strike, bid, spot, dte, delta)
        if not best:
            continue
        sc, exp, strike, bid, spot, dte, delta = best
        settle = close_on(sym, exp)
        if settle is None:
            break                        # expiry still ahead: the position is open, not a result
        trades.append(dict(sym=sym, entry=date, exp=exp, dte=dte, strike=strike, credit=bid,
                           spot=spot, delta=delta, settle=settle, score=sc, collat=strike * 100,
                           pl=bid * 100 + min(0.0, settle - strike) * 100))
        busy_until = exp
    return trades


def replay_stats(trades):
    if not trades:
        return None
    pl = [t["pl"] for t in trades]
    held = sum(t["dte"] for t in trades) or 1
    tied = max(t["collat"] for t in trades)   # worst single cash commitment: the real denominator
    run = peak = low = 0.0
    for x in pl:                              # equity-curve drawdown, in dollars
        run += x
        peak = max(peak, run)
        low = min(low, run - peak)
    return dict(n=len(trades), total=sum(pl), mean=sum(pl) / len(pl), worst=min(pl),
                wins=sum(1 for x in pl if x > 0) / len(pl),
                assigned=sum(1 for t in trades if t["settle"] < t["strike"]) / len(trades),
                days=held, tied=tied, drawdown=low,
                ann=sum(pl) / tied * 365 / held if tied else 0.0,
                first=trades[0]["entry"], last=trades[-1]["exp"])


def usd(v):
    return f"{'-' if v < 0 else '+'}${abs(v):.0f}"


def ev(r):
    """Mean historical P&L of this exact contract, in dollars. Memoized: the UI redraws 5x a second."""
    if "ev" not in r:
        bt = backtest(r, _px.get(r["sym"]))
        r["ev"] = bt["mean"] if bt else 0.0
    return r["ev"]


def explain(r, px=None):
    """Plain-language reading of one contract. The numbers above are the evidence; this is the claim."""
    be = r["strike"] - r["mid"]                              # break-even at expiry
    em = r["iv"] / 100 * math.sqrt(r["dte"] / 365) * 100     # expected move to expiry, %
    p = r["parts"]
    earn = (f"kazanç {r['earn']}" + (" ⚠ VADE İÇİNDE" if r["earn"] <= r["exp"] else ", vadeden sonra")
            if r["earn"] else "bilinen kazanç tarihi yok")
    out = [
        f" {r['sym']} {r['strike']:.2f} PUT · vade {r['exp']} · {r['dte']} gün · spot {r['spot']:.2f}",
        f" sat: +${r['mid'] * 100:.0f} prim, ${r['collat']:.0f} nakit bloke, %{r['roc'] * 100:.1f} yıllık"
        f" · atama olasılığı ~%{abs(r['delta']) * 100:.0f} · {earn}",
        f" başabaş {be:.2f} (spot'un %{(r['spot'] - be) / r['spot'] * 100:.1f} altı) · piyasa vadeye"
        f" ±%{em:.1f} bekliyor · strike {r['cushion']:.2f} beklenen hareket uzakta",
        f" skor {r['score']}: vrp {p['vrp']:.2f} (IV {r['iv']:.1f} vs RV {(r['rv30'] or 0):.1f})"
        f" · liq {p['liq']:.2f} (spread %{r['spread'] * 100:.1f}, OI {r['oi']:.0f})"
        f" · yield {p['yield']:.2f} · cushion {p['cushion']:.2f}",
    ]
    bt = backtest(r, px if px is not None else _px.get(r["sym"]))
    out.append(
        f" geçmiş {bt['years']} yıl / {bt['n']} pencere: bu mesafe %{bt['assign'] * 100:.0f} atama"
        f" (piyasa ~%{abs(r['delta']) * 100:.0f}) · %{bt['loss'] * 100:.0f} zarar"
        f" · ortalama {usd(bt['mean'])} · en kötü {usd(bt['worst'])}" if bt else
        " geçmiş: bu vade için yeterli fiyat geçmişi yok")
    return out


HDR_TRADES = (f"{'giriş':>10} {'vade':>10} {'dte':>4} {'strike':>7} {'prim':>6} {'spot':>7} "
              f"{'uzlaşma':>8} {'skor':>5} {'P&L$':>7}")


def print_replay(sym, capital):
    trades = replay(sym, capital)
    s = replay_stats(trades)
    if not s:
        sys.exit(f"{sym.upper()}: kayıtlı zincir yok ya da hiç kontrat filtreleri geçmedi.\n"
                 f"Veri biriktirmek için her gün: python3 csp.py --universe --snapshot")
    print(HDR_TRADES)
    for t in trades:
        print(f"{t['entry']:>10} {t['exp']:>10} {t['dte']:>4} {t['strike']:>7.2f} {t['credit']:>6.2f} "
              f"{t['spot']:>7.2f} {t['settle']:>8.2f} {t['score']:>5} {t['pl']:>+7.0f}")
    print(f"\n{s['n']} işlem · {s['first']} → {s['last']} · {s['days']} gün pozisyonda")
    print(f"toplam {usd(s['total'])} · işlem başına {usd(s['mean'])} · en kötü {usd(s['worst'])}")
    print(f"kazanan %{s['wins'] * 100:.0f} · atanan %{s['assigned'] * 100:.0f} · "
          f"en kötü seri {usd(s['drawdown'])} · bloke edilen tepe ${s['tied']:.0f}")
    print(f"teminata göre yıllık %{s['ann'] * 100:.1f}  (pozisyonda geçen günlerle ölçekli)")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="csp", description=__doc__.split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tickers", nargs="*", help="taranacak semboller (boşsa kayıtlı izleme listesi)")
    ap.add_argument("--capital", type=float, default=2000)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--allow-earnings", action="store_true")
    ap.add_argument("--tui", action="store_true", help="interactive curses UI")
    ap.add_argument("--universe", nargs="?", type=int, const=60, metavar="N",
                    help="watchlist yerine, sermayeyle alınabilen en işlek N ABD hissesi")
    ap.add_argument("--explain", action="store_true", help="prose reading under each row")
    ap.add_argument("--snapshot", action="store_true",
                    help="günün put zincirlerini ~/.csp_chains.db'ye yaz (gerçek backtest için veri biriktir)")
    ap.add_argument("--replay", metavar="SYM", help="kayıtlı zincirlerle gerçek backtest")
    ap.add_argument("--selftest", action="store_true", help="dahili testleri koştur ve çık")
    ap.add_argument("--version", action="version", version=f"csp {VERSION}")
    a = ap.parse_args(argv)

    if a.selftest:
        return selftest()

    if a.replay:
        return print_replay(a.replay, a.capital)

    tickers = ([t.upper() for t in a.tickers]
               or (universe(a.capital, a.universe) if a.universe else load_watchlist()))
    if not tickers:
        sys.exit("sembol yok: argüman olarak geç ya da --universe kullan")
    if a.snapshot:
        n = snapshot(tickers)
        with snap_db() as con:
            d, rows, syms = con.execute(
                "select count(distinct date), count(*), count(distinct sym) from puts").fetchone()
        return print(f"{n} satır yazıldı · defter: {rows} satır / {d} gün / {syms} sembol → {SNAP}")
    if a.tui:
        if a.tickers:
            WATCH.write_text(json.dumps(tickers))
        return curses.wrapper(tui, tickers, {"capital": a.capital, "earn": a.allow_earnings,
                                             "save": not a.universe})

    today = dt.date.today()
    rows, errs = scan_all(
        tickers, a.capital, today, a.allow_earnings,
        on_done=lambda n, sym, *_: print(f"\r  {n}/{len(tickers)} {sym:<6}",
                                         end="", file=sys.stderr, flush=True))
    print("\r" + " " * 24 + "\r", end="", file=sys.stderr)

    top = sorted(best_per_symbol(rows), key=lambda r: -r["score"])[:a.top]
    dropped = set(tickers) - {r["sym"] for r in rows} - {e.split(":")[0] for e in errs}
    for t in sorted(dropped):
        print(f"   {t}: no contract passed the filters (spread / delta band / OI / capital / earnings)",
              file=sys.stderr)
    hdr, lines = table(top, 200)
    print(hdr)
    for line in lines:
        print(line)
    for r in top:
        if a.explain:
            print()
            for line in explain(r):
                print(line)
        elif r["earn"]:
            print(f"   {r['sym']} earnings {r['earn']}")
    for e in errs:
        print("skip", e, file=sys.stderr)


# ----------------------------------------------------------------------- TUI
WATCH = pathlib.Path.home() / ".csp_watchlist.json"
SORTS = [("score", -1), ("ev", -1), ("roc", -1), ("iv30", -1), ("cushion", -1), ("spread", 1), ("dte", 1)]


def load_watchlist():
    return json.loads(WATCH.read_text()) if WATCH.exists() else []


def scan_async(tickers, cfg, st):
    """Runs in a thread; the UI only ever reads st. Rows appear as each symbol lands."""
    today = dt.date.today()
    st.update(rows=[], errs=[], done=0, total=len(tickers), running=True, last="")

    def landed(n, sym, got, err):
        st["done"], st["last"] = n, sym
        if got:
            st["rows"] = st["rows"] + got     # rebind, never mutate: the UI thread reads this list
        if err:
            st["errs"] = st["errs"] + [err]

    scan_all(tickers, cfg["capital"], today, cfg["earn"], on_done=landed)
    st["running"] = False


def ask(stdscr, msg):
    h, w = stdscr.getmaxyx()
    curses.echo(); curses.curs_set(1)
    stdscr.timeout(-1)                 # the 200ms poll would return an empty string instantly
    stdscr.move(h - 1, 0); stdscr.clrtoeol(); stdscr.addstr(h - 1, 0, msg[:w - 2])
    try:
        s = stdscr.getstr(h - 1, len(msg) + 1, 24).decode().strip()
    except Exception:
        s = ""
    curses.noecho(); curses.curs_set(0); stdscr.timeout(200)
    return s


def read_key(stdscr):
    """getch, plus manual CSI decoding: some terminals hand back raw ESC [ A/B here."""
    k = stdscr.getch()
    if k != 27:
        return k
    a, b = stdscr.getch(), stdscr.getch()
    return {(91, 65): curses.KEY_UP, (91, 66): curses.KEY_DOWN}.get((a, b), -1)


HELP = [                               # 24 satırlık bir terminalde bile kesilmemeli: kısa tut
 " CSP tarayıcı · ekrandaki her şey ne demek",
 " TABLO — sembol başına en iyi put adayı ('enter': o sembolün tüm kontratları)",
 "  sym strike exp dte  sattığın put: hisse, kullanım fiyatı, vade, kalan gün",
 "  spot / mid          hissenin gecikmeli fiyatı / (bid+ask)/2 prim. 1 kontrat = mid x 100 dolar",
 "  delta               atama olasılığına kaba yaklaşım: -0.30 ≈ %30",
 "  ROC%y               prim/teminat, yıllığa çevrilmiş. Nakit getirisi — olasılık değil",
 "  IV / RV30           sattığın implied vol % / son 30 günün gerçekleşeni %. vrp bu oran",
 "  cush                strike kaç 'beklenen hareket' (EM) uzakta. 1.00 = tam bir EM",
 "  sprd% / OI          (ask-bid)/mid giriş-çıkış maliyeti / kaç kontrat açık duruyor",
 "  $col                bloke olacak nakit: strike x 100 x 1 kontrat",
 "  EV$                 GEÇMİŞ TESTİ: bugünün primi, bu hissenin kendi geçmişindeki her",
 "                      DTE'lik pencereye uygulanınca ortalama kâr/zarar. Eksi = prim kuyruğu ödemiyor",
 "  vrp liq yld         skor bileşenleri 0-1; dördüncüsü cushion (cush ile aynı bilgi)",
 "  score               0-100 = 0.35 vrp + 0.25 liq + 0.20 yield + 0.20 cushion",
 "  renk                yeşil >=50 · sarı 35-49 · kırmızı <35; dar terminalde önemsiz kolon düşer",
 " ALT PANEL — seçili kontratın düz okuması: prim, bloke nakit, atama olasılığı, başabaş fiyat,",
 " piyasanın vadeye beklediği hareket, skorun bileşenleri, geçmiş testinin tamamı (atama %'si,",
 " zarar %'si, ortalama, en kötü pencere).",
 " FİLTRELER (başlıkta güncel) — DTE · |delta| bandı · spread eşiği · OI eşiği · teminat<=sermaye",
 " · vade içinde kazanç varsa ele. Filtreler listeye girişi belirler, skoru değil.",
 " TUŞLAR — jk/oklar gez · enter detay · s sırala · a/x sembol ekle-çıkar · c sermaye · d delta",
 " t dte · e kazanç filtresi · r veriyi yenile · q geri/çık",
 " VERİ — filtre düğmeleri cache'li zincirleri anında yeniden süzer, ağa çıkmaz. Yalnız 'r'",
 " zincirleri yeniden çeker. --universe ile taranan liste sermayeyle alınabilen en işlek isimler.",
]


def tui(stdscr, tickers, cfg):
    locale.setlocale(locale.LC_ALL, "")   # curses encodes addstr with the locale: 'ı' must survive
    curses.curs_set(0); stdscr.timeout(200); stdscr.keypad(True)
    curses.use_default_colors()
    for i, c in enumerate((curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_RED, curses.COLOR_CYAN), 1):
        curses.init_pair(i, c, -1)
    st = {"rows": [], "errs": [], "done": 0, "total": 0, "running": False, "last": ""}
    sel, sort_i, detail, showing_help = 0, 0, None, False

    def remember():                    # a --universe run must not overwrite the saved watchlist
        if cfg.get("save", True):
            WATCH.write_text(json.dumps(tickers))

    def rescan():
        if not st["running"]:
            threading.Thread(target=scan_async, args=(list(tickers), cfg, st), daemon=True).start()

    def view():
        key, sign = SORTS[sort_i]
        rows = [r for r in st["rows"] if r["sym"] == detail] if detail else best_per_symbol(st["rows"])
        for r in rows:
            ev(r)                      # memoized on the row; the EV$ column and sort both need it
        return sorted(rows, key=lambda r: sign * r[key])

    rescan()
    while True:
        if showing_help:
            h, w = stdscr.getmaxyx()
            stdscr.clear()
            for i, line in enumerate(HELP[:h - 1]):
                stdscr.addstr(i, 0, line[:w - 1], curses.A_BOLD if i == 0 else 0)
            stdscr.addstr(h - 1, 0, " herhangi bir tuş: geri "[:w - 1], curses.A_REVERSE)
            stdscr.refresh()
            if read_key(stdscr) != -1:
                showing_help = False
            continue
        rows = view()
        sel = max(0, min(sel, len(rows) - 1))
        h, w = stdscr.getmaxyx()
        stdscr.clear()                 # not erase(): a shrinking list must not leave ghost rows
        prog = (f"tarıyor {st['done']}/{st['total']} {st['last']}" if st["running"]
                else f"{len(tickers)} sembol · {len(st['rows'])} kontrat"
                     + (f" · {len(st['errs'])} hata" if st["errs"] else ""))
        title = (f" CSP · sermaye ${cfg['capital']:.0f} · DTE {DTE_MIN}-{DTE_MAX}"
                 f" · |delta| {DELTA_LO:.2f}-{DELTA_HI:.2f} · spread<%{MAX_REL_SPREAD*100:.0f}"
                 f" · kazanç:{'tut' if cfg['earn'] else 'atla'}"
                 f" · sıra:{SORTS[sort_i][0]}{'  [' + detail + ']' if detail else ''} ")
        stdscr.addstr(0, 0, f"{title}{prog:>{max(1, w - len(title) - 1)}}"[:w - 1], curses.A_REVERSE)

        hdr, lines = table(rows, w - 1)
        panel = explain(rows[sel])[:max(0, h - 6)] if rows else []
        vis = max(1, h - 3 - len(panel))
        top_i = max(0, min(sel - vis + 1, len(lines) - vis)) if len(lines) > vis else 0
        stdscr.addstr(1, 0, hdr[:w - 1], curses.A_BOLD)
        pos = f" {sel + 1}/{len(lines)} "
        if len(lines) > vis and w - 1 - len(pos) > len(hdr):   # never lands on a column header
            stdscr.addstr(1, w - 1 - len(pos), pos, curses.A_REVERSE)
        for i, line in enumerate(lines[top_i:top_i + vis]):
            r = rows[top_i + i]
            col = curses.color_pair(1 if r["score"] >= 50 else 2 if r["score"] >= 35 else 3)
            stdscr.addstr(2 + i, 0, line[:w - 1], col | (curses.A_REVERSE if top_i + i == sel else 0))
        for i, line in enumerate(panel):
            stdscr.addstr(h - 1 - len(panel) + i, 0, line[:w - 1],
                          curses.color_pair(4) | (curses.A_BOLD if i == 0 else 0))
        stdscr.addstr(h - 1, 0, (" ? yardım  enter detay  r tara  s sırala  a/x sembol  "
                                 "c sermaye  d delta  t dte  e kazanç  q çık")[:w - 1],
                      curses.A_REVERSE)
        stdscr.refresh()

        k = read_key(stdscr)
        if k == ord("q"):
            if detail:
                detail, sel = None, 0
            else:
                return
        elif k in (curses.KEY_DOWN, ord("j")):
            sel += 1
        elif k in (curses.KEY_UP, ord("k")):
            sel -= 1
        elif k in (curses.KEY_ENTER, 10, 13) and rows:
            detail, sel = (None, 0) if detail else (rows[sel]["sym"], 0)
        elif k == ord("s"):
            sort_i = (sort_i + 1) % len(SORTS)
        elif k == ord("r"):
            detail = None; _chains.clear(); rescan()   # r = refetch; every other knob re-filters cached chains
        elif k == ord("e"):
            cfg["earn"] = not cfg["earn"]; rescan()
        elif k == ord("x") and rows and not detail:
            tickers.remove(rows[sel]["sym"])
            remember(); rescan()
        elif k == ord("a"):
            t = ask(stdscr, "sembol ekle:").upper()
            if t and t not in tickers:
                tickers.append(t); remember(); rescan()
        elif k == ord("c"):
            v = ask(stdscr, "sermaye $:")
            if v.replace(".", "").isdigit():
                cfg["capital"] = float(v); rescan()
        elif k == ord("d"):
            v = ask(stdscr, "delta bandı alt üst:").split()
            if len(v) == 2:
                globals()["DELTA_LO"], globals()["DELTA_HI"] = float(v[0]), float(v[1]); rescan()
        elif k == ord("t"):
            v = ask(stdscr, "dte alt üst:").split()
            if len(v) == 2:
                globals()["DTE_MIN"], globals()["DTE_MAX"] = int(v[0]), int(v[1]); rescan()
        elif k == ord("?"):
            showing_help = True


def selftest():
    assert parse_occ("NOK260911P00003000") == (dt.date(2026, 9, 11), "P", 3.0)
    assert parse_occ("MARA261016C00012500") == (dt.date(2026, 10, 16), "C", 12.5)
    px = [100 * math.exp(0.01 * (-1) ** i) for i in range(60)]   # +-1% alternating
    rv = rv_from_closes(px, 30)
    assert abs(rv - 0.02 * math.sqrt(252) * 100) < 1, rv         # 2% daily swing annualized
    assert rv_from_closes([100, 101], 30) is None                # too few points

    # scoring: components move the right way; totals only where the sign is unambiguous
    base = dict(spot=10.0, iv=50.0, rv30=35.0, strike=9.0, dte=30, bid=0.30, ask=0.32, oi=500)
    s0, c0, *_ = score_contract(**base)
    assert 0 <= s0 <= 100
    # richer IV lifts VRP but widens the expected move, so cushion shrinks: net is ambiguous by design
    c_iv = score_contract(**{**base, "iv": 70.0})[1]
    assert c_iv["vrp"] > c0["vrp"] and c_iv["cushion"] < c0["cushion"]
    # IV under RV is a real signal, not a tie: it must still rank, and only bottom out well below RV
    v_lo = score_contract(**{**base, "rv30": 60.0})[1]["vrp"]
    v_lower = score_contract(**{**base, "rv30": 75.0})[1]["vrp"]
    assert 0.0 < v_lo < c0["vrp"] and v_lower < v_lo
    assert score_contract(**{**base, "rv30": 200.0})[1]["vrp"] == 0.0
    assert score_contract(**{**base, "rv30": 50.0})[1]["vrp"] == clip01(-VRP_LO / (VRP_HI - VRP_LO))
    assert score_contract(**{**base, "rv30": 60.0})[0] < s0
    assert score_contract(**{**base, "rv30": None})[1]["vrp"] == 0.0    # no history -> no claim
    assert score_contract(**{**base, "ask": 0.45})[1]["liq"] < c0["liq"]          # wider spread
    assert score_contract(**{**base, "oi": 25})[0] < s0                           # thinner book
    assert score_contract(**{**base, "strike": 7.0})[1]["cushion"] > c0["cushion"]
    # expected-move convention matches the CBOE card: EM = IV * sqrt(30/365)
    assert abs(math.sqrt(30 / 365) - 0.28669) < 1e-4

    # list logic the TUI drives: one row per underlying, every sort key orderable
    mk = lambda s, sc, roc, dte: dict(sym=s, score=sc, roc=roc, iv30=40.0, cushion=0.5, spread=0.02,
                                      dte=dte, ev=sc - 40.0)
    rows = [mk("F", 30, .2, 30), mk("F", 55, .3, 40), mk("NOK", 41, .1, 25)]
    bs = best_per_symbol(rows)
    assert sorted(r["sym"] for r in bs) == ["F", "NOK"]
    assert next(r for r in bs if r["sym"] == "F")["score"] == 55       # keeps the best, not the first
    for key, sign in SORTS:
        vals = [sign * r[key] for r in sorted(rows, key=lambda r: sign * r[key])]
        assert vals == sorted(vals), key

    # arrow keys arrive as raw CSI on some terminals; ESC alone must never quit
    class FakeScr:
        def __init__(self, seq): self.seq = list(seq)
        def getch(self): return self.seq.pop(0)
    assert read_key(FakeScr([27, 91, 66])) == curses.KEY_DOWN
    assert read_key(FakeScr([27, 91, 65])) == curses.KEY_UP
    assert read_key(FakeScr([27, 91, 67])) == -1                       # unhandled CSI -> ignored
    assert read_key(FakeScr([ord("q")])) == ord("q")

    # universe: the cut happens before the network, and every rejection is deliberate
    listed = [["SOFI", 18.22, 40_000_000, 2.2e10],      # affordable, busy, real
              ["NOK", 10.03, 20_000_000, 5.5e10],
              ["NVDA", 230.36, 200_000_000, 5.5e12],    # $23k of collateral: not with $2k
              ["PENNY", 0.42, 90_000_000, 4e8],         # sub-$1: no listed options worth selling
              ["THIN", 12.0, 900, 4e8],                 # nobody trades it
              ["SMALL", 9.0, 5_000_000, 1e8],           # micro cap
              ["BRK.A", 12.0, 9_000_000, 4e8]]          # dotted root: chain lives elsewhere
    assert universe(2000, rows=listed) == ["SOFI", "NOK"]              # busiest first
    assert universe(2000, limit=1, rows=listed) == ["SOFI"]
    assert universe(25000, rows=listed) == ["NVDA", "SOFI", "NOK"]     # more cash, more of the tape
    assert universe(2000, min_volume=10 ** 9, rows=listed) == []
    # rendering: a narrow terminal drops the cheap columns and never the ones you decide with
    row = dict(sym="SOFI", spot=18.22, iv=49.3, iv30=47.0, rv30=64.6, exp=dt.date(2026, 10, 16),
               dte=38, strike=17.0, delta=-0.30, mid=0.64, spread=0.016, oi=7175, roc=0.359,
               cushion=0.42, collat=1700.0, earn=dt.date(2026, 10, 27), score=49,
               parts={"vrp": 0.0, "liq": 0.93, "yield": 0.87, "cushion": 0.42})
    full = sum(len(h) + 1 for h, _, _ in COLUMNS) - 1
    assert columns(full) == COLUMNS and columns(full + 40) == COLUMNS
    assert [h.strip() for h, _, _ in columns(full - 1)] == [h.strip() for h, _, _ in COLUMNS if h.strip() != "yld"]
    for width in (full, 100, 80, 60, 40, 20):
        cols = columns(width)
        kept = {h.strip() for h, _, _ in cols}
        assert {"sym", "strike", "dte", "ROC%y", "cush", "score"} <= kept, width
        assert width < 45 or sum(len(h) + 1 for h, _, _ in cols) - 1 <= width, width
    hdr, lines = table([row], full)
    assert len(lines[0]) == len(hdr) and lines[0].startswith("SOFI ") and lines[0].endswith("    49")

    # the panel is the readable half of the UI: every number in it is derived, none is decoration
    ex = explain(row)
    assert "SOFI 17.00 PUT" in ex[0] and "38 gün" in ex[0] and "spot 18.22" in ex[0]
    assert "+$64 prim" in ex[1] and "$1700 nakit" in ex[1] and "~%30" in ex[1]     # delta as assignment odds
    assert "kazanç 2026-10-27, vadeden sonra" in ex[1]
    assert "VADE İÇİNDE" in explain({**row, "earn": dt.date(2026, 10, 1)})[1]
    assert "bilinen kazanç tarihi yok" in explain({**row, "earn": None})[1]
    assert "başabaş 16.36" in ex[2] and "%10.2 altı" in ex[2]     # strike - mid, measured off spot
    assert "±%15.9" in ex[2], ex[2]                               # IV 49.3 * sqrt(38/365)
    assert "skor 49" in ex[3] and "IV 49.3 vs RV 64.6" in ex[3]

    assert "yeterli fiyat geçmişi yok" in ex[4]               # no history handed in: say so, invent nothing

    # backtest: today's credit against this symbol's own path. Every number is checkable by hand.
    bt_row = dict(sym="X", spot=100.0, strike=90.0, mid=0.50, dte=30, delta=-0.25)
    flat = backtest(bt_row, [100.0] * 400)
    assert flat["n"] == 400 - 21 and flat["years"] == 1.6     # 30 calendar days = 21 sessions
    assert flat["assign"] == 0.0 and flat["loss"] == 0.0
    assert flat["mean"] == 50.0 and flat["worst"] == 50.0     # never touched: you keep the credit

    crash = backtest(bt_row, [100.0] * 200 + [50.0] * 200)    # one -50% gap, 21 windows straddle it
    assert crash["n"] == 379 and abs(crash["assign"] - 21 / 379) < 1e-12
    assert crash["loss"] == crash["assign"]                   # a 0.50 credit cannot cover a $10 breach
    assert abs(crash["worst"] - (50 + (-0.50 + 0.10) * 100 * 100)) < 1e-9      # -3950
    assert abs(crash["mean"] - (358 * 50 + 21 * -3950) / 379) < 1e-9

    # a strike below the crash floor is never assigned, even across the gap
    assert backtest({**bt_row, "strike": 40.0}, [100.0] * 200 + [50.0] * 200)["assign"] == 0.0
    assert backtest(bt_row, [100.0] * 140) is None            # too little overlap to claim anything
    assert backtest(bt_row, None) is None
    assert usd(-3.4) == "-$3" and usd(0) == "+$0"

    # EV$ is the backtest promoted to a column and a sort key, so it must be memoized and never crash
    assert "EV$" in [h.strip() for h, _, _ in columns(sum(len(h) + 1 for h, _, _ in COLUMNS) - 1)]
    _px["X"] = [100.0] * 400
    live = dict(bt_row)
    assert ev(live) == 50.0 and live["ev"] == 50.0
    _px["X"] = [100.0] * 200 + [50.0] * 200
    assert ev(live) == 50.0                                   # memoized: no re-read, no flicker
    _px.pop("X")
    assert ev(dict(bt_row)) == 0.0                            # no history: 0, so sorting still works


    # real backtest: recorded chains replayed by the shipping engine. Synthetic rows, real code path.
    db = pathlib.Path(tempfile.mkdtemp()) / "t.db"
    with snap_db(db) as con:
        for d, exp in (("2026-01-05", "2026-02-06"), ("2026-01-06", "2026-02-06"),
                       ("2026-02-09", "2026-03-13")):
            for strike, delta, bid, ask in ((9.0, -0.20, 0.20, 0.21),      # cheap, far, fits $900
                                            (9.5, -0.30, 0.40, 0.42),      # best score, needs $950
                                            (12.0, -0.60, 2.00, 2.02)):    # delta band + capital reject
                con.execute("insert or replace into puts values (?,?,?,?,?,?,?,?,?,?)",
                            (d, "T", exp, strike, bid, ask, 50.0, delta, 500.0, 10.0))
    ds = ([f"2025-12-{i:02d}" for i in range(1, 32)] + [f"2026-01-{i:02d}" for i in range(1, 32)]
          + [f"2026-02-{i:02d}" for i in range(1, 7)])
    _dates["T"], _px["T"] = ds, [10.0] * (len(ds) - 1) + [9.2]   # expiry close 9.2: 9.5 breached, 9.0 not

    tr = replay("T", capital=1000, path=db)
    assert [t["entry"] for t in tr] == ["2026-01-05"]            # one at a time; 01-06 is inside the trade
    t0 = tr[0]                                                   # and 02-09 has no settled expiry yet
    assert (t0["strike"], t0["credit"], t0["exp"], t0["dte"]) == (9.5, 0.40, "2026-02-06", 32)
    assert t0["settle"] == 9.2 and abs(t0["pl"] - (0.40 - 0.30) * 100) < 1e-9      # +$10, assigned
    s = replay_stats(tr)
    assert (s["n"], s["wins"], s["assigned"], s["tied"]) == (1, 1.0, 1.0, 950.0)
    assert abs(s["total"] - 10) < 1e-9 and s["drawdown"] == 0.0
    assert abs(s["ann"] - 10 / 950 * 365 / 32) < 1e-6

    cheap = replay("T", capital=900, path=db)                    # $950 collateral no longer affordable
    assert (cheap[0]["strike"], cheap[0]["credit"]) == (9.0, 0.20)
    assert abs(cheap[0]["pl"] - 20.0) < 1e-9 and replay_stats(cheap)["assigned"] == 0.0   # not breached
    assert replay("T", capital=100, path=db) == [] and replay_stats([]) is None
    assert replay("NOPE", capital=1000, path=db) == []
    _px.pop("T"); _dates.pop("T")

    print("selftest ok")


if __name__ == "__main__":
    main()
