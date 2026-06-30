"""Build a fixture catalog through the real extractor, then exercise multi-axis
similarity and the catalog adapter without touching the network.

Run:  PYTHONPATH=. python tests/test_music_catalog.py
"""
import os
import sys
import json
import sqlite3
import tempfile
import subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from digestcore.adapters.catalog_store import (
    open_catalog, resolve_seeds, similar_artists, artist_rows, norm_name)
from digestcore.adapters.music_catalog import MusicCatalogAdapter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def A(mbid, name, genres=(), decade_year=None, area="", tags=(), rels=()):
    doc = {"id": mbid, "name": name, "sort-name": name,
           "genres": [{"name": g} for g in genres],
           "tags": [{"name": t} for t in tags]}
    if decade_year:
        doc["life-span"] = {"begin": str(decade_year)}
    if area:
        doc["area"] = {"name": area}
    if rels:
        doc["relations"] = [{"type": "collaboration", "artist": {"id": r}} for r in rels]
    return doc


FIX = [
    A("m-miles", "Miles Davis", genres=["jazz"], decade_year=1945, area="United States",
      rels=["m-coltrane"]),
    A("m-coltrane", "John Coltrane", genres=["jazz"], decade_year=1955, area="United States"),
    A("m-evans", "Bill Evans", genres=["jazz"], decade_year=1956, area="United States"),
    A("m-ella", "Ella Fitzgerald", genres=["jazz", "vocal jazz"], decade_year=1935, area="United States"),
    A("m-kraftwerk", "Kraftwerk", genres=["electronic"], decade_year=1970, area="Germany"),
    A("m-tangerine", "Tangerine Dream", genres=["electronic"], decade_year=1970, area="Germany"),
    A("m-moroder", "Giorgio Moroder", genres=["disco"], decade_year=1972, area="Italy"),
    A("m-deadmau5", "Deadmau5", genres=["electronic"], decade_year=2005, area="Canada"),
    A("m-vivaldi", "Antonio Vivaldi", genres=["baroque"], decade_year=1705, area="Italy"),
    {"id": "m-stub", "name": "Nameless Stub"},  # no signal -> should be pruned
]


def build_fixture(tmp):
    ndjson = os.path.join(tmp, "artists.ndjson")
    with open(ndjson, "w") as f:
        for d in FIX:
            f.write(json.dumps(d) + "\n")
    out = os.path.join(tmp, "music_catalog.sqlite")
    r = subprocess.run(
        [sys.executable, os.path.join(ROOT, "scripts", "build_music_catalog.py"),
         "--in", ndjson, "--out", out],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return out


def rank_names(conn, seeds, **kw):
    ids, feats = resolve_seeds(conn, seeds)
    ranked = similar_artists(conn, ids, seed_features=feats, **kw)
    rows = artist_rows(conn, [a for a, _ in ranked])
    return [rows[a]["name"] for a, _ in ranked if a in rows]


def test_large_fanout(tmp):
    """A seed whose genre fans out to >999 artists must not trip SQLite's variable limit."""
    path = os.path.join(tmp, "big.sqlite")
    if os.path.exists(path):
        os.remove(path)
    from digestcore.adapters.catalog_store import init_schema, norm_name as nn
    conn = sqlite3.connect(path)
    init_schema(conn)
    conn.execute("INSERT INTO artist(id,mbid,name,name_norm) VALUES(1,'s','Seed',?)", (nn("Seed"),))
    conn.execute("INSERT INTO feature(artist_id,axis,value) VALUES(1,'genre','rock')")
    for i in range(2, 2002):  # 2000 other rock artists -> well over the 999 bound
        conn.execute("INSERT INTO artist(id,mbid,name,name_norm) VALUES(?,?,?,?)",
                     (i, f"m{i}", f"Band {i}", nn(f"Band {i}")))
        conn.execute("INSERT INTO feature(artist_id,axis,value) VALUES(?, 'genre','rock')", (i,))
    conn.commit(); conn.close()

    c = open_catalog(path, read_only=True)
    ids, feats = resolve_seeds(c, ["Seed"])
    ranked = similar_artists(c, ids, seed_features=feats, limit=200)  # must not raise
    rows = artist_rows(c, [a for a, _ in ranked])                     # chunked too
    c.close()
    assert len(ranked) == 200 and len(rows) == 200, (len(ranked), len(rows))


def main():
    tmp = tempfile.mkdtemp(prefix="mbcat-")
    out = build_fixture(tmp)
    conn = open_catalog(out, read_only=True)

    # stub artist with no genre/tag/relation was pruned
    n = conn.execute("SELECT COUNT(*) c FROM artist").fetchone()["c"]
    assert n == 9, f"expected 9 kept (stub pruned), got {n}"
    # derived axes landed
    kr = conn.execute("SELECT decade, area FROM artist WHERE mbid='m-kraftwerk'").fetchone()
    assert kr["decade"] == "1970s" and kr["area"] == "Germany", dict(kr)

    # seed Miles -> jazz neighbors, NOT electronic/baroque; Coltrane top (shared genre + edge)
    names = rank_names(conn, ["Miles Davis"])
    assert "Miles Davis" not in names, "seed must be excluded"
    assert names and names[0] == "John Coltrane", f"Coltrane should top (genre+edge): {names}"
    assert {"John Coltrane", "Bill Evans", "Ella Fitzgerald"} <= set(names), names
    assert "Kraftwerk" not in names and "Antonio Vivaldi" not in names, names

    # a genre seed ('jazz') expands to the jazz artists
    jz = rank_names(conn, ["jazz"])
    assert "John Coltrane" in jz and "Kraftwerk" not in jz, jz

    # homonym resolution: a duplicate empty 'Miles Davis' must not shadow the real one
    conn.close()
    w = sqlite3.connect(out)
    w.execute("INSERT INTO artist(mbid,name,name_norm) VALUES('dup','Miles Davis',?)",
              (norm_name("Miles Davis"),))   # blank homonym, zero features
    w.commit(); w.close()
    conn = open_catalog(out, read_only=True)
    ids, _ = resolve_seeds(conn, ["Miles Davis"])
    picked = artist_rows(conn, ids)[ids[0]]
    assert picked["mbid"] == "m-miles", f"should pick the rich homonym, got {picked['mbid']}"
    assert rank_names(conn, ["Miles Davis"])[0] == "John Coltrane", "still resolves correctly"

    # axis weighting changes clustering: seed Kraftwerk. Under genre-heavy default,
    # same-genre Tangerine Dream outranks same-decade-only Moroder. Crank decade and
    # drop genre, and Moroder (shares 1970s) climbs above where it was.
    default_order = rank_names(conn, ["Kraftwerk"])
    assert default_order.index("Tangerine Dream") < default_order.index("Giorgio Moroder"), default_order
    decade_order = rank_names(conn, ["Kraftwerk"],
                              axis_weights={"genre": 0.0, "decade": 1.0, "area": 0.0, "tag": 0.0})
    assert decade_order.index("Giorgio Moroder") < default_order.index("Giorgio Moroder"), \
        f"decade weighting should lift Moroder: default={default_order} decade={decade_order}"

    conn.close()

    # ---- adapter, with a stub linker (no network) ----
    os.environ["DIGEST_MUSIC_CATALOG_PATH"] = out
    stub = lambda artist: ("Some Track", f"http://example/{norm_name(artist)}")
    ad = MusicCatalogAdapter(linker=stub)

    cands = ad.fetch_candidates("Miles Davis", 7, context={})
    assert cands, "adapter returned nothing"
    titles = [c.title for c in cands]
    assert any("John Coltrane" in t for t in titles), titles
    assert all("taste_match" in c.signals for c in cands)
    assert all(c.url.startswith("http://example/") for c in cands), "stub linker not used"

    # negative feedback excludes an artist
    fb = sqlite3.connect(":memory:"); fb.row_factory = sqlite3.Row
    fb.execute("CREATE TABLE feedback(uuid TEXT, adapter TEXT, signal TEXT, title TEXT, ts INTEGER)")
    fb.execute("INSERT INTO feedback VALUES('u','music','down','John Coltrane — x', 1)")
    fb.commit()
    cands2 = ad.fetch_candidates("Miles Davis", 7, context={"db": fb, "uuid": "u"})
    assert not any("John Coltrane" in c.title for c in cands2), "down-voted artist should be filtered"

    # missing catalog -> graceful empty
    os.environ["DIGEST_MUSIC_CATALOG_PATH"] = os.path.join(tmp, "nope.sqlite")
    assert ad.fetch_candidates("Miles Davis", 7, context={}) == []

    test_large_fanout(tmp)

    print("PASS — extractor builds the catalog (stub pruned, axes derived), multi-axis "
          "similarity clusters correctly and re-clusters when axis weights change, and the "
          "adapter produces taste_match candidates, honors negative feedback, and degrades "
          "without a catalog.")


if __name__ == "__main__":
    main()
