"""Delivery ports.

A *delivery sink* turns a finished list of digest items into something a reader
sees, and returns an optional permalink (None if the medium has no addressable
location). The engine and runner never import a concrete sink — they depend only
on this protocol, so the front end chooses how delivery happens:

    OwuiChatSink   -> creates an OWUI chat (returns /c/<id>)
    StdoutSink     -> prints to the terminal (returns None)
    MarkdownSink   -> writes a .md file (returns file path)
    CallbackSink   -> calls any function you give it (Slack post, Discord webhook…)

A *notifier* is the optional out-of-band ping (ntfy today). Both are tiny on
purpose; adding a new front end is "implement deliver()", nothing more.
"""

from __future__ import annotations

from typing import Callable, Optional, Protocol, runtime_checkable


@runtime_checkable
class DeliverySink(Protocol):
    def deliver(self, subscription: dict, items: list[dict]) -> Optional[str]:
        """Deliver items for one subscription. Return a permalink or None."""
        ...


@runtime_checkable
class Notifier(Protocol):
    def notify(self, subscription: dict, title: str, message: str,
               click_url: Optional[str] = None) -> None:
        ...


class CallbackSink:
    """Wrap any ``fn(subscription, items) -> Optional[str]`` as a sink.

    This is the drop-in seam for new chat front ends: a Slack wrapper constructs
    ``DigestRunner(..., sink=CallbackSink(post_to_slack))`` and never touches the
    core. ``fn`` may return a permalink (passed to the notifier) or None.
    """

    def __init__(self, fn: Callable[[dict, list[dict]], Optional[str]]):
        self._fn = fn

    def deliver(self, subscription: dict, items: list[dict]) -> Optional[str]:
        return self._fn(subscription, items)


class NullNotifier:
    """A notifier that does nothing (the default when no out-of-band ping is wanted)."""

    def notify(self, subscription: dict, title: str, message: str,
               click_url: Optional[str] = None) -> None:
        return None
