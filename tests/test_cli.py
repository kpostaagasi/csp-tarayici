"""The entry point: argument surface, and Ctrl-C during a scan or in the TUI."""

import pytest

from csp import cli
from csp.score import Filters, scan_all


def test_capital_and_earnings_reach_the_filters(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "run_scan", lambda tickers, a, f: seen.update(tickers=tickers, f=f))
    cli.main(["nok", "sofi", "--capital", "900", "--allow-earnings"])
    assert seen["tickers"] == ["NOK", "SOFI"]  # upper-cased for the vendor
    assert seen["f"] == Filters(capital=900.0, allow_earnings=True)


def test_ctrl_c_exits_with_130_not_a_traceback(monkeypatch):
    """A TUI or a five-minute scan must not dump curses internals on the user's terminal."""

    def interrupted(*a, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run_scan", interrupted)
    assert cli.main(["nok"]) == 130


def test_no_tickers_and_no_watchlist_is_a_message_not_a_crash(monkeypatch):
    monkeypatch.setattr(cli, "load_watchlist", lambda: [])
    with pytest.raises(SystemExit) as e:
        cli.main([])
    assert "sembol yok" in str(e.value)


def test_scan_all_cancels_the_queue_on_ctrl_c(monkeypatch):
    """Otherwise the executor drains every queued symbol before the interrupt is seen."""
    calls = []

    def scan(sym, f, today=None):
        calls.append(sym)
        if sym == "A":
            raise KeyboardInterrupt
        return []

    monkeypatch.setattr("csp.score.scan_symbol", scan)
    monkeypatch.setattr("csp.score.WORKERS", 1)  # deterministic order
    with pytest.raises(KeyboardInterrupt):
        scan_all(["A"] + [f"S{i}" for i in range(40)], Filters())
    assert len(calls) < 40  # the rest never ran


# ------------------------------------------------------------------------------ --replay
def replays(monkeypatch, trades=(), still_open=()):
    """Capture what run_replay asks the engine for, without touching $HOME or the network."""
    seen = {}

    def fake(syms, f, path=None, max_per_sym=1):
        seen.update(syms=syms, f=f, max_per_sym=max_per_sym)
        return list(trades), list(still_open)

    monkeypatch.setattr(cli, "replay", fake)
    return seen


def test_replay_takes_one_symbol_a_list_or_everything(monkeypatch):
    for spec, want in [
        ("nok", ["NOK"]),
        ("nok,sofi", ["NOK", "SOFI"]),
        ("TÜMÜ", None),
        ("all", None),
    ]:
        seen = replays(monkeypatch)
        with pytest.raises(SystemExit):
            cli.main(["--replay", spec])
        assert seen["syms"] == want


def test_max_per_symbol_reaches_the_engine(monkeypatch):
    seen = replays(monkeypatch)
    with pytest.raises(SystemExit):
        cli.main(["--replay", "NOK", "--capital", "5000", "--max-per-symbol", "3"])
    assert seen["max_per_sym"] == 3
    assert seen["f"] == Filters(capital=5000.0)


def test_an_empty_chain_store_keeps_the_string_brew_test_asserts_on(monkeypatch):
    """Formula/csp-tarayici.rb greps for this exact phrase; changing it breaks `brew test`."""
    replays(monkeypatch)
    with pytest.raises(SystemExit) as e:
        cli.main(["--replay", "NOK"])
    assert "kayıtlı zincir yok" in str(e.value)


def test_only_unresolved_positions_says_so_instead(monkeypatch):
    """ "No data" and "recorded, but nothing has expired yet" are different answers."""
    replays(monkeypatch, still_open=[{"sym": "NOK", "exp": "2026-12-18", "collat": 900.0}])
    with pytest.raises(SystemExit) as e:
        cli.main(["--replay", "NOK"])
    assert "vadesi henüz geçmedi" in str(e.value)
