# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
make install          # pip install -e ".[dev]"  (pytest + ruff; no runtime deps)
make check            # pytest + ruff check + ruff format --check — the exact gate CI runs
make test             # pytest
make lint             # ruff check . && ruff format --check .
make format           # ruff check --fix . && ruff format .

pytest tests/test_score.py                              # one file
pytest tests/test_score.py::test_filters_cut_on_every_axis   # one test
pytest -k replay                                        # by name
```

The `csp` console script only exists after `make install`. `python -m csp` does not work —
there is no `__main__.py`.

`make check` must be green before any commit; the release workflow re-runs the same three
commands on a tag, and a formatting failure has reached a published tag before.

## Architecture

A cash-secured put scanner. Fetch delayed CBOE option chains → apply hard filters → score →
show in a table/TUI → measure against history. Python standard library only at runtime.

Module layering (imports only ever point downward):

```
cli.py ──► tui.py ──► render.py ──► backtest.py ──► score.py ──► sources.py ──► http.py
                                                    universe.py ─┘             cache.py
```

- **`http.py`** — one `get(url)`. A single process-wide lock enforces `MIN_INTERVAL = 0.35s`
  *between request starts* (~2.9 req/s — an earlier docstring called this "0.4 requests/second",
  confusing the interval with a rate). Downloads themselves overlap across threads. This gate,
  not CPU or JSON parsing, is what a scan's wall clock is made of, and it is why adding an HTTP
  library would buy nothing. Measured 2026-09-08: 12 chain requests through the gate, no 429.
  Sustained load at 180 requests is unmeasured, so do not loosen the interval on that sample.
- **`cache.py`** — dotfiles in `$HOME` (`.csp_hist.json`, `.csp_earnings.json`,
  `.csp_universe.json`, `.csp_watchlist.json`, `.csp_chains.db`). Writes re-read under a lock
  because scans are parallel; a half-written file is treated as an empty cache, never a
  traceback. It owns `snap_db()` too — the chain store is read by the *scanner* (IV rank), and
  `score` sits below `backtest`, so opening it cannot live next to the backtest that fills it.
  The read path (`sources.recorded_ivs`) returns `[]` for an absent file rather than creating
  one: a machine that never snapshots must not grow a database from a plain scan.
- **`sources.py`** — the only module that talks to vendors. In-process chain cache with
  `CHAIN_TTL = 600s`, so changing a filter never re-downloads. `known_closes()` is the
  never-fetches accessor the UI loop must use; `history()`/`closes()` may block on the network.
- **`score.py`** — the core. `Filters` (hard cuts, per account) is deliberately separate from
  the scoring scales (`W`, `VRP_LO/HI`, `SPREAD_SCALE`, `OI_SCALE`, `YIELD_LO/HI`), which are
  fixed constants so that widening a filter cannot silently re-scale scores. `score_contract()`
  is pure: pass it numbers, get back `(score, components, mid, spread, roc, cushion)`. IV rank
  lives here too (`iv_level`, `iv_levels`, `iv_rank`) and is **not** a score component — see the
  invariant below.
- **`backtest.py`** — two different measurements, do not conflate them:
  - `backtest()`/`ev()` — today's real premium replayed over the underlying's own daily path.
    No option is ever priced from a model. Memoized onto `Candidate.ev` because the TUI redraws
    ~5×/second.
  - `snapshot()`/`replay()` — records live put chains into SQLite (`puts` table, PK
    `(date, sym, exp, strike)`), then trades them back with the same filters and the same score,
    entering at the recorded **bid** and settling on the underlying's expiry close. `replay()` is
    a portfolio: each day it settles what expired, optionally buys back what hit `take_profit`,
    recomputes uncommitted cash, and fills the best-scoring contracts that fit, capped at
    `max_per_sym` per underlying. It returns `(trades, still_open)` — trades in **resolution
    order** (`exit`), because that is the order the cash arrived and the drawdown is an equity
    curve. Unresolved positions are reported, never counted.
- **`render.py`** — `COLUMNS` carries a drop priority per column; priority `0` never drops, so
  `sym strike dte ROC%y cush score` survive any terminal width. `explain()` produces the plain
  Turkish panel shared by `--explain` and the TUI.
- **`tui.py`** — curses. The scan thread only ever *rebinds* `st["rows"]` (never mutates) since
  the UI thread reads it concurrently. Every key except `r` re-filters cached chains with no
  network; `r` calls `forget_chains()` and refetches.

### Invariants worth preserving

- **`score.reject()` is shared by the live scan and `replay()`** — that shared call is what makes
  the backtest a test of the scanner rather than of a second, drifting rule set. Add a hard cut
  there, not in a caller. It returns `None` for a tradeable contract, else the *name* of the
  first cut it fails (`dte`, `cash`, `ivr`, `delta`, `quote`, `spread`, `oi`), and that order is
  chosen for what the name says, not for arithmetic: identity cuts (window, collateral, IV rank
  floor) before the delta band, liquidity last, so a far wing with no bid reports `delta` rather
  than `quote`. Its `cash` argument is the collateral available *right now* (the whole account
  for a scan, the uncommitted part for a portfolio replay) and defaults to `f.capital`, so a
  caller that does not think in portfolios cannot get it wrong.
- **A symbol that produced no rows still has something to say.** `scan_symbol()` returns
  `(rows, why)` and `scan_all()` returns `(rows, errs, why)`, where `why` counts the cut each
  rejected contract hit. `dte` is deliberately *not* counted — every chain has hundreds of
  weeklies and LEAPs outside the window, and a "300 dte" line would bury the knob that can be
  moved. An empty tally therefore means "nothing ever reached the window", which `render.WHY` /
  `why_line()` says in those words. The CLI prints the top three cuts per dropped symbol; the
  TUI puts the top cut per symbol on the last panel line.
- **`Filters` is one mutable object** flowing CLI → scan → TUI → backtest. TUI keys mutate it
  in place and rescan.
- **`snapshot()` stamps rows with the vendor's session, not `date.today()`** — derived from the
  underlying's `last_trade_time` (the envelope's own `timestamp` says today even on a holiday).
  One date for the whole run, since `replay()` treats a date as one decision point. This is what
  makes an unattended daily job idempotent: a holiday re-records the same session onto the same
  primary key instead of inventing a day in the IV series. No timestamp anywhere → record nothing.
- **No lookahead in `replay()`**: realized vol is computed with `realized_vol(sym, before=date)`,
  and entries only ever see that day's recorded quotes. A contract whose expiry has not passed
  (`close_on()` → `None`) stays in `still_open` and is never counted — settlement may reach past
  the last recorded chain (the stock kept trading after you stopped recording), the *decision*
  may not.
- **`replay_stats` is portfolio arithmetic**: `tied` is peak *concurrent* committed cash via
  `peak_committed()` (same-day releases before same-day entries), and `days` is the calendar span
  from the earliest entry to the last `exit` — idle cash counts. `trades[0]` is the first to
  resolve, not the first to open; never read the book's start date off it. `deployed` sums the
  days each position was actually held, which is its DTE only when it ran to expiry.
- **A trade resolves on `exit`, not on `exp`.** Every consumer — the sort, `peak_committed()`,
  the span, the position-days — reads `exit`, so a contract bought back early frees its
  collateral on the day it was bought back. `settle` (the underlying's expiry close) and
  `buyback` (the ask paid) are mutually exclusive; exactly one is `None`, and `assigned` may only
  ever look at the settled half.
- **The early exit is a recorded ask, never a model price.** `take_profit` closes a position only
  when that day's stored chain carries an ask for that exact contract: an ask of `0` is no offer,
  not a free buyback, and a contract closed on a date cannot be re-sold on the same date (paying
  the ask and taking the bid back is the spread, not a trade). Entry at the bid and exit at the
  ask is what keeps the round turn as expensive as it really was.
- **Runtime dependencies stay empty.** `pyproject.toml` documents why (rate limit dominates;
  math is sub-millisecond; curses is already a full-screen UI). Adding one needs a real argument.
- **IV rank is a signal, not a weight.** Its input is whatever this machine happened to record,
  so a symbol tracked for a year and one added yesterday would be scored on different evidence —
  and the score's whole job is to compare them in one table. It is a column, a panel line, a sort
  key and an optional `--min-iv-rank` cut. Under `IVR_MIN_DAYS` recorded days it reports nothing
  rather than a number. The band constants (`IVR_DTE_*`, `IVR_DELTA_*`) are fixed for the same
  reason the scoring scales are: if they followed `Filters`, widening DTE would re-rank the world.
- **The IV band is written twice** — as a SQL predicate in `sources.recorded_ivs` (a year of one
  symbol's chains is tens of thousands of rows and the TUI re-scans on every keypress) and as a
  comprehension in `score.iv_level` for the chain in hand. Nothing shares the predicate, so
  `test_a_recorded_day_and_a_live_chain_rank_the_same` is what keeps them equal. Change one, run it.
- **Language split**: user-facing strings (CLI help, TUI, error messages, README) are Turkish;
  code, comments and docstrings are English.
- Comments prefixed `ponytail:` mark known, accepted limitations (e.g. overlapping backtest
  windows are autocorrelated; OCC roots assume unadjusted equity symbols). Keep the marker when
  editing those lines.
- Version lives only in `csp/__init__.py` (`[tool.hatch.version]` reads it).

## Tests

`tests/conftest.py` supplies two fixtures: `row` (a realistic scored `Candidate`) and `history`
(installs a fake price series by monkeypatching `sources._px` / `sources._dates`).

Tests never touch the network or `$HOME`: fake the price series with `history`, monkeypatch
`csp.score.scan_symbol` or `chain()` for scan paths, and pass `path=tmp_path/...` to
`snapshot()`/`replay()`/`snap_db()`. A test that would issue an HTTP request is a bug in the test.

Numbers in backtest tests are chosen so expected P&L is checkable by hand — keep new cases that
way rather than asserting against whatever the code currently returns.

## Release

Tag `vX.Y.Z` → `.github/workflows/release.yml` runs pytest + ruff, builds, and publishes to PyPI
via trusted publishing (`pypi` environment, OIDC — no token in the repo). Bump `__version__` in
`csp/__init__.py` first.

The Homebrew tap lives in the separate `kpostaagasi/homebrew-csp` repo
(`Formula/csp-tarayici.rb`). After a release, update its `url` to the new tag tarball and the
matching `sha256`. The formula has no `resource` blocks by design — the package has no runtime
dependencies — and its `test do` block asserts on Turkish CLI output (`sembol yok`,
`kayıtlı zincir yok`), so changing those strings breaks `brew test`.
