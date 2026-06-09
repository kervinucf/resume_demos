"""
RSS source — the provider-specific read layer for the news loader.

Analog of app/lib/sources/geonames.py and open_metro.py: everything that knows
*how feeds encode data* (RSS 2.0 vs Atom, date formats, tag stripping) lives here
and nowhere else. To swap providers, write another module in app/lib/sources/
exposing the same callables and re-point the import in
app/lib/helpers/news/__init__.py:

    source_available()              -> (bool, str)       # transport constructs
    iter_article_candidates(feeds)  -> Iterator[dict]    # one normalized raw item per entry

Records are plain dicts with neutral keys — no app typed imports here, so a new
provider only has to match the key names, not our object model.
"""
from __future__ import annotations

import hashlib
import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterator
import ssl
from typing import Any
from urllib.request import Request, urlopen

import certifi


class RssApiError(RuntimeError):
    pass


class RssApiClient:
    """
    Transport-only RSS/Atom client.

    Responsibilities:
    - fetch raw feed bytes
    - fetch raw feed text
    - batch fetch feed payloads

    Non-responsibilities:
    - parse RSS/Atom
    - dedupe items
    - filter items
    - publish into HyperCore
    """

    USER_AGENT = "hypercore-rss-client/1.0"

    def __init__(
        self,
        *,
        timeout: int = 30,
        user_agent: str | None = None,
    ) -> None:
        self.timeout = int(timeout)
        self.user_agent = user_agent or self.USER_AGENT
        self.ssl_ctx = ssl.create_default_context(cafile=certifi.where())

    def _build_request(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        accept: str = "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    ) -> Request:
        merged_headers = {
            "User-Agent": self.user_agent,
            "Accept": accept,
        }
        if headers:
            merged_headers.update(headers)

        return Request(url, headers=merged_headers)

    def fetch_bytes(
        self,
        url: str,
        *,
        timeout: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        req = self._build_request(url, headers=headers)

        try:
            with urlopen(req, timeout=timeout or self.timeout, context=self.ssl_ctx) as resp:
                return resp.read()
        except Exception as exc:
            raise RssApiError(
                f"GET {url} failed: {type(exc).__name__}: {exc}"
            ) from exc

    def fetch_text(
        self,
        url: str,
        *,
        timeout: int | None = None,
        encoding: str = "utf-8",
        headers: dict[str, str] | None = None,
    ) -> str:
        raw = self.fetch_bytes(url, timeout=timeout, headers=headers)
        try:
            return raw.decode(encoding)
        except Exception as exc:
            raise RssApiError(
                f"Decode {url} with encoding={encoding!r} failed: {type(exc).__name__}: {exc}"
            ) from exc

    def fetch_feed(
        self,
        *,
        source: str,
        region: str,
        url: str,
        timeout: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        body = self.fetch_bytes(url, timeout=timeout, headers=headers)
        return {
            "source": source,
            "region": region,
            "url": url,
            "body": body,
        }

    def fetch_feeds(
        self,
        feeds: list[dict[str, str]],
        *,
        timeout: int | None = None,
        headers: dict[str, str] | None = None,
        include_errors: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Expected feed shape:
            {"source": "...", "region": "...", "url": "..."}
        """
        out: list[dict[str, Any]] = []

        for feed in feeds:
            source = str(feed.get("source", "")).strip()
            region = str(feed.get("region", "")).strip()
            url = str(feed.get("url", "")).strip()

            if not url:
                if include_errors:
                    out.append(
                        {
                            "source": source,
                            "region": region,
                            "url": url,
                            "ok": False,
                            "error": "Missing feed URL",
                        }
                    )
                continue

            try:
                payload = self.fetch_feed(
                    source=source,
                    region=region,
                    url=url,
                    timeout=timeout,
                    headers=headers,
                )
                payload["ok"] = True
                out.append(payload)
            except Exception as exc:
                if include_errors:
                    out.append(
                        {
                            "source": source,
                            "region": region,
                            "url": url,
                            "ok": False,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )

        return out
_TAG_RE = re.compile(r"<[^>]+>")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _strip_tags(s: str) -> str:
    return _TAG_RE.sub("", html.unescape(s or "")).strip()


def _parse_date(raw: str) -> datetime:
    raw = (raw or "").strip()
    if not raw:
        return _now_utc()
    try:
        dt = parsedate_to_datetime(raw)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return _now_utc()


def _stable_article_id(source: str, guid: str) -> str:
    digest = hashlib.sha1(f"{source}|{guid}".encode("utf-8")).hexdigest()[:16]
    return f"{source}-{digest}"


# ---------------------------------------------------------------------------
# Transport probe
# ---------------------------------------------------------------------------
def source_available() -> tuple[bool, str]:
    try:
        RssApiClient()
        return True, "rss"
    except Exception:  # pragma: no cover - defensive
        return False, "rss"


# ---------------------------------------------------------------------------
# Feed -> neutral raw items
# ---------------------------------------------------------------------------
def _items_from_feed(raw: bytes) -> list[dict[str, Any]]:
    """Parse RSS 2.0 or Atom payload into raw item dicts."""
    root = ET.fromstring(raw)

    items = root.findall(".//item")
    if items:
        out = []
        for it in items:
            link = (it.findtext("link") or "").strip()
            title = _strip_tags(it.findtext("title") or "")
            out.append({
                "title": title,
                "link": link,
                "summary": _strip_tags(it.findtext("description") or ""),
                "published": (it.findtext("pubDate") or "").strip(),
                "guid": (it.findtext("guid") or link or title).strip(),
            })
        return out

    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    for e in root.findall(".//a:entry", ns):
        link_el = e.find("a:link", ns)
        link = link_el.get("href") if link_el is not None else ""
        title = _strip_tags(e.findtext("a:title", default="", namespaces=ns) or "")
        out.append({
            "title": title,
            "link": link,
            "summary": _strip_tags(
                e.findtext("a:summary", default="", namespaces=ns)
                or e.findtext("a:content", default="", namespaces=ns) or ""
            ),
            "published": (e.findtext("a:published", default="", namespaces=ns)
                          or e.findtext("a:updated", default="", namespaces=ns)),
            "guid": e.findtext("a:id", default="", namespaces=ns) or link,
        })
    return out


def iter_article_candidates(
        feeds: list[dict[str, Any]],
) -> Iterator[dict[str, Any]]:
    """
    Stream normalized raw articles across feeds.

    Each `feed` is a neutral dict: {region, source, url, match_country?}.
    Feeds that fail to fetch/parse warn and yield nothing, mirroring the
    optional-source behavior in the geo provider.
    """
    rss = RssApiClient()
    fetched_at = _now_utc().isoformat()

    for feed in feeds:
        region = str(feed.get("region") or "")
        source = str(feed.get("source") or "")
        url = str(feed.get("url") or "")
        try:
            items = _items_from_feed(rss.fetch_bytes(url))
        except Exception as exc:
            print(f"  warning: feed fetch/parse failed for {region}/{source} ({exc})", flush=True)
            continue

        for raw in items:
            guid = (raw.get("guid") or raw.get("link") or raw.get("title") or "").strip()
            if not guid:
                continue
            pub = _parse_date(raw.get("published") or "")
            yield {
                "article_id": _stable_article_id(source, guid),
                "source": source,
                "region": region,
                "match_country": feed.get("match_country"),
                "title": raw.get("title") or "",
                "link": raw.get("link") or "",
                "summary": raw.get("summary") or "",
                "published_at": pub.astimezone(timezone.utc).isoformat(),
                "published_day": pub.astimezone(timezone.utc).strftime("%Y-%m-%d"),
                "fetched_at": fetched_at,
            }