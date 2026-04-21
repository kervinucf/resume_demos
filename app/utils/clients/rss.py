from __future__ import annotations

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