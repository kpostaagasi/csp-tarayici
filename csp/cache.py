"""On-disk caches. Small JSON files, merged under a lock because scans run in parallel."""

import json
import pathlib
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
