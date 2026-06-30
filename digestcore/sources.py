"""Source/option JSON loading (front-end agnostic).

Two layers:

* **Shipped defaults** live next to the code in ``digestcore/data`` and are
  read-only. They define the out-of-the-box outlet allowlist and the per-adapter
  setup options.
* **Runtime copies** live under a writable *data dir* and are the live, editable
  files. On first use the defaults are seeded into the data dir; after that,
  editing the runtime file needs no code change, and editing the default only
  affects fresh installs.

The data dir was historically hard-wired to ``/data`` (the OWUI shared volume).
It is now resolved from, in order: an explicit argument, the ``DIGEST_DATA_DIR``
environment variable, then ``/data``. A workstation CLI install simply points
``DIGEST_DATA_DIR`` at something like ``~/.local/share/digest`` and the rest is
unchanged.
"""

import os
import json

# Shipped defaults (read-only, live next to the code).
_PKG_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DEFAULT_OPTIONS_PATH = os.path.join(_PKG_DATA_DIR, "options.default.json")
DEFAULT_SOURCES_PATH = os.path.join(_PKG_DATA_DIR, "sources.default.json")


def data_dir(explicit: str | None = None) -> str:
    """Resolve the writable runtime data dir (explicit > env > /data)."""
    return explicit or os.environ.get("DIGEST_DATA_DIR") or "/data"


def options_path(dir_: str | None = None) -> str:
    return os.path.join(data_dir(dir_), "digest_options.json")


def sources_path(dir_: str | None = None) -> str:
    return os.path.join(data_dir(dir_), "digest_sources.json")


def _read(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def _seed_from_default(runtime_path: str, default_path: str):
    """Copy a shipped default into the runtime location if it isn't there yet.

    Best-effort: if the data dir isn't writable (e.g. the container's /data default on
    a host with no such writable path), silently skip — the loaders fall back to the
    packaged defaults, so read commands like `options` still work instead of crashing."""
    if os.path.exists(runtime_path):
        return
    try:
        os.makedirs(os.path.dirname(runtime_path) or ".", exist_ok=True)
        with open(runtime_path, "w") as f:
            json.dump(_read(default_path), f, indent=2)
    except OSError:
        return


def seed_runtime_files(dir_: str | None = None):
    _seed_from_default(options_path(dir_), DEFAULT_OPTIONS_PATH)
    _seed_from_default(sources_path(dir_), DEFAULT_SOURCES_PATH)


def _load(runtime_path: str, default_path: str) -> dict:
    try:
        return _read(runtime_path)
    except (OSError, ValueError):
        return _read(default_path)


def load_options(dir_: str | None = None) -> dict:
    return _load(options_path(dir_), DEFAULT_OPTIONS_PATH)


def load_sources(dir_: str | None = None) -> dict:
    return _load(sources_path(dir_), DEFAULT_SOURCES_PATH)


def default_news_options() -> dict:
    return _read(DEFAULT_OPTIONS_PATH)["adapters"]["news"]


def _localname(tag: str) -> str:
    """Strip an XML namespace from a tag, lowercased (used by the news adapter)."""
    return tag.rsplit("}", 1)[-1].lower()
