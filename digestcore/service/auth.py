"""Client token registry — the human-approved registration handshake.

Each interface (OWUI, CLI, Slack, …) generates its own token, stores it as its own
secret, and registers it with the core. The core never sees the token again in the
clear: it stores only a SHA-256 hash, and — crucially — marks every new registration
``pending`` until a human approves it out of band (``digest-core auth approve <name>``).
A token is honored only once approved, and only for its granted scope.

This is deliberately not a full identity system. It's a doorman for a single-user,
single-host deployment: a small set of known interfaces, each approved once by you.
"""

from __future__ import annotations

import time
import hashlib
import sqlite3
from dataclasses import dataclass
from typing import Optional

SCOPES = ("read", "write")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class Client:
    name: str
    scope: str
    status: str
    created_at: int
    approved_at: Optional[int]


@dataclass
class VerifyResult:
    ok: bool
    reason: str = ""
    client: Optional[Client] = None


class ClientRegistry:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    # ---- registration (called by an interface, no auth) ----
    def register(self, name: str, token: str, scope: str = "write") -> Client:
        """Record a registration request. Returns the resulting client row.

        Idempotent when the same name re-presents the same token while already
        approved. Any new name, or a changed token for an existing name, lands as
        ``pending`` and must be approved again — so 'approve every time something new
        tries to register' holds.
        """
        if scope not in SCOPES:
            raise ValueError(f"scope must be one of {SCOPES}")
        if not name or not token:
            raise ValueError("name and token are required")
        th = hash_token(token)
        row = self.db.execute("SELECT name, token_hash, scope, status FROM clients "
                              "WHERE name=?", (name,)).fetchone()
        now = int(time.time())
        if row and row["token_hash"] == th and row["status"] == "approved":
            # same client, same token, already approved -> no re-approval needed
            return self.get(name)
        # new client, or rotated token, or previously revoked -> require (re)approval
        self.db.execute(
            "INSERT INTO clients(name, token_hash, scope, status, created_at, approved_at) "
            "VALUES(?,?,?,?,?,NULL) "
            "ON CONFLICT(name) DO UPDATE SET token_hash=excluded.token_hash, "
            "scope=excluded.scope, status='pending', created_at=excluded.created_at, "
            "approved_at=NULL",
            (name, th, scope, "pending", now))
        self.db.commit()
        return self.get(name)

    # ---- verification (called on every API request) ----
    def verify(self, token: str, need_write: bool) -> VerifyResult:
        if not token:
            return VerifyResult(False, "missing token")
        row = self.db.execute(
            "SELECT name, scope, status, created_at, approved_at FROM clients "
            "WHERE token_hash=?", (hash_token(token),)).fetchone()
        if not row:
            return VerifyResult(False, "unknown token")
        if row["status"] != "approved":
            return VerifyResult(False, f"token {row['status']} (awaiting approval)")
        if need_write and row["scope"] != "write":
            return VerifyResult(False, "read-only token")
        return VerifyResult(True, client=Client(row["name"], row["scope"], row["status"],
                                                row["created_at"], row["approved_at"]))

    # ---- admin (run locally inside the core container; no token needed) ----
    def get(self, name: str) -> Optional[Client]:
        r = self.db.execute("SELECT name, scope, status, created_at, approved_at FROM clients "
                            "WHERE name=?", (name,)).fetchone()
        return Client(r["name"], r["scope"], r["status"], r["created_at"], r["approved_at"]) if r else None

    def list(self) -> list[Client]:
        return [Client(r["name"], r["scope"], r["status"], r["created_at"], r["approved_at"])
                for r in self.db.execute(
                    "SELECT name, scope, status, created_at, approved_at FROM clients "
                    "ORDER BY created_at")]

    def pending(self) -> list[Client]:
        return [c for c in self.list() if c.status == "pending"]

    def approve(self, name: str) -> bool:
        cur = self.db.execute(
            "UPDATE clients SET status='approved', approved_at=? WHERE name=? AND status!='approved'",
            (int(time.time()), name))
        self.db.commit()
        return cur.rowcount > 0

    def revoke(self, name: str) -> bool:
        cur = self.db.execute("UPDATE clients SET status='revoked' WHERE name=?", (name,))
        self.db.commit()
        return cur.rowcount > 0
