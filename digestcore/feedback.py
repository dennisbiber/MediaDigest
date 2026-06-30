"""The feedback/reaction mechanic — abstracted out of the OWUI Actions.

What the two OWUI Actions ("more like this" / "less like this") actually *do* is
two-fold:

* identify which delivered item was reacted to (parse the marker from the chat
  message), and
* record a directional rating, clearing any opposite rating on the same item.

Only the second part is domain logic; the first is a chat-transport detail (it
belongs to the marker scheme). This service owns the domain part. A reaction
wrapper for any interface — an OWUI 👍 Action, a Slack ``reaction_added`` event,
a Discord button, a CLI ``feedback`` command — translates its own event into a
call here.

Signals: ``up``/``down`` are the thumbs; ``save``/``mute`` are reserved softer
signals the profile already understands. Each clears its opposite on the item.
"""

from __future__ import annotations

import time
import sqlite3
from dataclasses import dataclass
from typing import Optional

from digestcore.marker import parse_marked_text

_OPPOSITE = {"up": "down", "down": "up", "save": "mute", "mute": "save"}


@dataclass
class FeedbackResult:
    ok: bool
    message: str
    adapter: str = ""
    item_id: str = ""
    title: str = ""
    signal: str = ""


class FeedbackService:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def record(self, user_id: str, adapter: str, item_id: str, signal: str,
               title: str = "", url: str = "") -> FeedbackResult:
        if signal not in _OPPOSITE:
            return FeedbackResult(False, f"Unknown signal '{signal}'.")
        if not (user_id and item_id):
            return FeedbackResult(False, "Missing user or item id.")
        try:
            # a vote in one direction clears the opposite vote on the same item
            self.db.execute(
                "DELETE FROM feedback WHERE uuid=? AND item_id=? AND signal=?",
                (user_id, item_id, _OPPOSITE[signal]))
            self.db.execute(
                "INSERT OR REPLACE INTO feedback VALUES (?,?,?,?,?,?,?)",
                (user_id, item_id, adapter, signal, title, url, int(time.time())))
            self.db.commit()
        except sqlite3.Error as e:
            return FeedbackResult(False, f"Couldn't save feedback: {e}")
        label = "more like this" if signal in ("up", "save") else "less like this"
        return FeedbackResult(True, f"Noted: {title[:60] or item_id} — {label}.",
                              adapter, item_id, title, signal)

    def record_from_text(self, user_id: str, text: str, signal: str) -> FeedbackResult:
        """Convenience for chat reaction wrappers: extract the marked item from a
        message body, then record. Returns ok=False if no digest item is present."""
        item = parse_marked_text(text)
        if not item:
            return FeedbackResult(False, "No digest item found to rate here.")
        return self.record(user_id, item.adapter, item.item_id, signal,
                           item.title, item.url)
