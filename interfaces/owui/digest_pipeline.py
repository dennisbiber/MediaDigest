"""
title: Generic Digest Pipeline
author: Dennis Biber
version: 2.0.0
requirements:
"""

# OWUI Pipeline as a thin core client. Its job is now twofold and small:
#   1. on_startup: bootstrap the shared "owui" token — generate it once, persist it
#      0600 to a volume the Tool/Actions also read, and register it with the core
#      (idempotent). This is the no-manual-setup handshake; you approve it once with
#      `digest-core auth approve owui`.
#   2. pipe: a chat "run [name]" asks the core to run + deliver. No DB, no scheduler
#      (the scheduler lives in the core now), no digestcore install.

import os
import json
import secrets
import urllib.request
import urllib.error
from pydantic import BaseModel

CORE_URL = os.environ.get("DIGEST_CORE_URL", "http://digest-core:8787")
TOKEN_PATH = os.environ.get("DIGEST_TOKEN_PATH", "/run/digest/owui.token")


def _read_token() -> str:
    try:
        with open(TOKEN_PATH) as f:
            return f.read().strip()
    except OSError:
        return ""


def _ensure_token() -> str:
    tok = _read_token()
    if not tok:
        tok = secrets.token_urlsafe(32)
        os.makedirs(os.path.dirname(TOKEN_PATH) or ".", exist_ok=True)
        fd = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(tok)
    try:  # idempotent registration; approved tokens stay approved
        req = urllib.request.Request(
            CORE_URL.rstrip("/") + "/auth/register",
            data=json.dumps({"name": "owui", "token": tok, "scope": "write"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=15).read()
    except Exception:
        pass
    return tok


def _run(name: str):
    body = {"name": name} if name else {}
    req = urllib.request.Request(
        CORE_URL.rstrip("/") + "/run", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {_read_token()}"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}
    except urllib.error.URLError as e:
        return 0, {"error": f"cannot reach the digest core: {e.reason}"}


class Pipeline:
    class Valves(BaseModel):
        CORE_URL: str = CORE_URL          # digest core base URL on the docker network
        TOKEN_PATH: str = TOKEN_PATH      # shared volume path for the owui token

    def __init__(self):
        self.name = "Digest Pipeline"
        self.valves = self.Valves()

    async def on_startup(self):
        global CORE_URL, TOKEN_PATH
        CORE_URL = self.valves.CORE_URL or CORE_URL
        TOKEN_PATH = self.valves.TOKEN_PATH or TOKEN_PATH
        _ensure_token()
        print(f"on_startup:{__name__} — core={CORE_URL}, token={TOKEN_PATH} "
              "(approve once: digest-core auth approve owui)")

    async def on_shutdown(self):
        pass

    async def on_valves_updated(self):
        await self.on_startup()

    def pipe(self, user_message: str, model_id: str, messages: list, body: dict) -> str:
        q = (user_message or "").strip().lower()
        q = q[3:].strip(" :") if q.startswith("run") else q
        s, p = _run(q)
        if s in (401, 403):
            return ("This digest pipeline isn't approved with the core yet. "
                    "On the core run: digest-core auth approve owui")
        if s == 0:
            return p.get("error", "The digest core is unreachable.")
        return p.get("message") or p.get("error") or "Run complete."
