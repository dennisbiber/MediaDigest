"""Local MusicBrainz-derived artist catalog: schema, open helpers, and the multi-axis
symbolic similarity query.

This is the read side of the keyless music source. The catalog is a single SQLite file
(no server, no vector DB) built offline from a MusicBrainz JSON dump by
scripts/build_music_catalog.py. Similarity here is *symbolic* — weighted overlap of
curated features (genre, tag, decade, area, ...) plus artist-artist relationship edges —
so it needs no training and covers every artist that has any curated metadata.

The feature table is deliberately generic: (artist_id, axis, value, weight). Adding a new
clustering axis later ("city recorded in", "label", "instrumentation", ...) is just
inserting rows with a new axis label and giving it a weight at query time — no schema
change. That's the "alternative axes to cluster on" knob.
"""

import re
import sqlite3
import unicodedata

SCHEMA_VERSION = 1

DDL = """
CREATE TABLE IF NOT EXISTS artist (
    id         INTEGER PRIMARY KEY,
    mbid       TEXT UNIQUE,
    name       TEXT NOT NULL,
    sort_name  TEXT,
    name_norm  TEXT,
    type       TEXT,
    begin_year INTEGER,
    decade     TEXT,
    area       TEXT
);
CREATE TABLE IF NOT EXISTS feature (
    artist_id INTEGER NOT NULL,
    axis      TEXT NOT NULL,
    value     TEXT NOT NULL,
    weight    REAL NOT NULL DEFAULT 1.0
);
CREATE TABLE IF NOT EXISTS edge (
    src    INTEGER NOT NULL,
    dst    INTEGER NOT NULL,
    kind   TEXT,
    weight REAL NOT NULL DEFAULT 1.0
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS ix_feature_axis_value ON feature(axis, value);
CREATE INDEX IF NOT EXISTS ix_feature_artist     ON feature(artist_id);
CREATE INDEX IF NOT EXISTS ix_artist_name_norm   ON artist(name_norm);
CREATE INDEX IF NOT EXISTS ix_edge_src           ON edge(src);
"""

# Sensible defaults for how much each curated axis counts toward similarity. Genre and
# the relationship graph carry the most signal; era and geography are gentle nudges that
# let you cluster by "time region" without dominating. Override per subscription.
DEFAULT_AXIS_WEIGHTS = {"genre": 1.0, "tag": 0.5, "decade": 0.3, "area": 0.25}
DEFAULT_EDGE_WEIGHT = 1.5


def open_catalog(path: str, read_only: bool = True) -> sqlite3.Connection:
    if read_only:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)
    conn.executescript(INDEXES)
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                 (str(SCHEMA_VERSION),))
    conn.commit()


def norm_name(name: str) -> str:
    """Fold a name to a lookup key: strip accents, lowercase, collapse non-alphanumerics.
    Never collapses to empty (falls back to the lowercased original)."""
    if not name:
        return ""
    n = unicodedata.normalize("NFKD", name)
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = re.sub(r"[^a-z0-9]+", " ", n.lower()).strip()
    return n or name.lower().strip()


def decade_of(year) -> str:
    try:
        y = int(year)
    except (TypeError, ValueError):
        return ""
    return f"{(y // 10) * 10}s" if y > 0 else ""


def resolve_seeds(conn, seeds: list[str]) -> tuple[list[int], list[tuple]]:
    """Map seed strings to either artists or features. A seed matching an artist name
    becomes an artist-seed (contributes its features AND is excluded from results). A seed
    matching a genre/tag value (e.g. 'jazz') becomes a feature-seed (axis,value) that we
    match on but do NOT exclude — you want those artists. Returns (artist_ids, features)."""
    ids: list[int] = []
    feats: list[tuple] = []
    for s in seeds:
        key = norm_name(s)
        if not key:
            continue
        row = conn.execute(
            "SELECT a.id AS id, COUNT(f.artist_id) AS c FROM artist a "
            "LEFT JOIN feature f ON f.artist_id = a.id WHERE a.name_norm = ? "
            "GROUP BY a.id ORDER BY c DESC LIMIT 1", (key,)).fetchone()
        if row:
            ids.append(row["id"])
            continue
        frow = conn.execute(
            "SELECT axis, value FROM feature WHERE value = ? AND axis IN ('genre','tag') LIMIT 1",
            (s.strip().lower(),)).fetchone()
        if frow:
            feats.append((frow["axis"], frow["value"]))
    seen = set(); out = []
    for i in ids:
        if i not in seen:
            seen.add(i); out.append(i)
    return out, feats


def _chunked_in(conn, sql_template: str, ids, prefix_params=(), batch=900):
    """Run a query whose WHERE has a single {qmarks} IN-list, in batches under SQLite's
    ~999 bound-variable limit, and return all rows. ids are assumed unique, so a GROUP BY
    inside the template never splits a group across batches."""
    rows = []
    ids = list(ids)
    for i in range(0, len(ids), batch):
        chunk = ids[i:i + batch]
        q = sql_template.format(qmarks=",".join("?" * len(chunk)))
        rows.extend(conn.execute(q, (*prefix_params, *chunk)).fetchall())
    return rows


def _seed_feature_weights(conn, seed_ids, seed_features, axis_weights) -> dict:
    """Accumulate seed signal into a weighted (axis,value)->weight bag, from both the
    artist-seeds' own features and any explicit feature-seeds (e.g. a genre seed)."""
    bag: dict = {}
    if seed_ids:
        rows = _chunked_in(
            conn, "SELECT axis, value, weight FROM feature WHERE artist_id IN ({qmarks})",
            seed_ids)
        for r in rows:
            aw = axis_weights.get(r["axis"])
            if not aw:
                continue
            key = (r["axis"], r["value"])
            bag[key] = bag.get(key, 0.0) + aw * (r["weight"] or 1.0)
    for axis, value in (seed_features or []):
        aw = axis_weights.get(axis)
        if aw:
            bag[(axis, value)] = bag.get((axis, value), 0.0) + aw
    return bag


def similar_artists(conn, seed_ids: list[int], seed_features: list[tuple] = None,
                    axis_weights: dict = None, edge_weight: float = DEFAULT_EDGE_WEIGHT,
                    limit: int = 200, two_hop: bool = False) -> list[tuple]:
    """Rank artists similar to the seeds by weighted shared-feature overlap plus
    relationship-edge adjacency. Artist-seeds are excluded; feature-seeds (genres) are
    matched but their artists are not excluded. Returns [(artist_id, score)] desc.

    two_hop expands from the strongest first-hop artists (similar-to-the-similar)."""
    if not seed_ids and not seed_features:
        return []
    axis_weights = axis_weights or DEFAULT_AXIS_WEIGHTS
    seed_set = set(seed_ids or [])
    scores: dict = {}

    bag = _seed_feature_weights(conn, seed_ids, seed_features, axis_weights)
    for (axis, value), w in bag.items():
        rows = conn.execute(
            "SELECT artist_id FROM feature WHERE axis = ? AND value = ?", (axis, value))
        for r in rows:
            aid = r["artist_id"]
            if aid in seed_set:
                continue
            scores[aid] = scores.get(aid, 0.0) + w

    # relationship edges from artist-seeds (members, collaborations, ...) are strong signal
    if edge_weight and seed_ids:
        for r in _chunked_in(
                conn, "SELECT dst, weight FROM edge WHERE src IN ({qmarks})", seed_ids):
            if r["dst"] not in seed_set:
                scores[r["dst"]] = scores.get(r["dst"], 0.0) + edge_weight * (r["weight"] or 1.0)

    # temper artists with huge feature counts (hubs) so breadth doesn't masquerade as match
    if scores:
        ids = list(scores.keys())
        counts = {row["artist_id"]: row["c"] for row in _chunked_in(
            conn, "SELECT artist_id, COUNT(*) c FROM feature WHERE artist_id IN ({qmarks}) "
            "GROUP BY artist_id", ids)}
        import math
        for aid in ids:
            scores[aid] /= 1.0 + math.log1p(counts.get(aid, 1))

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)

    if two_hop and ranked:
        hop1 = [aid for aid, _ in ranked[:25]]
        hop2 = similar_artists(conn, hop1, axis_weights=axis_weights,
                               edge_weight=edge_weight, limit=limit, two_hop=False)
        merged = dict(ranked)
        for aid, sc in hop2:
            if aid not in seed_set:
                merged[aid] = merged.get(aid, 0.0) + 0.4 * sc  # decayed second hop
        ranked = sorted(merged.items(), key=lambda kv: kv[1], reverse=True)

    return ranked[:limit]


def artist_rows(conn, ids: list[int]) -> dict:
    if not ids:
        return {}
    return {r["id"]: r for r in _chunked_in(
        conn, "SELECT * FROM artist WHERE id IN ({qmarks})", ids)}
