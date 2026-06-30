#!/usr/bin/env python3
"""Scan a source tree for things that must never be published: API keys, private IPs,
user UUIDs, home-directory paths, and committed .env files. Exits non-zero if any are
found, so it can gate a push or the production build.

    python scripts/check_secrets.py [root]   # default root: current dir
"""

import os
import re
import sys

# (label, compiled pattern). Tuned to catch the specific shapes that show up in this
# project's configs and command snippets without drowning in false positives.
PATTERNS = [
    ("API key (sk-...)",        re.compile(r"\bsk-[A-Za-z0-9]{16,}")),
    ("Last.fm-style 32-hex key", re.compile(r"\b[a-f0-9]{32}\b")),
    ("Tailscale IP (100.64/10)", re.compile(r"\b100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b")),
    ("private IP (10.x)",        re.compile(r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")),
    ("private IP (192.168.x)",   re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}\b")),
    ("UUID",                     re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")),
    ("home path (/home/<user>)", re.compile(r"/home/(?!claude\b)[A-Za-z0-9_]+")),
]

# files/dirs we never scan (binaries, vendored, the example file itself may show shapes)
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", "build", "dist"}
SKIP_EXT = {".sqlite", ".db", ".db-wal", ".db-shm", ".pyc", ".tar", ".gz", ".bz2", ".xz",
            ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip"}
# lines carrying these markers are placeholders/docs, not real secrets
ALLOW_MARKERS = ("example", "placeholder", "your_", "<", "xxxx", "0000", "host.docker.internal")


def scan_file(path):
    hits = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for lineno, line in enumerate(f, 1):
                low = line.lower()
                if any(m in low for m in ALLOW_MARKERS):
                    continue
                for label, pat in PATTERNS:
                    m = pat.search(line)
                    if m:
                        hits.append((lineno, label, m.group(0)))
    except OSError:
        pass
    return hits


def main(argv=None):
    argv = argv or sys.argv[1:]
    root = argv[0] if argv else "."
    found = []
    if os.path.isfile(root):
        for lineno, label, snippet in scan_file(root):
            found.append((root, lineno, label, snippet))
        walk_root = None
    else:
        walk_root = root
    for dirpath, dirnames, filenames in os.walk(walk_root or "."):
        if walk_root is None:
            break
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in SKIP_EXT:
                continue
            # a committed real .env is itself a finding
            if fn == ".env":
                found.append((os.path.join(dirpath, fn), 0, "committed .env file", fn))
                continue
            p = os.path.join(dirpath, fn)
            for lineno, label, snippet in scan_file(p):
                found.append((p, lineno, label, snippet))

    if found:
        print("SECRETS DETECTED — do not publish:\n", file=sys.stderr)
        for path, lineno, label, snippet in found:
            rel = os.path.relpath(path, root)
            where = f"{rel}:{lineno}" if lineno else rel
            print(f"  {where}  [{label}]  {snippet}", file=sys.stderr)
        print(f"\n{len(found)} potential secret(s). Scrub or gitignore before pushing.",
              file=sys.stderr)
        return 1
    print("clean — no secrets detected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
