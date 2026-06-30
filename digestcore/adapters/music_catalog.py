"""Keyless music discovery from the local MusicBrainz-derived catalog.

Parallel to MusicDiscoveryAdapter (Last.fm): same Candidate shape, same taste_match
signal, so the shared scorer ranks both identically and you can A/B by pointing one
subscription at adapter 'music' and another at 'music_catalog'.

Pipeline: seeds (subscription topic + your thumbs) -> resolve to catalog artists ->
multi-axis symbolic similar_artists() -> exclude seeds -> one representative track per
artist via ytmusicapi (keyless) -> Candidates. No API key, no external account, no
for-profit source, and every artist is a real catalogued MusicBrainz entity.

Config (env, set by the core):
  DIGEST_MUSIC_CATALOG_PATH   catalog sqlite (default <DIGEST_DATA_DIR>/music_catalog.sqlite)
  DIGEST_MUSIC_AXIS_WEIGHTS   e.g. "genre:1.0,tag:0.5,decade:0.3,area:0.25"
  DIGEST_MUSIC_TWO_HOP        "1" to expand similar-to-the-similar
"""

import os
import sqlite3
import hashlib
import urllib.parse

from digestcore.models import Candidate, SourceAdapter
from digestcore.adapters.catalog_store import (
    open_catalog, resolve_seeds, similar_artists, artist_rows,
    norm_name, DEFAULT_AXIS_WEIGHTS, DEFAULT_EDGE_WEIGHT)


def _parse_axis_weights(spec: str) -> dict:
    if not spec:
        return dict(DEFAULT_AXIS_WEIGHTS)
    out = {}
    for part in spec.split(","):
        if ":" in part:
            k, v = part.split(":", 1)
            try:
                out[k.strip()] = float(v)
            except ValueError:
                pass
    return out or dict(DEFAULT_AXIS_WEIGHTS)


def _ytmusic_linker(artist: str):
    """Best-effort keyless link to a representative track. Returns (track, url).
    Degrades to a YouTube Music search URL if ytmusicapi is unavailable/offline."""
    try:
        from ytmusicapi import YTMusic
        yt = YTMusic()
        hits = yt.search(artist, filter="songs", limit=1) or []
        if hits:
            h = hits[0]
            vid = h.get("videoId")
            track = h.get("title") or ""
            if vid:
                return track, f"https://music.youtube.com/watch?v={vid}"
    except Exception:
        pass
    q = urllib.parse.quote(artist)
    return "", f"https://music.youtube.com/search?q={q}"


class MusicCatalogAdapter(SourceAdapter):
    signal_weights = {"taste_match": 1.0}
    POOL_LIMIT = 200      # artists ranked before linking
    MAX_PICKS = 40        # artists we actually resolve to tracks per run

    def __init__(self, linker=None):
        # linker is injectable so tests don't hit the network
        self._link = linker or _ytmusic_linker

    def _catalog_path(self, valves) -> str:
        return (os.environ.get("DIGEST_MUSIC_CATALOG_PATH")
                or getattr(valves, "MUSIC_CATALOG_PATH", "")
                or os.path.join(os.environ.get("DIGEST_DATA_DIR", "/data"),
                                "music_catalog.sqlite"))

    def _feedback_anchors(self, db, uuid_: str) -> tuple:
        """Positive/negative artist names from thumbs on EITHER music adapter, so the
        catalog adapter benefits from existing feedback rather than starting cold."""
        pos, neg = [], []
        if not (db and uuid_):
            return pos, neg
        try:
            rows = db.execute(
                "SELECT signal, title FROM feedback WHERE uuid=? AND adapter IN "
                "('music','music_catalog') ORDER BY ts DESC LIMIT 80", (uuid_,)).fetchall()
        except sqlite3.Error:
            return pos, neg
        for r in rows:
            artist = (r["title"] or "").split(" — ")[0].strip()
            if artist:
                (pos if r["signal"] in ("up", "save") else neg).append(artist)
        return pos, neg

    def fetch_candidates(self, topic, window_days, context=None):
        context = context or {}
        valves = context.get("valves")
        path = self._catalog_path(valves)
        if not os.path.exists(path):
            return []  # no catalog built yet -> degrade silently (like music without a key)

        seeds = [s.strip() for s in (topic or "").split(",") if s.strip()]
        pos_fb, neg_fb = self._feedback_anchors(context.get("db"), context.get("uuid"))
        seed_strings = seeds + pos_fb
        if not seed_strings:
            return []

        axis_weights = _parse_axis_weights(os.environ.get("DIGEST_MUSIC_AXIS_WEIGHTS", ""))
        two_hop = os.environ.get("DIGEST_MUSIC_TWO_HOP", "") in ("1", "true", "yes")
        neg_norm = {norm_name(n) for n in neg_fb}

        conn = open_catalog(path, read_only=True)
        try:
            seed_ids, seed_features = resolve_seeds(conn, seed_strings)
            if not seed_ids and not seed_features:
                return []
            ranked = similar_artists(conn, seed_ids, seed_features=seed_features,
                                     axis_weights=axis_weights,
                                     edge_weight=DEFAULT_EDGE_WEIGHT,
                                     limit=self.POOL_LIMIT, two_hop=two_hop)
            if not ranked:
                return []
            top_ids = [aid for aid, _ in ranked]
            rows = artist_rows(conn, top_ids)
        finally:
            conn.close()

        if ranked:
            hi = ranked[0][1] or 1.0
        out = []
        for aid, score in ranked:
            row = rows.get(aid)
            if not row:
                continue
            artist = row["name"]
            if norm_name(artist) in neg_norm:
                continue
            track, url = self._link(artist)
            norm = score / hi if hi else 0.0
            tag = ("in your wheelhouse" if norm >= 0.6 else
                   "adjacent to your taste" if norm >= 0.3 else "a stretch from your usuals")
            era = f" · {row['decade']}" if row["decade"] else ""
            title = f"{artist} — {track}" if track else artist
            rid = hashlib.md5(f"{artist}|{track}".encode()).hexdigest()[:12]
            out.append(Candidate(
                id=f"musiccat:{rid}", title=title, url=url,
                summary=f"{tag}{era} · discovered from the catalog via your seeds",
                signals={"taste_match": round(norm, 3)},
                raw={"mbid": row["mbid"], "artist": artist, "decade": row["decade"]},
            ))
            if len(out) >= self.MAX_PICKS:
                break
        return out
