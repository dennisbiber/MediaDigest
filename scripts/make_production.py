#!/usr/bin/env python3
"""Generate a clean, publishable production tree from this (development) repo.

The production repo is a build artifact, never hand-edited: this strips the Last.fm
adapter, swaps in placeholder config, installs the production README, and refuses to
finish if the secret scanner finds anything publishable. Run it from the dev repo root:

    python scripts/make_production.py --out ../digest-engine-public

Then review, `git init` the output, add a LICENSE, and push.
"""

import os
import sys
import shutil
import argparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# directories/files never copied into the public tree
EXCLUDE_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", "build", "dist",
                "transcripts", "mnt"}
EXCLUDE_NAMES = {".env", "admin.token", "journal.txt"}
EXCLUDE_EXT = {".sqlite", ".db", ".db-wal", ".db-shm", ".pyc", ".tar", ".gz", ".bz2",
               ".xz", ".egg-info", ".log", ".part"}

PROD_REGISTRY = '''"""Adapter registry. Add a new source by implementing SourceAdapter and listing it here."""

from digestcore.models import SourceAdapter
from digestcore.adapters.arxiv_hf import ArxivHFAdapter
from digestcore.adapters.rss_news import RssNewsAdapter
from digestcore.adapters.music_catalog import MusicCatalogAdapter

ADAPTERS: dict[str, SourceAdapter] = {
    "arxiv_hf": ArxivHFAdapter(),
    "news": RssNewsAdapter(),
    "music": MusicCatalogAdapter(),   # keyless local catalog
}

__all__ = ["ADAPTERS", "ArxivHFAdapter", "RssNewsAdapter", "MusicCatalogAdapter"]
'''

PROD_ENV_EXAMPLE = '''# Core overrides (DIGEST_<Config field>). The installer writes most of these for you.
#
# Set by hand only what's specific to you:
#
# ntfy server the core pushes to (OWUI front-end only; the core has a host-gateway alias
# so host.docker.internal works for a host-run ntfy):
# DIGEST_NTFY_BASE_URL=http://host.docker.internal:5555
#
# Music catalog: download the prebuilt file with scripts/fetch_catalog.py, or build your
# own with scripts/build_music_catalog.py. Point the core at it:
# DIGEST_MUSIC_CATALOG_PATH=/data/music_catalog.sqlite
DIGEST_MUSIC_CATALOG_URL=https://github.com/dennisbiber/MediaDigest/releases/download/v0.1.0/music_catalog.sqlite
DIGEST_MUSIC_CATALOG_SHA256=0117201950a6ede74bf6f77995b53529397ade9eea6410a5b81fa44de9c15fe0
# DIGEST_MUSIC_AXIS_WEIGHTS=genre:1.0,tag:0.5,decade:0.3,area:0.25
'''


def _included(path: str) -> bool:
    name = os.path.basename(path)
    if name in EXCLUDE_NAMES:
        return False
    base, ext = os.path.splitext(name)
    if ext in EXCLUDE_EXT:
        return False
    if name.endswith(".env") and not name.endswith(".env.example"):
        return False
    return True


def copy_tree(src: str, dst: str):
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS
                       and not d.endswith(".egg-info")]
        rel = os.path.relpath(dirpath, src)
        out_dir = os.path.join(dst, rel) if rel != "." else dst
        os.makedirs(out_dir, exist_ok=True)
        for fn in filenames:
            sp = os.path.join(dirpath, fn)
            if _included(sp):
                shutil.copy2(sp, os.path.join(out_dir, fn))


def strip_lastfm(dst: str):
    # remove the adapter module
    mus = os.path.join(dst, "digestcore/adapters/music.py")
    if os.path.exists(mus):
        os.remove(mus)
    # replace the registry with the Last.fm-free version
    with open(os.path.join(dst, "digestcore/adapters/__init__.py"), "w") as f:
        f.write(PROD_REGISTRY)
    # drop the LASTFM_API_KEY config field
    cfg = os.path.join(dst, "digestcore/config.py")
    lines = [ln for ln in open(cfg) if "LASTFM_API_KEY" not in ln]
    open(cfg, "w").writelines(lines)


def install_docs(dst: str):
    prod_readme = os.path.join(dst, "docs/README.production.md")
    if os.path.exists(prod_readme):
        shutil.copy2(prod_readme, os.path.join(dst, "README.md"))
    prod_install = os.path.join(dst, "docs/INSTALL.production.md")
    if os.path.exists(prod_install):
        shutil.copy2(prod_install, os.path.join(dst, "INSTALL.md"))
    with open(os.path.join(dst, "deploy/.env.example"), "w") as f:
        f.write(PROD_ENV_EXAMPLE)
    if not os.path.exists(os.path.join(dst, "LICENSE")):
        with open(os.path.join(dst, "LICENSE"), "w") as f:
            f.write("Add a license here before publishing. See README.md.\n")


def main(argv=None):
    p = argparse.ArgumentParser(description="Build the public production tree.")
    p.add_argument("--out", required=True, help="target directory for the production repo")
    p.add_argument("--force", action="store_true", help="overwrite the target if it exists")
    a = p.parse_args(argv)

    dst = os.path.abspath(a.out)
    # Preserve version control and a license you've already added, so you can point this
    # at a cloned repo and just review/commit the diff on every regeneration.
    PRESERVE = {".git", "LICENSE", ".github"}
    if os.path.exists(dst):
        if not a.force and os.listdir(dst):
            raise SystemExit(f"{dst} exists and is non-empty; pass --force to refresh it.")
        for entry in os.listdir(dst):
            if entry in PRESERVE:
                continue
            p = os.path.join(dst, entry)
            shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)

    print(f"building production tree at {dst}")
    copy_tree(ROOT, dst)
    strip_lastfm(dst)
    install_docs(dst)

    # sanity: the production package must import without the Last.fm adapter
    import subprocess
    r = subprocess.run([sys.executable, "-c",
                        "import sys; sys.path.insert(0, '.'); "
                        "from digestcore.adapters import ADAPTERS; "
                        "assert 'music' in ADAPTERS and 'music_catalog' not in ADAPTERS; "
                        "import digestcore.config as c; "
                        "assert not hasattr(c.Config(), 'LASTFM_API_KEY')"],
                       cwd=dst, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"production tree failed import sanity check:\n{r.stderr}")
    print("  import sanity OK (music_catalog registered as 'music', no Last.fm, no LASTFM key)")

    # gate on the secret scanner
    scan = subprocess.run([sys.executable, os.path.join(dst, "scripts/check_secrets.py"), dst],
                          capture_output=True, text=True)
    sys.stderr.write(scan.stderr)
    print(scan.stdout.strip())
    if scan.returncode != 0:
        raise SystemExit("secret scan FAILED — production tree not safe to publish.")

    print(f"\nproduction tree ready: {dst}\n"
          "next: review it, add a LICENSE, `git init && git add . && git commit`, push.")


if __name__ == "__main__":
    main()
