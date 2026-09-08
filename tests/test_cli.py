"""The entry point: argument surface, and Ctrl-C during a scan or in the TUI."""
import pytest

from csp import cli
from csp.score import Filters, scan_all


def test_capital_and_earnings_reach_the_filters(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "run_scan", lambda tickers, a, f: seen.update(tickers=tickers, f=f))
    cli.main(["nok", "sofi", "--capital", "900", "--allow-earnings"])
    assert seen["tickers"] == ["NOK", "SOFI"]                  # upper-cased for the vendor
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
    monkeypatch.setattr("csp.score.WORKERS", 1)                # deterministic order
    with pytest.raises(KeyboardInterrupt):
        scan_all(["A"] + [f"S{i}" for i in range(40)], Filters())
    assert len(calls) < 40                                     # the rest never ran
