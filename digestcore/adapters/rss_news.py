"""Allowlist-driven news via RSS.

Pulls recent items directly from each rated outlet's feed (no aggregator, no rate
limit), clusters by headline with recall-favoring single-linkage (overlap coefficient
over title+summary tokens), and keeps only stories carried by >= MIN_SOURCES distinct
outlets spanning >= MIN_BIAS distinct bias labels. The fetch+cluster pass is
user-agnostic and cached so one run can serve many users; per-user exclusion and
ranking happen downstream.
"""

import re
import time
import email.utils
import hashlib
import datetime as dt
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
from xml.etree import ElementTree as ET

import requests

from digestcore.sources import load_options, load_sources, default_news_options, _localname
from digestcore.models import Candidate, SourceAdapter
from digestcore.net import AdapterRetryable, is_transport_error


class RssNewsAdapter(SourceAdapter):
    signal_weights = {"bias_diversity": 1.0, "source_count": 0.3}

    MIN_SOURCES = 3            # distinct outlets carrying a story
    MIN_BIAS = 2               # distinct 5-point bias labels required (tier 1)
    MIN_RESULTS = 8            # if fewer than this many tier-1 stories, allow 1-bias backfill
    BUFFER_HOURS = 4           # extend the lookback window by this much
    SIM_THRESHOLD = 0.5        # overlap-coefficient threshold for merging
    DESC_CHARS = 220           # cap of summary text used for matching
    MAX_WORKERS = 10           # parallel feed fetches (independent servers, safe)
    POOL_TTL = 600             # seconds to reuse a fetched pool across users/subs
    UA = "Mozilla/5.0 (compatible; DigestBot/1.0; +local)"

    _COARSE = {"left": "L", "lean-left": "L", "center": "C", "lean-right": "R", "right": "R"}
    _CENTRALITY = {"center": 0, "lean-left": 1, "lean-right": 1, "left": 2, "right": 2}
    STOP = {"the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "with", "as",
            "at", "by", "from", "is", "are", "was", "were", "be", "after", "over", "amid",
            "says", "say", "said", "new", "its", "his", "her", "their", "this", "that",
            "out", "how", "why", "what", "who", "will", "has", "have", "but", "not", "you",
            "your", "they", "them", "about", "into", "more", "than", "up", "off"}

    def __init__(self):
        self._pool_cache: dict[int, tuple] = {}     # window_days -> (ts, clusters)
        self.last_health: dict[str, object] = {}

    # ---- options / exclusion helpers ----
    def _news_opts(self) -> dict:
        return load_options().get("adapters", {}).get("news", default_news_options())

    def _fragment_terms(self, frag: str) -> list[str]:
        frag = frag.strip().strip("()")
        return [p.strip().strip('"').lower() for p in re.split(r"\s+OR\s+", frag) if p.strip()]

    def _tokens(self, text: str) -> set:
        return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
                if w not in self.STOP and len(w) > 2}

    @staticmethod
    def _overlap(a: set, b: set) -> float:
        inter = len(a & b)
        if inter < 2:                       # guard against single-token coincidences
            return 0.0
        return inter / min(len(a), len(b))

    # ---- feed fetching / parsing ----
    def _parse_date(self, s: Optional[str]) -> Optional[dt.datetime]:
        if not s:
            return None
        try:
            d = email.utils.parsedate_to_datetime(s)
            return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
        except (TypeError, ValueError, IndexError):
            pass
        try:
            d = dt.datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            return None

    def _parse_feed(self, xml_text: str, domain: str) -> list[dict]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return []
        items = []
        for el in root.iter():
            if _localname(el.tag) not in ("item", "entry"):
                continue
            title = link = summary = pub = None
            for child in el:
                c = _localname(child.tag)
                if c == "title" and child.text and not title:
                    title = child.text.strip()
                elif c == "link":
                    if child.text and child.text.strip():
                        link = child.text.strip()
                    elif child.get("href") and (not link or child.get("rel") in (None, "alternate")):
                        link = child.get("href")
                elif c in ("description", "summary", "encoded") and child.text and not summary:
                    summary = re.sub(r"<[^>]+>", " ", child.text)
                elif c in ("pubdate", "published", "updated", "date") and child.text and not pub:
                    pub = child.text.strip()
            if not title:
                continue
            items.append({"title": title, "url": link or "", "summary": (summary or "").strip(),
                          "published": self._parse_date(pub), "domain": domain})
        return items

    def _fetch_feed(self, domain: str, url: str) -> tuple:
        try:
            r = requests.get(url, headers={"User-Agent": self.UA}, timeout=20)
            r.raise_for_status()
            return domain, self._parse_feed(r.text, domain), None, False
        except Exception as e:                                   # noqa: BLE001 (report any failure)
            return domain, [], str(e)[:120], is_transport_error(e)

    # ---- user-agnostic fetch + cluster (cacheable) ----
    def _build_pool(self, window_days: int) -> list[dict]:
        sources = load_sources()
        feeds = [(d, m.get("feed")) for d, m in sources.items() if m.get("feed")]
        window_start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=window_days, hours=self.BUFFER_HOURS)

        health: dict[str, object] = {}
        articles: list[dict] = []
        n_transport = 0
        with ThreadPoolExecutor(max_workers=self.MAX_WORKERS) as ex:
            for domain, items, err, transport in ex.map(lambda f: self._fetch_feed(*f), feeds):
                if err:
                    health[domain] = f"ERR: {err}"
                    n_transport += 1 if transport else 0
                    continue
                kept = [it for it in items if it["published"] is None or it["published"] >= window_start]
                health[domain] = len(kept)
                articles.extend(kept)
        self.last_health = health
        print("news feed health: " + self._health_summary())

        # News tolerates individual feeds being down by design (it clusters across
        # outlets). But if nothing came through AND the failures were transport-level,
        # the network is down rather than a few dead feeds — retry instead of
        # reporting a misleading "nothing new".
        n_live = sum(1 for v in health.values() if isinstance(v, int) and v > 0)
        if n_live == 0 and n_transport > 0:
            raise AdapterRetryable(
                f"news feeds unreachable — {n_transport} of {len(feeds)} failed to resolve")

        # dedupe by url (fall back to title)
        seen, uniq = set(), []
        for a in articles:
            key = a["url"] or a["title"]
            if key and key not in seen:
                seen.add(key)
                uniq.append(a)

        # single-linkage clustering with token inverted index (recall-favoring)
        clusters: list[dict] = []
        token_index: dict[str, set] = {}
        for art in uniq:
            toks = self._tokens(art["title"] + " " + art["summary"][: self.DESC_CHARS])
            if not toks:
                continue
            cand_ids = set()
            for t in toks:
                cand_ids |= token_index.get(t, set())
            best_id, best_sim = None, 0.0
            for cid in cand_ids:
                sim = max(self._overlap(toks, m) for m in clusters[cid]["members"])
                if sim > best_sim:
                    best_sim, best_id = sim, cid
            if best_id is not None and best_sim >= self.SIM_THRESHOLD:
                c = clusters[best_id]
                c["members"].append(toks)
                c["items"].append(art)
                c["domains"].add(art["domain"])
                for t in toks:
                    token_index.setdefault(t, set()).add(best_id)
            else:
                cid = len(clusters)
                clusters.append({"members": [toks], "items": [art], "domains": {art["domain"]}})
                for t in toks:
                    token_index.setdefault(t, set()).add(cid)
        return clusters

    def _pool(self, window_days: int) -> list[dict]:
        now = time.time()
        cached = self._pool_cache.get(window_days)
        if cached and now - cached[0] < self.POOL_TTL:
            return cached[1]
        clusters = self._build_pool(window_days)
        self._pool_cache[window_days] = (now, clusters)
        return clusters

    # ---- diagnostics ----
    def _health_summary(self) -> str:
        h = self.last_health
        live = sum(1 for v in h.values() if isinstance(v, int) and v > 0)
        empty = [d for d, v in h.items() if v == 0]
        errs = {d: v for d, v in h.items() if isinstance(v, str)}
        parts = [f"{live} live, {len(empty)} empty, {len(errs)} errored"]
        if errs:
            parts.append("errors: " + "; ".join(f"{d}({v})" for d, v in list(errs.items())[:8]))
        if empty:
            parts.append("empty: " + ", ".join(empty[:12]))
        return " | ".join(parts)

    def health_report(self) -> str:
        return self._health_summary()

    def diagnostic(self) -> str:
        """Per-run source health, surfaced by the runner alongside this sub."""
        return self._health_summary()

    # ---- per-user candidates (exclusion + qualify) ----
    def fetch_candidates(self, topic: str, window_days: int,
                         context: Optional[dict] = None) -> list[Candidate]:
        clusters = self._pool(window_days)
        ratings = load_sources()

        def bias_of(domain: str):
            m = ratings.get(domain)
            return m.get("bias") if m else None

        opts = self._news_opts()
        cat_defs = opts.get("categories", {})
        max_excl = int(opts.get("max_exclusions", 4))
        excluded = set(list({c.strip().lower() for c in (topic or "").split(",") if c.strip()})[:max_excl])
        excluded_terms = []
        for name, frag in cat_defs.items():
            if name.lower() in excluded:
                excluded_terms += self._fragment_terms(frag)

        tier1, tier2 = [], []
        for c in clusters:
            domains_c = c["domains"]
            if len(domains_c) < self.MIN_SOURCES:
                continue
            biases = {bias_of(d) for d in domains_c}
            biases.discard(None)
            if not biases:
                continue
            rep = min(c["items"], key=lambda it: self._CENTRALITY.get(bias_of(it["domain"]), 3))
            text = (rep["title"] + " " + rep["summary"]).lower()
            if any(term and term in text for term in excluded_terms):
                continue
            coarse = {self._COARSE[b] for b in biases if b in self._COARSE}
            label = "/".join(s for s in ("L", "C", "R") if s in coarse)
            freq = Counter()
            for m in c["members"]:
                freq.update(m)
            sig = hashlib.md5(" ".join(sorted(t for t, _ in freq.most_common(6))).encode()).hexdigest()[:12]
            cand = Candidate(
                id=f"news:{sig}", title=rep["title"], url=rep["url"],
                summary=f"Covered by {len(domains_c)} outlets, {len(biases)} viewpoints ({label}): "
                        f"{', '.join(sorted(domains_c)[:8])}",
                signals={"source_count": len(domains_c), "bias_diversity": len(biases)},
            )
            (tier1 if len(biases) >= self.MIN_BIAS else tier2).append(cand)

        # tier-1 (>= MIN_BIAS viewpoints) is the digest; only when it's too thin
        # for the day do we backfill with single-viewpoint stories.
        if len(tier1) >= self.MIN_RESULTS:
            return tier1
        return tier1 + tier2