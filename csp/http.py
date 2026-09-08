"""One JSON GET, rate-limited.

CBOE sits behind Cloudflare burst protection and answers roughly 0.4 requests/second from a
single IP before it starts returning 429 — measured, not guessed. So every request start passes
through one global gate; downloads themselves overlap across worker threads.
"""

import gzip
import json
import threading
import time
import urllib.error
import urllib.request

UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "Chrome/124 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Encoding": "gzip",
}

MIN_INTERVAL = 0.35
_last = [0.0]
_gate = threading.Lock()


def get(url, tries=6):
    for i in range(tries):
        with _gate:
            time.sleep(max(0, _last[0] + MIN_INTERVAL - time.monotonic()))
            _last[0] = time.monotonic()
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30)
            body = r.read()
            gzipped = r.headers.get("Content-Encoding") == "gzip"
            return json.loads(gzip.decompress(body) if gzipped else body)
        except urllib.error.HTTPError as e:
            if e.code != 429 or i == tries - 1:
                raise
            time.sleep(float(e.headers.get("Retry-After", 1)) + 2**i)
