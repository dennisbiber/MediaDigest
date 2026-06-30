"""OWUI delivery sink: one assistant message per digest item in a new OWUI chat.

This is the original ``deliver_to_owui`` re-homed behind the ``DeliverySink``
port. It is the *only* delivery path that talks to Open WebUI; swapping it for
``StdoutSink``/``CallbackSink`` is what makes the engine front-end agnostic.

Each item's link carries the invisible feedback marker (see ``digestcore.marker``)
so OWUI's reaction Action can attribute a rating back to the item. The sink also
owns building its own permalink (``/c/<chat_id>``) — the runner just forwards
whatever permalink the sink returns to the notifier.
"""

from __future__ import annotations

import time
import uuid
import datetime as dt
from typing import Optional

import requests

from digestcore.marker import add_marker


class OwuiChatSink:
    def __init__(self, config):
        self.cfg = config

    def deliver(self, subscription: dict, items: list[dict]) -> Optional[str]:
        chat_id = self._create_chat(subscription.get("owui_token", ""),
                                    subscription.get("name", "Digest"), items)
        if not chat_id:
            return None
        base = self.cfg.OWUI_PUBLIC_URL or self.cfg.OWUI_BASE_URL
        return f"{base.rstrip('/')}/c/{chat_id}"

    def _create_chat(self, user_token: str, title: str, items: list[dict]) -> Optional[str]:
        now = int(time.time())
        messages: list[dict] = []
        history: dict[str, dict] = {}

        lead_id = str(uuid.uuid4())
        lead = {"id": lead_id, "role": "user",
                "content": f"{title} — {dt.date.today():%a %d %b}",
                "timestamp": now, "models": [self.cfg.DISPLAY_MODEL],
                "parentId": None, "childrenIds": []}
        messages.append(lead)
        history[lead_id] = lead

        prev = lead
        for idx, it in enumerate(items, 1):
            aid = str(uuid.uuid4())
            why = f" — {it['why']}" if it.get("why") else ""
            marked_url = add_marker(it["url"], it.get("adapter", ""), it["id"])
            content = f"**{idx}. [{it['title']}]({marked_url})**{why}"
            msg = {"id": aid, "role": "assistant", "content": content, "timestamp": now,
                   "model": self.cfg.DISPLAY_MODEL, "done": True,
                   "parentId": prev["id"], "childrenIds": []}
            prev["childrenIds"].append(aid)
            messages.append(msg)
            history[aid] = msg
            prev = msg

        payload = {"chat": {"title": title, "models": [self.cfg.DISPLAY_MODEL],
                            "messages": messages,
                            "history": {"messages": history, "currentId": prev["id"]},
                            "timestamp": now}}
        resp = requests.post(f"{self.cfg.OWUI_BASE_URL}/api/v1/chats/new",
                             headers={"Authorization": f"Bearer {user_token}",
                                      "Content-Type": "application/json"},
                             json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json().get("id")
