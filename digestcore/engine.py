"""Shared core: embedding-based preference scoring, an LLM judge, and digest assembly.
Everything here is domain-agnostic; the adapters supply the candidates and signals."""

import json
import math
import time
import sqlite3
from typing import Optional

import requests

from digestcore.models import Candidate
from digestcore.adapters import ADAPTERS


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def _minmax(values):
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return [0.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


class DigestEngine:
    def __init__(self, valves, db: sqlite3.Connection):
        self.v = valves
        self.db = db

    def _already_sent(self, uuid_: str, sub: str, item_id: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM sent WHERE uuid=? AND sub_name=? AND item_id=?", (uuid_, sub, item_id)
        ).fetchone() is not None

    def _record_sent(self, uuid_: str, sub: str, ids: list[str]):
        now = int(time.time())
        self.db.executemany("INSERT OR IGNORE INTO sent VALUES (?,?,?,?)",
                            [(uuid_, sub, i, now) for i in ids])
        self.db.commit()

    def _embed(self, text: str) -> Optional[list[float]]:
        if not self.v.EMBED_MODEL:
            return None
        try:
            resp = requests.post(f"{self.v.OLLAMA_BASE_URL}/api/embeddings",
                                 json={"model": self.v.EMBED_MODEL, "prompt": text[:4000]}, timeout=30)
            resp.raise_for_status()
            return resp.json().get("embedding")
        except requests.RequestException:
            return None

    def _judge(self, profile: str, shortlist: list[Candidate], n: int, audience: str = "") -> list[dict]:
        catalog = [{"id": c.id, "title": c.title, "summary": c.summary[:600], "signals": c.signals}
                   for c in shortlist]
        prompt = (
            f"A reader's interests/profile: {profile or '(no profile on file)'}\n"
            f"Audience: {audience or 'a general reader'}.\n\n"
            f"From the candidates below, choose the {n} the audience would most value, ordered best-first. "
            f"Strongly favor substantive developments of broad significance. "
            f"If the profile names a location, give some preference to news relevant to that place. "
            f"Avoid trivial items and reaction / opinion / 'who-said-what' pieces unless of major importance. "
            f"Judge only on the candidates given; never invent items.\n"
            f"Return STRICT JSON only: {{\"picks\":[{{\"id\":\"...\",\"why\":\"one sentence\"}}]}}\n\n"
            f"CANDIDATES:\n{json.dumps(catalog, ensure_ascii=False)}"
        )
        try:
            resp = requests.post(f"{self.v.OLLAMA_BASE_URL}/v1/chat/completions",
                                 json={"model": self.v.JUDGE_MODEL,
                                       "messages": [{"role": "user", "content": prompt}],
                                       "temperature": 0.2}, timeout=120)
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"].strip()
        except (requests.RequestException, KeyError, IndexError):
            return []
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            return json.loads(text).get("picks", [])
        except json.JSONDecodeError:
            return []

    def build_digest(self, sub: dict, profile: str) -> list[dict]:
        adapter = ADAPTERS[sub["adapter"]]
        context = {"uuid": sub["uuid"], "db": self.db, "profile": profile, "valves": self.v}
        candidates = adapter.fetch_candidates(sub["topic_query"], sub["window_days"], context)
        candidates = [c for c in candidates if not self._already_sent(sub["uuid"], sub["name"], c.id)]
        if not candidates:
            return []

        if profile.strip():
            prof_vec = self._embed(profile)
            pref = []
            for c in candidates:
                if prof_vec:
                    cv = self._embed(f"{c.title}. {c.summary}")
                    pref.append(_cosine(prof_vec, cv) if cv else 0.0)
                else:
                    terms = {w.lower() for w in profile.split() if len(w) > 3}
                    blob = f"{c.title} {c.summary}".lower()
                    pref.append(sum(t in blob for t in terms) / (len(terms) or 1))
            pref = _minmax(pref)
        else:
            pref = [0.0] * len(candidates)

        sig_norm = {sig: _minmax([float(c.signals.get(sig, 0)) for c in candidates])
                    for sig in adapter.signal_weights}

        scored = []
        for i, c in enumerate(candidates):
            score = self.v.PREF_WEIGHT * pref[i]
            for sig, w in adapter.signal_weights.items():
                score += w * sig_norm[sig][i]
            scored.append((score, c))
        scored.sort(key=lambda x: x[0], reverse=True)

        shortlist = [c for _, c in scored[: self.v.SHORTLIST_SIZE]]
        picks = self._judge(profile, shortlist, sub["n"], self.v.AUDIENCE_CONTEXT)
        why_by = {p["id"]: p.get("why", "") for p in picks if p.get("id")}

        # delivery order: the judge's picks first (in its order), then everything
        # else by score as backfill.
        ranked = [c for _, c in scored]
        by_id = {c.id: c for c in ranked}
        judged = [p["id"] for p in picks if p.get("id") in by_id]
        ordered = judged + [c.id for c in ranked if c.id not in set(judged)]

        # adapters may resolve a playable URL (e.g. music -> YouTube); if one can't
        # be resolved, the item is dropped and the next-ranked candidate backfills.
        resolver = getattr(adapter, "resolve_url", None)
        out = []
        for cid in ordered:
            if len(out) >= sub["n"]:
                break
            c = by_id.get(cid)
            if not c:
                continue
            url = c.url
            if resolver:
                url = resolver(c, context)
                if not url:
                    continue                                  # no playable link -> drop, backfill
            out.append({"id": c.id, "title": c.title, "url": url, "why": why_by.get(c.id, "")})
        return out