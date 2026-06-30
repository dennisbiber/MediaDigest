"""Non-chat delivery sinks for CLI / scripting front ends.

None of these talk to a chat service; they render the same item list to a
terminal or a file. Items carry the same fields the OWUI sink uses
(``id``/``title``/``url``/``why``/``adapter``); these sinks surface the
``<adapter>__<item_id>`` identity in plain sight so a ``feedback`` command can
quote it back.
"""

from __future__ import annotations

import os
import json
import datetime as dt
from typing import Optional

from digestcore.marker import MARKER_SEP


def _item_ref(it: dict) -> str:
    return f"{it.get('adapter', '')}{MARKER_SEP}{it['id']}"


def _render_markdown(title: str, items: list[dict]) -> str:
    lines = [f"# {title} — {dt.date.today():%a %d %b %Y}", ""]
    for idx, it in enumerate(items, 1):
        why = f" — {it['why']}" if it.get("why") else ""
        lines.append(f"{idx}. [{it['title']}]({it['url']}){why}")
        lines.append(f"   `{_item_ref(it)}`")
    return "\n".join(lines) + "\n"


class StdoutSink:
    """Print the digest to the terminal. Returns None (no permalink)."""

    def __init__(self, stream=None):
        self._stream = stream  # defaults to print() when None

    def deliver(self, subscription: dict, items: list[dict]) -> Optional[str]:
        text = _render_markdown(subscription.get("name", "Digest"), items)
        if self._stream is not None:
            self._stream.write(text + "\n")
        else:
            print(text)
        return None


class MarkdownSink:
    """Write each run to ``<dir>/<name>-<date>.md``. Returns the file path."""

    def __init__(self, out_dir: str):
        self.out_dir = out_dir

    def deliver(self, subscription: dict, items: list[dict]) -> Optional[str]:
        os.makedirs(self.out_dir, exist_ok=True)
        name = subscription.get("name", "digest").replace("/", "_")
        path = os.path.join(self.out_dir, f"{name}-{dt.date.today():%Y%m%d}.md")
        with open(path, "w") as f:
            f.write(_render_markdown(name, items))
        return path


class JsonSink:
    """Write the raw item list to ``<dir>/<name>-<date>.json``. Returns the path."""

    def __init__(self, out_dir: str):
        self.out_dir = out_dir

    def deliver(self, subscription: dict, items: list[dict]) -> Optional[str]:
        os.makedirs(self.out_dir, exist_ok=True)
        name = subscription.get("name", "digest").replace("/", "_")
        path = os.path.join(self.out_dir, f"{name}-{dt.date.today():%Y%m%d}.json")
        with open(path, "w") as f:
            json.dump({"subscription": name, "date": str(dt.date.today()),
                       "items": items}, f, indent=2, ensure_ascii=False)
        return path
