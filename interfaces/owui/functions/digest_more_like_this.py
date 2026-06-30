"""
title: Digest - More like this
author: Dennis Biber
version: 2.0.0
required_open_webui_version: 0.5.0
"""

# OWUI reaction wrapper (thumbs-UP). Owns no feedback logic and needs no digestcore
# install: it finds the reacted-to digest message and posts it to the core's
# /feedback/from_text, which parses the item marker and records the rating. Its twin
# ("Less like this") differs only in SIGNAL/EMOJI. Reads the shared owui token the
# Pipeline bootstraps on startup.

import os
import re
import json
import urllib.request
import urllib.error
from typing import Optional
from pydantic import BaseModel, Field

SIGNAL = "up"
EMOJI = "\U0001F44D"

CORE_URL = os.environ.get("DIGEST_CORE_URL", "http://digest-core:8787")
TOKEN_PATH = os.environ.get("DIGEST_TOKEN_PATH", "/run/digest/owui.token")
_MARKER = re.compile(r"[#&]digest=")


def _read_token() -> str:
    try:
        with open(TOKEN_PATH) as f:
            return f.read().strip()
    except OSError:
        return ""


def _post(path, body):
    req = urllib.request.Request(
        CORE_URL.rstrip("/") + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {_read_token()}"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}
    except urllib.error.URLError as e:
        return 0, {"error": f"cannot reach the digest core: {e.reason}"}


def _marked_message(messages) -> Optional[str]:
    for m in reversed(messages or []):
        if m.get("role") == "assistant" and _MARKER.search(m.get("content", "") or ""):
            return m["content"]
    return None


class Action:
    class Valves(BaseModel):
        CORE_URL: str = Field(CORE_URL, description="Digest core base URL")
        TOKEN_PATH: str = Field(TOKEN_PATH, description="Path to the shared owui token")

    def __init__(self):
        self.valves = self.Valves()

    async def action(self, body: dict, __user__=None, __event_emitter__=None,
                     __event_call__=None) -> Optional[dict]:
        global CORE_URL, TOKEN_PATH
        CORE_URL = self.valves.CORE_URL or CORE_URL
        TOKEN_PATH = self.valves.TOKEN_PATH or TOKEN_PATH
        uid = (__user__ or {}).get("id")
        content = _marked_message(body.get("messages", []) or [])
        if not (uid and content):
            await self._notify(__event_emitter__, "warning", "No digest item found to rate here.")
            return None
        s, p = _post("/feedback/from_text", {"user_id": str(uid), "text": content, "signal": SIGNAL})
        if s in (401, 403):
            msg = "Not approved with the digest core yet (digest-core auth approve owui)."
            kind = "error"
        elif s == 0:
            msg = p.get("error", "Digest core unreachable."); kind = "error"
        else:
            msg = p.get("message") or p.get("error") or "Done."
            kind = "success" if p.get("ok") else "error"
        await self._notify(__event_emitter__, kind, f"{EMOJI} {msg}")
        return None

    async def _notify(self, emitter, kind, content):
        if emitter:
            await emitter({"type": "notification", "data": {"type": kind, "content": content}})
