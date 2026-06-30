"""Offline end-to-end test of the core /run path over HTTP.

The core (in a background thread) builds a digest from a fake adapter with the
engine's network calls stubbed, "delivers" via the null sink, marks items sent,
and returns a status report — proving the single delivery path: trigger -> core
delivers to the configured front-end -> caller gets counts only.

Run:  PYTHONPATH=. python tests/test_service_run.py
"""

import os
import sys
import time
import threading
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from digestcore.config import Config
from digestcore.db import open_db
from digestcore.engine import DigestEngine
from digestcore.models import Candidate, SourceAdapter
from digestcore import adapters as adapters_pkg
from digestcore.service.auth import ClientRegistry
from digestcore.service.server import build_server
from digestcore.client import DigestClient, register


class FakeAdapter(SourceAdapter):
    signal_weights = {}
    def fetch_candidates(self, topic, window_days, context=None):
        return [Candidate(id=f"x{i}", title=f"Item {i}", url=f"https://e.com/{i}",
                          summary="s") for i in range(3)]


def main():
    tmp = tempfile.mkdtemp(prefix="digest-run-")
    # DELIVERY_SINK=none -> null sink, so no network and no OWUI needed.
    cfg = Config(DB_PATH=os.path.join(tmp, "digest.db"), DATA_DIR=tmp,
                 MEM0_BASE_URL="", EMBED_MODEL="", DELIVERY_SINK="none")

    # make the in-process engine offline and give it a fake source
    adapters_pkg.ADAPTERS["fake"] = FakeAdapter()
    DigestEngine._embed = lambda self, text: None
    DigestEngine._judge = lambda self, profile, shortlist, n, audience="": []

    httpd = build_server(cfg, host="127.0.0.1", port=0)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.2)

    register(base, "cli", "tok-1", "write")
    ClientRegistry(open_db(cfg.DB_PATH)).approve("cli")
    c = DigestClient(base, "tok-1", user_id="u-1")

    assert c.register_account(tz="UTC")["ok"]
    assert c.add_subscription("Daily", adapter="fake", topic_query="x", count=5)["ok"]

    rep = c.run("Daily")
    assert rep["ok"], rep
    assert rep["runs"] and rep["runs"][0]["count"] == 3, rep
    # delivered items were marked sent, so a second run delivers nothing
    rep2 = c.run("Daily")
    assert rep2["runs"][0]["count"] == 0, rep2

    db = open_db(cfg.DB_PATH)
    n = db.execute("SELECT COUNT(*) FROM sent WHERE uuid='u-1' AND sub_name='Daily'").fetchone()[0]
    assert n == 3, f"expected 3 sent rows, got {n}"

    httpd.shutdown()
    print("PASS — /run over HTTP: core built, delivered to the configured sink, marked "
          "sent, and reported counts (3 then 0). Caller got status only, never items.")


if __name__ == "__main__":
    main()
