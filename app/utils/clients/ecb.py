from __future__ import annotations

import json
import ssl
import time
from pathlib import Path
from urllib.request import Request, urlopen

import certifi


class EcbApiError(RuntimeError):
    pass


class EcbApiClient:
    """
    Transport-only ECB client.

    Responsibilities:
    - fetch raw ECB daily XML
    - optionally cache raw responses on disk

    Non-responsibilities:
    - parse FX rates
    - compute currency crosses
    - publish into HyperCore
    """

    DAILY_XML_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
    USER_AGENT = "hypercore-ecb-client/1.0"

    def __init__(
        self,
        *,
        timeout: int = 20,
        user_agent: str | None = None,
    ) -> None:
        self.timeout = int(timeout)
        self.user_agent = user_agent or self.USER_AGENT
        self.ssl_ctx = ssl.create_default_context(cafile=certifi.where())

    def _request(
        self,
        url: str,
        *,
        timeout: int | None = None,
        accept: str = "application/xml, text/xml, */*",
    ) -> Request:
        return Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": accept,
            },
        )

    def _fetch_bytes(
        self,
        url: str,
        *,
        timeout: int | None = None,
    ) -> bytes:
        req = self._request(url, timeout=timeout)

        try:
            with urlopen(req, timeout=timeout or self.timeout, context=self.ssl_ctx) as resp:
                return resp.read()
        except Exception as exc:
            raise EcbApiError(
                f"GET {url} failed: {type(exc).__name__}: {exc}"
            ) from exc

    def fetch_daily_xml_bytes(
        self,
        *,
        timeout: int | None = None,
    ) -> bytes:
        return self._fetch_bytes(self.DAILY_XML_URL, timeout=timeout)

    def fetch_daily_xml_text(
        self,
        *,
        timeout: int | None = None,
        encoding: str = "utf-8",
    ) -> str:
        raw = self.fetch_daily_xml_bytes(timeout=timeout)
        try:
            return raw.decode(encoding)
        except Exception as exc:
            raise EcbApiError(
                f"Decode ECB XML with encoding={encoding!r} failed: {type(exc).__name__}: {exc}"
            ) from exc

    def fetch_daily_xml_text_cached(
        self,
        *,
        cache_dir: str | Path = ".fx-cache",
        cache_file_name: str = "ecb_daily.xml",
        cache_ttl_seconds: int = 12 * 3600,
        timeout: int | None = None,
        encoding: str = "utf-8",
    ) -> str:
        """
        Raw-response cache only.
        """
        cache_dir = Path(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / cache_file_name

        now = time.time()
        if cache_file.exists():
            age = now - cache_file.stat().st_mtime
            if age <= cache_ttl_seconds:
                return cache_file.read_text(encoding=encoding)

        xml_text = self.fetch_daily_xml_text(timeout=timeout, encoding=encoding)
        cache_file.write_text(xml_text, encoding=encoding)
        return xml_text

    def fetch_daily_snapshot_cached(
        self,
        *,
        cache_dir: str | Path = ".fx-cache",
        cache_file_name: str = "ecb_daily_snapshot.json",
        cache_ttl_seconds: int = 12 * 3600,
        timeout: int | None = None,
        encoding: str = "utf-8",
    ) -> dict[str, str]:
        """
        Stores fetch metadata plus raw XML text.
        Parsing remains the caller's job.
        """
        cache_dir = Path(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / cache_file_name

        now = time.time()
        if cache_file.exists():
            age = now - cache_file.stat().st_mtime
            if age <= cache_ttl_seconds:
                try:
                    cached = json.loads(cache_file.read_text(encoding=encoding))
                    if isinstance(cached, dict) and isinstance(cached.get("xml_text"), str):
                        return cached
                except Exception:
                    pass

        xml_text = self.fetch_daily_xml_text(timeout=timeout, encoding=encoding)
        payload = {
            "source_url": self.DAILY_XML_URL,
            "fetched_at_epoch": str(int(now)),
            "xml_text": xml_text,
        }

        cache_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding=encoding,
        )
        return payload