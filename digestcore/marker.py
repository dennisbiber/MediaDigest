"""The per-item feedback marker — one source of truth.

Chat-style delivery sinks (OWUI today; Slack/Discord later) embed an invisible
item identity into each delivered link as a URL fragment::

    [Title](https://example.com/article#digest=<adapter>__<item_id>)

The fragment never renders as visible text (it lives in the link target, not the
label). When a reader reacts to that message, the reaction wrapper parses the
fragment back out to attribute the rating to the right item.

This used to be duplicated: ``add_marker`` lived in delivery, and the parsing
regexes were copy-pasted into both feedback Actions. Both now live here so any
sink and any reaction wrapper share the exact same scheme.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

MARKER_KEY = "digest"
MARKER_SEP = "__"          # separates <adapter> from <item_id>; neither contains it

_MARKER = re.compile(rf"[#&]{MARKER_KEY}=([a-z0-9_]+){MARKER_SEP}([^)\s&]+)")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def add_marker(url: str, adapter: str, item_id: str) -> str:
    """Append the feedback marker to a link as a URL fragment (invisible in render)."""
    sep = "&" if "#" in url else "#"
    return f"{url}{sep}{MARKER_KEY}={adapter}{MARKER_SEP}{item_id}"


@dataclass
class MarkedItem:
    adapter: str
    item_id: str
    title: str = ""
    url: str = ""          # the clean link, marker stripped


def parse_marked_text(text: str) -> Optional[MarkedItem]:
    """Extract item identity from a chat message that carries a marked link.

    Returns None when no digest marker is present. Used by any reaction wrapper
    that receives the message text (e.g. an OWUI Action acting on the chat body).
    """
    if not text:
        return None
    mk = _MARKER.search(text)
    if not mk:
        return None
    adapter, item_id = mk.group(1), mk.group(2)
    link = _LINK.search(text)
    title = link.group(1) if link else ""
    url = _MARKER.sub("", link.group(2)) if link else ""
    return MarkedItem(adapter=adapter, item_id=item_id, title=title, url=url)


def find_marked_message(messages: list[dict]) -> Optional[str]:
    """Given a chat history, return the content of the most recent assistant
    message that carries a digest marker (the acted-on item)."""
    for m in reversed(messages or []):
        if m.get("role") == "assistant" and _MARKER.search(m.get("content", "") or ""):
            return m["content"]
    return None
