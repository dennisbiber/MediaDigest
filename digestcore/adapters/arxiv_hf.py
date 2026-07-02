"""arXiv for recall, Hugging Face daily papers for the curation signal."""

import datetime as dt
from typing import Optional
from xml.etree import ElementTree as ET

import requests

from digestcore.models import Candidate, SourceAdapter
from digestcore.net import AdapterRetryable, is_transport_error


class ArxivHFAdapter(SourceAdapter):
    signal_weights = {"hf_upvotes": 1.0}

    ARXIV = "http://export.arxiv.org/api/query"
    HF = "https://huggingface.co/api/daily_papers"
    NS = {"a": "http://www.w3.org/2005/Atom"}
    last_diagnostic = ""      # HF (secondary) degradation from the last run, if any

    def diagnostic(self) -> str:
        return self.last_diagnostic

    def fetch_candidates(self, topic: str, window_days: int,
                         context: Optional[dict] = None) -> list[Candidate]:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=window_days)
        by_id: dict[str, Candidate] = {}
        self.last_diagnostic = ""

        params = {"search_query": topic, "sortBy": "submittedDate",
                  "sortOrder": "descending", "max_results": 150}
        try:
            r = requests.get(self.ARXIV, params=params, timeout=30)
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001 - classify: transport retries, the rest is a real error
            if is_transport_error(e):
                raise AdapterRetryable(f"arXiv unreachable ({type(e).__name__})") from e
            raise
        feed = ET.fromstring(r.text)
        for entry in feed.findall("a:entry", self.NS):
            arxiv_url = entry.findtext("a:id", default="", namespaces=self.NS)
            aid = arxiv_url.rsplit("/", 1)[-1].split("v")[0]
            pub = entry.findtext("a:published", default="", namespaces=self.NS)
            published = dt.datetime.fromisoformat(pub.replace("Z", "+00:00")) if pub else None
            if published and published < cutoff:
                continue
            by_id[aid] = Candidate(
                id=aid,
                title=(entry.findtext("a:title", "", self.NS) or "").strip().replace("\n", " "),
                url=arxiv_url,
                summary=(entry.findtext("a:summary", "", self.NS) or "").strip().replace("\n", " "),
                published=published,
                signals={"hf_upvotes": 0},
            )

        hf_fail = 0
        for d in range(window_days):
            day = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=d)).strftime("%Y-%m-%d")
            try:
                hr = requests.get(self.HF, params={"date": day}, timeout=30)
                hr.raise_for_status()
                for item in hr.json():
                    paper = item.get("paper", item)
                    aid = (paper.get("id") or "").split("v")[0]
                    upvotes = paper.get("upvotes", item.get("upvotes", 0)) or 0
                    if aid in by_id:
                        by_id[aid].signals["hf_upvotes"] = max(by_id[aid].signals["hf_upvotes"], upvotes)
                    elif aid:
                        by_id[aid] = Candidate(
                            id=aid, title=(paper.get("title") or "").strip(),
                            url=f"https://arxiv.org/abs/{aid}",
                            summary=(paper.get("summary") or "").strip(),
                            signals={"hf_upvotes": upvotes},
                        )
            except requests.RequestException:
                # HF is a secondary curation signal — degrade and report, never abort:
                # arXiv recall still stands, so the run delivers on that alone.
                hf_fail += 1
                continue
        if hf_fail:
            self.last_diagnostic = (
                f"Hugging Face daily-papers unavailable ({hf_fail}/{window_days} days); "
                "ranked on arXiv recall only")
        return list(by_id.values())