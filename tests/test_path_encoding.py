"""Lock the fix for subscription names with spaces/specials in URL paths.

A name like "Day's top news" must percent-encode on the client and decode on the
core, or urllib rejects the URL before it leaves the machine. This round-trips a
space-named sub through the real server over HTTP.

Run:  PYTHONPATH=. python tests/test_path_encoding.py
"""
import os, sys, threading, time, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from digestcore.config import Config
from digestcore.db import open_db
from digestcore.service.auth import ClientRegistry
from digestcore.service.server import build_server
from digestcore.client import DigestClient, register


def main():
    tmp = tempfile.mkdtemp(prefix="digest-enc-")
    cfg = Config(DB_PATH=os.path.join(tmp, "d.db"), DATA_DIR=tmp, MEM0_BASE_URL="", DELIVERY_SINK="none")
    h = build_server(cfg, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{h.server_address[1]}"
    threading.Thread(target=h.serve_forever, daemon=True).start(); time.sleep(0.2)
    register(base, "cli", "t", "write"); ClientRegistry(open_db(cfg.DB_PATH)).approve("cli")
    c = DigestClient(base, "t", user_id="u1"); c.register_account(tz="UTC")

    for name in ("Day's top news", "Music Digest", "Top AI papers"):
        assert c.add_subscription(name, adapter="rss_news", count=3)["ok"], f"add {name}"
        assert c.set_enabled(name, False)["ok"], f"toggle {name}"
        assert c.remove(name)["ok"], f"remove {name}"
    h.shutdown()
    print("PASS — space-containing subscription names add/toggle/remove over HTTP.")


if __name__ == "__main__":
    main()
