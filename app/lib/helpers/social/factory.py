"""
ATProto factory — the typed object + builder for the atproto database.

Mirrors weather/factory.py and geo/factory.py:
    WeatherEventObject  <->  AtprotoRecordObject  (frozen dataclass, the stored body)
    WeatherFactory      <->  AtprotoFactory       (builds typed objects from NORMALIZED fields)

Nothing here knows how Bluesky/Jetstream encodes data — AtprotoFactory takes plain
named fields. Provider decoding (websocket, commit/record shape) lives in
app/lib/sources/bluesky.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_SLUG_RE = re.compile(r"[^a-zA-Z0-9]+")
_DASHES_RE = re.compile(r"-{2,}")


def slugify(value: Any, fallback: str = "record") -> str:
    text = _DASHES_RE.sub("-", _SLUG_RE.sub("-", str(value or "").strip().lower())).strip("-")
    return text or fallback


@dataclass(frozen=True)
class AtprotoRecordObject:
    did: str
    collection: str
    rkey: str
    operation: str
    uri: str
    cid: str | None
    record_type: str
    created_at: str | None
    received_at: str
    activity_latest_at: int

    text: str = ""
    langs: list[str] | None = None
    has_text: bool = False
    has_embed: bool = False
    has_reply: bool = False
    hashtags: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    value: dict[str, Any] = field(default_factory=dict)

    _atproto_root: str = "atproto"

    @property
    def did_key(self) -> str:
        return slugify(self.did, "did")

    @property
    def collection_key(self) -> str:
        return slugify(self.collection, "collection")

    @property
    def rkey_key(self) -> str:
        return slugify(self.rkey, "record")

    def record_key(self) -> str:
        """Flat leaf like geo's locations/<key> — no deep shared ancestors."""
        return f"{self.did_key}-{self.collection_key}-{self.rkey_key}"

    def ref_payload(self) -> dict[str, Any]:
        return {
            "did": self.did, "collection": self.collection, "rkey": self.rkey,
            "record_type": self.record_type, "created_at": self.created_at,
        }


class AtprotoFactory:
    """Builds typed AtprotoRecordObjects from already-normalized provider fields."""

    @classmethod
    def create_record_object(cls, *, record_event: dict[str, Any]) -> AtprotoRecordObject:
        return AtprotoRecordObject(
            did=str(record_event.get("did") or "").strip(),
            collection=str(record_event.get("collection") or "").strip(),
            rkey=str(record_event.get("rkey") or "").strip(),
            operation=str(record_event.get("operation") or "").strip(),
            uri=str(record_event.get("uri") or ""),
            cid=record_event.get("cid"),
            record_type=str(record_event.get("record_type") or "record"),
            created_at=record_event.get("created_at"),
            received_at=str(record_event.get("received_at") or ""),
            activity_latest_at=int(record_event.get("activity_latest_at") or 0),
            text=str(record_event.get("text") or ""),
            langs=record_event.get("langs"),
            has_text=bool(record_event.get("has_text")),
            has_embed=bool(record_event.get("has_embed")),
            has_reply=bool(record_event.get("has_reply")),
            hashtags=list(record_event.get("hashtags") or []),
            urls=list(record_event.get("urls") or []),
            value=dict(record_event.get("value") or {}),
        )