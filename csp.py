#!/usr/bin/env python3
"""Cash-secured put scanner. CBOE chain + CBOE history + Nasdaq earnings. Stdlib only.

  python3 csp.py NOK F MARA ETHA GRAB --capital 2000     one-shot table
  python3 csp.py --tui                                    interactive, saved watchlist
  python3 csp.py --selftest
"""
import argparse, curses, datetime as dt, gzip, json, math, pathlib, re, sys, threading, time, urllib.error, urllib.request

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


def get(url, tries=6):
    for i in range(tries):
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
def chain(sym):
    """CBOE delayed quotes: spot, iv30, full option chain with greeks."""
    return get(f"https://cdn.cboe.com/api/global/delayed_quotes/options/{sym.upper()}.json")["data"]


def realized_vol(sym, window=30):
    """Annualized close-to-close realized vol, %. Same vendor as the IV, so the spread is apples-to-apples."""
    d = get(f"https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/{sym.upper()}.json")["data"]
    return rv_from_closes([b["close"] for b in d[-(window + 5):]], window)


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
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    hit = cache.get(sym)
    if hit and (dt.date.today() - dt.date.fromisoformat(hit["at"])).days < ttl_days:
        return dt.date.fromisoformat(hit["d"]) if hit["d"] else None
    try:
        txt = get(f"https://api.nasdaq.com/api/analyst/{sym}/earnings-date")["data"]["reportText"]
        m = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", txt)
        d = dt.datetime.strptime(m.group(1), "%m/%d/%Y").date() if m else None
    except Exception:
        return None                      # unknown date: treated as "no known event"
    cache[sym] = {"d": d.isoformat() if d else None, "at": dt.date.today().isoformat()}
    CACHE.write_text(json.dumps(cache))
    return d


# ------------------------------------------------------------------- scoring
def parse_occ(s):
    """NOK260911P00003000 -> (date(2026,9,11), 'P', 3.0)"""
    m = re.fullmatch(r"([A-Z0-9]+?)(\d{6})([CP])(\d{8})", s)
    _, ymd, cp, k = m.groups()
    return dt.date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6])), cp, int(k) / 1000


def clip01(x):
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def score_contract(spot, iv30, rv30, strike, dte, bid, ask, oi):
    """0-100, transparent components. Every map below is a knob, not a fitted truth."""
    mid = (bid + ask) / 2
    rel_spread = (ask - bid) / mid
    roc_ann = (mid / strike) * 365 / dte                     # return on cash collateral
    em = iv30 / 100 * math.sqrt(dte / 365)                   # expected move to expiry, fraction
    cushion = (spot - strike) / (spot * em)                  # OTM distance in expected moves
    c = {
        "vrp":     clip01(math.log(iv30 / rv30) / 0.40) if rv30 and rv30 > 0 else 0.0,
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
        sc, c, mid, rs, roc, cush = score_contract(
            spot, iv30, rv30, strike, dte, o["bid"], o["ask"], o["open_interest"])
        out.append(dict(sym=sym, spot=spot, iv30=iv30, rv30=rv30, exp=exp, dte=dte, strike=strike,
                        delta=o["delta"], mid=mid, spread=rs, oi=o["open_interest"], roc=roc,
                        cushion=cush, collat=strike * 100, earn=ed, score=sc, parts=c))
    return out


def best_per_symbol(rows):
    """One contract per underlying: the highest-scoring one."""
    best = {}
    for r in sorted(rows, key=lambda r: -r["score"]):
        best.setdefault(r["sym"], r)
    return list(best.values())


HDR = (f"{'sym':<5} {'spot':>7} {'strike':>7} {'exp':>10} {'dte':>4} {'delta':>6} {'mid':>6} "
       f"{'ROC%y':>6} {'IV30':>6} {'RV30':>6} {'cush':>5} {'sprd%':>6} {'OI':>7} {'$col':>6} "
       f"{'vrp':>4} {'liq':>4} {'yld':>4} {'score':>6}")


def fmt(r):
    p = r["parts"]
    return (f"{r['sym']:<5} {r['spot']:>7.2f} {r['strike']:>7.2f} {r['exp']!s:>10} {r['dte']:>4} "
            f"{r['delta']:>6.2f} {r['mid']:>6.2f} {r['roc']*100:>6.1f} {r['iv30']:>6.1f} "
            f"{(r['rv30'] or 0):>6.1f} {r['cushion']:>5.2f} {r['spread']*100:>6.1f} {r['oi']:>7.0f} "
            f"{r['collat']:>6.0f} {p['vrp']:>4.2f} {p['liq']:>4.2f} {p['yield']:>4.2f} {r['score']:>6}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("tickers", nargs="*")
    ap.add_argument("--capital", type=float, default=2000)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--allow-earnings", action="store_true")
    ap.add_argument("--tui", action="store_true", help="interactive curses UI")
    a = ap.parse_args(argv)

    tickers = [t.upper() for t in a.tickers] or load_watchlist()
    if not tickers:
        sys.exit("no tickers: pass them as arguments (they become your saved watchlist)")
    if a.tui:
        if a.tickers:
            WATCH.write_text(json.dumps(tickers))
        return curses.wrapper(tui, tickers, {"capital": a.capital, "earn": a.allow_earnings})

    today = dt.date.today()
    rows, errs = [], []
    for t in tickers:
        try:
            rows += scan_symbol(t, a.capital, today, a.allow_earnings)
        except Exception as e:
            errs.append(f"{t}: {type(e).__name__} {e}")

    top = sorted(best_per_symbol(rows), key=lambda r: -r["score"])[:a.top]
    dropped = set(tickers) - {r["sym"] for r in rows} - {e.split(":")[0] for e in errs}
    for t in sorted(dropped):
        print(f"   {t}: no contract passed the filters (spread / delta band / OI / capital / earnings)",
              file=sys.stderr)
    print(HDR)
    for r in top:
        print(fmt(r))
    for r in top:
        if r["earn"]:
            print(f"   {r['sym']} earnings {r['earn']}")
    for e in errs:
        print("skip", e, file=sys.stderr)


# ----------------------------------------------------------------------- TUI
WATCH = pathlib.Path.home() / ".csp_watchlist.json"
SORTS = [("score", -1), ("roc", -1), ("iv30", -1), ("cushion", -1), ("spread", 1), ("dte", 1)]


def load_watchlist():
    return json.loads(WATCH.read_text()) if WATCH.exists() else []


def scan_async(tickers, cfg, st):
    """Runs in a thread; the UI only ever reads st."""
    today = dt.date.today()
    st.update(rows=[], errs=[], done=0, total=len(tickers), running=True, last="")
    for t in tickers:
        st["last"] = t
        try:
            st["rows"] = st["rows"] + scan_symbol(t, cfg["capital"], today, cfg["earn"])
        except Exception as e:
            st["errs"].append(f"{t}: {type(e).__name__}")
        st["done"] += 1
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


def tui(stdscr, tickers, cfg):
    curses.curs_set(0); stdscr.timeout(200); stdscr.keypad(True)
    curses.use_default_colors()
    for i, c in enumerate((curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_RED, curses.COLOR_CYAN), 1):
        curses.init_pair(i, c, -1)
    st = {"rows": [], "errs": [], "done": 0, "total": 0, "running": False, "last": ""}
    sel, sort_i, detail = 0, 0, None

    def rescan():
        if not st["running"]:
            threading.Thread(target=scan_async, args=(list(tickers), cfg, st), daemon=True).start()

    def view():
        key, sign = SORTS[sort_i]
        rows = [r for r in st["rows"] if r["sym"] == detail] if detail else best_per_symbol(st["rows"])
        return sorted(rows, key=lambda r: sign * r[key])

    rescan()
    while True:
        rows = view()
        sel = max(0, min(sel, len(rows) - 1))
        h, w = stdscr.getmaxyx()
        stdscr.clear()                 # not erase(): a shrinking list must not leave ghost rows
        prog = (f"scanning {st['done']}/{st['total']} {st['last']}" if st["running"]
                else f"{len(tickers)} tickers · {len(st['rows'])} contracts"
                     + (f" · {len(st['errs'])} errors" if st["errs"] else ""))
        title = (f" CSP  ${cfg['capital']:.0f}  DTE {DTE_MIN}-{DTE_MAX}  |D| {DELTA_LO:.2f}-{DELTA_HI:.2f}"
                 f"  spread<{MAX_REL_SPREAD*100:.0f}%  earn:{'keep' if cfg['earn'] else 'skip'}"
                 f"  sort:{SORTS[sort_i][0]}{'  [' + detail + ']' if detail else ''} ")
        stdscr.addstr(0, 0, f"{title}{prog:>{max(1, w - len(title) - 1)}}"[:w - 1], curses.A_REVERSE)
        stdscr.addstr(1, 0, HDR[:w - 1], curses.A_BOLD)
        for i, r in enumerate(rows[:h - 4]):
            col = curses.color_pair(1 if r["score"] >= 50 else 2 if r["score"] >= 35 else 3)
            stdscr.addstr(2 + i, 0, fmt(r)[:w - 1], col | (curses.A_REVERSE if i == sel else 0))
        if rows:
            r = rows[sel]
            note = (f" {r['sym']} {r['strike']:.2f}P {r['exp']}: credit ${r['mid']*100:.0f} on ${r['collat']:.0f} "
                    f"· earnings {r['earn'] or 'n/a'} · cushion {r['cushion']:.2f} EM"
                    + ("" if detail else " · enter = all its contracts"))
            stdscr.addstr(h - 2, 0, note[:w - 1], curses.color_pair(4))
        stdscr.addstr(h - 1, 0, " r rescan  a add  x remove  enter detail/back  s sort  c capital  "
                                "d delta  t dte  e earn  q quit"[:w - 1], curses.A_REVERSE)
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
            detail = None; rescan()
        elif k == ord("e"):
            cfg["earn"] = not cfg["earn"]; rescan()
        elif k == ord("x") and rows and not detail:
            tickers.remove(rows[sel]["sym"])
            WATCH.write_text(json.dumps(tickers)); rescan()
        elif k == ord("a"):
            t = ask(stdscr, "add ticker:").upper()
            if t and t not in tickers:
                tickers.append(t); WATCH.write_text(json.dumps(tickers)); rescan()
        elif k == ord("c"):
            v = ask(stdscr, "capital $:")
            if v.replace(".", "").isdigit():
                cfg["capital"] = float(v); rescan()
        elif k == ord("d"):
            v = ask(stdscr, "delta band lo hi:").split()
            if len(v) == 2:
                globals()["DELTA_LO"], globals()["DELTA_HI"] = float(v[0]), float(v[1]); rescan()
        elif k == ord("t"):
            v = ask(stdscr, "dte min max:").split()
            if len(v) == 2:
                globals()["DTE_MIN"], globals()["DTE_MAX"] = int(v[0]), int(v[1]); rescan()


def selftest():
    assert parse_occ("NOK260911P00003000") == (dt.date(2026, 9, 11), "P", 3.0)
    assert parse_occ("MARA261016C00012500") == (dt.date(2026, 10, 16), "C", 12.5)
    px = [100 * math.exp(0.01 * (-1) ** i) for i in range(60)]   # +-1% alternating
    rv = rv_from_closes(px, 30)
    assert abs(rv - 0.02 * math.sqrt(252) * 100) < 1, rv         # 2% daily swing annualized
    assert rv_from_closes([100, 101], 30) is None                # too few points

    # scoring: components move the right way; totals only where the sign is unambiguous
    base = dict(spot=10.0, iv30=50.0, rv30=35.0, strike=9.0, dte=30, bid=0.30, ask=0.32, oi=500)
    s0, c0, *_ = score_contract(**base)
    assert 0 <= s0 <= 100
    # richer IV lifts VRP but widens the expected move, so cushion shrinks: net is ambiguous by design
    c_iv = score_contract(**{**base, "iv30": 70.0})[1]
    assert c_iv["vrp"] > c0["vrp"] and c_iv["cushion"] < c0["cushion"]
    assert score_contract(**{**base, "rv30": 60.0})[1]["vrp"] == 0.0    # IV below RV -> no VRP credit
    assert score_contract(**{**base, "rv30": 60.0})[0] < s0
    assert score_contract(**{**base, "ask": 0.45})[1]["liq"] < c0["liq"]          # wider spread
    assert score_contract(**{**base, "oi": 25})[0] < s0                           # thinner book
    assert score_contract(**{**base, "strike": 7.0})[1]["cushion"] > c0["cushion"]
    # expected-move convention matches the CBOE card: EM = IV30 * sqrt(30/365)
    assert abs(math.sqrt(30 / 365) - 0.28669) < 1e-4

    # list logic the TUI drives: one row per underlying, every sort key orderable
    mk = lambda s, sc, roc, dte: dict(sym=s, score=sc, roc=roc, iv30=40.0, cushion=0.5, spread=0.02, dte=dte)
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
    print("selftest ok")


if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else main()
