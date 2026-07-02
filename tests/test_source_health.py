"""Per-adapter source health: report what failed, retry only when the failure
means the output can't be trusted — and never let one adapter's health leak onto
a run that didn't involve it.

  1. News tolerates individual dead feeds (clusters across outlets) but treats a
     *total transport* failure as a retry, not a misleading "nothing new".
  2. arXiv (primary) transport failure retries; Hugging Face (secondary) failures
     are reported and degrade to arXiv-only, never aborting the run.
  3. Music reports benign no-hit skips as a diagnostic.
  4. The runner attaches each adapter's diagnostic to *its* sub only — a music-only
     run never surfaces stale news health (the old global-readout wart).

Run:  PYTHONPATH=. python tests/test_source_health.py
"""
import os
import sys
import time
import threading
import tempfile
import sqlite3
import datetime as dt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from digestcore.net import AdapterRetryable


# ------------------------------------------------- 1. news total-vs-partial
def test_news_policy():
    from digestcore.adapters.rss_news import RssNewsAdapter

    def live_item(domain):
        return {"published": None, "url": f"http://{domain}/a", "title": "Something happened",
                "summary": "details", "domain": domain}

    # (a) every feed fails at the transport level -> retry, not silent empty
    ad = RssNewsAdapter()
    ad._fetch_feed = lambda d, u: (d, [], "Max retries exceeded (NameResolutionError)", True)
    try:
        ad._build_pool(2)
        assert False, "total transport failure must raise AdapterRetryable"
    except AdapterRetryable as e:
        assert "unreachable" in str(e)

    # (b) some feeds live, some errored -> no raise; errors reported in diagnostic
    ad = RssNewsAdapter()
    def mixed(d, u):
        if d in ("cnn.com", "apnews.com"):
            return (d, [live_item(d)], None, False)
        return (d, [], "boom", True)
    ad._fetch_feed = mixed
    ad._build_pool(2)  # must not raise
    diag = ad.diagnostic()
    assert "live" in diag and "errored" in diag, diag

    # (c) total failure but NON-transport (e.g. 404s) -> NOT a retry; just nothing new
    ad = RssNewsAdapter()
    ad._fetch_feed = lambda d, u: (d, [], "404 Not Found", False)
    clusters = ad._build_pool(2)  # must not raise
    assert clusters == [], "non-transport total failure is empty, not a retry"

    print("  [1] news: total transport -> retry; partial -> deliver+report; 404s -> nothing new")


# --------------------------------------- 2. arxiv primary vs secondary
def test_arxiv_split():
    from digestcore.adapters import arxiv_hf
    from digestcore.adapters.arxiv_hf import ArxivHFAdapter

    orig_get = arxiv_hf.requests.get

    class FakeResp:
        def __init__(self, text="", data=None):
            self.text, self._data = text, data
        def raise_for_status(self):  # 2xx
            return None
        def json(self):
            return self._data

    pub = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    atom = (f'<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
            f'<id>http://arxiv.org/abs/2607.00001v1</id><title>A Test Paper</title>'
            f'<summary>Summary.</summary><published>{pub}</published></entry></feed>')

    try:
        # (a) arXiv itself unreachable -> retry
        def arxiv_down(url, **kw):
            raise requests.exceptions.ConnectionError("NameResolutionError: export.arxiv.org")
        arxiv_hf.requests.get = arxiv_down
        try:
            ArxivHFAdapter().fetch_candidates("cat:cs.LG", 2)
            assert False, "arXiv transport failure must raise AdapterRetryable"
        except AdapterRetryable:
            pass

        # (b) arXiv fine, HF down -> deliver on arXiv recall + report HF degradation
        def hf_down(url, params=None, **kw):
            if url == ArxivHFAdapter.ARXIV:
                return FakeResp(text=atom)
            raise requests.exceptions.ConnectionError("HF unreachable")
        arxiv_hf.requests.get = hf_down
        ad = ArxivHFAdapter()
        cands = ad.fetch_candidates("cat:cs.LG", 2)
        assert len(cands) >= 1, "arXiv recall should still deliver when HF is down"
        assert "Hugging Face" in ad.diagnostic(), ad.diagnostic()
    finally:
        arxiv_hf.requests.get = orig_get

    print("  [2] arxiv: arXiv transport -> retry; HF down -> deliver on arXiv + report HF")


# ------------------------------------------------- 3. music diagnostic
def test_music_diagnostic(tmp):
    from digestcore.adapters.music_catalog import MusicCatalogAdapter
    from digestcore.adapters.catalog_store import init_schema, norm_name

    path = os.path.join(tmp, "cat.sqlite")
    conn = sqlite3.connect(path); init_schema(conn)
    for i, name in enumerate(["Alpha", "Bravo", "Charlie", "Delta"], start=1):
        conn.execute("INSERT INTO artist(id,mbid,name,name_norm) VALUES(?,?,?,?)",
                     (i, f"m{i}", name, norm_name(name)))
        conn.execute("INSERT INTO feature(artist_id,axis,value) VALUES(?, 'genre','jazz')", (i,))
    conn.commit(); conn.close()
    os.environ["DIGEST_MUSIC_CATALOG_PATH"] = path

    def two_missing(artist):
        if norm_name(artist) in (norm_name("Bravo"), norm_name("Delta")):
            return None
        return ("Track", f"https://music.youtube.com/watch?v={norm_name(artist)}")
    ad = MusicCatalogAdapter(linker=two_missing)
    cands = ad.fetch_candidates("jazz", 7, context={})
    assert cands, "resolvable artists still delivered"
    assert ad.diagnostic() == "skipped 2 artist(s) with no track found", ad.diagnostic()
    print("  [3] music: benign no-hit skips are reported as a diagnostic")


# ----------------------------------- 4. runner scopes diagnostics per-sub
def test_runner_scopes_notes(tmp):
    from digestcore.config import Config
    from digestcore.db import open_db
    from digestcore.engine import DigestEngine
    from digestcore.models import SourceAdapter, Candidate
    from digestcore import adapters as adapters_pkg
    from digestcore.service.auth import ClientRegistry
    from digestcore.service.server import build_server
    from digestcore.client import DigestClient, register

    class NoteAdapter(SourceAdapter):
        signal_weights = {}
        def fetch_candidates(self, topic, window_days, context=None):
            return [Candidate(id="n1", title="t", url="http://u/1", summary="s", signals={})]
        def diagnostic(self):
            return "note-A"

    class PlainAdapter(SourceAdapter):
        signal_weights = {}
        def fetch_candidates(self, topic, window_days, context=None):
            return [Candidate(id="p1", title="t2", url="http://u/2", summary="s2", signals={})]

    cfg = Config(DB_PATH=os.path.join(tmp, "notes.db"), DATA_DIR=tmp,
                 MEM0_BASE_URL="", EMBED_MODEL="", DELIVERY_SINK="none")
    adapters_pkg.ADAPTERS["notea"] = NoteAdapter()
    adapters_pkg.ADAPTERS["plain"] = PlainAdapter()
    # simulate an earlier failed news run leaving stale health on the singleton
    if hasattr(adapters_pkg.ADAPTERS.get("news"), "last_health"):
        adapters_pkg.ADAPTERS["news"].last_health = {"cnn.com": "ERR: STALE_NEWS_HEALTH"}
    DigestEngine._embed = lambda self, text: None
    DigestEngine._judge = lambda self, profile, shortlist, n, audience="": []

    httpd = build_server(cfg, host="127.0.0.1", port=0)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.2)

    register(base, "cli", "tok-n", "write")
    ClientRegistry(open_db(cfg.DB_PATH)).approve("cli")
    c = DigestClient(base, "tok-n", user_id="u-n")
    assert c.register_account(tz="UTC")["ok"]
    assert c.add_subscription("HasNote", adapter="notea", topic_query="x", count=3)["ok"]
    assert c.add_subscription("NoNote", adapter="plain", topic_query="y", count=3)["ok"]

    rep = c.run("")  # run all
    notes = {r["name"]: r.get("note", "") for r in rep["runs"]}
    assert notes.get("HasNote") == "note-A", notes
    assert notes.get("NoNote") == "", notes
    # the crux: news didn't run, so its stale health must not appear anywhere
    assert "STALE_NEWS_HEALTH" not in rep["message"], rep["message"]
    assert "STALE_NEWS_HEALTH" not in rep["health"], rep["health"]
    assert rep["health"] == "HasNote: note-A", rep["health"]

    httpd.shutdown()
    print("  [4] runner: notes scoped per-sub; a run that skips news never shows news health")


def main():
    tmp = tempfile.mkdtemp(prefix="digest-health-")
    test_news_policy()
    test_arxiv_split()
    test_music_diagnostic(tmp)
    test_runner_scopes_notes(tmp)
    print("PASS — every adapter reports what failed to fetch; only untrustworthy-output "
          "failures retry; and source health is scoped to the run that produced it.")


if __name__ == "__main__":
    main()
