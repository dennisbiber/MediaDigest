"""Configuration authority for the digest engine.

This replaces OWUI ``Valves`` as the *source of truth* for settings. Every front
end is a thin wrapper that surfaces these fields in its own idiom — OWUI exposes
them as Valves, the CLI reads them from a TOML file / env / flags, a Slack app
would read them from its app-config screen — but they all resolve to one
``Config`` object that the engine, adapters, delivery sinks and services consume.

Field *names* are intentionally identical to the original Valves so the moved
engine/adapter code (which does ``self.v.OLLAMA_BASE_URL`` etc.) needs no edits.

Precedence, lowest to highest: model defaults < TOML file < ``DIGEST_*`` env
vars < explicit overrides passed to ``load()``.
"""

from __future__ import annotations

import os
import tomllib
from typing import Optional

from pydantic import BaseModel


class Config(BaseModel):
    # --- shared infrastructure ---
    DB_PATH: str = "/data/digest.db"
    DATA_DIR: str = "/data"               # runtime sources/options live here
    OLLAMA_BASE_URL: str = "http://host.docker.internal:11434"
    MEM0_BASE_URL: str = ""               # blank disables mem0 recall
    NTFY_BASE_URL: str = "http://host.docker.internal:5555"

    # --- ranking / judging ---
    JUDGE_MODEL: str = "qwen2.5:14b"
    EMBED_MODEL: str = "nomic-embed-text"
    AUDIENCE_CONTEXT: str = "a general reader"
    PREF_WEIGHT: float = 1.0
    SHORTLIST_SIZE: int = 20
    DEFAULT_TZ: str = "UTC"

    # --- adapter credentials ---

    # --- OWUI delivery-sink settings (ignored by non-OWUI sinks) ---
    # Which front-end the core delivers digests to: "owui" today; "none" for a
    # no-op (testing). Slack/Discord sinks slot in here later.
    DELIVERY_SINK: str = "owui"
    # How the user is pinged a digest is ready — a front-end concern. "none" by default
    # (chat-native front-ends are their own notification); the OWUI bundle sets "ntfy".
    NOTIFIER: str = "none"
    # Keyless music catalog (the 'music_catalog' adapter). Path defaults under DATA_DIR.
    MUSIC_CATALOG_PATH: str = ""
    MUSIC_AXIS_WEIGHTS: str = "genre:1.0,tag:0.5,decade:0.3,area:0.25"
    MUSIC_TWO_HOP: bool = False
    OWUI_BASE_URL: str = "http://host.docker.internal:3000"
    OWUI_PUBLIC_URL: str = ""
    # DISPLAY_MODEL must be a REAL OWUI model id: the 👍/👎 rating buttons render from
    # this model's actions, so a non-existent id (the old "digest") shows no buttons.
    # Defaults to the model the installer creates, which it also binds the actions to.
    DISPLAY_MODEL: str = "digest-setup-assistant"

    # ---------- loading ----------
    @classmethod
    def load(cls, path: Optional[str] = None, **overrides) -> "Config":
        """Build a Config by layering TOML file, ``DIGEST_*`` env vars, overrides."""
        data: dict = {}
        toml_path = path or os.environ.get("DIGEST_CONFIG") or _default_config_path()
        if toml_path and os.path.exists(toml_path):
            with open(toml_path, "rb") as f:
                data.update(tomllib.load(f))
        for field in cls.model_fields:
            env_val = os.environ.get(f"DIGEST_{field}")
            if env_val is not None:
                data[field] = env_val          # pydantic coerces int/float
        data.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**data)

    @classmethod
    def from_valves(cls, valves) -> "Config":
        """Bridge an OWUI Valves object (or any attr bag) into a Config."""
        data = {}
        for field in cls.model_fields:
            if hasattr(valves, field):
                data[field] = getattr(valves, field)
        return cls(**data)

    def apply_env(self) -> None:
        """Export the data dir so adapter loaders that read it lazily agree with us."""
        os.environ.setdefault("DIGEST_DATA_DIR", self.DATA_DIR)

    def to_toml(self) -> str:
        lines = []
        for field, value in self.model_dump().items():
            if isinstance(value, str):
                esc = value.replace("\\", "\\\\").replace('"', '\\"')
                lines.append(f'{field} = "{esc}"')
            elif isinstance(value, bool):
                lines.append(f"{field} = {str(value).lower()}")
            else:
                lines.append(f"{field} = {value}")
        return "\n".join(lines) + "\n"

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "w") as f:
            f.write(self.to_toml())


def _default_config_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "digest", "config.toml")
