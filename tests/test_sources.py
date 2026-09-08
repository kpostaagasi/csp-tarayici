"""Caching layers: what must not be re-fetched, and what a broken cache file may not do.

Every vendor call is monkeypatched and every cache path is redirected into tmp_path, so a test
that reaches the network or the user's $HOME is a bug in the test.
"""

import datetime as dt
import json

import pytest

from csp import cache, sources


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """No $HOME, no memory carried between tests, no network unless a test installs one."""
    for name in ("HIST", "EARNINGS", "UNIV"):
        monkeypatch.setattr(sources, name, tmp_path / f"{name.lower()}.json")
    monkeypatch.setattr(sources, "_chains", {})
    monkeypatch.setattr(sources, "_px", {})
    monkeypatch.setattr(sources, "_dates", {})
    monkeypatch.setattr(sources, "get", lambda url: pytest.fail(f"unexpected request: {url}"))


def vendor(monkeypatch, payload, log=None):
    def get(url):
        if log is not None:
            log.append(url)
        return payload

    monkeypatch.setattr(sources, "get", get)


CHAIN = {"close": 10.0, "current_price": 10.0, "iv30": 50.0, "options": []}


# ------------------------------------------------------------------------------- chains
def test_a_chain_is_fetched_once_within_the_ttl(monkeypatch):
    log = []
    vendor(monkeypatch, {"data": CHAIN}, log)
    assert sources.chain("nok") == CHAIN
    assert sources.chain("NOK") == CHAIN  # case-folded onto the same entry
    assert len(log) == 1  # every filter knob re-reads this; none of them may re-download


def test_forget_chains_is_what_the_r_key_costs(monkeypatch):
    log = []
    vendor(monkeypatch, {"data": CHAIN}, log)
    sources.chain("NOK")
    sources.forget_chains()
    sources.chain("NOK")
    assert len(log) == 2


def test_an_expired_chain_is_refetched(monkeypatch):
    log = []
    vendor(monkeypatch, {"data": CHAIN}, log)
    sources.chain("NOK")
    stamp, data = sources._chains["NOK"]
    sources._chains["NOK"] = (stamp - sources.CHAIN_TTL - 1, data)
    sources.chain("NOK")
    assert len(log) == 2


# ------------------------------------------------------------------------------- price history
BARS = {"data": [{"date": f"2026-09-0{i}", "close": float(i)} for i in range(1, 6)]}


def test_history_drops_pre_listing_zero_bars(monkeypatch):
    payload = {"data": [{"date": "2026-08-31", "close": 0.0}] + BARS["data"]}
    vendor(monkeypatch, payload)
    px, ds = sources.history("NOK")
    assert px == [1.0, 2.0, 3.0, 4.0, 5.0] and ds[0] == "2026-09-01"


def test_history_hits_the_disk_cache_on_a_second_process(monkeypatch):
    log = []
    vendor(monkeypatch, BARS, log)
    sources.history("NOK")
    monkeypatch.setattr(sources, "_px", {})  # new process: memory gone, disk cache still there
    monkeypatch.setattr(sources, "_dates", {})
    sources.history("NOK")
    assert len(log) == 1


def test_a_stale_daily_cache_is_refetched(monkeypatch):
    log = []
    vendor(monkeypatch, BARS, log)
    cache.store(sources.HIST, "NOK", {"px": [1.0], "d": ["2020-01-01"], "at": "2020-01-01"})
    sources.history("NOK")
    assert len(log) == 1  # yesterday's closes are not today's


def test_known_closes_never_fetches(monkeypatch):
    assert sources.known_closes("NOK") is None  # the autouse fixture fails the test on a request
    vendor(monkeypatch, BARS)
    sources.history("NOK")
    assert sources.known_closes("nok") == [1.0, 2.0, 3.0, 4.0, 5.0]


def test_close_on_settles_only_once_the_date_is_in_the_past(monkeypatch):
    vendor(monkeypatch, BARS)
    assert sources.close_on("NOK", "2026-09-03") == 3.0
    assert sources.close_on("NOK", "2026-09-04") == 4.0
    assert sources.close_on("NOK", "2026-09-06") is None  # expiry still ahead: not a result
    assert sources.close_on("NOK", "2026-08-01") is None  # before the series starts


# ------------------------------------------------------------------------------- earnings
def report(txt):
    return {"data": {"reportText": txt}}


def test_an_earnings_date_is_parsed_and_cached(monkeypatch):
    log = []
    vendor(monkeypatch, report("Expected to report on 10/27/2026"), log)
    assert sources.earnings_date("SOFI") == dt.date(2026, 10, 27)
    assert sources.earnings_date("SOFI") == dt.date(2026, 10, 27)
    assert len(log) == 1


def test_a_vendor_failure_is_cached_so_a_scan_asks_once(monkeypatch):
    """The bug this pins: an un-cached failure cost one rate-gate slot per symbol per scan."""
    log = []

    def boom(url):
        log.append(url)
        raise RuntimeError("nasdaq is down")

    monkeypatch.setattr(sources, "get", boom)
    assert sources.earnings_date("SOFI") is None
    assert sources.earnings_date("SOFI") is None
    assert len(log) == 1
    assert cache.cached(sources.EARNINGS)["SOFI"]["err"] is True


def test_a_cached_failure_expires_sooner_than_a_real_answer(monkeypatch):
    """An outage must not be remembered for the full week a real report date is."""
    stale = (dt.date.today() - dt.timedelta(days=2)).isoformat()
    cache.store(sources.EARNINGS, "SOFI", {"d": None, "at": stale, "err": True})
    vendor(monkeypatch, report("Expected to report on 10/27/2026"))
    assert sources.earnings_date("SOFI") == dt.date(2026, 10, 27)

    cache.store(sources.EARNINGS, "NOK", {"d": None, "at": stale})  # a real "no date", still fresh
    assert sources.earnings_date("NOK") is None


def test_unparseable_report_text_is_a_none_not_a_crash(monkeypatch):
    vendor(monkeypatch, report("Earnings date to be announced"))
    assert sources.earnings_date("SOFI") is None


# ------------------------------------------------------------------------------- screener
SCREENER = {
    "data": {
        "rows": [
            {"symbol": "SOFI", "lastsale": "$18.22", "volume": "40000000", "marketCap": "2.2e10"},
            {"symbol": "WARRANT", "lastsale": "NA", "volume": "", "marketCap": ""},
            {"symbol": "BRK.A", "lastsale": "$1,200,000.00", "volume": "10", "marketCap": "9e11"},
        ]
    }
}


def test_the_screener_skips_rows_that_do_not_quote(monkeypatch):
    vendor(monkeypatch, SCREENER)
    rows = sources.us_stocks()
    assert [r[0] for r in rows] == ["SOFI", "BRK.A"]
    assert rows[0][1:] == [18.22, 40_000_000, 2.2e10]
    assert rows[1][1] == 1_200_000.0  # thousands separators must not become 1.2


def test_the_screener_is_one_request_a_day(monkeypatch):
    log = []
    vendor(monkeypatch, SCREENER, log)
    sources.us_stocks()
    sources.us_stocks()
    assert len(log) == 1
    assert json.loads(sources.UNIV.read_text())["at"] == dt.date.today().isoformat()


# ------------------------------------------------------------------------------- session date
def test_the_session_is_read_from_the_last_trade_not_the_envelope():
    """The envelope's timestamp says today even on a holiday; the last trade tells the truth."""
    assert sources.session_date({"last_trade_time": "2026-09-04T15:59:59"}) == dt.date(2026, 9, 4)
    assert sources.session_date({"last_trade_time": ""}) is None
    assert sources.session_date({"last_trade_time": None}) is None
    assert sources.session_date({}) is None
    assert sources.session_date({"last_trade_time": "n/a"}) is None
