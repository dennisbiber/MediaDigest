"""Adapter registry. Add a new source by implementing SourceAdapter and listing it here."""

from digestcore.models import SourceAdapter
from digestcore.adapters.arxiv_hf import ArxivHFAdapter
from digestcore.adapters.rss_news import RssNewsAdapter
from digestcore.adapters.music_catalog import MusicCatalogAdapter

ADAPTERS: dict[str, SourceAdapter] = {
    "arxiv_hf": ArxivHFAdapter(),
    "news": RssNewsAdapter(),
    "music": MusicCatalogAdapter(),   # keyless local catalog
}

__all__ = ["ADAPTERS", "ArxivHFAdapter", "RssNewsAdapter", "MusicCatalogAdapter"]
