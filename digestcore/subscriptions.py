"""Subscription & account management — abstracted out of the OWUI Tool schema.

The OWUI "Digest Manager" Tool is a thin shell: it pulls the user id and a few
settings off the OWUI ``__user__`` object, then registers an account and CRUDs
subscription rows. This service is that mechanic with the OWUI shell removed —
every method takes plain values, so the OWUI Tool, a CLI, or a Slack
slash-command can each wrap it identically.

Design notes
------------
* **Identity is a plain ``user_id`` string.** In OWUI that's ``__user__["id"]``;
  in single-user CLI it's a fixed id like ``"local"``; in Slack it's the Slack
  user id. The service neither knows nor cares which.
* **Required-field policy lives in the wrapper, not here.** OWUI insists on an
  API key + ntfy topic before registering; a CLI install may require nothing.
  So ``register_user`` validates only the timezone (a domain constraint) and
  stores whatever it's given.
* **``owui_token``** is kept as-is: it is the OWUI delivery sink's per-user
  credential. Other sinks ignore it. When a second platform needs per-user
  delivery routing, generalize then (a ``delivery_targets`` table) rather than
  prematurely renaming a column only one sink reads.
* Results are structured dataclasses with a ready ``.message``; a wrapper can use
  ``.message`` directly (OWUI shows it to the setup assistant) or reformat from
  the fields (the CLI prints its own way).
"""

from __future__ import annotations

import time
import sqlite3
from dataclasses import dataclass, field
from typing import Callable, Optional

from digestcore import sources as _sources


@dataclass
class Result:
    ok: bool
    message: str


@dataclass
class Account:
    user_id: str
    owui_token: str = ""
    ntfy_topic: str = ""
    tz: str = ""


@dataclass
class Subscription:
    name: str
    adapter: str
    topic_query: str
    n: int
    window_days: int
    cron: str
    enabled: bool


@dataclass
class ListResult:
    ok: bool
    message: str
    account: Optional[Account] = None
    subscriptions: list = field(default_factory=list)


def _valid_tz(tz: str) -> bool:
    if not tz:
        return True
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(tz)
        return True
    except Exception:  # noqa: BLE001
        return False


class SubscriptionService:
    def __init__(self, db: sqlite3.Connection,
                 options_loader: Optional[Callable[[], dict]] = None):
        # db must already carry the shared schema (digestcore.db.open_db owns it).
        self.db = db
        self._load_options = options_loader or _sources.load_options

    # ---------- discovery ----------
    def describe_options(self) -> str:
        """Currently available digest types, their settings, live news categories,
        topic codes and defaults. A setup assistant calls this first so it offers
        the correct, up-to-date options instead of a memorized list."""
        try:
            opts = self._load_options()
        except (OSError, ValueError):
            return ("Options aren't available yet — confirm the engine has seeded its "
                    "data dir, then try again.")
        lines = ["Available digest types:"]
        for key, a in opts.get("adapters", {}).items():
            lines.append(f'\n• adapter "{key}" — {a.get("label", key)}')
            if a.get("topic_query"):
                lines.append(f"  topic_query: {a['topic_query']}")
            d = a.get("defaults", {})
            if d:
                lines.append("  defaults: " + ", ".join(f"{k}={v}" for k, v in d.items()))
            if a.get("needs"):
                lines.append(f"  needs (set up by the admin, not the user): {a['needs']}")
            if "categories" in a:
                lines.append(f"  categories (exclude up to {a.get('max_exclusions', 4)}): "
                             + ", ".join(a["categories"].keys()))
            if "arxiv_codes" in a:
                lines.append("  topic codes: "
                             + "; ".join(f"{k} = {v}" for k, v in a["arxiv_codes"].items()))
        return "\n".join(lines)

    def adapter_defaults(self, adapter: str) -> dict:
        try:
            return self._load_options().get("adapters", {}).get(adapter, {}).get("defaults", {})
        except (OSError, ValueError):
            return {}

    # ---------- account ----------
    def is_registered(self, user_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM users WHERE uuid=?", (user_id,)).fetchone() is not None

    def register_user(self, user_id: str, owui_token: str = "",
                      ntfy_topic: str = "", tz: str = "") -> Result:
        if not user_id:
            return Result(False, "Could not determine your user id.")
        if not _valid_tz(tz):
            return Result(False, f"'{tz}' isn't a valid IANA timezone "
                                 "(examples: America/Chicago, Europe/London).")
        self.db.execute(
            "INSERT INTO users(uuid, owui_token, ntfy_topic, tz, updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(uuid) DO UPDATE SET owui_token=excluded.owui_token, "
            "ntfy_topic=excluded.ntfy_topic, tz=excluded.tz, updated_at=excluded.updated_at",
            (user_id, owui_token, ntfy_topic, tz, int(time.time())))
        self.db.commit()
        where = f"timezone '{tz}'" if tz else "the system default timezone"
        return Result(True, f"Registered user '{user_id}', using {where}.")

    def get_account(self, user_id: str) -> Optional[Account]:
        r = self.db.execute(
            "SELECT owui_token, ntfy_topic, tz FROM users WHERE uuid=?", (user_id,)).fetchone()
        if not r:
            return None
        return Account(user_id, r["owui_token"] or "", r["ntfy_topic"] or "", r["tz"] or "")

    # ---------- subscriptions ----------
    def add_subscription(self, user_id: str, name: str, adapter: str = "arxiv_hf",
                         topic_query: str = "", count: Optional[int] = None,
                         window_days: Optional[int] = None, hour: Optional[int] = None,
                         day_of_week: Optional[str] = None,
                         day_of_month: Optional[str] = None) -> Result:
        if not user_id:
            return Result(False, "Could not determine your user id.")
        if not self.is_registered(user_id):
            return Result(False, "You're not registered yet. Register first.")
        # Fill anything omitted from this adapter's published defaults (safe daily
        # values, never a blanket Monday-only schedule).
        d = self.adapter_defaults(adapter)
        count = count if count is not None else int(d.get("count", 5))
        window_days = window_days if window_days is not None else int(d.get("window_days", 7))
        hour = hour if hour is not None else int(d.get("hour", 7))
        day_of_week = day_of_week if day_of_week is not None else str(d.get("day_of_week", "*"))
        day_of_month = day_of_month if day_of_month is not None else str(d.get("day_of_month", "*"))
        cron = f"0 {hour} {day_of_month} * {day_of_week}"
        self.db.execute(
            "INSERT INTO subscriptions(uuid,name,adapter,topic_query,n,window_days,cron,enabled) "
            "VALUES(?,?,?,?,?,?,?,1) "
            "ON CONFLICT(uuid,name) DO UPDATE SET adapter=excluded.adapter, "
            "topic_query=excluded.topic_query, n=excluded.n, window_days=excluded.window_days, "
            "cron=excluded.cron, enabled=1",
            (user_id, name, adapter, topic_query, count, window_days, cron))
        self.db.commit()
        return Result(True, f"Subscription '{name}' saved: {adapter}, top {count}, "
                            f"last {window_days}d, schedule '{cron}'.")

    def list_subscriptions(self, user_id: str) -> ListResult:
        if not user_id:
            return ListResult(False, "Could not determine your user id.")
        acct = self.get_account(user_id)
        if not acct:
            return ListResult(False, "Not registered. Register first.")
        rows = self.db.execute(
            "SELECT name, adapter, topic_query, n, window_days, cron, enabled "
            "FROM subscriptions WHERE uuid=? ORDER BY name", (user_id,)).fetchall()
        subs = [Subscription(r["name"], r["adapter"], r["topic_query"], r["n"],
                             r["window_days"], r["cron"], bool(r["enabled"])) for r in rows]
        tz = acct.tz or "system default"
        head = f"Account '{user_id}': ntfy '{acct.ntfy_topic}', timezone {tz}."
        if not subs:
            return ListResult(True, head + " No subscriptions yet.", acct, subs)
        lines = [head, "Subscriptions:"]
        for s in subs:
            state = "on" if s.enabled else "PAUSED"
            lines.append(f"- {s.name} [{state}]: {s.adapter}, top {s.n}, "
                         f"last {s.window_days}d, cron '{s.cron}'")
        return ListResult(True, "\n".join(lines), acct, subs)

    def set_enabled(self, user_id: str, name: str, enabled: bool) -> Result:
        if not user_id:
            return Result(False, "Could not determine your user id.")
        cur = self.db.execute("UPDATE subscriptions SET enabled=? WHERE uuid=? AND name=?",
                              (1 if enabled else 0, user_id, name))
        self.db.commit()
        if cur.rowcount == 0:
            return Result(False, f"No subscription named '{name}' found.")
        return Result(True, f"'{name}' is now {'enabled' if enabled else 'paused'}.")

    def remove(self, user_id: str, name: str) -> Result:
        if not user_id:
            return Result(False, "Could not determine your user id.")
        cur = self.db.execute("DELETE FROM subscriptions WHERE uuid=? AND name=?", (user_id, name))
        self.db.commit()
        if cur.rowcount == 0:
            return Result(False, f"No subscription named '{name}' found.")
        return Result(True, f"Removed '{name}'.")
