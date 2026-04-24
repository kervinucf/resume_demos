# app/utils/clients/espn.py
from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import certifi


DEBUG_ESPN_CLIENT = os.getenv("DEBUG_ESPN_CLIENT", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


LEAGUE_TO_SPORT_MAP: dict[str, str] = {
    "nba": "basketball",
    "wnba": "basketball",
    "mens-college-basketball": "basketball",
    "womens-college-basketball": "basketball",
    "nba-development": "basketball",
    "nbl": "basketball",
    "fiba": "basketball",

    "mlb": "baseball",
    "college-baseball": "baseball",
    "world-baseball-classic": "baseball",
    "dominican-winter-league": "baseball",

    "nfl": "football",
    "college-football": "football",
    "cfl": "football",
    "ufl": "football",
    "xfl": "football",

    "nhl": "hockey",
    "mens-college-hockey": "hockey",
    "womens-college-hockey": "hockey",

    "f1": "racing",
    "irl": "racing",
    "nascar-premier": "racing",
    "nascar-secondary": "racing",
    "nascar-truck": "racing",

    "pga": "golf",
    "lpga": "golf",
    "eur": "golf",
    "liv": "golf",
    "champions-tour": "golf",
    "ntw": "golf",

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
    "fifa.wwc": "soccer",
    "aus.1": "soccer",
    "jpn.1": "soccer",
    "ksa.1": "soccer",

    "atp": "tennis",
    "wta": "tennis",

    "ipl": "cricket",
}


class EspnApiError(RuntimeError):
    pass


def _debug_print(*args: Any) -> None:
    if DEBUG_ESPN_CLIENT:
        print("[ESPN_CLIENT]", *args)


def _short_json(value: Any, *, max_chars: int = 3000) -> str:
    try:
        text = json.dumps(value, indent=2, ensure_ascii=False)
    except Exception:
        text = repr(value)

    if len(text) > max_chars:
        return text[:max_chars] + f"\n... <truncated {len(text) - max_chars} chars>"

    return text


def _summarize_dict(data: dict[str, Any]) -> None:
    if not DEBUG_ESPN_CLIENT:
        return

    _debug_print("top-level keys:", sorted(data.keys()))

    for key in [
        "events",
        "leagues",
        "sports",
        "teams",
        "athletes",
        "leaders",
        "boxscore",
        "competitions",
        "plays",
        "atBats",
        "standings",
        "news",
        "items",
        "categories",
        "injuries",
        "transactions",
        "rosters",
        "gamepackageJSON",
    ]:
        value = data.get(key)

        if isinstance(value, list):
            _debug_print(f"{key}: list len={len(value)}")
            if value:
                first = value[0]
                if isinstance(first, dict):
                    _debug_print(f"{key}[0] keys:", sorted(first.keys()))
                else:
                    _debug_print(f"{key}[0] type:", type(first).__name__)

        elif isinstance(value, dict):
            _debug_print(f"{key}: dict keys={sorted(value.keys())}")

    events = data.get("events")
    if isinstance(events, list) and events and isinstance(events[0], dict):
        _debug_print("events[0] sample:")
        _debug_print(_short_json(events[0], max_chars=2500))

    boxscore = data.get("boxscore")
    if isinstance(boxscore, dict):
        _debug_print("boxscore keys:", sorted(boxscore.keys()))

        players = boxscore.get("players")
        if isinstance(players, list):
            _debug_print("boxscore.players len:", len(players))
            if players and isinstance(players[0], dict):
                _debug_print("boxscore.players[0] keys:", sorted(players[0].keys()))
                _debug_print("boxscore.players[0] sample:")
                _debug_print(_short_json(players[0], max_chars=2500))

        teams = boxscore.get("teams")
        if isinstance(teams, list):
            _debug_print("boxscore.teams len:", len(teams))
            if teams and isinstance(teams[0], dict):
                _debug_print("boxscore.teams[0] keys:", sorted(teams[0].keys()))
                _debug_print("boxscore.teams[0] sample:")
                _debug_print(_short_json(teams[0], max_chars=2500))

    leaders = data.get("leaders")
    if isinstance(leaders, list):
        _debug_print("leaders len:", len(leaders))
        if leaders and isinstance(leaders[0], dict):
            _debug_print("leaders[0] keys:", sorted(leaders[0].keys()))
            _debug_print("leaders[0] sample:")
            _debug_print(_short_json(leaders[0], max_chars=2500))

    competitions = data.get("competitions")
    if isinstance(competitions, list) and competitions and isinstance(competitions[0], dict):
        comp = competitions[0]
        _debug_print("competitions[0] keys:", sorted(comp.keys()))

        competitors = comp.get("competitors")
        if isinstance(competitors, list):
            _debug_print("competitions[0].competitors len:", len(competitors))
            if competitors and isinstance(competitors[0], dict):
                _debug_print(
                    "competitions[0].competitors[0] keys:",
                    sorted(competitors[0].keys()),
                )
                _debug_print("competitions[0].competitors[0] sample:")
                _debug_print(_short_json(competitors[0], max_chars=2500))


class EspnApiClient:
    """
    Rich transport client for ESPN's public JSON surfaces.

    This client does not decide your storage schema. It gives loaders access to:
      - scoreboards for past/present/future score bugs
      - game summaries
      - CDN game packages / boxscores / play-by-play
      - teams, rosters, schedules
      - athletes, athlete overview/stats/gamelogs
      - league leaders, standings, injuries, transactions, news
      - venues, franchises, odds/predictor/play endpoints where ids are known
    """

    SITE_API_BASE = "https://site.api.espn.com/apis/site/v2"
    SITE_API_V3_BASE = "https://site.api.espn.com/apis/site/v3"
    SITE_API_V2_ALT_BASE = "https://site.api.espn.com/apis/v2"
    SITE_WEB_API_BASE = "https://site.web.api.espn.com/apis"
    CORE_API_BASE = "https://sports.core.api.espn.com/v2"
    CORE_API_V3_BASE = "https://sports.core.api.espn.com/v3"
    CDN_BASE = "https://cdn.espn.com/core"
    NOW_BASE = "https://now.core.api.espn.com/v1"

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
        normalized = (league or "").strip().lower()
        sport = LEAGUE_TO_SPORT_MAP.get(normalized)

        _debug_print(
            "sport_for_league:",
            {"league": league, "normalized": normalized, "sport": sport},
        )

        if not sport:
            raise KeyError(f"Unsupported league={league!r}")

        return sport

    def _get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: int | None = None,
        label: str | None = None,
        debug: bool | None = None,
    ) -> dict[str, Any]:
        full_url = url
        if params:
            clean_params = {
                k: v
                for k, v in params.items()
                if v is not None and v != ""
            }
            query_string = urllib.parse.urlencode(clean_params, doseq=True)
            if query_string:
                full_url = f"{url}?{query_string}"

        should_debug = DEBUG_ESPN_CLIENT if debug is None else bool(debug)

        if should_debug:
            _debug_print("=" * 100)
            _debug_print("REQUEST", label or "")
            _debug_print("URL:", full_url)

        req = urllib.request.Request(full_url, headers=self.headers)

        try:
            with urllib.request.urlopen(
                req,
                timeout=timeout or self.timeout,
                context=self.ssl_ctx,
            ) as response:
                status = getattr(response, "status", None)
                headers = dict(response.headers.items())
                body = response.read().decode("utf-8")
                data = json.loads(body)

                if should_debug:
                    _debug_print("STATUS:", status)
                    _debug_print("CONTENT-TYPE:", headers.get("Content-Type"))
                    _debug_print("BODY CHARS:", len(body))

        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            json.JSONDecodeError,
        ) as exc:
            if should_debug:
                _debug_print("FAILED:", type(exc).__name__, exc)
            raise EspnApiError(
                f"GET {full_url} failed: {type(exc).__name__}: {exc}"
            ) from exc

        if not isinstance(data, dict):
            if should_debug:
                _debug_print("NON-DICT RESPONSE TYPE:", type(data).__name__)
            raise EspnApiError(f"GET {full_url} returned non-object JSON")

        if should_debug:
            _summarize_dict(data)

        return data

    # ------------------------------------------------------------------
    # Scoreboards / game surfaces
    # ------------------------------------------------------------------

    def get_scoreboard(
        self,
        *,
        league: str,
        date: str | None = None,
        dates: str | None = None,
        week: int | str | None = None,
        season: int | str | None = None,
        seasontype: int | str | None = None,
        groups: int | str | None = None,
        limit: int = 500,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        params: dict[str, Any] = {"limit": limit}
        if dates:
            params["dates"] = dates
        elif date:
            params["dates"] = date
        if week is not None:
            params["week"] = week
        if season is not None:
            params["season"] = season
        if seasontype is not None:
            params["seasontype"] = seasontype
        if groups is not None:
            params["groups"] = groups

        return self._get(
            f"{self.SITE_API_BASE}/sports/{sport}/{league}/scoreboard",
            params=params,
            timeout=timeout,
            label=f"get_scoreboard league={league} date={date or dates}",
        )

    def get_scoreboard_v3(
        self,
        *,
        league: str,
        date: str | None = None,
        dates: str | None = None,
        limit: int = 500,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        params: dict[str, Any] = {"limit": limit}
        if dates:
            params["dates"] = dates
        elif date:
            params["dates"] = date

        return self._get(
            f"{self.SITE_API_V3_BASE}/sports/{sport}/{league}/scoreboard",
            params=params,
            timeout=timeout,
            label=f"get_scoreboard_v3 league={league} date={date or dates}",
        )

    def get_scoreboard_window(
        self,
        *,
        league: str,
        dates: str,
        limit: int = 500,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        return self.get_scoreboard(
            league=league,
            dates=dates,
            limit=limit,
            timeout=timeout,
        )

    def get_scoreboard_header(
        self,
        *,
        sport: str | None = None,
        league: str | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if sport:
            params["sport"] = sport
        if league:
            params["league"] = league

        return self._get(
            f"{self.SITE_WEB_API_BASE}/v2/scoreboard/header",
            params=params or None,
            timeout=timeout,
            label=f"get_scoreboard_header sport={sport} league={league}",
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
            label=f"get_game_summary league={league} game_id={game_id}",
        )

    def get_game_summary_v3(
        self,
        *,
        league: str,
        game_id: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        return self._get(
            f"{self.SITE_API_V3_BASE}/sports/{sport}/{league}/summary",
            params={"event": game_id},
            timeout=timeout,
            label=f"get_game_summary_v3 league={league} game_id={game_id}",
        )

    def get_cdn_scoreboard(
        self,
        *,
        sport: str,
        league: str | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"xhr": 1}
        if league:
            params["league"] = league

        return self._get(
            f"{self.CDN_BASE}/{sport}/scoreboard",
            params=params,
            timeout=timeout,
            label=f"get_cdn_scoreboard sport={sport} league={league}",
        )

    def get_cdn_game(
        self,
        *,
        sport: str,
        game_id: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        return self._get(
            f"{self.CDN_BASE}/{sport}/game",
            params={"xhr": 1, "gameId": game_id},
            timeout=timeout,
            label=f"get_cdn_game sport={sport} game_id={game_id}",
        )

    def get_cdn_boxscore(
        self,
        *,
        sport: str,
        game_id: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        return self._get(
            f"{self.CDN_BASE}/{sport}/boxscore",
            params={"xhr": 1, "gameId": game_id},
            timeout=timeout,
            label=f"get_cdn_boxscore sport={sport} game_id={game_id}",
        )

    def get_cdn_playbyplay(
        self,
        *,
        sport: str,
        game_id: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        return self._get(
            f"{self.CDN_BASE}/{sport}/playbyplay",
            params={"xhr": 1, "gameId": game_id},
            timeout=timeout,
            label=f"get_cdn_playbyplay sport={sport} game_id={game_id}",
        )

    # ------------------------------------------------------------------
    # Teams / schedules / rosters
    # ------------------------------------------------------------------

    def get_teams(
        self,
        *,
        league: str,
        limit: int = 1000,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        return self._get(
            f"{self.SITE_API_BASE}/sports/{sport}/{league}/teams",
            params={"limit": limit},
            timeout=timeout,
            label=f"get_teams league={league}",
        )

    def get_team(
        self,
        *,
        league: str,
        team_id: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        return self._get(
            f"{self.SITE_API_BASE}/sports/{sport}/{league}/teams/{team_id}",
            timeout=timeout,
            label=f"get_team league={league} team_id={team_id}",
        )

    def get_team_roster(
        self,
        *,
        league: str,
        team_id: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        return self._get(
            f"{self.SITE_API_BASE}/sports/{sport}/{league}/teams/{team_id}/roster",
            timeout=timeout,
            label=f"get_team_roster league={league} team_id={team_id}",
        )

    def get_team_schedule(
        self,
        *,
        league: str,
        team_id: str,
        season: str | int,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        return self._get(
            f"{self.SITE_API_BASE}/sports/{sport}/{league}/teams/{team_id}/schedule",
            params={"season": season},
            timeout=timeout,
            label=f"get_team_schedule league={league} team_id={team_id} season={season}",
        )

    def get_team_depthcharts(
        self,
        *,
        league: str,
        team_id: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        return self._get(
            f"{self.SITE_API_BASE}/sports/{sport}/{league}/teams/{team_id}/depthcharts",
            timeout=timeout,
            label=f"get_team_depthcharts league={league} team_id={team_id}",
        )

    def get_team_injuries(
        self,
        *,
        league: str,
        team_id: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        return self._get(
            f"{self.SITE_API_BASE}/sports/{sport}/{league}/teams/{team_id}/injuries",
            timeout=timeout,
            label=f"get_team_injuries league={league} team_id={team_id}",
        )

    def get_team_transactions(
        self,
        *,
        league: str,
        team_id: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        return self._get(
            f"{self.SITE_API_BASE}/sports/{sport}/{league}/teams/{team_id}/transactions",
            timeout=timeout,
            label=f"get_team_transactions league={league} team_id={team_id}",
        )

    def get_team_history(
        self,
        *,
        league: str,
        team_id: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        return self._get(
            f"{self.SITE_API_BASE}/sports/{sport}/{league}/teams/{team_id}/history",
            timeout=timeout,
            label=f"get_team_history league={league} team_id={team_id}",
        )

    # ------------------------------------------------------------------
    # Athletes
    # ------------------------------------------------------------------

    def get_athlete(
        self,
        *,
        league: str,
        athlete_id: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        return self._get(
            f"{self.SITE_API_BASE}/sports/{sport}/{league}/athletes/{athlete_id}",
            timeout=timeout,
            label=f"get_athlete league={league} athlete_id={athlete_id}",
        )

    def get_athlete_bio(
        self,
        *,
        league: str,
        athlete_id: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        return self._get(
            f"{self.SITE_API_BASE}/sports/{sport}/{league}/athletes/{athlete_id}/bio",
            timeout=timeout,
            label=f"get_athlete_bio league={league} athlete_id={athlete_id}",
        )

    def get_athlete_news(
        self,
        *,
        league: str,
        athlete_id: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        return self._get(
            f"{self.SITE_API_BASE}/sports/{sport}/{league}/athletes/{athlete_id}/news",
            timeout=timeout,
            label=f"get_athlete_news league={league} athlete_id={athlete_id}",
        )

    def get_athlete_overview(
        self,
        *,
        league: str,
        athlete_id: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        return self._get(
            f"{self.SITE_WEB_API_BASE}/common/v3/sports/{sport}/{league}/athletes/{athlete_id}/overview",
            timeout=timeout,
            label=f"get_athlete_overview league={league} athlete_id={athlete_id}",
        )

    def get_athlete_stats(
        self,
        *,
        league: str,
        athlete_id: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        return self._get(
            f"{self.SITE_WEB_API_BASE}/common/v3/sports/{sport}/{league}/athletes/{athlete_id}/stats",
            timeout=timeout,
            label=f"get_athlete_stats league={league} athlete_id={athlete_id}",
        )

    def get_athlete_gamelog(
        self,
        *,
        league: str,
        athlete_id: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        return self._get(
            f"{self.SITE_WEB_API_BASE}/common/v3/sports/{sport}/{league}/athletes/{athlete_id}/gamelog",
            timeout=timeout,
            label=f"get_athlete_gamelog league={league} athlete_id={athlete_id}",
        )

    def get_athlete_splits(
        self,
        *,
        league: str,
        athlete_id: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        return self._get(
            f"{self.SITE_WEB_API_BASE}/common/v3/sports/{sport}/{league}/athletes/{athlete_id}/splits",
            timeout=timeout,
            label=f"get_athlete_splits league={league} athlete_id={athlete_id}",
        )

    def get_athlete_eventlog(
        self,
        *,
        league: str,
        athlete_id: str,
        season: str | int,
        page: int = 1,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        return self._get(
            f"{self.CORE_API_BASE}/sports/{sport}/leagues/{league}/seasons/{season}/athletes/{athlete_id}/eventlog",
            params={"page": page},
            timeout=timeout,
            label=f"get_athlete_eventlog league={league} athlete_id={athlete_id} season={season}",
        )

    def get_core_athlete(
        self,
        *,
        league: str,
        athlete_id: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        return self._get(
            f"{self.CORE_API_BASE}/sports/{sport}/leagues/{league}/athletes/{athlete_id}",
            timeout=timeout,
            label=f"get_core_athlete league={league} athlete_id={athlete_id}",
        )

    def get_core_athletes(
        self,
        *,
        league: str,
        season: str | int | None = None,
        limit: int = 1000,
        page: int | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)
        params: dict[str, Any] = {"limit": limit}
        if page is not None:
            params["page"] = page

        if season:
            url = f"{self.CORE_API_BASE}/sports/{sport}/leagues/{league}/seasons/{season}/athletes"
        else:
            url = f"{self.CORE_API_BASE}/sports/{sport}/leagues/{league}/athletes"

        return self._get(
            url,
            params=params,
            timeout=timeout,
            label=f"get_core_athletes league={league} season={season}",
        )

    def get_core_athlete_statistics(
        self,
        *,
        league: str,
        athlete_id: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        return self._get(
            f"{self.CORE_API_BASE}/sports/{sport}/leagues/{league}/athletes/{athlete_id}/statistics",
            timeout=timeout,
            label=f"get_core_athlete_statistics league={league} athlete_id={athlete_id}",
        )

    def get_athletes_v3(
        self,
        *,
        league: str,
        limit: int = 1000,
        page: int | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)
        params: dict[str, Any] = {"limit": limit}
        if page is not None:
            params["page"] = page

        return self._get(
            f"{self.CORE_API_V3_BASE}/sports/{sport}/{league}/athletes",
            params=params,
            timeout=timeout,
            label=f"get_athletes_v3 league={league}",
        )

    def get_athlete_v3(
        self,
        *,
        league: str,
        athlete_id: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        return self._get(
            f"{self.CORE_API_V3_BASE}/sports/{sport}/{league}/athletes/{athlete_id}",
            timeout=timeout,
            label=f"get_athlete_v3 league={league} athlete_id={athlete_id}",
        )

    # ------------------------------------------------------------------
    # Stats / leaders
    # ------------------------------------------------------------------

    def get_leaders(
        self,
        *,
        league: str,
        season: str | int | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)
        params: dict[str, Any] = {}
        if season:
            params["season"] = season

        return self._get(
            f"{self.CORE_API_BASE}/sports/{sport}/leagues/{league}/leaders",
            params=params or None,
            timeout=timeout,
            label=f"get_leaders league={league} season={season}",
        )

    def get_leaders_v3(
        self,
        *,
        league: str,
        season: str | int | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)
        params: dict[str, Any] = {}
        if season:
            params["season"] = season

        return self._get(
            f"{self.CORE_API_V3_BASE}/sports/{sport}/{league}/leaders",
            params=params or None,
            timeout=timeout,
            label=f"get_leaders_v3 league={league} season={season}",
        )

    def get_statistics_by_athlete(
        self,
        *,
        league: str,
        category: str | None = None,
        sort: str | None = None,
        limit: int = 50,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)
        params: dict[str, Any] = {"limit": limit}
        if category:
            params["category"] = category
        if sort:
            params["sort"] = sort

        return self._get(
            f"{self.SITE_WEB_API_BASE}/common/v3/sports/{sport}/{league}/statistics/byathlete",
            params=params,
            timeout=timeout,
            label=f"get_statistics_by_athlete league={league} category={category} sort={sort}",
        )

    # ------------------------------------------------------------------
    # League-wide surfaces
    # ------------------------------------------------------------------

    def get_standings(
        self,
        *,
        league: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        return self._get(
            f"{self.SITE_API_V2_ALT_BASE}/sports/{sport}/{league}/standings",
            timeout=timeout,
            label=f"get_standings league={league}",
        )

    def get_groups(
        self,
        *,
        league: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        return self._get(
            f"{self.SITE_API_BASE}/sports/{sport}/{league}/groups",
            timeout=timeout,
            label=f"get_groups league={league}",
        )

    def get_calendar(
        self,
        *,
        league: str,
        calendar_type: str | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        suffix = f"/calendar/{calendar_type}" if calendar_type else "/calendar"

        return self._get(
            f"{self.SITE_API_BASE}/sports/{sport}/{league}{suffix}",
            timeout=timeout,
            label=f"get_calendar league={league} calendar_type={calendar_type}",
        )

    def get_news(
        self,
        *,
        league: str,
        limit: int = 25,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        return self._get(
            f"{self.SITE_API_BASE}/sports/{sport}/{league}/news",
            params={"limit": limit},
            timeout=timeout,
            label=f"get_news league={league}",
        )

    def get_now_news(
        self,
        *,
        league: str | None = None,
        sport: str | None = None,
        team: str | None = None,
        limit: int = 25,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if league:
            params["leagues"] = league
        if sport:
            params["sport"] = sport
        if team:
            params["team"] = team

        return self._get(
            f"{self.NOW_BASE}/sports/news",
            params=params,
            timeout=timeout,
            label=f"get_now_news league={league} sport={sport} team={team}",
        )

    def get_league_injuries(
        self,
        *,
        league: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        return self._get(
            f"{self.SITE_API_BASE}/sports/{sport}/{league}/injuries",
            timeout=timeout,
            label=f"get_league_injuries league={league}",
        )

    def get_league_transactions(
        self,
        *,
        league: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        return self._get(
            f"{self.SITE_API_BASE}/sports/{sport}/{league}/transactions",
            timeout=timeout,
            label=f"get_league_transactions league={league}",
        )

    def get_rankings(
        self,
        *,
        league: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        return self._get(
            f"{self.SITE_API_BASE}/sports/{sport}/{league}/rankings",
            timeout=timeout,
            label=f"get_rankings league={league}",
        )

    # ------------------------------------------------------------------
    # Core resources
    # ------------------------------------------------------------------

    def get_core_events(
        self,
        *,
        league: str,
        season: str | int | None = None,
        limit: int = 1000,
        page: int | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)
        params: dict[str, Any] = {"limit": limit}
        if page is not None:
            params["page"] = page

        if season:
            url = f"{self.CORE_API_BASE}/sports/{sport}/leagues/{league}/seasons/{season}/events"
        else:
            url = f"{self.CORE_API_BASE}/sports/{sport}/leagues/{league}/events"

        return self._get(
            url,
            params=params,
            timeout=timeout,
            label=f"get_core_events league={league} season={season}",
        )

    def get_core_event(
        self,
        *,
        league: str,
        event_id: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        return self._get(
            f"{self.CORE_API_BASE}/sports/{sport}/leagues/{league}/events/{event_id}",
            timeout=timeout,
            label=f"get_core_event league={league} event_id={event_id}",
        )

    def get_core_teams(
        self,
        *,
        league: str,
        season: str | int | None = None,
        limit: int = 1000,
        page: int | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)
        params: dict[str, Any] = {"limit": limit}
        if page is not None:
            params["page"] = page

        if season:
            url = f"{self.CORE_API_BASE}/sports/{sport}/leagues/{league}/seasons/{season}/teams"
        else:
            url = f"{self.CORE_API_BASE}/sports/{sport}/leagues/{league}/teams"

        return self._get(
            url,
            params=params,
            timeout=timeout,
            label=f"get_core_teams league={league} season={season}",
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
            label=f"list_franchises league={league}",
        )

    def get_venues(
        self,
        *,
        league: str,
        limit: int = 1000,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        return self._get(
            f"{self.CORE_API_BASE}/sports/{sport}/leagues/{league}/venues",
            params={"limit": limit},
            timeout=timeout,
            label=f"get_venues league={league}",
        )

    def get_seasons(
        self,
        *,
        league: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        return self._get(
            f"{self.CORE_API_BASE}/sports/{sport}/leagues/{league}/seasons",
            timeout=timeout,
            label=f"get_seasons league={league}",
        )

    # ------------------------------------------------------------------
    # Event competition details
    # ------------------------------------------------------------------

    def get_competition_odds(
        self,
        *,
        league: str,
        event_id: str,
        competition_id: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        return self._get(
            f"{self.CORE_API_BASE}/sports/{sport}/leagues/{league}/events/{event_id}/competitions/{competition_id}/odds",
            timeout=timeout,
            label=f"get_competition_odds league={league} event_id={event_id}",
        )

    def get_competition_probabilities(
        self,
        *,
        league: str,
        event_id: str,
        competition_id: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        return self._get(
            f"{self.CORE_API_BASE}/sports/{sport}/leagues/{league}/events/{event_id}/competitions/{competition_id}/probabilities",
            timeout=timeout,
            label=f"get_competition_probabilities league={league} event_id={event_id}",
        )

    def get_competition_predictor(
        self,
        *,
        league: str,
        event_id: str,
        competition_id: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        return self._get(
            f"{self.CORE_API_BASE}/sports/{sport}/leagues/{league}/events/{event_id}/competitions/{competition_id}/predictor",
            timeout=timeout,
            label=f"get_competition_predictor league={league} event_id={event_id}",
        )

    def get_competition_powerindex(
        self,
        *,
        league: str,
        event_id: str,
        competition_id: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        return self._get(
            f"{self.CORE_API_BASE}/sports/{sport}/leagues/{league}/events/{event_id}/competitions/{competition_id}/powerindex",
            timeout=timeout,
            label=f"get_competition_powerindex league={league} event_id={event_id}",
        )

    def get_competition_plays(
        self,
        *,
        league: str,
        event_id: str,
        competition_id: str,
        limit: int = 1000,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        return self._get(
            f"{self.CORE_API_BASE}/sports/{sport}/leagues/{league}/events/{event_id}/competitions/{competition_id}/plays",
            params={"limit": limit},
            timeout=timeout,
            label=f"get_competition_plays league={league} event_id={event_id}",
        )

    def get_competition_situation(
        self,
        *,
        league: str,
        event_id: str,
        competition_id: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        return self._get(
            f"{self.CORE_API_BASE}/sports/{sport}/leagues/{league}/events/{event_id}/competitions/{competition_id}/situation",
            timeout=timeout,
            label=f"get_competition_situation league={league} event_id={event_id}",
        )

    def get_competition_broadcasts(
        self,
        *,
        league: str,
        event_id: str,
        competition_id: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        return self._get(
            f"{self.CORE_API_BASE}/sports/{sport}/leagues/{league}/events/{event_id}/competitions/{competition_id}/broadcasts",
            timeout=timeout,
            label=f"get_competition_broadcasts league={league} event_id={event_id}",
        )

    def get_competitor_statistics(
        self,
        *,
        league: str,
        event_id: str,
        competition_id: str,
        competitor_id: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        return self._get(
            f"{self.CORE_API_BASE}/sports/{sport}/leagues/{league}/events/{event_id}/competitions/{competition_id}/competitors/{competitor_id}/statistics",
            timeout=timeout,
            label=f"get_competitor_statistics league={league} event_id={event_id} competitor_id={competitor_id}",
        )

    def get_competitor_linescores(
        self,
        *,
        league: str,
        event_id: str,
        competition_id: str,
        competitor_id: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        return self._get(
            f"{self.CORE_API_BASE}/sports/{sport}/leagues/{league}/events/{event_id}/competitions/{competition_id}/competitors/{competitor_id}/linescores",
            timeout=timeout,
            label=f"get_competitor_linescores league={league} event_id={event_id} competitor_id={competitor_id}",
        )

    # ------------------------------------------------------------------
    # Coaching / power / futures
    # ------------------------------------------------------------------

    def get_coaches(
        self,
        *,
        league: str,
        season: str | int,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        return self._get(
            f"{self.CORE_API_BASE}/sports/{sport}/leagues/{league}/seasons/{season}/coaches",
            timeout=timeout,
            label=f"get_coaches league={league} season={season}",
        )

    def get_coach(
        self,
        *,
        league: str,
        coach_id: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        return self._get(
            f"{self.CORE_API_BASE}/sports/{sport}/leagues/{league}/coaches/{coach_id}",
            timeout=timeout,
            label=f"get_coach league={league} coach_id={coach_id}",
        )

    def get_power_index(
        self,
        *,
        league: str,
        season: str | int,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        return self._get(
            f"{self.CORE_API_BASE}/sports/{sport}/leagues/{league}/seasons/{season}/powerindex",
            timeout=timeout,
            label=f"get_power_index league={league} season={season}",
        )

    def get_power_index_leaders(
        self,
        *,
        league: str,
        season: str | int,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        return self._get(
            f"{self.CORE_API_BASE}/sports/{sport}/leagues/{league}/seasons/{season}/powerindex/leaders",
            timeout=timeout,
            label=f"get_power_index_leaders league={league} season={season}",
        )

    def get_futures(
        self,
        *,
        league: str,
        season: str | int,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        sport = self.sport_for_league(league)

        return self._get(
            f"{self.CORE_API_BASE}/sports/{sport}/leagues/{league}/seasons/{season}/futures",
            timeout=timeout,
            label=f"get_futures league={league} season={season}",
        )

    # ------------------------------------------------------------------
    # Generic escape hatch
    # ------------------------------------------------------------------

    def get_url(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        label: str | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        return self._get(
            url,
            params=params,
            timeout=timeout,
            label=label or "get_url",
        )