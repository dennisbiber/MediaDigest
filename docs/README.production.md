# Digest Engine

A self-hosted, local-first digest and recommendation engine. It runs on your own
hardware, owns its own data, and depends on no paid API or external account to work.

## Why this exists

Most recommendation systems are rented. They live on someone else's servers, require an
account and an API key, lock your taste profile inside a product you don't control, and —
increasingly — pad their results with AI-generated filler. This project is built on the
opposite premises:

- **Local-first and self-owned.** The core, the database, and the recommendation data all
  live on your machine. Nothing phones home. If the upstream services disappeared
  tomorrow, your install would keep working.
- **No keys, no accounts.** The music recommender is built from a downloadable, public-domain
  catalog, not a keyed third-party API. You install it and it works.
- **No AI-generated music.** Recommendations are drawn only from real, catalogued artists.
  The system never surfaces synthetic or AI-generated "artists."
- **Non-profit data sources.** The artist catalog is derived from MusicBrainz, maintained
  by the non-profit MetaBrainz Foundation, rather than any for-profit recommender.

It aggregates from multiple sources — research papers, news, and music discovery — on a
schedule you set, judges items against your interests with a local LLM, and delivers a
digest you can rate to refine future runs.

## How the music recommender works

Instead of calling a similarity API, the engine queries a local catalog: a single SQLite
file derived from a MusicBrainz data dump, holding every artist that carries real curated
metadata (genres, tags, era, region, and relationships). Similarity is computed from
overlap across those axes plus relationship edges — so it covers the deep catalog, from
classical to contemporary, not just whatever was streamed recently. Your thumbs-up/down
feedback reshapes how the catalog is traversed for you over time.

The catalog is a build artifact, not part of this repository (it's large, and it's
regenerated from upstream dumps). You have two ways to get it:

1. **Download the prebuilt catalog** (recommended for most people):

   ```bash
   python scripts/fetch_catalog.py --url <CATALOG_URL> --out ~/digest-data-live/music_catalog.sqlite
   ```

2. **Build it yourself** from a MusicBrainz JSON artist dump (see
   `scripts/build_music_catalog.py --help`). This is what produces the prebuilt file, and
   it's how you'd refresh the catalog as a snapshot "as of" a newer dump.

## Quick start

See `INSTALL.md` for the full walkthrough. In brief: copy `deploy/.env.example` to
`deploy/.env` and fill in what's specific to you, fetch or build the catalog, then run the
installer, which stands up the core plus your chosen front-end and local LLM in
containers.

## Honest limitations

This is beta software, and the recommender has real edges worth knowing:

- **Freshness lag.** The catalog is a snapshot. A brand-new artist won't appear until you
  rebuild or re-download from a newer dump.
- **Mainstream skew.** Out of the box, very well-tagged artists can crowd out subtler
  picks. The clustering axes are tunable (`DIGEST_MUSIC_AXIS_WEIGHTS`) to push toward
  relationships or era over raw genre popularity.
- **Deep-catalog coverage varies.** Obscure or sparsely-tagged artists have thinner
  similarity signal than popular ones.

## Attribution and data licensing

Artist data is derived from [MusicBrainz](https://musicbrainz.org/), maintained by the
[MetaBrainz Foundation](https://metabrainz.org/). MusicBrainz core data is released into
the public domain (CC0); some supplementary data is under CC BY-NC-SA. Please review and
respect [MetaBrainz's licensing](https://metabrainz.org/datasets) when redistributing any
derived catalog. This project is not affiliated with or endorsed by MetaBrainz.

## License

See `LICENSE`.
