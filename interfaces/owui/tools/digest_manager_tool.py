"""
title: Digest Manager
author: Dennis Biber
version: 2.0.0
description: OWUI front end for digest subscription management. Talks to the digest
             core service over HTTP — it owns no data and needs no digestcore install.
"""

# Thin OWUI Tool: adapts OWUI idioms (__user__ identity, UserValves) into calls on the
# core's HTTP API. It reads the shared "owui" token that the Pipeline bootstraps on
# startup; it never opens a database. OWUI-specific policy (require API key + ntfy
# topic before registering) stays here; the core stays permissive.

import os
import json
import urllib.request
import urllib.error
from typing import Optional
from pydantic import BaseModel, Field

CORE_URL = os.environ.get("DIGEST_CORE_URL", "http://digest-core:8787")
TOKEN_PATH = os.environ.get("DIGEST_TOKEN_PATH", "/run/digest/owui.token")


def _read_token() -> str:
    try:
        with open(TOKEN_PATH) as f:
            return f.read().strip()
    except OSError:
        return ""


def _core(method: str, path: str, body=None, params=None):
    url = CORE_URL.rstrip("/") + path
    if params:
        from urllib.parse import urlencode
        url += "?" + urlencode({k: v for k, v in params.items() if v is not None})
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    tok = _read_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
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


def _msg(status, payload, ok_default="Done.") -> str:
    if status == 0:
        return payload.get("error", "The digest core is unreachable.")
    if status in (401, 403):
        return ("This OWUI integration isn't approved with the digest core yet. "
                "On the core run: digest-core auth approve owui")
    return payload.get("message") or payload.get("error") or ok_default


class Tools:
    class Valves(BaseModel):
        CORE_URL: str = Field(CORE_URL, description="Digest core base URL")
        TOKEN_PATH: str = Field(TOKEN_PATH, description="Path to the shared owui token")

    class UserValves(BaseModel):
        OWUI_API_KEY: str = Field("", description="Your OWUI API key (Settings > Account > API Keys)")
        NTFY_TOPIC: str = Field("", description="Your ntfy topic, e.g. Home-agent-yourname")
        TIMEZONE: str = Field("", description="Your IANA timezone, e.g. America/Chicago. Blank uses the system default.")

    def __init__(self):
        self.valves = self.Valves()

    def _apply_valves(self):
        global CORE_URL, TOKEN_PATH
        CORE_URL = self.valves.CORE_URL or CORE_URL
        TOKEN_PATH = self.valves.TOKEN_PATH or TOKEN_PATH

    def _uid(self, __user__) -> Optional[str]:
        return str(__user__["id"]) if (__user__ and __user__.get("id")) else None

    def _uv(self, __user__, field, default=""):
        uv = (__user__ or {}).get("valves")
        if uv is None:
            return default
        return uv.get(field, default) if isinstance(uv, dict) else getattr(uv, field, default)

    def describe_options(self, __user__: dict = {}) -> str:
        """Return the currently available digest types, their settings, the live news
        categories, topic codes and defaults. Call this at the START of a setup chat."""
        self._apply_valves()
        s, p = _core("GET", "/options")
        return p.get("options", _msg(s, p)) if s == 200 else _msg(s, p)

    def register_account(self, __user__: dict = {}) -> str:
        """Register or update YOUR digest account from this tool's user settings
        (OWUI_API_KEY, NTFY_TOPIC, optional TIMEZONE). Run once before subscribing."""
        self._apply_valves()
        uid = self._uid(__user__)
        if not uid:
            return "Could not determine your user id. Make sure you're signed in."
        key = self._uv(__user__, "OWUI_API_KEY").strip()
        topic = self._uv(__user__, "NTFY_TOPIC").strip()
        tz = self._uv(__user__, "TIMEZONE").strip()
        if not key or not topic:
            return ("Missing settings. Open this chat's controls (sliders icon), fill in "
                    "OWUI_API_KEY and NTFY_TOPIC (and optionally TIMEZONE), then register again.")
        s, p = _core("POST", "/account",
                     body={"user_id": uid, "owui_token": key, "ntfy_topic": topic, "tz": tz})
        return _msg(s, p)

    def add_subscription(self, name: str, adapter: str = "arxiv_hf", topic_query: str = "",
                         count: Optional[int] = None, window_days: Optional[int] = None,
                         hour: Optional[int] = None, day_of_week: Optional[str] = None,
                         day_of_month: Optional[str] = None, __user__: dict = {}) -> str:
        """Create or update one of YOUR digest subscriptions. Use describe_options first
        to learn valid adapters and the meaning of topic_query per adapter."""
        self._apply_valves()
        s, p = _core("POST", "/subscriptions",
                     body={"user_id": self._uid(__user__), "name": name, "adapter": adapter,
                           "topic_query": topic_query, "count": count, "window_days": window_days,
                           "hour": hour, "day_of_week": day_of_week, "day_of_month": day_of_month})
        return _msg(s, p)

    def list_subscriptions(self, __user__: dict = {}) -> str:
        """List YOUR account status and all of your digest subscriptions."""
        self._apply_valves()
        s, p = _core("GET", "/subscriptions", params={"user_id": self._uid(__user__)})
        return _msg(s, p)

    def set_subscription_enabled(self, name: str, enabled: bool, __user__: dict = {}) -> str:
        """Pause (enabled=false) or resume (enabled=true) one of YOUR subscriptions."""
        self._apply_valves()
        s, p = _core("POST", f"/subscriptions/{name}/enabled",
                     body={"user_id": self._uid(__user__), "enabled": enabled})
        return _msg(s, p)

    def remove_subscription(self, name: str, __user__: dict = {}) -> str:
        """Delete one of YOUR digest subscriptions by name."""
        self._apply_valves()
        s, p = _core("DELETE", f"/subscriptions/{name}", params={"user_id": self._uid(__user__)})
        return _msg(s, p)
