from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

_SLUG_RE = re.compile(r"[^a-zA-Z0-9]+")
_DASHES_RE = re.compile(r"-{2,}")


def slugify(value: str, *, fallback: str = "article") -> str:
    text = _SLUG_RE.sub("-", str(value or "").strip().lower())
    text = _DASHES_RE.sub("-", text).strip("-")
    return text or fallback


@dataclass(frozen=True)
class NewsArticle:
    article_id: str              # e.g. "bbc-3a7f2c1d9e0b4a56"
    source: str                  # "bbc", "nyt", "reuters"
    region: str                  # "world", "us", "uk"
    title: str
    link: str
    summary: str
    published_at: str            # ISO-8601
    fetched_at: str              # ISO-8601 UTC
    location_keys: list[str] = field(default_factory=list)

    def record_key(self) -> str:
        """
        Stable, time-partitioned id:
            <region>/<source>/<yyyy>/<mm>/<dd>/<article_id>
        """
        dt = datetime.fromisoformat(self.published_at)
        return (
            f"{self.region}/{self.source}/"
            f"{dt.year:04d}/{dt.month:02d}/{dt.day:02d}/"
            f"{self.article_id}"
        )

    def latest_key(self) -> str:
        """Per-feed pointer to the most recent article from this source."""
        return f"{self.region}/{self.source}"

    def to_dict(self) -> dict[str, Any]:
        return {"model": "news-article", **asdict(self)}

    def latest_dict(self, canonical_path: str) -> dict[str, Any]:
        return {
            "model": "news-latest",
            "target": canonical_path,
            "title": self.title,
            "link": self.link,
            "source": self.source,
            "region": self.region,
            "published_at": self.published_at,
        }

    def ref_payload(self) -> dict[str, Any]:
        """
        Compact snapshot merged into each index entry's `data` and into
        the per-location back-refs — lets listings render headlines
        without hydrating the full article.
        """
        return {
            "title": self.title,
            "link": self.link,
            "source": self.source,
            "region": self.region,
            "published_at": self.published_at,
        }