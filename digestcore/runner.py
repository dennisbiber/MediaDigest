"""Digest orchestration — the heart of the engine, with no front end attached.

This is everything the OWUI ``Pipeline`` did *around* the engine — load the
profile, build the digest, deliver it, ping the notifier, record what was sent,
and decide which subscriptions are due — lifted into a plain object. The OWUI
``Pipeline`` and the CLI both become thin shells that construct a ``DigestRunner``
and call it; a Slack bot would do the same.

Delivery and notification are injected (the ``DeliverySink`` / ``Notifier``
ports), so the runner has no idea whether output lands in an OWUI chat, a
terminal, or a Slack channel.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from digestcore.engine import DigestEngine
from digestcore.profile import ProfileService
from digestcore.scheduler import cron_match, safe_zone
from digestcore.delivery.base import DeliverySink, Notifier, NullNotifier
from digestcore.adapters import ADAPTERS
from digestcore.net import AdapterRetryable


@dataclass
class SubRun:
    name: str
    count: int = 0
    error: str = ""       # a hard failure (a bug); shown as an error
    retry: str = ""        # a transport failure; nothing sent, next run retries
    note: str = ""         # per-run source diagnostic from the adapter that ran


@dataclass
class RunReport:
    runs: list = field(default_factory=list)

    @property
    def health(self) -> str:
        # scoped to the adapters that actually ran this batch — no stale global readout
        return "; ".join(f"{r.name}: {r.note}" for r in self.runs if r.note)

    @property
    def message(self) -> str:
        if not self.runs:
            return "No matching enabled subscriptions found."
        lines = []
        for r in self.runs:
            if r.error:
                lines.append(f"- {r.name}: error — {r.error}")
            elif r.retry:
                lines.append(f"- {r.name}: {r.retry}; nothing sent, will retry next run")
            elif r.count:
                lines.append(f"- {r.name}: delivered {r.count} items")
            else:
                lines.append(f"- {r.name}: nothing new")
            if r.note:
                lines.append(f"    · {r.note}")
        return "Run complete:\n" + "\n".join(lines)


class DigestRunner:
    def __init__(self, config, db: sqlite3.Connection, sink: DeliverySink,
                 notifier: Optional[Notifier] = None,
                 profile_service: Optional[ProfileService] = None,
                 engine: Optional[DigestEngine] = None):
        self.cfg = config
        self.db = db
        self.sink = sink
        self.notifier = notifier or NullNotifier()
        self.profiles = profile_service or ProfileService(db, config)
        self.engine = engine or DigestEngine(config, db)

    # ---- one subscription ----
    def run_subscription(self, sub: dict) -> int:
        profile = self.profiles.load(sub["uuid"], sub["topic_query"])
        items = self.engine.build_digest(sub, profile)
        if not items:
            return 0
        for it in items:
            it["adapter"] = sub["adapter"]
        permalink = self.sink.deliver(sub, items)
        self.notifier.notify(sub, sub["name"], f"{len(items)} new picks ready", permalink)
        self.engine._record_sent(sub["uuid"], sub["name"], [it["id"] for it in items])
        return len(items)

    # ---- subscription queries ----
    def enabled_subscriptions(self) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT s.uuid, s.name, s.adapter, s.topic_query, s.n, s.window_days, s.cron,"
            " u.owui_token, u.ntfy_topic, u.tz FROM subscriptions s JOIN users u ON s.uuid=u.uuid"
            " WHERE s.enabled=1").fetchall()]

    # ---- manual runs (maps to OWUI 'run'/'run <name>' and CLI 'digest run') ----
    def run_all(self) -> RunReport:
        return self._run_many(self.enabled_subscriptions())

    def run_named(self, query: str) -> RunReport:
        q = (query or "").strip().lower()
        rows = [r for r in self.enabled_subscriptions()
                if not q or q in r["name"].lower() or r["name"].lower() in q]
        return self._run_many(rows)

    def _adapter_diagnostic(self, adapter_key: str) -> str:
        """This run's source diagnostic from the adapter that produced it, if any.
        Read right after the sub runs, so it reflects this batch — not a stale
        global readout, and never an adapter that didn't run."""
        fn = getattr(ADAPTERS.get(adapter_key), "diagnostic", None)
        try:
            return fn() if callable(fn) else ""
        except Exception:  # noqa: BLE001 - a diagnostic must never break a run
            return ""

    def _run_many(self, rows: list[dict]) -> RunReport:
        report = RunReport()
        for sub in rows:
            try:
                sr = SubRun(sub["name"], count=self.run_subscription(sub))
            except AdapterRetryable as e:
                # transport failure — passive retry, nothing delivered or marked sent
                sr = SubRun(sub["name"], retry=str(e))
            except Exception as e:  # noqa: BLE001
                sr = SubRun(sub["name"], error=str(e))
            sr.note = self._adapter_diagnostic(sub["adapter"])
            report.runs.append(sr)
        return report

    # ---- scheduler tick (maps to OWUI Pipeline._tick) ----
    def tick(self, now_utc: dt.datetime) -> None:
        for sub in self.enabled_subscriptions():
            local = now_utc.astimezone(safe_zone(sub.get("tz") or self.cfg.DEFAULT_TZ))
            if cron_match(sub["cron"], local):
                try:
                    self.run_subscription(sub)
                except AdapterRetryable as e:
                    print(f"digest retry ({sub.get('name')}): {e} — will retry next scheduled run")
                except Exception as e:  # noqa: BLE001
                    print(f"digest run error ({sub.get('name')}): {e}")
