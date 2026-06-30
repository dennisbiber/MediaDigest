#!/usr/bin/env python3
"""Download the prebuilt artist catalog so a tester doesn't need the 1.6GB dump or the
build step. Works with any plain-HTTPS host — a GitHub Release asset, a Hugging Face
`resolve/main/...` URL, anything that serves the file.

    python scripts/fetch_catalog.py --url <URL> --out ~/digest-data-live/music_catalog.sqlite
    python scripts/fetch_catalog.py --url <URL> --out <path> --sha256 <hex>   # verified

Skips the download if the target already exists and (when given) matches the checksum,
so it's safe to call from an installer on every run.
"""

import os
import sys
import time
import hashlib
import argparse
import urllib.request

CHUNK = 1 << 20  # 1 MiB


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def verify(path: str, sha256: str) -> bool:
    """True if the file exists and (if a checksum is given) matches it."""
    if not os.path.exists(path):
        return False
    if not sha256:
        return True
    return sha256_of(path).lower() == sha256.lower()


def fetch(url: str, out: str, sha256: str = "", force: bool = False) -> str:
    if not force and verify(out, sha256):
        print(f"catalog already present and valid: {out}")
        return out
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    tmp = out + ".part"
    print(f"downloading catalog from {url}")
    t0 = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": "digest-engine-catalog-fetch"})
    with urllib.request.urlopen(req) as resp, open(tmp, "wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        got = 0
        while True:
            block = resp.read(CHUNK)
            if not block:
                break
            f.write(block)
            got += len(block)
            if total:
                pct = 100 * got / total
                print(f"\r  {got/1e6:.0f}/{total/1e6:.0f} MB ({pct:.0f}%)", end="", file=sys.stderr)
    print(file=sys.stderr)

    if sha256:
        actual = sha256_of(tmp)
        if actual.lower() != sha256.lower():
            os.remove(tmp)
            raise SystemExit(f"checksum mismatch: expected {sha256}, got {actual}. Aborted.")
    os.replace(tmp, out)
    print(f"done: {out} ({os.path.getsize(out)/1e6:.0f} MB) in {time.time()-t0:.0f}s")
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description="Fetch the prebuilt music catalog.")
    p.add_argument("--url", default=os.environ.get("DIGEST_MUSIC_CATALOG_URL", ""),
                   help="catalog URL (or set DIGEST_MUSIC_CATALOG_URL)")
    p.add_argument("--out", required=True, help="destination path for the .sqlite catalog")
    p.add_argument("--sha256", default=os.environ.get("DIGEST_MUSIC_CATALOG_SHA256", ""),
                   help="optional expected sha256 for integrity")
    p.add_argument("--force", action="store_true", help="re-download even if present/valid")
    a = p.parse_args(argv)
    if not a.url:
        raise SystemExit("no --url given (or DIGEST_MUSIC_CATALOG_URL unset).")
    fetch(a.url, a.out, a.sha256, a.force)


if __name__ == "__main__":
    main()
