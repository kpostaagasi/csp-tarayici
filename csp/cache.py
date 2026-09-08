"""On-disk state: small JSON caches merged under a lock, and the recorded-chain database.

The SQLite store lives here rather than next to the backtest that fills it, because the scanner
reads it too — IV rank comes out of the same rows — and `score` sits below `backtest` in the
import order. Owning the path already, this module owns opening it.
"""

import json
import pathlib
import sqlite3
import threading

HOME = pathlib.Path.home()
HIST = HOME / ".csp_hist.json"  # daily closes per symbol, refreshed once a calendar day
EARNINGS = HOME / ".csp_earnings.json"  # estimated report dates, refreshed weekly
UNIV = HOME / ".csp_universe.json"  # the Nasdaq screener dump, refreshed once a calendar day
WATCH = HOME / ".csp_watchlist.json"  # the symbols the TUI opens with
CHAINS = HOME / ".csp_chains.db"  # recorded option chains, for the real backtest

_disk = threading.Lock()


def cached(path):
    """Whole cache file as a dict. A half-written file is an empty cache, not a traceback."""
    try:
        with _disk:
            return json.loads(path.read_text()) if path.exists() else {}
    except ValueError:
        return {}


def store(path, key, value):
    """Merge one key into a cache file. Re-reads under the lock: a parallel scan writes it too."""
    with _disk:
        try:
            c = json.loads(path.read_text()) if path.exists() else {}
        except ValueError:
            c = {}
        c[key] = value
        path.write_text(json.dumps(c))


DDL = """create table if not exists puts (
  date text, sym text, exp text, strike real, bid real, ask real, iv real, delta real,
  oi real, spot real, primary key (date, sym, exp, strike))"""


def snap_db(path=None):
    """The recorded chain store, created on first touch so a fresh install can just read it."""
    con = sqlite3.connect(path or CHAINS)
    con.execute(DDL)
    return con
