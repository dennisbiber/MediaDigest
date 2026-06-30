"""Test the operator admin path: the core writes an admin token into its data dir,
the admin endpoints gate on it, and the host CLI uses it to approve a client without
docker.

Run:  PYTHONPATH=. python tests/test_admin.py
"""

import os
import sys
import io
import time
import stat
import threading
import tempfile
import contextlib

_TMP = tempfile.mkdtemp(prefix="digest-admin-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP, "config")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from digestcore.config import Config
from digestcore.db import open_db
from digestcore.service.server import build_server, admin_token_path
from digestcore.client import (register, admin_pending, admin_approve, admin_revoke,
                               DigestClient, CoreError)
from interfaces.cli.__main__ import main as cli


def run_cli(*argv):
    out = io.StringIO(); code = 0
    try:
        with contextlib.redirect_stdout(out):
            cli(list(argv))
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
        if isinstance(e.code, str):
            out.write(e.code)
    return code, out.getvalue()


def main():
    data_dir = os.path.join(_TMP, "data")
    cfg = Config(DB_PATH=os.path.join(data_dir, "digest.db"), DATA_DIR=data_dir,
                 MEM0_BASE_URL="", DELIVERY_SINK="none")
    httpd = build_server(cfg, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start(); time.sleep(0.2)

    # admin token file was created in the data dir, world-readable
    apath = admin_token_path(cfg)
    assert os.path.exists(apath), "admin token not created"
    admin = open(apath).read().strip()
    assert admin, "admin token empty"
    assert stat.S_IMODE(os.stat(apath).st_mode) == 0o644, "admin token should be 0644"

    # a client registers (pending)
    register(base, "owui", "tok-owui", "write")

    # wrong admin token is rejected
    try:
        admin_approve(base, "wrong-token", "owui")
        raise AssertionError("wrong admin token should be rejected")
    except CoreError as e:
        assert e.status == 403, e

    # right admin token: pending lists it, approve works
    pend = admin_pending(base, admin)
    assert any(c["name"] == "owui" for c in pend["clients"]), pend
    assert admin_approve(base, admin, "owui")["ok"], "approve failed"
    # approved token now works for data ops
    assert DigestClient(base, "tok-owui", user_id="u1").register_account(tz="UTC")["ok"]
    # revoke works
    assert admin_revoke(base, admin, "owui")["ok"], "revoke failed"

    # ---- the no-docker CLI path ----
    code, out = run_cli("core", "set", "--url", base, "--user", "u1", "--data-dir", data_dir)
    assert code == 0, out
    # the CLI's own token is pending; approve it via the CLI (reads admin.token from data dir)
    code, out = run_cli("auth", "pending")
    assert code == 0 and "cli" in out, out
    code, out = run_cli("auth", "approve", "cli")
    assert code == 0 and "approved 'cli'" in out, out
    # now the CLI is approved and a normal command works
    code, out = run_cli("core", "status")
    assert code == 0 and "approved" in out, out

    httpd.shutdown()
    print("PASS — admin token gates the admin endpoints, and `digest auth approve` "
          "approves clients from the host with no docker.")


if __name__ == "__main__":
    main()
