"""The rate gate and the retry ladder: the two things that decide whether a scan finishes.

Nothing here touches the network — urlopen is replaced, and the gate's own clock is the only
real thing being measured.
"""

import gzip
import itertools
import json
import threading
import time
import urllib.error

import pytest

from csp import http


class FakeResponse:
    def __init__(self, payload, gzipped=False):
        body = json.dumps(payload).encode()
        self.body = gzip.compress(body) if gzipped else body
        self.headers = {"Content-Encoding": "gzip"} if gzipped else {}

    def read(self):
        return self.body


@pytest.fixture(autouse=True)
def fast_gate(monkeypatch):
    """A gate the test can actually wait out, reset between tests."""
    monkeypatch.setattr(http, "MIN_INTERVAL", 0.02)
    monkeypatch.setattr(http, "_last", [0.0])


@pytest.mark.parametrize("gzipped", [False, True])
def test_plain_and_gzipped_bodies_both_decode(monkeypatch, gzipped):
    monkeypatch.setattr(http.urllib.request, "urlopen", lambda *a, **k: FakeResponse({"a": 1}, gzipped))
    assert http.get("https://example.invalid/x") == {"a": 1}


def test_the_gate_spaces_request_starts(monkeypatch):
    starts = []
    monkeypatch.setattr(
        http.urllib.request,
        "urlopen",
        lambda *a, **k: (starts.append(time.monotonic()), FakeResponse({}))[1],
    )
    for _ in range(4):
        http.get("https://example.invalid/x")
    gaps = [b - a for a, b in itertools.pairwise(starts)]
    assert all(g >= http.MIN_INTERVAL * 0.9 for g in gaps), gaps


def test_the_gate_is_global_across_threads(monkeypatch):
    """8 workers share one gate: parallelism overlaps downloads, it must not overlap starts."""
    starts = []
    lock = threading.Lock()

    def urlopen(*a, **k):
        with lock:
            starts.append(time.monotonic())
        time.sleep(0.03)  # a download slower than the interval must not let the next one jump it
        return FakeResponse({})

    monkeypatch.setattr(http.urllib.request, "urlopen", urlopen)
    threads = [threading.Thread(target=http.get, args=("https://example.invalid/x",)) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    starts.sort()
    gaps = [b - a for a, b in itertools.pairwise(starts)]
    assert all(g >= http.MIN_INTERVAL * 0.9 for g in gaps), gaps


def error(code, retry_after=None):
    hdrs = {"Retry-After": str(retry_after)} if retry_after is not None else {}
    return urllib.error.HTTPError("https://example.invalid/x", code, "boom", hdrs, None)


def test_429_is_retried_and_then_succeeds(monkeypatch):
    calls = []
    monkeypatch.setattr(http.time, "sleep", lambda s: calls.append(s))

    attempts = []

    def urlopen(*a, **k):
        attempts.append(1)
        if len(attempts) < 3:
            raise error(429, retry_after=0)
        return FakeResponse({"ok": True})

    monkeypatch.setattr(http.urllib.request, "urlopen", urlopen)
    assert http.get("https://example.invalid/x") == {"ok": True}
    assert len(attempts) == 3
    assert [c for c in calls if c >= 1] == [1.0, 2.0]  # Retry-After 0 plus 2**i backoff


def test_a_non_429_is_raised_immediately(monkeypatch):
    attempts = []

    def urlopen(*a, **k):
        attempts.append(1)
        raise error(404)

    monkeypatch.setattr(http.urllib.request, "urlopen", urlopen)
    with pytest.raises(urllib.error.HTTPError):
        http.get("https://example.invalid/x")
    assert attempts == [1]  # a dead symbol must not cost six rate-limited slots


def test_a_wall_of_429s_eventually_raises(monkeypatch):
    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    monkeypatch.setattr(http.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(error(429, 0)))
    with pytest.raises(urllib.error.HTTPError):
        http.get("https://example.invalid/x", tries=3)
