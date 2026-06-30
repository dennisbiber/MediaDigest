# Installing MediaDigest

A walkthrough from a clean machine to your first digest. Expect 15–20 minutes, most of it
the one-time catalog download.

## What you need

- A Linux machine (or WSL2) with **Docker** and the **docker compose** plugin.
- **Python 3.12+** on the host (for the installer and the `digest` CLI).
- ~1 GB of free disk for the catalog and containers.
- Optional, for phone notifications: a [Tailscale](https://tailscale.com/) tailnet and an
  [ntfy](https://ntfy.sh/) server. Skip these if you only use MediaDigest on the same
  machine.

## 1. Clone

```bash
git clone https://github.com/dennisbiber/MediaDigest.git
cd MediaDigest
```

## 2. Get the music catalog

The catalog is a single SQLite file of artists derived from MusicBrainz. It's not in the
repo (it's large); download the prebuilt one into a data directory you'll keep:

```bash
mkdir -p ~/mediadigest-data
python scripts/fetch_catalog.py \
  --url https://github.com/dennisbiber/MediaDigest/releases/download/v0.1.0/music_catalog.sqlite \
  --sha256 0117201950a6ede74bf6f77995b53529397ade9eea6410a5b81fa44de9c15fe0 \
  --out ~/mediadigest-data/music_catalog.sqlite
```

The checksum verifies the file is intact. (Prefer to build it yourself from a MusicBrainz
JSON dump? See `python scripts/build_music_catalog.py --help`.)

## 3. Configure

Copy the example environment file and open it:

```bash
cp deploy/.env.example deploy/.env
```

Set what's specific to you. The two that matter for most installs:

- `DIGEST_DATA_HOST=/home/<you>/mediadigest-data` — the host directory holding the catalog
  and the engine's database. The catalog you downloaded must live here.
- `DIGEST_MUSIC_CATALOG_PATH=/data/music_catalog.sqlite` — where the core sees it inside
  the container (`/data` is the mount of `DIGEST_DATA_HOST`).

If you want phone notifications via the Open WebUI front-end, also set
`DIGEST_NTFY_BASE_URL` to your ntfy server. Otherwise leave it unset.

## 4. Run the installer

```bash
python scripts/install.py
```

It discovers the available front-ends and local LLM engines and prompts you to choose. It
stands up the core service, your chosen front-end (e.g. Open WebUI), and a local LLM
(e.g. Ollama) in containers. For the Open WebUI front-end it will also ask for the address
your phone uses to reach it (your Tailscale IP, if you use one) so notification links work.

## 5. Approve the host CLI

The core only accepts an interface after you approve it once — this is what keeps a rogue
container from registering itself.

```bash
pip install -e .
digest core set --url http://localhost:8787 --data-dir ~/mediadigest-data
digest auth approve cli
digest core status        # should report: reachable + approved
```

## 6. Your first digest

```bash
digest sub add "Music" --adapter music --query "Miles Davis, Kraftwerk, Bill Evans"
digest run "Music"
```

You'll get a digest of recommended tracks. If you set up Open WebUI it arrives as a chat
with rating buttons; rate items and future runs adapt to you. Subscriptions you create run
on whatever schedule you give them.

Other sources work the same way — try `--adapter arxiv_hf --query "cat:cs.AI"` for AI
papers or `--adapter news` for headlines.

## Updating later

After pulling new code or changing `deploy/.env`, re-deploy with the helper, which rebuilds
the core image, recreates containers, and reinstalls the CLI in the right order:

```bash
./scripts/redeploy.sh owui ollama
```

To refresh the music catalog to a newer snapshot, re-run `fetch_catalog.py` (or rebuild it)
and `./scripts/redeploy.sh`.

## Tuning the music recommendations

If picks feel too mainstream, the clustering axes are adjustable in `deploy/.env`:

```bash
DIGEST_MUSIC_AXIS_WEIGHTS=genre:0.6,tag:0.5,decade:0.4,area:0.3
DIGEST_MUSIC_TWO_HOP=1
```

Lowering `genre` and raising `decade`/`area` clusters more by era and region; `TWO_HOP`
broadens discovery by stepping out to similar-of-similar artists. Recreate the core after
changing these (`./scripts/redeploy.sh`).

## Troubleshooting

- **Music digest returns nothing** — confirm the catalog is in `DIGEST_DATA_HOST` and that
  `DIGEST_MUSIC_CATALOG_PATH` points at it inside the container
  (`docker exec digest-core printenv | grep CATALOG`).
- **Notifications don't arrive** — confirm `DIGEST_NOTIFIER=ntfy` is set (the Open WebUI
  bundle sets this) and that the core can reach your ntfy server.
- **Changes didn't take effect** — code changes need the core image rebuilt; use
  `./scripts/redeploy.sh`, which does this for you.
