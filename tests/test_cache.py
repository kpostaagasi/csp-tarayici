"""The disk cache has one hard promise: a corrupt file is an empty cache, never a traceback."""

import json
import threading

from csp import cache


def test_a_missing_file_reads_as_empty(tmp_path):
    assert cache.cached(tmp_path / "nope.json") == {}


def test_a_half_written_file_reads_as_empty_not_a_crash(tmp_path):
    """A scan killed mid-write leaves this; the next run must scan, not die."""
    p = tmp_path / "hist.json"
    p.write_text('{"NOK": {"px": [1.0, 2.0')
    assert cache.cached(p) == {}


def test_store_merges_instead_of_replacing(tmp_path):
    p = tmp_path / "hist.json"
    cache.store(p, "NOK", {"px": [1.0]})
    cache.store(p, "SOFI", {"px": [2.0]})
    assert cache.cached(p) == {"NOK": {"px": [1.0]}, "SOFI": {"px": [2.0]}}


def test_store_repairs_a_corrupt_file_rather_than_inheriting_it(tmp_path):
    p = tmp_path / "hist.json"
    p.write_text("{not json")
    cache.store(p, "NOK", {"px": [1.0]})
    assert cache.cached(p) == {"NOK": {"px": [1.0]}}


def test_parallel_writers_do_not_lose_each_other(tmp_path):
    """Eight scan threads write this file. Re-reading under the lock is the whole point."""
    p = tmp_path / "hist.json"
    threads = [threading.Thread(target=cache.store, args=(p, f"S{i}", i)) for i in range(24)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert cache.cached(p) == {f"S{i}": i for i in range(24)}
    assert len(json.loads(p.read_text())) == 24
