"""Shared SQLite schema and connection helper."""

import os
import sqlite3

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS users ("
    " uuid TEXT PRIMARY KEY, owui_token TEXT, ntfy_topic TEXT, tz TEXT, updated_at INTEGER)",
    "CREATE TABLE IF NOT EXISTS subscriptions ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT, uuid TEXT, name TEXT, adapter TEXT,"
    " topic_query TEXT, n INTEGER, window_days INTEGER, cron TEXT, enabled INTEGER DEFAULT 1,"
    " UNIQUE(uuid, name))",
    "CREATE TABLE IF NOT EXISTS sent ("
    " uuid TEXT, sub_name TEXT, item_id TEXT, ts INTEGER,"
    " PRIMARY KEY (uuid, sub_name, item_id))",
    # per-item feedback from OWUI Actions (thumbs up/down). Adapter-agnostic so any
    # current or future adapter's items can be rated. Folded into the profile so
    # ratings tilt future ranking. Written by the OWUI actions in ../owui_functions.
    "CREATE TABLE IF NOT EXISTS feedback ("
    " uuid TEXT, item_id TEXT, adapter TEXT, signal TEXT, title TEXT, url TEXT, ts INTEGER,"
    " PRIMARY KEY (uuid, item_id, signal))",
    # cache of resolved YouTube video ids (shared across users) so we stay well under
    # the API's daily search quota. video_id='' is a cached 'no video found'.
    "CREATE TABLE IF NOT EXISTS youtube_cache ("
    " key TEXT PRIMARY KEY, video_id TEXT, ts INTEGER)",
    # registered API clients (interfaces) for the core service. Tokens are generated
    # by each interface and registered here; the core stores only a hash and refuses
    # to honor a token until a human approves it (status: pending -> approved).
    "CREATE TABLE IF NOT EXISTS clients ("
    " name TEXT PRIMARY KEY, token_hash TEXT UNIQUE, scope TEXT, status TEXT,"
    " created_at INTEGER, approved_at INTEGER)",
]

# Best-effort migrations for databases created before a column existed.
_MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN tz TEXT",
]


def open_db(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    db = sqlite3.connect(path, check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL")
    db.row_factory = sqlite3.Row
    for stmt in SCHEMA:
        db.execute(stmt)
    for stmt in _MIGRATIONS:
        try:
            db.execute(stmt)
        except sqlite3.OperationalError:
            pass  # column already exists
    db.commit()
    return db