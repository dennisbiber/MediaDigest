"""End-to-end test of the converted OWUI wrappers against a live core.

The OWUI wrappers import NO digestcore — they're standalone urllib clients. This
test loads them from file (as OWUI would load them), points their valves at an
in-process core, runs the Pipeline's on_startup bootstrap (which writes + registers
the shared 'owui' token), approves it, then drives the Tool and an Action through
the core. Proves the whole 3b path without a real OWUI.

Run:  PYTHONPATH=. python tests/test_owui_client.py
"""

import os
import sys
import time
import asyncio
import threading
import tempfile
import importlib.util

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from digestcore.config import Config
from digestcore.db import open_db
from digestcore.engine import DigestEngine
from digestcore.models import Candidate, SourceAdapter
from digestcore import adapters as adapters_pkg
from digestcore.service.auth import ClientRegistry
from digestcore.service.server import build_server

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, path))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class FakeAdapter(SourceAdapter):
    signal_weights = {}
    def fetch_candidates(self, topic, window_days, context=None):
        return [Candidate(id=f"x{i}", title=f"Item {i}", url=f"https://e.com/{i}", summary="s")
                for i in range(3)]


def main():
    tmp = tempfile.mkdtemp(prefix="digest-owui-")
    tokpath = os.path.join(tmp, "owui.token")
    cfg = Config(DB_PATH=os.path.join(tmp, "digest.db"), DATA_DIR=tmp,
                 MEM0_BASE_URL="", EMBED_MODEL="", DELIVERY_SINK="none")
    adapters_pkg.ADAPTERS["fake"] = FakeAdapter()
    DigestEngine._embed = lambda self, text: None
    DigestEngine._judge = lambda self, profile, shortlist, n, audience="": []

    httpd = build_server(cfg, host="127.0.0.1", port=0)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.2)

    pipe = load("interfaces/owui/digest_pipeline.py", "dp")
    tool = load("interfaces/owui/tools/digest_manager_tool.py", "dtool")
    act = load("interfaces/owui/functions/digest_more_like_this.py", "dup")

    # 1) Pipeline.on_startup bootstraps the shared token + registers 'owui' (pending)
    p = pipe.Pipeline()
    p.valves.CORE_URL = base
    p.valves.TOKEN_PATH = tokpath
    asyncio.run(p.on_startup())
    assert os.path.exists(tokpath), "pipeline did not write the shared token"

    # before approval, the Tool is refused with a friendly message
    t = tool.Tools()
    t.valves.CORE_URL = base
    t.valves.TOKEN_PATH = tokpath
    user = {"id": "u-1", "valves": {"OWUI_API_KEY": "sk-x", "NTFY_TOPIC": "topic-1", "TIMEZONE": "UTC"}}
    assert "approve owui" in t.register_account(__user__=user), "expected approval prompt"

    # 2) approve 'owui' (the core-side admin gate)
    ClientRegistry(open_db(cfg.DB_PATH)).approve("owui")

    # 3) Tool now works through the core
    assert "Registered" in t.register_account(__user__=user), "register failed after approval"
    assert "saved" in t.add_subscription("Daily", adapter="fake", topic_query="x", count=5,
                                         __user__=user), "add_subscription failed"
    assert "Daily" in t.list_subscriptions(__user__=user), "list failed"
    assert "Available digest types" in t.describe_options(__user__=user), "options failed"

    # 4) Pipeline 'run Daily' asks the core to deliver; status comes back
    msg = p.pipe("run Daily", "m", [], {})
    assert "delivered 3" in msg, f"run failed: {msg}"

    # 5) Action: thumbs-up on a marked digest message records feedback via the core
    a = act.Action()
    a.valves.CORE_URL = base
    a.valves.TOKEN_PATH = tokpath
    captured = {}
    async def emitter(ev):
        captured["content"] = ev["data"]["content"]
    marked = {"role": "assistant",
              "content": "**1. [Item 0](https://e.com/0#digest=fake__x0)**"}
    asyncio.run(a.action({"messages": [marked]}, __user__={"id": "u-1"}, __event_emitter__=emitter))
    assert "Noted" in captured.get("content", ""), f"action feedback failed: {captured}"

    # parity: feedback landed in the core's DB
    db = open_db(cfg.DB_PATH)
    row = db.execute("SELECT signal FROM feedback WHERE uuid='u-1' AND item_id='x0'").fetchone()
    assert row and row["signal"] == "up", "feedback not persisted via the core"

    httpd.shutdown()
    print("PASS — OWUI wrappers as core clients: pipeline bootstrap+approval, tool CRUD, "
          "run, and action feedback all worked over HTTP with no digestcore in the wrappers.")


if __name__ == "__main__":
    main()
