from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone

from ___.HyperCoreSDK import HyperClient, HyperCoreNode


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


@dataclass(frozen=True)
class NewsArticle(HyperCoreNode):
    id: str
    source: str
    region: str
    feed_url: str
    title: str
    link: str
    published_at: str
    summary: str
    text: str
    tags: list[str]
    image_links: list[str]
    video_links: list[str]
    audio_links: list[str]
    fetched_at: str

    def to_kv(self) -> dict:
        return asdict(self)

    @classmethod
    def properties(cls) -> list[str]:
        return [f.name for f in fields(cls)]

    def save_to(self, hyper_client: HyperClient) -> None:
        old_root = hyper_client.root
        try:
            hyper_client.root = "news"
            self.commit(
                hyper_client,
                sub_paths=[
                    "items",
                    self.region.lower(),
                    self.source.lower(),
                    self.id,
                ],
            )
        finally:
            hyper_client.root = old_root

    @classmethod
    def load_from(
        cls,
        hyper_client: HyperClient,
        *,
        region: str,
        source: str,
        article_id: str,
    ) -> "NewsArticle | None":
        old_root = hyper_client.root
        try:
            hyper_client.root = "news"
            clean_data, metadata = cls.read_out(
                hyper_client,
                sub_paths=[
                    "items",
                    region.lower(),
                    source.lower(),
                    article_id,
                ],
            )
        finally:
            hyper_client.root = old_root

        if not clean_data:
            return None

        try:
            instance = cls(**clean_data)
            object.__setattr__(instance, "_metadata", metadata)
            return instance
        except (TypeError, ValueError, KeyError):
            return None


@dataclass(frozen=True)
class NewsFeedState(HyperCoreNode):
    region: str
    source: str
    feed_url: str
    last_checked_at: str
    refreshed_at: str | None
    latest_item_published_at: str | None
    item_count: int
    ok: bool
    error: str | None

    def to_kv(self) -> dict:
        return asdict(self)

    @classmethod
    def properties(cls) -> list[str]:
        return [f.name for f in fields(cls)]

    def save_to(self, hyper_client: HyperClient) -> None:
        old_root = hyper_client.root
        try:
            hyper_client.root = "news"
            self.commit(
                hyper_client,
                sub_paths=[
                    "state",
                    "feeds",
                    self.region.lower(),
                    self.source.lower(),
                ],
            )
        finally:
            hyper_client.root = old_root

    @classmethod
    def load_from(
        cls,
        hyper_client: HyperClient,
        region: str,
        source: str,
        max_age_minutes: int = 30,
    ) -> tuple["NewsFeedState | None", bool]:
        old_root = hyper_client.root
        try:
            hyper_client.root = "news"
            clean_data, metadata = cls.read_out(
                hyper_client,
                sub_paths=[
                    "state",
                    "feeds",
                    region.lower(),
                    source.lower(),
                ],
            )
        finally:
            hyper_client.root = old_root

        if not clean_data:
            return None, True

        try:
            instance = cls(**clean_data)
            object.__setattr__(instance, "_metadata", metadata)

            basis = _parse_iso_datetime(instance.refreshed_at) or _parse_iso_datetime(
                instance.last_checked_at
            )
            if basis is None:
                return instance, True

            update_required = (
                datetime.now(timezone.utc) - basis
            ) >= timedelta(minutes=max_age_minutes)

            return instance, update_required
        except (TypeError, ValueError, KeyError):
            return None, True