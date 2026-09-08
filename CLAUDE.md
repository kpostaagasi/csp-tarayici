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
  *between request starts*; CBOE 429s above ~0.4 req/s from one IP. Downloads themselves
  overlap across threads. This gate, not CPU or JSON parsing, is why a 60-symbol scan takes
  2-3 minutes, and it is why adding an HTTP library would buy nothing.
- **`cache.py`** — dotfiles in `$HOME` (`.csp_hist.json`, `.csp_earnings.json`,
  `.csp_universe.json`, `.csp_watchlist.json`, `.csp_chains.db`). Writes re-read under a lock
  because scans are parallel; a half-written file is treated as an empty cache, never a
  traceback.
- **`sources.py`** — the only module that talks to vendors. In-process chain cache with
  `CHAIN_TTL = 600s`, so changing a filter never re-downloads. `known_closes()` is the
  never-fetches accessor the UI loop must use; `history()`/`closes()` may block on the network.
- **`score.py`** — the core. `Filters` (hard cuts, per account) is deliberately separate from
  the scoring scales (`W`, `VRP_LO/HI`, `SPREAD_SCALE`, `OI_SCALE`, `YIELD_LO/HI`), which are
  fixed constants so that widening a filter cannot silently re-scale scores. `score_contract()`
  is pure: pass it numbers, get back `(score, components, mid, spread, roc, cushion)`.
- **`backtest.py`** — two different measurements, do not conflate them:
  - `backtest()`/`ev()` — today's real premium replayed over the underlying's own daily path.
    No option is ever priced from a model. Memoized onto `Candidate.ev` because the TUI redraws
    ~5×/second.
  - `snapshot()`/`replay()` — records live put chains into SQLite (`puts` table, PK
    `(date, sym, exp, strike)`), then trades them back with the same filters and the same score,
    entering at the recorded **bid** and settling on the underlying's expiry close.
- **`render.py`** — `COLUMNS` carries a drop priority per column; priority `0` never drops, so
  `sym strike dte ROC%y cush score` survive any terminal width. `explain()` produces the plain
  Turkish panel shared by `--explain` and the TUI.
- **`tui.py`** — curses. The scan thread only ever *rebinds* `st["rows"]` (never mutates) since
  the UI thread reads it concurrently. Every key except `r` re-filters cached chains with no
  network; `r` calls `forget_chains()` and refetches.

### Invariants worth preserving

- **`score.passes()` is shared by the live scan and `replay()`** — that shared call is what makes
  the backtest a test of the scanner rather than of a second, drifting rule set. Add a hard cut
  there, not in a caller.
- **`Filters` is one mutable object** flowing CLI → scan → TUI → backtest. TUI keys mutate it
  in place and rescan.
- **No lookahead in `replay()`**: realized vol is computed with `realized_vol(sym, before=date)`.
  A contract whose expiry has not passed ends the replay (`settle is None` → `break`), it is
  never counted.
- **Runtime dependencies stay empty.** `pyproject.toml` documents why (rate limit dominates;
  math is sub-millisecond; curses is already a full-screen UI). Adding one needs a real argument.
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
