"""Shared network-failure vocabulary for adapters.

Two small pieces, deliberately dependency-light:

* ``AdapterRetryable`` — the signal an adapter raises when a *transport* failure
  (DNS, refused, timeout) means it can't produce trustworthy output this run.
  The runner reports it as a passive retry: nothing is delivered, nothing is
  marked sent, and the next scheduled run tries again. It is distinct from a
  plain ``Exception`` (a real bug, reported as a hard error) and from an empty
  result (the source was reachable and simply had nothing).

* ``is_transport_error`` — decides whether an exception is a connectivity
  failure as opposed to an application-level problem. The bias is conservative:
  only things that are *unambiguously* transport count, so a lookup that merely
  found nothing is never mistaken for the network being down.
"""

from __future__ import annotations

import socket

try:                                    # requests is a core dep, but stay defensive
    import requests
    _REQUESTS_TRANSPORT: tuple = (
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
    )
except Exception:                       # pragma: no cover - requests always present in prod
    _REQUESTS_TRANSPORT = ()

_STDLIB_TRANSPORT: tuple = (socket.gaierror, socket.timeout, ConnectionError, TimeoutError)

# Fallback markers for when a library wraps a transport failure in its own
# exception type (e.g. ytmusicapi surfacing a requests ConnectionError). Matched
# against the message across the __cause__/__context__ chain.
_TRANSPORT_MARKERS = (
    "nameresolution", "name or service not known", "failed to resolve",
    "temporary failure in name resolution", "no address associated with hostname",
    "getaddrinfo", "max retries exceeded", "connection refused", "connection reset",
    "connection aborted", "network is unreachable", "timed out",
)


class AdapterRetryable(Exception):
    """A transport failure prevented trustworthy output this run; retry later."""


def is_transport_error(exc: BaseException | None) -> bool:
    """True iff ``exc`` (or something it wraps) is a connectivity failure.

    Walks the ``__cause__``/``__context__`` chain so a library-wrapped requests
    error is still recognized. Returns False for ordinary application errors,
    so a genuine "nothing found" is never read as a network outage.
    """
    seen: set[int] = set()
    e: BaseException | None = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if _REQUESTS_TRANSPORT and isinstance(e, _REQUESTS_TRANSPORT):
            return True
        if isinstance(e, _STDLIB_TRANSPORT):
            return True
        msg = str(e).lower()
        if any(m in msg for m in _TRANSPORT_MARKERS):
            return True
        e = e.__cause__ or e.__context__
    return False
