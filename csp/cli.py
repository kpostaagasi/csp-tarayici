"""Argument parsing and the one-shot table. Everything else lives in a module of its own."""

import argparse
import json
import sys

from . import __version__
from .backtest import recorded, replay, replay_stats, snapshot
from .cache import CHAINS, WATCH
from .render import explain, print_trades, table
from .score import Filters, best_per_symbol, scan_all
from .tui import load_watchlist
from .universe import universe

DESC = "Cash-secured put tarayıcı: CBOE zinciri, gerçekleşen oynaklık, kazanç takvimi. API key yok."


def parse(argv=None):
    ap = argparse.ArgumentParser(prog="csp", description=DESC)
    ap.add_argument("tickers", nargs="*", help="taranacak semboller (boşsa kayıtlı izleme listesi)")
    ap.add_argument("--capital", type=float, default=2000, help="bloke edilebilir nakit, $")
    ap.add_argument("--top", type=int, default=10, help="tabloda kaç satır")
    ap.add_argument("--allow-earnings", action="store_true", help="vade içi kazanç olsa da göster")
    ap.add_argument("--tui", action="store_true", help="etkileşimli curses arayüzü")
    ap.add_argument(
        "--universe",
        nargs="?",
        type=int,
        const=60,
        metavar="N",
        help="watchlist yerine, sermayeyle alınabilen en işlek N ABD hissesi",
    )
    ap.add_argument("--explain", action="store_true", help="her satırın altına düz Türkçe okuma")
    ap.add_argument(
        "--snapshot",
        action="store_true",
        help=f"günün put zincirlerini {CHAINS.name} dosyasına yaz (backtest verisi)",
    )
    ap.add_argument(
        "--replay",
        metavar="SYM",
        help="kayıtlı zincirlerle gerçek backtest: tek sembol, virgüllü liste ya da TÜMÜ",
    )
    ap.add_argument(
        "--max-per-symbol",
        type=int,
        default=1,
        metavar="N",
        help="--replay sırasında aynı sembolde en fazla kaç açık pozisyon (varsayılan 1)",
    )
    ap.add_argument("--version", action="version", version=f"csp {__version__}")
    return ap.parse_args(argv)


ALL = {"TÜMÜ", "TUMU", "ALL"}


def run_replay(spec, f, max_per_sym=1):
    syms = None if spec.strip().upper() in ALL else [s for s in spec.upper().split(",") if s.strip()]
    trades, still_open = replay(syms, f, max_per_sym=max_per_sym)
    stats = replay_stats(trades, still_open)
    if not stats:
        who = "TÜMÜ" if syms is None else ",".join(syms)
        if still_open:
            sys.exit(
                f"{who}: {len(still_open)} kontrat seçildi ama hiçbirinin vadesi henüz geçmedi.\n"
                "Geçmiş, kaydetmeye başladığın günden ileri doğru birikiyor."
            )
        sys.exit(
            f"{who}: kayıtlı zincir yok ya da hiç kontrat filtreleri geçmedi.\n"
            "Veri biriktirmek için her gün: csp --universe --snapshot"
        )
    print_trades(trades, stats, still_open)


def run_snapshot(tickers):
    n = snapshot(tickers)
    rows, days, syms = recorded()
    print(f"{n} satır yazıldı · defter: {rows} satır / {days} gün / {syms} sembol → {CHAINS}")


def run_scan(tickers, a, f):
    rows, errs = scan_all(
        tickers,
        f,
        on_done=lambda n, sym, *_: print(
            f"\r  {n}/{len(tickers)} {sym:<6}", end="", file=sys.stderr, flush=True
        ),
    )
    print("\r" + " " * 24 + "\r", end="", file=sys.stderr)

    top = sorted(best_per_symbol(rows), key=lambda r: -r.score)[: a.top]
    dropped = set(tickers) - {r.sym for r in rows} - {e.split(":")[0] for e in errs}
    for t in sorted(dropped):
        print(
            f"   {t}: hiçbir kontrat filtreleri geçmedi (spread / delta / OI / sermaye / kazanç)",
            file=sys.stderr,
        )
    hdr, lines = table(top, 200)
    print(hdr)
    for line in lines:
        print(line)
    for r in top:
        if a.explain:
            print()
            for line in explain(r):
                print(line)
        elif r.earn:
            print(f"   {r.sym} kazanç {r.earn}")
    for e in errs:
        print("atlandı", e, file=sys.stderr)


def main(argv=None):
    """Every path funnels through here, so Ctrl-C is caught once, not in each command."""
    try:
        return dispatch(argv)
    except KeyboardInterrupt:
        print(file=sys.stderr)  # the shell prompt should not land mid-line
        return 130  # 128 + SIGINT, what a shell expects


def dispatch(argv=None):
    a = parse(argv)
    f = Filters(capital=a.capital, allow_earnings=a.allow_earnings)

    if a.replay:
        return run_replay(a.replay, f, a.max_per_symbol)

    tickers = [t.upper() for t in a.tickers] or (
        universe(a.capital, a.universe) if a.universe else load_watchlist()
    )
    if not tickers:
        sys.exit("sembol yok: argüman olarak geç ya da --universe kullan")
    if a.snapshot:
        return run_snapshot(tickers)
    if a.tui:
        from .tui import run

        if a.tickers:
            WATCH.write_text(json.dumps(tickers))
        return run(tickers, f, save=not a.universe)
    return run_scan(tickers, a, f)
