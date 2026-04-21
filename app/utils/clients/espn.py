from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import certifi


LEAGUE_TO_SPORT_MAP: dict[str, str] = {
    "nba": "basketball",
    "wnba": "basketball",
    "mens-college-basketball": "basketball",
    "mlb": "baseball",
    "college-baseball": "baseball",
    "nfl": "football",
    "college-football": "football",
    "nhl": "hockey",
    "f1": "racing",
    "eng.1": "soccer",
    "esp.1": "soccer",
    "ita.1": "soccer",
    "ger.1": "soccer",
    "fra.1": "soccer",
    "ned.1": "soccer",
    "por.1": "soccer",
    "usa.1": "soccer",
    "mex.1": "soccer",
    "usa.nwsl": "soccer",
    "uefa.champions": "soccer",
    "uefa.europa": "soccer",
    "fifa.world": "soccer",
    "aus.1": "soccer",
    "jpn.1": "soccer",
    "ksa.1": "soccer",
}


class EspnApiError(RuntimeError):
    pass


class EspnApiClient:
    """
    Transport-only ESPN client.

    Responsibilities:
    - resolve sport for a league
    - fetch raw JSON payloads from ESPN APIs

    Non-responsibilities:
    - parse scoreboard events
    - decide relevance dates
    - publish into HyperCore
    """

    SITE_API_BASE = "https://site.api.espn.com/apis/site/v2"
    CORE_API_BASE = "https://sports.core.api.espn.com/v2"

    DEFAULT_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/58.0.3029.110 Safari/537.36"
        ),
        "Accept": "application/json, */*",
    }

    def __init__(
        self,
        *,
        timeout: int = 15,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.timeout = int(timeout)
        self.headers = dict(self.DEFAULT_HEADERS)
        if headers:
            self.headers.update(headers)
        self.ssl_ctx = ssl.create_default_context(cafile=certifi.where())

    def sport_for_league(self, league: str) -> str:
        sport = LEAGUE_TO_SPORT_MAP.get((league or "").strip().lower())
        if not sport:
            raise KeyError(f"Unsupported league={league!r}")
        return sport

    def _get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        full_url = url
        if params:
            query_string = urllib.parse.urlencode(params, doseq=True)
            full_url = f"{url}?{query_string}"

        req = urllib.request.Request(full_url, headers=self.headers)

        try:
            with urllib.request.urlopen(
                req,
                timeout=timeout or self.timeout,
                context=self.ssl_ctx,
            ) as response:
                body = response.read().decode("utf-8")
                data = json.loads(body)
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            json.JSONDecodeError,
        ) as exc:
            raise EspnApiError(
                f"GET {full_url} failed: {type(exc).__name__}: {exc}"
            ) from exc

        if not isinstance(data, dict):
            raise EspnApiError(f"GET {full_url} returned non-object JSON")

        return data

    def get_scoreboard(
        self,
        *,
        league: str,
        date: str | None = None,
        limit: int = 500,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)
        params: dict[str, Any] = {"limit": limit}
        if date:
            params["dates"] = date

        return self._get(
            f"{self.SITE_API_BASE}/sports/{sport}/{league}/scoreboard",
            params=params,
            timeout=timeout,
        )

    def get_game_summary(
        self,
        *,
        league: str,
        game_id: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)
        return self._get(
            f"{self.SITE_API_BASE}/sports/{sport}/{league}/summary",
            params={"event": game_id},
            timeout=timeout,
        )

    def get_team_schedule(
        self,
        *,
        league: str,
        team_id: str,
        season: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)
        return self._get(
            f"{self.SITE_API_BASE}/sports/{sport}/{league}/teams/{team_id}/schedule",
            params={"season": season},
            timeout=timeout,
        )

    def get_athlete_eventlog(
        self,
        *,
        league: str,
        athlete_id: str,
        season: str,
        page: int = 1,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)
        return self._get(
            f"{self.CORE_API_BASE}/sports/{sport}/leagues/{league}/seasons/{season}/athletes/{athlete_id}/eventlog",
            params={"page": page},
            timeout=timeout,
        )

    def list_franchises(
        self,
        *,
        league: str,
        limit: int = 1000,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)
        return self._get(
            f"{self.CORE_API_BASE}/sports/{sport}/leagues/{league}/franchises",
            params={"limit": limit},
            timeout=timeout,
        )