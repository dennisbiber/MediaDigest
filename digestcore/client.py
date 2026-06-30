"""Thin HTTP client for the core service — standard library only.

Every interface (OWUI Tool/Actions, CLI, future Slack bot) uses this instead of
opening the database. It carries an approved bearer token and mirrors the service
methods, returning plain dicts (the service's dataclass results, serialized).
"""

from __future__ import annotations

import json
import urllib.request
from urllib.parse import quote
import urllib.error
from typing import Optional


class CoreError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"core {status}: {message}")
        self.status = status
        self.message = message


def register(base_url: str, name: str, token: str, scope: str = "write") -> dict:
    """Register this interface's self-generated token with the core (no auth).
    Returns {name, status, scope}; status is 'pending' until a human approves."""
    return _call(base_url, "POST", "/auth/register", token=None,
                 body={"name": name, "token": token, "scope": scope})


def admin_pending(base_url: str, admin_token: str) -> dict:
    return _call(base_url, "GET", "/admin/pending", admin_token)


def admin_list(base_url: str, admin_token: str) -> dict:
    return _call(base_url, "GET", "/admin/clients", admin_token)


def admin_approve(base_url: str, admin_token: str, name: str) -> dict:
    return _call(base_url, "POST", "/admin/approve", admin_token, body={"name": name})


def admin_revoke(base_url: str, admin_token: str, name: str) -> dict:
    return _call(base_url, "POST", "/admin/revoke", admin_token, body={"name": name})


class DigestClient:
    def __init__(self, base_url: str, token: str, user_id: str = ""):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.user_id = user_id

    def _uid(self, user_id: Optional[str]) -> str:
        return user_id or self.user_id

    # ---- reads ----
    def healthz(self) -> dict:
        return _call(self.base_url, "GET", "/healthz", token=None)

    def options(self) -> str:
        return _call(self.base_url, "GET", "/options", self.token)["options"]

    def get_account(self, user_id: str = "") -> Optional[dict]:
        try:
            return _call(self.base_url, "GET", "/account", self.token,
                         params={"user_id": self._uid(user_id)})
        except CoreError as e:
            if e.status == 404:
                return None
            raise

    def list_subscriptions(self, user_id: str = "") -> dict:
        return _call(self.base_url, "GET", "/subscriptions", self.token,
                     params={"user_id": self._uid(user_id)})

    # ---- writes ----
    def register_account(self, user_id: str = "", owui_token: str = "",
                         ntfy_topic: str = "", tz: str = "") -> dict:
        return _call(self.base_url, "POST", "/account", self.token,
                     body={"user_id": self._uid(user_id), "owui_token": owui_token,
                           "ntfy_topic": ntfy_topic, "tz": tz})

    def add_subscription(self, name: str, adapter: str = "arxiv_hf", topic_query: str = "",
                         count=None, window_days=None, hour=None, day_of_week=None,
                         day_of_month=None, user_id: str = "") -> dict:
        return _call(self.base_url, "POST", "/subscriptions", self.token,
                     body={"user_id": self._uid(user_id), "name": name, "adapter": adapter,
                           "topic_query": topic_query, "count": count, "window_days": window_days,
                           "hour": hour, "day_of_week": day_of_week, "day_of_month": day_of_month})

    def set_enabled(self, name: str, enabled: bool, user_id: str = "") -> dict:
        return _call(self.base_url, "POST", f"/subscriptions/{quote(name, safe='')}/enabled", self.token,
                     body={"user_id": self._uid(user_id), "enabled": enabled})

    def remove(self, name: str, user_id: str = "") -> dict:
        return _call(self.base_url, "DELETE", f"/subscriptions/{quote(name, safe='')}", self.token,
                     params={"user_id": self._uid(user_id)})

    def run(self, name: str = "") -> dict:
        """Ask the core to run a digest now. The core delivers to the configured
        front-end; this returns only a status report (counts), never the items.
        A cold build (model load + per-item embeds + judge) can take minutes, so this
        waits far longer than the other calls."""
        return _call(self.base_url, "POST", "/run", self.token,
                     body={"name": name} if name else {}, timeout=600)

    def record_feedback(self, adapter: str, item_id: str, signal: str,
                        title: str = "", url: str = "", user_id: str = "") -> dict:
        return _call(self.base_url, "POST", "/feedback", self.token,
                     body={"user_id": self._uid(user_id), "adapter": adapter, "item_id": item_id,
                           "signal": signal, "title": title, "url": url})

    def record_feedback_from_text(self, text: str, signal: str, user_id: str = "") -> dict:
        return _call(self.base_url, "POST", "/feedback/from_text", self.token,
                     body={"user_id": self._uid(user_id), "text": text, "signal": signal})


def _call(base_url, method, path, token, body=None, params=None, timeout=30) -> dict:
    url = base_url.rstrip("/") + path
    if params:
        from urllib.parse import urlencode
        url += "?" + urlencode({k: v for k, v in params.items() if v is not None})
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8") or "{}")
            msg = payload.get("error") or payload.get("message") or str(e)
        except Exception:  # noqa: BLE001
            msg = str(e)
        raise CoreError(e.code, msg)
    except urllib.error.URLError as e:
        raise CoreError(0, f"cannot reach core at {base_url}: {e.reason}")
