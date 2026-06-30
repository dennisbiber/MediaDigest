"""Per-interface token bootstrap — the client side of the approval handshake.

Each interface (the CLI here; OWUI Tool/Actions/Pipeline next) runs this on boot: it
generates its own token once, persists it as a local 0600 secret, and registers it
with the core. The core marks it pending until you approve it. Re-registering is
safe and self-healing: an already-approved token stays approved; if the core was
reset and no longer knows the token, registering again lands it pending so you can
re-approve.

The token is the interface's secret — only the interface (and the host, for the CLI)
ever holds it. The core stores only its hash.
"""

from __future__ import annotations

import os
import secrets

from digestcore.client import register, CoreError


def default_token_path(name: str) -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "digest", f"{name}.token")


def _read_token(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def _write_token(path: str, token: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)


def ensure_registered(core_url: str, name: str, scope: str = "write",
                      token_path: str | None = None) -> str:
    """Return this interface's token, creating + registering it if needed.

    Generates and persists a token on first boot, then registers it with the core
    (idempotent). Registration failures (core down) are swallowed — the persisted
    token remains valid, and the next real call surfaces any connectivity error.
    """
    token_path = token_path or default_token_path(name)
    token = _read_token(token_path)
    if not token:
        token = secrets.token_urlsafe(32)
        _write_token(token_path, token)
    try:
        register(core_url, name, token, scope)
    except CoreError:
        pass
    return token
