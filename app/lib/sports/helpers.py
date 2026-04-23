from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.utils.dtos.SportsGame import GameTeam, SportsGame


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(raw: str | None) -> str:
    """Normalize ESPN's ISO strings (usually already UTC with 'Z') to ISO-8601."""
    if not raw:
        return now_utc().isoformat()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except Exception:
        return now_utc().isoformat()


def _as_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def _competitor(c: dict[str, Any]) -> GameTeam | None:
    team = c.get("team") or {}
    team_id = str(team.get("id") or "").strip()
    if not team_id:
        return None

    winner = c.get("winner")
    return GameTeam(
        team_id=team_id,
        abbreviation=str(team.get("abbreviation") or "").upper(),
        display_name=str(team.get("displayName") or team.get("name") or ""),
        score=_as_int(c.get("score")),
        is_home=(str(c.get("homeAway") or "").lower() == "home"),
        is_winner=bool(winner) if winner is not None else None,
    )


def game_from_event(
    event: dict[str, Any],
    *,
    league: str,
    sport: str,
    season: str,
    fetched_at: str | None = None,
) -> SportsGame | None:
    """
    Build a SportsGame from one element of
    `GET /scoreboard`'s `events[]` array. Returns None if required
    fields are missing (malformed event, postponed with no teams, etc.).
    """
    game_id = str(event.get("id") or "").strip()
    if not game_id:
        return None

    comps = (event.get("competitions") or [{}])[0]
    competitors = comps.get("competitors") or []
    if len(competitors) < 2:
        return None

    teams = [_competitor(c) for c in competitors]
    teams = [t for t in teams if t is not None]
    if len(teams) < 2:
        return None

    home = next((t for t in teams if t.is_home), None)
    away = next((t for t in teams if not t.is_home), None)
    if home is None or away is None:
        # Fall back to ordering if homeAway is missing
        home, away = teams[0], teams[1]

    status = (event.get("status") or {}).get("type") or {}
    venue = comps.get("venue") or {}
    venue_addr = venue.get("address") or {}

    return SportsGame(
        game_id=game_id,
        league=league,
        sport=sport,
        season=season,
        start_time=_parse_iso(event.get("date")),
        status=str(status.get("state") or "").lower(),
        status_detail=str(status.get("shortDetail") or status.get("detail") or ""),
        venue_name=str(venue.get("fullName") or ""),
        venue_city=str(venue_addr.get("city") or ""),
        venue_country=str(venue_addr.get("country") or ""),
        home=home,
        away=away,
        fetched_at=fetched_at or now_utc().isoformat(),
    )


def games_from_scoreboard(
    payload: dict[str, Any],
    *,
    league: str,
    sport: str,
    season: str,
    fetched_at: str | None = None,
) -> list[SportsGame]:
    """Pure transform: a `/scoreboard` payload → list of SportsGame DTOs."""
    fetched = fetched_at or now_utc().isoformat()
    out: list[SportsGame] = []
    for event in payload.get("events") or []:
        game = game_from_event(
            event,
            league=league,
            sport=sport,
            season=season,
            fetched_at=fetched,
        )
        if game is not None:
            out.append(game)
    return out