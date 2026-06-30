"""Normalized item every adapter produces, and the adapter contract."""

import datetime as dt
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Candidate:
    id: str
    title: str
    url: str
    summary: str = ""
    published: Optional[dt.datetime] = None
    signals: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)


class SourceAdapter(ABC):
    # Per-signal weights used by the shared scorer. Each adapter declares which
    # of its Candidate.signals matter and how much.
    signal_weights: dict = {}

    @abstractmethod
    def fetch_candidates(self, topic: str, window_days: int,
                         context: Optional[dict] = None) -> list[Candidate]:
        # context (optional) carries per-user info for adapters that personalize at
        # fetch time: {"uuid", "db", "profile", "valves"}. Most adapters ignore it.
        ...