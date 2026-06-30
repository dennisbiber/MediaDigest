#!/usr/bin/env python3
"""Build the local artist catalog (SQLite) from a MusicBrainz JSON artist dump.

The MusicBrainz JSON dumps give one JSON document per artist, including curated tags,
genres, life-span, area, and relationships — everything the symbolic similarity needs,
in a stable self-describing format (far nicer than the raw Postgres TSV). This reads that
stream, derives a few extra axes (decade, area), prunes pure metadata-less stubs, and
writes a compact single-file catalog with no runtime dependency.

Get the dump from https://metabrainz.org/datasets (the JSON "artist" dump). It arrives as
.tar.xz containing an ndjson file; extract it, then:

    python scripts/build_music_catalog.py --in mbdump/artist --out music_catalog.sqlite

or stream it without a temp file:

    xzcat mbdump-json-artist.tar.xz | tar -xO | \\
        python scripts/build_music_catalog.py --in - --out music_catalog.sqlite

Flags:
  --keep-all     keep artists even with zero curated signal (default: prune stubs)
  --max-tags N   cap tags kept per artist (default 12; genres are always kept)
  --dump-date    label stored in meta (defaults to today) so you know the snapshot age
"""

import os
import sys
import gzip
import lzma
import json
import time
import argparse
import datetime as dt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from digestcore.adapters.catalog_store import (
    init_schema, norm_name, decade_of, SCHEMA_VERSION)


def _open_stream(path: str):
    if path == "-":
        return sys.stdin
    if path.endswith(".xz"):
        return lzma.open(path, "rt", encoding="utf-8")
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8")
    return open(path, "r", encoding="utf-8")


def _area_name(doc: dict) -> str:
    for key in ("area", "begin-area", "begin_area"):
        a = doc.get(key)
        if isinstance(a, dict) and a.get("name"):
            return a["name"]
    return ""


def _begin_year(doc: dict):
    ls = doc.get("life-span") or doc.get("life_span") or {}
    begin = (ls.get("begin") or "")[:4]
    try:
        return int(begin)
    except (TypeError, ValueError):
        return None


def build(in_path: str, out_path: str, keep_all: bool, max_tags: int, dump_date: str):
    if os.path.exists(out_path):
        os.remove(out_path)
    import sqlite3
    conn = sqlite3.connect(out_path)
    # build-time pragmas: fast, durability not needed for a rebuildable artifact
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    init_schema(conn)
    conn.execute("CREATE TABLE edge_raw (src_mbid TEXT, dst_mbid TEXT, kind TEXT, weight REAL)")

    n_in = n_kept = n_feat = n_edge = 0
    art_batch, feat_batch, edge_batch = [], [], []
    t0 = time.time()

    def flush():
        if art_batch:
            conn.executemany(
                "INSERT OR IGNORE INTO artist(mbid,name,sort_name,name_norm,type,begin_year,decade,area)"
                " VALUES(?,?,?,?,?,?,?,?)", art_batch)
        if feat_batch:
            conn.executemany(
                "INSERT INTO feature(artist_id,axis,value,weight) "
                "SELECT id,?,?,? FROM artist WHERE mbid=?",
                [(ax, v, w, mb) for (mb, ax, v, w) in feat_batch])
        if edge_batch:
            conn.executemany(
                "INSERT INTO edge_raw(src_mbid,dst_mbid,kind,weight) VALUES(?,?,?,?)", edge_batch)
        art_batch.clear(); feat_batch.clear(); edge_batch.clear()

    with _open_stream(in_path) as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                doc = json.loads(line)
            except json.JSONDecodeError:
                continue
            n_in += 1
            mbid = doc.get("id")
            name = doc.get("name")
            if not mbid or not name:
                continue

            genres = [(g.get("name") or "").lower() for g in (doc.get("genres") or []) if g.get("name")]
            tags = [(t.get("name") or "").lower() for t in (doc.get("tags") or []) if t.get("name")]
            tags = [t for t in tags if t not in genres][:max_tags]
            rel_artists = [r for r in (doc.get("relations") or [])
                           if isinstance(r.get("artist"), dict) and r["artist"].get("id")]

            if not keep_all and not genres and not tags and not rel_artists:
                continue  # pure stub: no signal to relate it to anything

            by = _begin_year(doc)
            dec = decade_of(by)
            area = _area_name(doc)
            art_batch.append((mbid, name, doc.get("sort-name") or doc.get("sort_name"),
                              norm_name(name), doc.get("type"), by, dec, area))
            n_kept += 1

            for g in genres:
                feat_batch.append((mbid, "genre", g, 1.0)); n_feat += 1
            for t in tags:
                feat_batch.append((mbid, "tag", t, 1.0)); n_feat += 1
            if dec:
                feat_batch.append((mbid, "decade", dec, 1.0)); n_feat += 1
            if area:
                feat_batch.append((mbid, "area", area.lower(), 1.0)); n_feat += 1
            for r in rel_artists:
                edge_batch.append((mbid, r["artist"]["id"], r.get("type") or "related", 1.0))
                n_edge += 1

            if len(art_batch) >= 5000:
                flush()
                if n_kept % 100000 == 0:
                    print(f"  {n_kept:,} artists kept ({n_in:,} read) "
                          f"{time.time()-t0:.0f}s", file=sys.stderr)
    flush()

    print("resolving relationship edges...", file=sys.stderr)
    conn.execute(
        "INSERT INTO edge(src,dst,kind,weight) "
        "SELECT a.id, b.id, r.kind, r.weight FROM edge_raw r "
        "JOIN artist a ON a.mbid=r.src_mbid JOIN artist b ON b.mbid=r.dst_mbid")
    conn.execute("DROP TABLE edge_raw")

    for k, v in (("schema_version", str(SCHEMA_VERSION)),
                 ("dump_date", dump_date or dt.date.today().isoformat()),
                 ("built_at", dt.datetime.now().isoformat(timespec="seconds")),
                 ("artists", str(n_kept)), ("features", str(n_feat))):
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (k, v))
    conn.commit()
    conn.execute("PRAGMA optimize")
    conn.execute("VACUUM")
    conn.close()

    size_mb = os.path.getsize(out_path) / 1e6
    print(f"\ndone: {n_kept:,} artists, {n_feat:,} features, {n_edge:,} edges "
          f"({n_in:,} read) -> {out_path} ({size_mb:.1f} MB) in {time.time()-t0:.0f}s",
          file=sys.stderr)


def main(argv=None):
    p = argparse.ArgumentParser(description="Build the local artist catalog from a MusicBrainz JSON dump.")
    p.add_argument("--in", dest="in_path", required=True, help="ndjson artist dump path, or - for stdin")
    p.add_argument("--out", dest="out_path", required=True, help="output SQLite catalog path")
    p.add_argument("--keep-all", action="store_true", help="keep metadata-less stub artists too")
    p.add_argument("--max-tags", type=int, default=12, help="cap tags kept per artist (default 12)")
    p.add_argument("--dump-date", default="", help="snapshot label stored in meta")
    a = p.parse_args(argv)
    build(a.in_path, a.out_path, a.keep_all, a.max_tags, a.dump_date)


if __name__ == "__main__":
    main()
