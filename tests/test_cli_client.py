"""End-to-end test of the CLI as a pure core client.

Runs the real core in a thread, points the CLI's config/token at a temp dir, and
drives the CLI through main(): it bootstraps + registers its token, is refused until
approved, then performs reads, writes, and a run — all over HTTP, no DB access in the
CLI at all.

Run:  PYTHONPATH=. python tests/test_cli_client.py
"""

import os
import sys
import io
import time
import threading
import tempfile
import contextlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# isolate ~/.config/digest to a temp dir BEFORE the CLI reads it
_TMP = tempfile.mkdtemp(prefix="digest-cli-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP, "config")

from digestcore.config import Config
from digestcore.db import open_db
from digestcore.engine import DigestEngine
from digestcore.models import Candidate, SourceAdapter
from digestcore import adapters as adapters_pkg
from digestcore.service.auth import ClientRegistry
from digestcore.service.server import build_server
from interfaces.cli.__main__ import main as cli


class FakeAdapter(SourceAdapter):
    signal_weights = {}
    def fetch_candidates(self, topic, window_days, context=None):
        return [Candidate(id=f"x{i}", title=f"Item {i}", url=f"https://e.com/{i}", summary="s")
                for i in range(3)]


def run_cli(*argv):
    """Invoke the CLI, capturing stdout and any SystemExit message."""
    out = io.StringIO()
    code = 0
    try:
        with contextlib.redirect_stdout(out):
            cli(list(argv))
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
        if isinstance(e.code, str):
            out.write(e.code)
    return code, out.getvalue()


def main():
    cfg = Config(DB_PATH=os.path.join(_TMP, "digest.db"), DATA_DIR=_TMP,
                 MEM0_BASE_URL="", EMBED_MODEL="", DELIVERY_SINK="none")
    adapters_pkg.ADAPTERS["fake"] = FakeAdapter()
    DigestEngine._embed = lambda self, text: None
    DigestEngine._judge = lambda self, profile, shortlist, n, audience="": []

    httpd = build_server(cfg, host="127.0.0.1", port=0)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.2)

    # configure the core URL + user; this also registers the CLI token (pending)
    code, out = run_cli("core", "set", "--url", base, "--user", "u-1")
    assert code == 0 and "approve cli" in out, out

    # before approval, any data command is refused with the friendly message
    code, out = run_cli("options")
    assert code != 0 and "approve cli" in out, f"expected approval prompt, got: {out!r}"

    # approve (the core-side admin action)
    ClientRegistry(open_db(cfg.DB_PATH)).approve("cli")

    code, out = run_cli("core", "status")
    assert code == 0 and "approved" in out, out

    code, out = run_cli("options")
    assert code == 0 and "Available digest types" in out, out

    code, out = run_cli("register", "--tz", "UTC")
    assert code == 0 and "Registered" in out, out

    code, out = run_cli("sub", "add", "Daily", "--adapter", "fake", "--query", "x", "--count", "5")
    assert code == 0 and "saved" in out, out

    code, out = run_cli("sub", "list")
    assert code == 0 and "Daily" in out, out

    code, out = run_cli("run", "Daily")
    assert code == 0 and "delivered 3" in out, out

    code, out = run_cli("feedback", "fake__x0", "up")
    assert code == 0 and "Noted" in out, out

    httpd.shutdown()
    print("PASS — CLI as a pure core client: token bootstrap, approval gate, reads, "
          "writes, and run all worked over HTTP with no DB access in the CLI.")


if __name__ == "__main__":
    main()
