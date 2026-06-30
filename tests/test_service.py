"""End-to-end test of the core service over real HTTP.

Starts the actual ThreadingHTTPServer on an ephemeral port against a temp DB, then
exercises the full handshake and data path through the real DigestClient:
  - register a token -> pending -> calls are refused (403)
  - approve it (the local-admin action) -> calls succeed
  - read-only token can read but not write
  - data written via the API is visible directly in the DB (parity)

Run:  PYTHONPATH=. python tests/test_service.py
"""

import os
import sys
import time
import threading
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from digestcore.config import Config
from digestcore.db import open_db
from digestcore.service.auth import ClientRegistry
from digestcore.service.server import build_server
from digestcore.client import DigestClient, register, CoreError


def main():
    tmp = tempfile.mkdtemp(prefix="digest-svc-")
    cfg = Config(DB_PATH=os.path.join(tmp, "digest.db"), DATA_DIR=tmp, MEM0_BASE_URL="")
    httpd = build_server(cfg, host="127.0.0.1", port=0)   # port 0 -> ephemeral
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.2)

    assert DigestClient(base, "").healthz()["status"] == "ok", "healthz failed"

    # 1) register a write token -> pending; calls refused until approved
    r = register(base, "owui", "tok-write-123", "write")
    assert r["status"] == "pending", r
    writer = DigestClient(base, "tok-write-123", user_id="u-1")
    try:
        writer.options()
        raise AssertionError("pending token should be refused")
    except CoreError as e:
        assert e.status == 403, e

    # 2) approve (local admin action, direct DB) -> calls now work
    reg = ClientRegistry(open_db(cfg.DB_PATH))
    assert reg.approve("owui"), "approve failed"

    assert "Available digest types" in writer.options(), "options failed after approval"
    acct = writer.register_account(owui_token="sk-x", ntfy_topic="topic-1", tz="America/Chicago")
    assert acct["ok"], acct
    add = writer.add_subscription("AI papers", adapter="arxiv_hf", topic_query="cat:cs.AI",
                                  count=5, window_days=7, hour=8, day_of_week="mon")
    assert add["ok"], add
    lst = writer.list_subscriptions()
    assert lst["ok"] and any(s["name"] == "AI papers" for s in lst["subscriptions"]), lst
    fb = writer.record_feedback("music", "track42", "up", title="Some Song")
    assert fb["ok"], fb

    # 3) parity: the write landed in the DB the core owns
    db = open_db(cfg.DB_PATH)
    row = db.execute("SELECT signal,title FROM feedback WHERE uuid='u-1' AND item_id='track42'").fetchone()
    assert row and row["signal"] == "up", "feedback not persisted"
    subrow = db.execute("SELECT cron FROM subscriptions WHERE uuid='u-1' AND name='AI papers'").fetchone()
    assert subrow and subrow["cron"] == "0 8 * * mon", "subscription not persisted"

    # 4) scope enforcement: a read-only token can read, not write
    register(base, "viewer", "tok-read-9", "read")
    reg.approve("viewer")
    reader = DigestClient(base, "tok-read-9", user_id="u-1")
    assert reader.list_subscriptions()["ok"], "read token should read"
    try:
        reader.record_feedback("music", "track99", "down")
        raise AssertionError("read token should not write")
    except CoreError as e:
        assert e.status == 403, e

    # 5) unknown token -> 401
    try:
        DigestClient(base, "nope").list_subscriptions()
        raise AssertionError("unknown token should 401")
    except CoreError as e:
        assert e.status == 401, e

    httpd.shutdown()
    print("PASS — core service: handshake, scope enforcement, and DB parity all hold.")
    print(f"  approved write+read tokens, refused pending/read-write/unknown correctly.")
    print(f"  data written over HTTP is present in {os.path.basename(cfg.DB_PATH)}.")


if __name__ == "__main__":
    main()
