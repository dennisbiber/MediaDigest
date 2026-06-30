"""Reader-profile assembly — front-end agnostic.

Lifted verbatim (in behaviour) from the OWUI ``Pipeline._load_profile`` /
``_feedback_profile``. It folds two signals into a single text profile that both
the embedding scorer and the LLM judge consume:

1. optional long-term memory recalled from a mem0 service (if configured), and
2. the reader's recent thumbs up/down, turned into a short natural-language hint.

It depends only on a SQLite connection and the Config — nothing about any chat
front end.
"""

from __future__ import annotations

import sqlite3

import requests


class ProfileService:
    def __init__(self, db: sqlite3.Connection, config):
        self.db = db
        self.cfg = config

    def load(self, user_id: str, query: str) -> str:
        mem = self._recall_mem0(user_id, query)
        return (mem + " " + self._feedback_profile(user_id)).strip()

    def _recall_mem0(self, user_id: str, query: str) -> str:
        if not self.cfg.MEM0_BASE_URL:
            return ""
        try:
            r = requests.post(
                f"{self.cfg.MEM0_BASE_URL}/recall",
                json={"query": query, "user_id": user_id}, timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            mems = data.get("results", data) if isinstance(data, dict) else data
            return " ".join(m.get("memory", m.get("text", "")) for m in mems)
        except requests.RequestException:
            return ""

    def _feedback_profile(self, user_id: str) -> str:
        """Turn recent thumbs up/down (and save/mute) into a profile fragment so the
        embedding scorer and the judge both see what this reader values."""
        try:
            rows = self.db.execute(
                "SELECT signal, title FROM feedback WHERE uuid=? ORDER BY ts DESC LIMIT 40",
                (user_id,)).fetchall()
        except sqlite3.Error:
            return ""
        ups = [r["title"] for r in rows if r["signal"] in ("up", "save") and r["title"]][:12]
        downs = [r["title"] for r in rows if r["signal"] in ("down", "mute") and r["title"]][:12]
        parts = []
        if ups:
            parts.append("The reader liked stories like: " + "; ".join(ups) + ".")
        if downs:
            parts.append("The reader was less interested in stories like: " + "; ".join(downs) + ".")
        return " ".join(parts)
