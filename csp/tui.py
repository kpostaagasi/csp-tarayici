"""The curses UI: a scan view with an explanation panel, and a help screen that defines everything.

curses (stdlib) already gives a full-screen app, resize-aware geometry and color pairs. The only
hand-written plumbing is ~40 lines of scrolling and column dropping — a widget framework would
replace that with a 15 MB dependency, so it stays.
"""

import curses
import json
import locale
import threading

from .backtest import ev
from .cache import WATCH
from .render import SORTS, explain, table
from .score import best_per_symbol, scan_all
from .sources import forget_chains

HELP = [  # 24 satırlık bir terminalde bile kesilmemeli: kısa tut
    " CSP tarayıcı · ekrandaki her şey ne demek",
    " TABLO — sembol başına en iyi put adayı ('enter': o sembolün tüm kontratları)",
    "  sym strike exp dte  sattığın put: hisse, kullanım fiyatı, vade, kalan gün",
    "  spot / mid          hissenin gecikmeli fiyatı / (bid+ask)/2 prim. 1 kontrat = mid x 100 dolar",
    "  delta / $col        atama olasılığına kaba yaklaşım (-0.30 ≈ %30) / bloke nakit: strike x 100",
    "  ROC%y               prim/teminat, yıllığa çevrilmiş. Nakit getirisi — olasılık değil",
    "  IV / RV30           sattığın implied vol % / son 30 günün gerçekleşeni %. vrp bu oran",
    "  cush                strike kaç 'beklenen hareket' (EM) uzakta. 1.00 = tam bir EM",
    "  sprd% / OI          (ask-bid)/mid giriş-çıkış maliyeti / kaç kontrat açık duruyor",
    "  EV$                 GEÇMİŞ TESTİ: bugünün primi, bu hissenin geçmişindeki her DTE'lik",
    "                      pencerede ortalama kâr/zarar. Eksi = prim ödemiyor. '—' = ölçüm yok",
    "  IVR                 IV rank 0-1: bugünkü vol, --snapshot ile kaydettiğin günlerin",
    "                      aralığında nerede. Skora girmiyor; '—' = yeterli kayıt yok",
    "  vrp liq yld score   bileşenler 0-1 (dördüncüsü cush) · score = 0.35vrp+0.25liq+0.20yld+0.20cush",
    "  renk                yeşil >=50 · sarı 35-49 · kırmızı <35; dar terminalde önemsiz kolon düşer",
    " ALT PANEL — seçili kontratın düz okuması: prim, bloke nakit, atama olasılığı, başabaş fiyat,",
    " beklenen hareket, skorun bileşenleri, IV rank ve geçmiş testinin tamamı (atama/zarar %'si,",
    " ortalama, en kötü pencere).",
    " FİLTRELER (başlıkta güncel) — DTE · |delta| · spread · OI · teminat<=sermaye · vade içi kazanç",
    " · --min-iv-rank. Filtreler listeye girişi belirler, skoru değil.",
    " TUŞLAR — jk/oklar gez · enter detay · s sırala · a/x sembol · c sermaye · d delta · t dte",
    " · e kazanç filtresi · r veriyi yenile · q geri/çık",
    " VERİ — filtre düğmeleri cache'li zincirleri anında süzer, ağa çıkmaz; yalnız 'r' yeniden çeker.",
]


def load_watchlist():
    return json.loads(WATCH.read_text()) if WATCH.exists() else []


def scan_async(tickers, f, st):
    """Runs in a thread; the UI only ever reads st. Rows appear as each symbol lands."""
    st.update(rows=[], errs=[], done=0, total=len(tickers), running=True, last="")

    def landed(n, sym, got, err):
        st["done"], st["last"] = n, sym
        if got:
            st["rows"] = st["rows"] + got  # rebind, never mutate: the UI thread reads this
        if err:
            st["errs"] = st["errs"] + [err]

    scan_all(tickers, f, on_done=landed)
    st["running"] = False


def ask(stdscr, msg):
    h, w = stdscr.getmaxyx()
    curses.echo()
    curses.curs_set(1)
    stdscr.timeout(-1)  # the 200ms poll would return an empty string instantly
    stdscr.move(h - 1, 0)
    stdscr.clrtoeol()
    stdscr.addstr(h - 1, 0, msg[: w - 2])
    try:
        s = stdscr.getstr(h - 1, len(msg) + 1, 24).decode().strip()
    except Exception:
        s = ""
    curses.noecho()
    curses.curs_set(0)
    stdscr.timeout(200)
    return s


def read_key(stdscr):
    """getch, plus manual CSI decoding: some terminals hand back raw ESC [ A/B here."""
    k = stdscr.getch()
    if k != 27:
        return k
    a, b = stdscr.getch(), stdscr.getch()
    return {(91, 65): curses.KEY_UP, (91, 66): curses.KEY_DOWN}.get((a, b), -1)


def numeric(s):
    return s.replace(".", "", 1).isdigit()


def tui(stdscr, tickers, f, save=True):
    locale.setlocale(locale.LC_ALL, "")  # curses encodes addstr with the locale: 'ı' must survive
    curses.curs_set(0)
    stdscr.timeout(200)
    stdscr.keypad(True)
    curses.use_default_colors()
    for i, c in enumerate((curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_RED, curses.COLOR_CYAN), 1):
        curses.init_pair(i, c, -1)
    st = {"rows": [], "errs": [], "done": 0, "total": 0, "running": False, "last": ""}
    sel, sort_i, detail, showing_help = 0, 0, None, False

    def remember():  # a --universe run must not overwrite the saved watchlist
        if save:
            WATCH.write_text(json.dumps(tickers))

    def rescan():
        if not st["running"]:
            threading.Thread(target=scan_async, args=(list(tickers), f, st), daemon=True).start()

    def view():
        key, sign = SORTS[sort_i]
        rows = [r for r in st["rows"] if r.sym == detail] if detail else best_per_symbol(st["rows"])
        for r in rows:
            ev(r)  # memoized on the row; EV$ column and sort both need it
        return sorted(rows, key=lambda r: sign * (getattr(r, key) or 0))

    rescan()
    while True:
        if showing_help:
            h, w = stdscr.getmaxyx()
            stdscr.clear()
            for i, line in enumerate(HELP[: h - 1]):
                stdscr.addstr(i, 0, line[: w - 1], curses.A_BOLD if i == 0 else 0)
            stdscr.addstr(h - 1, 0, " herhangi bir tuş: geri "[: w - 1], curses.A_REVERSE)
            stdscr.refresh()
            if read_key(stdscr) != -1:
                showing_help = False
            continue

        rows = view()
        sel = max(0, min(sel, len(rows) - 1))
        h, w = stdscr.getmaxyx()
        stdscr.clear()  # not erase(): a shrinking list must not leave ghost rows
        prog = (
            f"tarıyor {st['done']}/{st['total']} {st['last']}"
            if st["running"]
            else f"{len(tickers)} sembol · {len(st['rows'])} kontrat"
            + (f" · {len(st['errs'])} hata" if st["errs"] else "")
        )
        title = (
            f" CSP · sermaye ${f.capital:.0f} · DTE {f.dte_min}-{f.dte_max}"
            f" · |delta| {f.delta_lo:.2f}-{f.delta_hi:.2f} · spread<%{f.max_spread * 100:.0f}"
            f" · kazanç:{'tut' if f.allow_earnings else 'atla'}"
            f" · sıra:{SORTS[sort_i][0]}{'  [' + detail + ']' if detail else ''} "
        )
        stdscr.addstr(0, 0, f"{title}{prog:>{max(1, w - len(title) - 1)}}"[: w - 1], curses.A_REVERSE)

        hdr, lines = table(rows, w - 1)
        panel = explain(rows[sel])[: max(0, h - 6)] if rows else []
        vis = max(1, h - 3 - len(panel))
        top_i = max(0, min(sel - vis + 1, len(lines) - vis)) if len(lines) > vis else 0
        stdscr.addstr(1, 0, hdr[: w - 1], curses.A_BOLD)
        pos = f" {sel + 1}/{len(lines)} "
        if len(lines) > vis and w - 1 - len(pos) > len(hdr):  # never lands on a column header
            stdscr.addstr(1, w - 1 - len(pos), pos, curses.A_REVERSE)
        for i, line in enumerate(lines[top_i : top_i + vis]):
            r = rows[top_i + i]
            col = curses.color_pair(1 if r.score >= 50 else 2 if r.score >= 35 else 3)
            stdscr.addstr(2 + i, 0, line[: w - 1], col | (curses.A_REVERSE if top_i + i == sel else 0))
        for i, line in enumerate(panel):
            stdscr.addstr(
                h - 1 - len(panel) + i,
                0,
                line[: w - 1],
                curses.color_pair(4) | (curses.A_BOLD if i == 0 else 0),
            )
        stdscr.addstr(
            h - 1,
            0,
            (
                " ? yardım  enter detay  r tara  s sırala  a/x sembol  "
                "c sermaye  d delta  t dte  e kazanç  q çık"
            )[: w - 1],
            curses.A_REVERSE,
        )
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
            detail, sel = (None, 0) if detail else (rows[sel].sym, 0)
        elif k == ord("s"):
            sort_i = (sort_i + 1) % len(SORTS)
        elif k == ord("r"):
            detail = None
            forget_chains()  # r = refetch; every other knob re-filters cached chains
            rescan()
        elif k == ord("e"):
            f.allow_earnings = not f.allow_earnings
            rescan()
        elif k == ord("x") and rows and not detail:
            tickers.remove(rows[sel].sym)
            remember()
            rescan()
        elif k == ord("a"):
            t = ask(stdscr, "sembol ekle:").upper()
            if t and t not in tickers:
                tickers.append(t)
                remember()
                rescan()
        elif k == ord("c"):
            v = ask(stdscr, "sermaye $:")
            if numeric(v):
                f.capital = float(v)
                rescan()
        elif k == ord("d"):
            v = ask(stdscr, "delta bandı alt üst:").split()
            if len(v) == 2 and all(numeric(x) for x in v):
                f.delta_lo, f.delta_hi = float(v[0]), float(v[1])
                rescan()
        elif k == ord("t"):
            v = ask(stdscr, "dte alt üst:").split()
            if len(v) == 2 and all(x.isdigit() for x in v):
                f.dte_min, f.dte_max = int(v[0]), int(v[1])
                rescan()
        elif k == ord("?"):
            showing_help = True


def run(tickers, f, save=True):
    """Entry point the CLI calls: curses.wrapper restores the terminal even on a traceback."""
    return curses.wrapper(tui, tickers, f, save)
