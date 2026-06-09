"""
News factory — the typed object + builder for the news database.

Mirrors weather/factory.py and geo/factory.py:
    WeatherEventObject  <->  NewsArticleObject   (frozen dataclass, the stored body)
    WeatherFactory      <->  NewsFactory         (builds typed objects from NORMALIZED fields)

Nothing here knows how feeds encode data — NewsFactory takes plain named fields.
Provider decoding (RSS/Atom, dates, tag stripping) lives in app/lib/sources/rss.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from HyperCoreSDK.python.dtos.object import HyperObject


# ---------------------------------------------------------------------------
# Article object  (analog of WeatherEventObject)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class NewsArticleObject:
    article_id: str
    source: str
    region: str
    title: str
    link: str
    summary: str
    published_at: str
    published_day: str
    fetched_at: str

    location_keys: list[str] = field(default_factory=list)
    location_count: int = 0
    has_locations: bool = False

    _news_root: str = "news"
    _geo_root: str = "geo"

    def record_key(self) -> str:
        """Flat leaf like geo's locations/<key> — no deep shared ancestors."""
        return f"{self.region}-{self.source}-{self.published_day}-{self.article_id}"

    def latest_key(self) -> str:
        return f"{self.region}-{self.source}"

    def ref_payload(self) -> dict[str, Any]:
        """Compact snapshot for index entries / backrefs."""
        return {
            "title": self.title, "link": self.link, "source": self.source,
            "region": self.region, "published_at": self.published_at,
            "published_day": self.published_day, "location_count": self.location_count,
            "has_locations": self.has_locations,
        }

    def latest_payload(self, canonical_dot: str) -> dict[str, Any]:
        # NOTE: distinct keys — the old latest_dict had "source" twice, silently
        # dropping the canonical path. `origin` carries the record path now.
        return {
            "model": "news-latest", "origin": canonical_dot,
            "title": self.title, "link": self.link, "source": self.source,
            "region": self.region, "published_at": self.published_at,
        }


# ---------------------------------------------------------------------------
# Article builder  (analog of WeatherFactory / LocationFactory)
# ---------------------------------------------------------------------------
class NewsFactory(HyperObject):
    """Builds typed NewsArticleObjects from already-normalized provider fields."""

    @classmethod
    def create_article_object(
            cls,
            *,
            article_record: dict[str, Any],
            location_keys: list[str] | None = None,
    ) -> NewsArticleObject:
        keys = list(location_keys or [])
        return NewsArticleObject(
            article_id=str(article_record.get("article_id") or "").strip(),
            source=str(article_record.get("source") or "").strip(),
            region=str(article_record.get("region") or "").strip(),
            title=str(article_record.get("title") or ""),
            link=str(article_record.get("link") or ""),
            summary=str(article_record.get("summary") or ""),
            published_at=str(article_record.get("published_at") or "").strip(),
            published_day=str(article_record.get("published_day") or "").strip(),
            fetched_at=str(article_record.get("fetched_at") or "").strip(),
            location_keys=keys,
            location_count=len(keys),
            has_locations=bool(keys),
        )