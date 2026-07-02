"""A transport failure must be reported as a passive retry, distinct from both a
hard error and an empty result — and a song that simply has no link must NOT be
mistaken for the network being down.

Covers three layers:
  1. is_transport_error: connectivity failures (incl. library-wrapped) are caught;
     ordinary "nothing found" errors are not.
  2. MusicCatalogAdapter: a no-hit artist is skipped (backfilled), a real hit is
     linked, and a transport failure raises AdapterRetryable rather than emitting
     a bare-artist link.
  3. DigestRunner over HTTP: AdapterRetryable -> run report says "will retry",
     nothing is delivered, and nothing is marked sent.

Run:  PYTHONPATH=. python tests/test_adapter_retry.py
"""
import os
import sys
import time
import socket
import threading
import tempfile
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from digestcore.net import AdapterRetryable, is_transport_error
from digestcore.adapters.music_catalog import MusicCatalogAdapter
from digestcore.adapters.catalog_store import init_schema, norm_name


# ---------------------------------------------------------------- 1. classifier
def test_classifier():
    # unambiguous transport failures
    assert is_transport_error(requests.exceptions.ConnectionError(
        "HTTPSConnectionPool(host='music.youtube.com'): Max retries exceeded "
        "(Caused by NameResolutionError)"))
    assert is_transport_error(requests.exceptions.ReadTimeout("read timed out"))
    assert is_transport_error(socket.gaierror(-2, "Name or service not known"))

    # a library that wraps a transport error in its own type is still caught (chain walk)
    try:
        try:
            raise requests.exceptions.ConnectionError("Failed to resolve 'music.youtube.com'")
        except Exception as inner:
            raise RuntimeError("ytmusicapi: search failed") from inner
    except RuntimeError as wrapped:
        assert is_transport_error(wrapped), "should walk __cause__ to find the transport error"

    # NOT transport: a plain application error / empty-result signal
    assert not is_transport_error(ValueError("no songs found for artist"))
    assert not is_transport_error(KeyError("videoId"))
    print("  [1] classifier: transport failures caught (incl. wrapped); no-hit errors are not")


# ------------------------------------------------- 2. music three-way behaviour
def _mini_catalog(tmp):
    """Five jazz artists sharing a genre so a 'jazz' seed ranks all of them."""
    path = os.path.join(tmp, "cat.sqlite")
    conn = sqlite3.connect(path)
    init_schema(conn)
    for i, name in enumerate(["Alpha", "Bravo", "Charlie", "Delta", "Echo"], start=1):
        conn.execute("INSERT INTO artist(id,mbid,name,name_norm) VALUES(?,?,?,?)",
                     (i, f"m{i}", name, norm_name(name)))
        conn.execute("INSERT INTO feature(artist_id,axis,value) VALUES(?, 'genre','jazz')", (i,))
    conn.commit()
    conn.close()
    return path


def test_music_three_way(tmp):
    path = _mini_catalog(tmp)
    os.environ["DIGEST_MUSIC_CATALOG_PATH"] = path

    # (a) transport failure -> propagates as AdapterRetryable (not swallowed, no junk)
    def unreachable(_artist):
        raise AdapterRetryable("music resolver unreachable (ConnectionError)")
    try:
        MusicCatalogAdapter(linker=unreachable).fetch_candidates("jazz", 7, context={})
        assert False, "a transport failure must raise, not return degraded items"
    except AdapterRetryable:
        pass

    # (b) one artist has no song (None) -> skipped and backfilled; the rest are real links
    def some_missing(artist):
        if norm_name(artist) == norm_name("Charlie"):
            return None
        return ("A Track", f"https://music.youtube.com/watch?v={norm_name(artist)}")
    cands = MusicCatalogAdapter(linker=some_missing).fetch_candidates("jazz", 7, context={})
    assert cands, "reachable artists with songs should still be delivered"
    assert not any("Charlie" in c.title for c in cands), "no-hit artist must be skipped"
    assert all(c.url.startswith("https://music.youtube.com/watch?v=") for c in cands), \
        "every delivered pick is a real watch link — never a bare-artist search URL"
    assert all(" — " in c.title for c in cands), "every delivered pick carries a track name"

    # (c) nobody resolves (all None) -> empty digest, NOT a retry and NOT junk
    empty = MusicCatalogAdapter(linker=lambda _a: None).fetch_candidates("jazz", 7, context={})
    assert empty == [], "all-no-hit should yield nothing rather than search links"
    print("  [2] music: transport raises; no-hit skips+backfills; all-no-hit is empty (no junk)")


# --------------------------------------------------- 3. runner reports the retry
def test_runner_reports_retry(tmp):
    from digestcore.config import Config
    from digestcore.db import open_db
    from digestcore.engine import DigestEngine
    from digestcore.models import SourceAdapter
    from digestcore import adapters as adapters_pkg
    from digestcore.service.auth import ClientRegistry
    from digestcore.service.server import build_server
    from digestcore.client import DigestClient, register

    class FlakyAdapter(SourceAdapter):
        signal_weights = {}
        def fetch_candidates(self, topic, window_days, context=None):
            raise AdapterRetryable("music resolver unreachable (ConnectionError)")

    cfg = Config(DB_PATH=os.path.join(tmp, "flaky.db"), DATA_DIR=tmp,
                 MEM0_BASE_URL="", EMBED_MODEL="", DELIVERY_SINK="none")
    adapters_pkg.ADAPTERS["flaky"] = FlakyAdapter()
    DigestEngine._embed = lambda self, text: None
    DigestEngine._judge = lambda self, profile, shortlist, n, audience="": []

    httpd = build_server(cfg, host="127.0.0.1", port=0)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.2)

    register(base, "cli", "tok-r", "write")
    ClientRegistry(open_db(cfg.DB_PATH)).approve("cli")
    c = DigestClient(base, "tok-r", user_id="u-r")
    assert c.register_account(tz="UTC")["ok"]
    assert c.add_subscription("Flaky", adapter="flaky", topic_query="x", count=5)["ok"]

    rep = c.run("Flaky")
    assert rep["ok"], rep
    run0 = rep["runs"][0]
    assert run0["retry"], f"expected a retry note, got {run0}"
    assert run0["count"] == 0 and not run0["error"], run0
    assert "will retry" in rep["message"], rep["message"]

    # the crux: a transient failure must not burn anything as sent
    n = open_db(cfg.DB_PATH).execute(
        "SELECT COUNT(*) FROM sent WHERE uuid='u-r' AND sub_name='Flaky'").fetchone()[0]
    assert n == 0, f"a retryable run must mark nothing sent, found {n}"

    httpd.shutdown()
    print("  [3] runner: retryable -> report says 'will retry', 0 delivered, 0 marked sent")


def main():
    tmp = tempfile.mkdtemp(prefix="digest-retry-")
    test_classifier()
    test_music_three_way(tmp)
    test_runner_reports_retry(tmp)
    print("PASS — transport failures are reported as passive retries that burn nothing; "
          "a song with no link is skipped and backfilled, never surfaced as a network error.")


if __name__ == "__main__":
    main()
