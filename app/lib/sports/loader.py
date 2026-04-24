# app/lib/sports/loader.py
"""
Pull ESPN scoreboards into the hypergraph.

Compact hypermedia shape:

    sports.games.<league>.<yyyy>.<mm>.<dd>.<game_id>
    sports.latest.<league>

    sports.teams.<league>.<team-slug-id>
    sports.teams.<league>.<team-slug-id>.refs.games.<matchup>
    sports.teams.<league>.<team-slug-id>.refs.athletes.<athlete-slug-id>

    sports.athletes.<league>.<athlete-slug-id>
    sports.athletes.<league>.<athlete-slug-id>.refs.games.<matchup>

    sports.venues.<venue-slug>
    sports.venues.<venue-slug>.refs.games.<matchup>

    sports.leaderboards.<league>.<yyyy-mm-dd>.<category>
    sports.leaderboards.<league>.<yyyy-mm-dd>.<category>.refs.<rank>-<athlete>

This is intentionally a score-bug / hypermedia loader, not a full ESPN mirror.
"""

from __future__ import annotations

import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from HyperCoreSDK.python.client import HyperClient
from HyperCoreSDK.python.helpers.server import create_hyper_server
from HyperCoreSDK.python.helpers.storage import create_default_storage_directory
from HyperCoreSDK.python.helpers.indexes import ScopeSpec, ValueIndexSpec
from app.utils.dtos.SportsEvent import SportsGame
from app.utils.clients.espn import EspnApiClient, EspnApiError
from app.lib.sports.helpers import games_from_scoreboard, now_utc


SPORTS_ROOT = "sports"

DEFAULT_LEAGUES = ["nba", "nfl", "nhl", "mlb", "eng.1"]
DEFAULT_LOOKBACK_HOURS = 18
DEFAULT_LOOKAHEAD_DAYS = 7


GAME_INDEXES: list[ValueIndexSpec] = [
    ValueIndexSpec(
        name="league",
        path="league",
        normalize="slug",
        link_projections={
            "sport": "sport",
            "start_time": "start_time",
            "status": "status",
            "status_detail": "status_detail",
            "score_bug_mode": "score_bug_mode",
            "matchup": "matchup",
        },
    ),
    ValueIndexSpec(
        name="status",
        path="status",
        normalize="slug",
        scopes=[ScopeSpec(path="league", normalize="slug")],
        link_projections={
            "sport": "sport",
            "start_time": "start_time",
            "status_detail": "status_detail",
            "score_bug_mode": "score_bug_mode",
            "matchup": "matchup",
        },
    ),
    ValueIndexSpec(
        name="score_bug_mode",
        path="score_bug_mode",
        normalize="slug",
        scopes=[ScopeSpec(path="league", normalize="slug")],
        link_projections={
            "sport": "sport",
            "start_time": "start_time",
            "status": "status",
            "status_detail": "status_detail",
            "matchup": "matchup",
        },
    ),
    ValueIndexSpec(
        name="start_day",
        path="start_day",
        normalize="slug",
        scopes=[ScopeSpec(path="league", normalize="slug")],
        link_projections={
            "sport": "sport",
            "start_time": "start_time",
            "status": "status",
            "status_detail": "status_detail",
            "score_bug_mode": "score_bug_mode",
            "matchup": "matchup",
        },
    ),
    ValueIndexSpec(
        name="team",
        path="team_keys",
        normalize="slug",
        multi=True,
        link_projections={
            "league": "league",
            "sport": "sport",
            "start_time": "start_time",
            "status": "status",
            "status_detail": "status_detail",
            "matchup": "matchup",
        },
    ),
]


LEADERBOARD_INDEXES: list[ValueIndexSpec] = [
    ValueIndexSpec(
        name="league",
        path="league",
        normalize="slug",
        link_projections={
            "sport": "sport",
            "date": "date",
            "category": "category",
            "leader_count": "leader_count",
        },
    ),
    ValueIndexSpec(
        name="date",
        path="date",
        normalize="slug",
        scopes=[ScopeSpec(path="league", normalize="slug")],
        link_projections={
            "sport": "sport",
            "category": "category",
            "leader_count": "leader_count",
        },
    ),
    ValueIndexSpec(
        name="category",
        path="category",
        normalize="slug",
        scopes=[
            ScopeSpec(path="league", normalize="slug"),
            ScopeSpec(path="date", normalize="slug"),
        ],
        link_projections={
            "sport": "sport",
            "category_display": "category_display",
            "leader_count": "leader_count",
        },
    ),
]


# ---------------------------------------------------------------------------
# Small primitives
# ---------------------------------------------------------------------------

def slug(value: Any, fallback: str = "unknown") -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", str(value or "").strip().lower())
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text or fallback


def key(name: Any, espn_id: Any, fallback: str) -> str:
    base = slug(name, fallback=fallback)
    ident = str(espn_id or "").strip()
    return f"{base}-{ident}" if ident else base


def parse_dt(value: str) -> datetime:
    text = str(value or "").strip()

    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return now_utc()

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(timezone.utc)


def ms(value: str) -> int:
    return int(parse_dt(value).timestamp() * 1000)


def day(value: str) -> str:
    return parse_dt(value).strftime("%Y-%m-%d")


def stream_link(path: str) -> str:
    return f"{path}?stream=true"


def changes_link(path: str) -> str:
    return f"{path}/api/changes-since"


def team_key(team: Any) -> str:
    return key(
        getattr(team, "display_name", None) or getattr(team, "abbreviation", None),
        getattr(team, "team_id", None),
        "team",
    )


def team_key_from_values(name: Any, team_id: Any) -> str:
    return key(name, team_id, "team")


def athlete_key_from_values(name: Any, athlete_id: Any) -> str:
    return key(name, athlete_id, "athlete")


def team_path(game: SportsGame, team: Any) -> str:
    return f"{SPORTS_ROOT}.teams.{game.league}.{team_key(team)}"


def team_path_from_key(league: str, tkey: str) -> str:
    return f"{SPORTS_ROOT}.teams.{league}.{tkey}"


def athlete_path_from_key(league: str, akey: str) -> str:
    return f"{SPORTS_ROOT}.athletes.{league}.{akey}"


def venue_key(game: SportsGame) -> str:
    return key(
        "-".join(
            str(x)
            for x in [game.venue_name, game.venue_city, game.venue_country]
            if x
        ),
        "",
        "venue",
    )


def venue_path(game: SportsGame) -> str:
    return f"{SPORTS_ROOT}.venues.{venue_key(game)}"


def game_path(game: SportsGame) -> str:
    return f"{SPORTS_ROOT}.games.{game.record_key().replace('/', '.')}"


def leaderboards_day_path(game: SportsGame) -> str:
    return f"{SPORTS_ROOT}.leaderboards.{game.league}.{day(game.start_time)}"


def leaderboard_path(game: SportsGame, category: str) -> str:
    return f"{leaderboards_day_path(game)}.{category}"


# ---------------------------------------------------------------------------
# Score-bug selection
# ---------------------------------------------------------------------------

def score_bug_mode(
    game: SportsGame,
    *,
    now: datetime,
    explicit_date: bool,
    lookback_hours: int,
    lookahead_days: int,
) -> str | None:
    start = parse_dt(game.start_time)
    status = str(game.status or "").lower()

    if explicit_date:
        return {"in": "live", "pre": "future", "post": "past"}.get(status, "window")

    if status == "in":
        return "live"

    if status == "pre" and now <= start <= now + timedelta(days=lookahead_days):
        return "future"

    if status == "post" and now - timedelta(hours=lookback_hours) <= start <= now + timedelta(hours=1):
        return "past"

    if now - timedelta(hours=lookback_hours) <= start <= now + timedelta(days=lookahead_days):
        return "window"

    return None


# ---------------------------------------------------------------------------
# ESPN summary extraction
# ---------------------------------------------------------------------------

def link_map(items: Any) -> dict[str, str]:
    out: dict[str, str] = {}

    if not isinstance(items, list):
        return out

    for item in items:
        if not isinstance(item, dict):
            continue

        href = item.get("href")
        rels = item.get("rel")

        if not href or not isinstance(rels, list):
            continue

        for rel in rels:
            out[str(rel)] = str(href)

    return out


def headshot(value: Any) -> str | None:
    if isinstance(value, dict):
        return value.get("href")
    if isinstance(value, str):
        return value
    return None


def position(value: Any) -> str | None:
    if isinstance(value, dict):
        return value.get("abbreviation") or value.get("displayName") or value.get("name")
    if value:
        return str(value)
    return None


def extract_leaders(summary: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    for team_group in summary.get("leaders") or []:
        if not isinstance(team_group, dict):
            continue

        team = team_group.get("team") if isinstance(team_group.get("team"), dict) else {}
        team_id = str(team.get("id") or "")
        team_name = team.get("displayName") or team.get("name") or ""
        team_abbr = team.get("abbreviation") or ""
        tkey = team_key_from_values(team_name or team_abbr, team_id)

        for category in team_group.get("leaders") or []:
            if not isinstance(category, dict):
                continue

            category_key = slug(category.get("name") or category.get("displayName"), "leader")
            category_display = category.get("displayName") or category.get("name") or category_key

            for rank, item in enumerate(category.get("leaders") or [], start=1):
                if not isinstance(item, dict):
                    continue

                athlete = item.get("athlete") if isinstance(item.get("athlete"), dict) else {}
                athlete_id = str(athlete.get("id") or "").strip()
                display_name = (
                    athlete.get("displayName")
                    or athlete.get("fullName")
                    or athlete.get("shortName")
                )

                if not athlete_id and not display_name:
                    continue

                akey = athlete_key_from_values(display_name or athlete.get("shortName"), athlete_id)

                out.append({
                    "category": category_key,
                    "category_display": category_display,
                    "rank": rank,
                    "value": item.get("value"),
                    "display_value": item.get("displayValue"),
                    "athlete_id": athlete_id,
                    "athlete_key": akey,
                    "display_name": display_name or athlete_id,
                    "short_name": athlete.get("shortName"),
                    "headshot": headshot(athlete.get("headshot")),
                    "position": position(athlete.get("position")),
                    "team_id": team_id,
                    "team_key": tkey,
                    "team_display_name": team_name,
                    "team_abbreviation": team_abbr,
                    "espn_links": link_map(athlete.get("links")),
                })

    return out


def extract_boxscore_athletes(summary: dict[str, Any]) -> list[dict[str, Any]]:
    boxscore = summary.get("boxscore")
    if not isinstance(boxscore, dict):
        return []

    athletes: dict[str, dict[str, Any]] = {}

    for group in boxscore.get("players") or []:
        if not isinstance(group, dict):
            continue

        team = group.get("team") if isinstance(group.get("team"), dict) else {}
        team_id = str(team.get("id") or "")
        team_name = team.get("displayName") or team.get("name") or ""
        team_abbr = team.get("abbreviation") or ""
        tkey = team_key_from_values(team_name or team_abbr, team_id)

        for stat_group in group.get("statistics") or []:
            if not isinstance(stat_group, dict):
                continue

            stat_group_name = str(stat_group.get("type") or stat_group.get("name") or "stats")
            stat_names = stat_group.get("names") or stat_group.get("keys") or []

            for entry in stat_group.get("athletes") or []:
                if not isinstance(entry, dict):
                    continue

                athlete = entry.get("athlete") if isinstance(entry.get("athlete"), dict) else {}
                athlete_id = str(athlete.get("id") or "").strip()
                if not athlete_id:
                    continue

                display_name = (
                    athlete.get("displayName")
                    or athlete.get("fullName")
                    or athlete.get("shortName")
                    or athlete_id
                )
                akey = athlete_key_from_values(display_name, athlete_id)

                stats = entry.get("stats") or []
                stat_map = {
                    str(k): v
                    for k, v in zip(stat_names, stats)
                } if isinstance(stat_names, list) and isinstance(stats, list) else {}

                old = athletes.get(athlete_id, {})
                old_groups = old.get("stat_groups") if isinstance(old.get("stat_groups"), dict) else {}

                athletes[athlete_id] = {
                    **old,
                    "athlete_id": athlete_id,
                    "athlete_key": akey,
                    "display_name": display_name,
                    "short_name": athlete.get("shortName"),
                    "headshot": headshot(athlete.get("headshot")),
                    "jersey": athlete.get("jersey"),
                    "position": position(athlete.get("position")),
                    "team_id": team_id,
                    "team_key": tkey,
                    "team_abbreviation": team_abbr,
                    "espn_links": link_map(athlete.get("links")),
                    "stat_groups": {
                        **old_groups,
                        stat_group_name: stat_map,
                    },
                }

    return list(athletes.values())


def extract_athletes(summary: dict[str, Any]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}

    for row in extract_leaders(summary):
        athlete_id = str(row.get("athlete_id") or "")
        if not athlete_id:
            continue

        by_id[athlete_id] = {
            **by_id.get(athlete_id, {}),
            "athlete_id": athlete_id,
            "athlete_key": row.get("athlete_key"),
            "display_name": row.get("display_name"),
            "short_name": row.get("short_name"),
            "headshot": row.get("headshot"),
            "position": row.get("position"),
            "team_id": row.get("team_id"),
            "team_key": row.get("team_key"),
            "team_abbreviation": row.get("team_abbreviation"),
            "espn_links": row.get("espn_links") or {},
        }

    for row in extract_boxscore_athletes(summary):
        athlete_id = str(row.get("athlete_id") or "")
        if not athlete_id:
            continue

        old = by_id.get(athlete_id, {})
        merged = {**old, **row}

        if old.get("stat_groups") and row.get("stat_groups"):
            merged["stat_groups"] = {
                **old.get("stat_groups", {}),
                **row.get("stat_groups", {}),
            }

        by_id[athlete_id] = merged

    return list(by_id.values())


def extract_plays(summary: dict[str, Any], limit: int = 25) -> list[dict[str, Any]]:
    plays = summary.get("plays")
    if not isinstance(plays, list):
        return []

    out: list[dict[str, Any]] = []

    for play in plays[-limit:]:
        if not isinstance(play, dict):
            continue

        out.append({
            "id": play.get("id"),
            "sequence_number": play.get("sequenceNumber"),
            "text": play.get("text"),
            "short_text": play.get("shortText"),
            "score_value": play.get("scoreValue"),
            "scoring_play": play.get("scoringPlay"),
            "home_score": play.get("homeScore"),
            "away_score": play.get("awayScore"),
            "period": (
                play.get("period", {}).get("number")
                if isinstance(play.get("period"), dict)
                else play.get("period")
            ),
        })

    return out


def summary_bits(summary: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(summary, dict):
        return {
            "leaders": [],
            "athletes": [],
            "plays": [],
            "news": [],
            "odds": None,
            "predictor": None,
            "winprobability": [],
        }

    news = summary.get("news")
    articles = news.get("articles") if isinstance(news, dict) else []

    return {
        "leaders": extract_leaders(summary),
        "athletes": extract_athletes(summary),
        "plays": extract_plays(summary),
        "news": articles if isinstance(articles, list) else [],
        "odds": summary.get("odds"),
        "predictor": summary.get("predictor"),
        "winprobability": (
            summary.get("winprobability")
            if isinstance(summary.get("winprobability"), list)
            else []
        ),
    }


# ---------------------------------------------------------------------------
# Query payloads and hypermedia links
# ---------------------------------------------------------------------------

def game_links(game: SportsGame) -> dict[str, str]:
    path = game_path(game)

    return {
        "home_team": team_path(game, game.home),
        "away_team": team_path(game, game.away),
        "venue": venue_path(game),
        "leaderboards": leaderboards_day_path(game),
        "latest": f"{SPORTS_ROOT}.latest.{game.latest_key()}",
        "league_games": f"{SPORTS_ROOT}.index.by.league.{slug(game.league)}",
        "day_games": f"{SPORTS_ROOT}.index.scoped.league.{slug(game.league)}.start_day.{slug(day(game.start_time))}",
        "status_games": f"{SPORTS_ROOT}.index.scoped.league.{slug(game.league)}.status.{slug(game.status)}",
        "mode_games": f"{SPORTS_ROOT}.index.scoped.league.{slug(game.league)}.score_bug_mode",
        "stream": stream_link(path),
        "changes_since": changes_link(path),
    }


def query_for_game(game: SportsGame, path: str, fetched_at: str, *, mode: str) -> dict[str, Any]:
    return {
        "entity_id": path,
        "entity_type": "sports_game",
        "canonical_path": path,
        "display": f"{game.away.abbreviation} @ {game.home.abbreviation}",
        "text": " ".join(str(x) for x in [
            game.league,
            game.sport,
            game.status,
            game.status_detail,
            game.away.display_name,
            game.away.abbreviation,
            game.home.display_name,
            game.home.abbreviation,
            game.venue_name,
            game.venue_city,
        ] if x),
        "facets": {
            "league": game.league,
            "sport": game.sport,
            "status": game.status,
            "score_bug_mode": mode,
            "start_day": day(game.start_time),
            "home_team_key": team_key(game.home),
            "away_team_key": team_key(game.away),
            "venue_key": venue_key(game),
        },
        "numbers": {
            "home_score": float(game.home.score or 0),
            "away_score": float(game.away.score or 0),
        },
        "times": {
            "start_time": ms(game.start_time),
            "fetched_at": ms(fetched_at),
            "activity_latest_at": max(ms(game.start_time), ms(fetched_at)),
        },
        "refs": {
            "home_team": team_path(game, game.home),
            "away_team": team_path(game, game.away),
            "venue": venue_path(game),
            "leaderboards": leaderboards_day_path(game),
            "team": [
                team_path(game, game.home),
                team_path(game, game.away),
            ],
        },
        "tokens": [
            game.league,
            game.sport,
            game.status,
            game.status_detail,
            game.away.display_name,
            game.away.abbreviation,
            game.home.display_name,
            game.home.abbreviation,
            game.venue_name,
            game.venue_city,
        ],
    }


def payload_for_game(game: SportsGame, *, mode: str, summary: dict[str, Any] | None) -> dict[str, Any]:
    bits = summary_bits(summary)

    payload = game.to_dict()
    payload.update({
        "start_day": day(game.start_time),
        "matchup": f"{game.away.abbreviation} @ {game.home.abbreviation}",
        "score_bug_mode": mode,
        "home_team_key": team_key(game.home),
        "away_team_key": team_key(game.away),
        "team_keys": [team_key(game.home), team_key(game.away)],
        "venue_key": venue_key(game),
        "home_score": game.home.score,
        "away_score": game.away.score,
    })

    if bits["plays"]:
        payload["recent_plays"] = bits["plays"]

    if bits["news"]:
        payload["news"] = bits["news"][:5]

    if bits["odds"]:
        payload["odds"] = bits["odds"]

    if bits["predictor"]:
        payload["predictor"] = bits["predictor"]

    if bits["winprobability"]:
        payload["winprobability_tail"] = bits["winprobability"][-25:]

    return payload


# ---------------------------------------------------------------------------
# Hypermedia writers
# ---------------------------------------------------------------------------

def write_game_refs(client: HyperClient, game: SportsGame) -> None:
    path = game_path(game)

    refs = {
        "home_team": team_path(game, game.home),
        "away_team": team_path(game, game.away),
        "venue": venue_path(game),
        "leaderboards": leaderboards_day_path(game),
        "latest": f"{SPORTS_ROOT}.latest.{game.latest_key()}",
    }

    for rel, target in refs.items():
        client.write_backref(
            source=path,
            rel=rel,
            target=target,
            data={
                "kind": "ref",
                "rel": rel,
                "target": target,
            },
        )


def write_team(
    client: HyperClient,
    game: SportsGame,
    team: Any,
    game_abs: str,
) -> str:
    path = team_path(game, team)
    tkey = team_key(team)

    client.write_pointer(
        path=path,
        target=game_abs,
        data={
            "model": "sports-team",
            "team_key": tkey,
            "team_id": team.team_id,
            "espn_id": team.team_id,
            "abbreviation": team.abbreviation,
            "display_name": team.display_name,
            "league": game.league,
            "sport": game.sport,
            "latest_game": game_abs,
        },
        links={
            "latest_game": game_abs,
            "games": f"{path}.refs.games",
            "athletes": f"{path}.refs.athletes",
            "league": f"{SPORTS_ROOT}.index.by.league.{slug(game.league)}",
            "stream": stream_link(path),
            "changes_since": changes_link(path),
        },
        query={
            "entity_id": path,
            "entity_type": "sports_team",
            "canonical_path": path,
            "display": team.display_name,
            "text": f"{team.display_name} {team.abbreviation} {game.league} {game.sport}",
            "facets": {
                "league": game.league,
                "sport": game.sport,
                "team_key": tkey,
                "team_id": team.team_id,
                "abbreviation": team.abbreviation,
            },
            "refs": {
                "latest_game": game_abs,
            },
            "tokens": [
                team.display_name,
                team.abbreviation,
                game.league,
                game.sport,
                tkey,
            ],
        },
    )

    return path


def write_venue(
    client: HyperClient,
    game: SportsGame,
    game_abs: str,
    fetched_at: str,
) -> str:
    path = venue_path(game)
    vkey = venue_key(game)

    client.write_pointer(
        path=path,
        target=game_abs,
        data={
            "model": "sports-venue",
            "venue_key": vkey,
            "name": game.venue_name,
            "city": game.venue_city,
            "country": game.venue_country,
            "latest_game": game_abs,
        },
        links={
            "latest_game": game_abs,
            "games": f"{path}.refs.games",
            "stream": stream_link(path),
            "changes_since": changes_link(path),
        },
        query={
            "entity_id": path,
            "entity_type": "sports_venue",
            "canonical_path": path,
            "display": game.venue_name or vkey,
            "text": " ".join(
                str(x)
                for x in [game.venue_name, game.venue_city, game.venue_country]
                if x
            ),
            "facets": {
                "venue_key": vkey,
                "city": game.venue_city,
                "country": game.venue_country,
            },
            "times": {
                "fetched_at": ms(fetched_at),
                "activity_latest_at": ms(fetched_at),
            },
            "refs": {
                "latest_game": game_abs,
            },
            "tokens": [
                game.venue_name,
                game.venue_city,
                game.venue_country,
                vkey,
            ],
        },
    )

    return path


def write_athletes(
    client: HyperClient,
    *,
    game: SportsGame,
    game_abs: str,
    athletes: list[dict[str, Any]],
    fetched_at: str,
) -> int:
    count = 0

    for athlete in athletes:
        athlete_id = str(athlete.get("athlete_id") or "")
        if not athlete_id:
            continue

        akey = athlete.get("athlete_key") or athlete_key_from_values(
            athlete.get("display_name"),
            athlete_id,
        )
        path = athlete_path_from_key(game.league, akey)

        tkey = athlete.get("team_key") or team_key_from_values(
            athlete.get("team_abbreviation"),
            athlete.get("team_id"),
        )
        tpath = team_path_from_key(game.league, tkey)

        client.write_pointer(
            path=path,
            target=game_abs,
            data={
                "model": "sports-athlete",
                **athlete,
                "athlete_key": akey,
                "league": game.league,
                "sport": game.sport,
                "latest_game": game_abs,
            },
            links={
                "latest_game": game_abs,
                "team": tpath,
                "games": f"{path}.refs.games",
                "stream": stream_link(path),
                "changes_since": changes_link(path),
            },
            query={
                "entity_id": path,
                "entity_type": "sports_athlete",
                "canonical_path": path,
                "display": athlete.get("display_name") or athlete_id,
                "text": " ".join(str(x) for x in [
                    athlete.get("display_name"),
                    athlete.get("short_name"),
                    athlete.get("team_abbreviation"),
                    athlete.get("position"),
                    game.league,
                    game.sport,
                ] if x),
                "facets": {
                    "league": game.league,
                    "sport": game.sport,
                    "athlete_key": akey,
                    "athlete_id": athlete_id,
                    "team_key": tkey,
                    "team_id": athlete.get("team_id") or "",
                    "position": athlete.get("position") or "",
                },
                "refs": {
                    "latest_game": game_abs,
                    "team": tpath,
                },
                "tokens": [
                    athlete.get("display_name"),
                    athlete.get("short_name"),
                    athlete.get("team_abbreviation"),
                    athlete.get("position"),
                    game.league,
                    game.sport,
                    akey,
                ],
            },
        )

        client.write_backref(
            source=path,
            rel=f"games.{game.matchup_key()}",
            target=game_abs,
            data={
                "kind": "sports-game",
                "target": game_abs,
            },
            links={
                "team": tpath,
            },
        )

        count += 1

    return count


def write_team_athlete_refs(
    client: HyperClient,
    *,
    game: SportsGame,
    game_abs: str,
    athletes: list[dict[str, Any]],
) -> None:
    for athlete in athletes:
        athlete_id = str(athlete.get("athlete_id") or "")
        if not athlete_id:
            continue

        akey = athlete.get("athlete_key") or athlete_key_from_values(
            athlete.get("display_name"),
            athlete_id,
        )
        target = athlete_path_from_key(game.league, akey)

        tkey = athlete.get("team_key") or team_key_from_values(
            athlete.get("team_abbreviation"),
            athlete.get("team_id"),
        )
        tpath = team_path_from_key(game.league, tkey)

        client.write_backref(
            source=tpath,
            rel=f"athletes.{akey}",
            target=target,
            data={
                "kind": "athlete-ref",
                "target": target,
                "athlete_id": athlete_id,
                "athlete_key": akey,
                "display_name": athlete.get("display_name"),
                "short_name": athlete.get("short_name"),
                "headshot": athlete.get("headshot"),
                "position": athlete.get("position"),
                "team_id": athlete.get("team_id"),
                "team_key": tkey,
                "team_abbreviation": athlete.get("team_abbreviation"),
                "latest_game": game_abs,
            },
            links={
                "athlete": target,
                "team": tpath,
                "latest_game": game_abs,
            },
        )


def write_leaderboards(
    client: HyperClient,
    *,
    game: SportsGame,
    leaders: list[dict[str, Any]],
    fetched_at: str,
) -> int:
    grouped: dict[str, list[dict[str, Any]]] = {}

    for row in leaders:
        category = slug(row.get("category"), "leader")
        grouped.setdefault(category, []).append(row)

    for category, rows in grouped.items():
        start_day = day(game.start_time)
        rel = f"leaderboards/{game.league}/{start_day}/{category}"
        path = f"{SPORTS_ROOT}.{rel.replace('/', '.')}"
        category_display = str(rows[0].get("category_display") or category)

        clean_rows = []

        for row in rows:
            akey = row.get("athlete_key") or athlete_key_from_values(
                row.get("display_name"),
                row.get("athlete_id"),
            )
            tkey = row.get("team_key") or team_key_from_values(
                row.get("team_abbreviation"),
                row.get("team_id"),
            )

            clean_rows.append({
                "rank": row.get("rank"),
                "value": row.get("value"),
                "display_value": row.get("display_value"),
                "display_name": row.get("display_name"),
                "athlete_id": row.get("athlete_id"),
                "athlete_key": akey,
                "team_id": row.get("team_id"),
                "team_key": tkey,
                "team_abbreviation": row.get("team_abbreviation"),
                "athlete_path": athlete_path_from_key(game.league, akey),
                "team_path": team_path_from_key(game.league, tkey),
                "game_path": game_path(game),
            })

        clean_rows.sort(key=lambda r: int(r.get("rank") or 999999))

        data = {
            "model": "sports-leaderboard",
            "league": game.league,
            "sport": game.sport,
            "date": start_day,
            "category": category,
            "category_display": category_display,
            "leader_count": len(clean_rows),
            "updated_from_game_id": game.game_id,
            "updated_from_game_path": game_path(game),
            "fetched_at": fetched_at,
            "leaders": clean_rows,
        }

        client.write_record_with_indexes(
            root=SPORTS_ROOT,
            record_path=rel,
            record_data=data,
            index_specs=LEADERBOARD_INDEXES,
            ref_key=f"{game.league}-{start_day}-{category}",
            ref_payload=data,
        )

        client.write_pointer(
            path=path,
            target=game_path(game),
            data=data,
            links={
                "updated_from_game": game_path(game),
                "day": leaderboards_day_path(game),
                "league": f"{SPORTS_ROOT}.leaderboards.{game.league}",
                "stream": stream_link(path),
                "changes_since": changes_link(path),
            },
            query={
                "entity_id": path,
                "entity_type": "sports_leaderboard",
                "canonical_path": path,
                "display": f"{game.league} {start_day} {category_display}",
                "text": " ".join(str(x) for x in [
                    game.league,
                    game.sport,
                    start_day,
                    category,
                    category_display,
                    *[r.get("display_name") for r in clean_rows],
                ] if x),
                "facets": {
                    "league": game.league,
                    "sport": game.sport,
                    "date": start_day,
                    "category": category,
                },
                "numbers": {
                    "leader_count": len(clean_rows),
                },
                "times": {
                    "fetched_at": ms(fetched_at),
                    "activity_latest_at": ms(fetched_at),
                },
                "refs": {
                    "updated_from_game": game_path(game),
                },
                "tokens": [
                    game.league,
                    game.sport,
                    start_day,
                    category,
                    category_display,
                    *[r.get("display_name") for r in clean_rows],
                ],
            },
        )

        for idx, row in enumerate(clean_rows, start=1):
            client.write_backref(
                source=path,
                rel=f"{idx:02d}-{row['athlete_key']}",
                target=row.get("athlete_path"),
                data={
                    "kind": "leaderboard-entry",
                    "rank": row.get("rank"),
                    "display_name": row.get("display_name"),
                    "display_value": row.get("display_value"),
                    "athlete_path": row.get("athlete_path"),
                    "team_path": row.get("team_path"),
                    "game_path": row.get("game_path"),
                },
                links={
                    "athlete": row.get("athlete_path"),
                    "team": row.get("team_path"),
                    "game": row.get("game_path"),
                },
            )

    return sum(len(v) for v in grouped.values())


def write_game(
    client: HyperClient,
    game: SportsGame,
    *,
    fetched_at: str,
    summary: dict[str, Any] | None,
    mode: str,
) -> str:
    path = game_path(game)
    rel = f"games/{game.record_key()}"
    data = payload_for_game(game, mode=mode, summary=summary)

    client.write_record_with_indexes(
        root=SPORTS_ROOT,
        record_path=rel,
        record_data=data,
        index_specs=GAME_INDEXES,
        ref_key=game.game_id,
        ref_payload={
            "league": game.league,
            "sport": game.sport,
            "start_time": game.start_time,
            "start_day": day(game.start_time),
            "status": game.status,
            "status_detail": game.status_detail,
            "score_bug_mode": mode,
            "matchup": f"{game.away.abbreviation} @ {game.home.abbreviation}",
            "team_keys": [team_key(game.home), team_key(game.away)],
            "venue_key": venue_key(game),
        },
    )

    client.write_pointer(
        path=path,
        target=path,
        data=data,
        links=game_links(game),
        query=query_for_game(game, path, fetched_at, mode=mode),
    )

    write_game_refs(client, game)

    client.write_pointer(
        path=f"{SPORTS_ROOT}.latest.{game.latest_key()}",
        target=path,
        data={
            **game.latest_dict(path),
            "score_bug_mode": mode,
        },
        links={
            "home_team": team_path(game, game.home),
            "away_team": team_path(game, game.away),
            "venue": venue_path(game),
            "leaderboards": leaderboards_day_path(game),
        },
        query=query_for_game(
            game,
            f"{SPORTS_ROOT}.latest.{game.latest_key()}",
            fetched_at,
            mode=mode,
        ),
    )

    matchup = game.matchup_key()

    for team in (game.home, game.away):
        role = "home" if team.is_home else "away"
        tpath = write_team(client, game, team, path)

        client.write_backref(
            source=tpath,
            rel=f"games.{matchup}",
            target=path,
            data={
                "kind": "sports-game",
                "target": path,
                "role": role,
                "matchup": f"{game.away.abbreviation} @ {game.home.abbreviation}",
                "status": game.status,
                "status_detail": game.status_detail,
                "start_time": game.start_time,
            },
            links={
                "opponent": team_path(game, game.away if team.is_home else game.home),
                "venue": venue_path(game),
            },
        )

    vpath = write_venue(client, game, path, fetched_at)

    client.write_backref(
        source=vpath,
        rel=f"games.{matchup}",
        target=path,
        data={
            "kind": "sports-game",
            "target": path,
            "matchup": f"{game.away.abbreviation} @ {game.home.abbreviation}",
            "status": game.status,
            "status_detail": game.status_detail,
            "start_time": game.start_time,
        },
        links={
            "home_team": team_path(game, game.home),
            "away_team": team_path(game, game.away),
        },
    )

    bits = summary_bits(summary)

    athlete_count = write_athletes(
        client,
        game=game,
        game_abs=path,
        athletes=bits["athletes"],
        fetched_at=fetched_at,
    )

    write_team_athlete_refs(
        client,
        game=game,
        game_abs=path,
        athletes=bits["athletes"],
    )

    leader_count = write_leaderboards(
        client,
        game=game,
        leaders=bits["leaders"],
        fetched_at=fetched_at,
    )

    if athlete_count or leader_count:
        print(f"    enriched: athletes={athlete_count}, leaderboard_rows={leader_count}")

    return path


def sync_league(
    client: HyperClient,
    espn: EspnApiClient,
    *,
    league: str,
    date: str | None = None,
    season: str | None = None,
    limit: int | None = None,
    include_summaries: bool = True,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
    lookahead_days: int = DEFAULT_LOOKAHEAD_DAYS,
) -> int:
    print(f"league: {league}" + (f" — date {date}" if date else ""))

    try:
        sport = espn.sport_for_league(league)
        payload = espn.get_scoreboard(league=league, date=date, season=season)
    except (KeyError, EspnApiError) as exc:
        print(f"  fetch failed: {exc}")
        return 0

    season_guess = season or str(
        (payload.get("season") or {}).get("year")
        or (payload.get("leagues") or [{}])[0].get("season", {}).get("year")
        or now_utc().year
    )

    fetched_at = now_utc().isoformat()
    now = now_utc()

    games = games_from_scoreboard(
        payload,
        league=league,
        sport=sport,
        season=season_guess,
        fetched_at=fetched_at,
    )

    selected = []

    for game in games:
        mode = score_bug_mode(
            game,
            now=now,
            explicit_date=bool(date),
            lookback_hours=lookback_hours,
            lookahead_days=lookahead_days,
        )

        if mode:
            selected.append((game, mode))

    skipped = len(games) - len(selected)

    if skipped:
        print(f"  pruned out-of-window games: {skipped}")

    written = 0

    for game, mode in selected:
        if limit is not None and written >= limit:
            break

        summary = None

        if include_summaries:
            try:
                summary = espn.get_game_summary(league=league, game_id=game.game_id)
            except Exception as exc:
                print(f"    summary skipped for {game.game_id}: {type(exc).__name__}: {exc}")

        try:
            write_game(
                client,
                game,
                fetched_at=fetched_at,
                summary=summary,
                mode=mode,
            )

            written += 1

            print(
                f"  ok: {game.away.abbreviation} {game.away.score} "
                f"@ {game.home.abbreviation} {game.home.score} "
                f"[{game.status_detail}] ({mode})"
            )

        except Exception as exc:
            print(f"  write failed for {game.game_id}: {type(exc).__name__}: {exc}")

    return written


def run(
    client_instance: HyperClient,
    *,
    leagues: list[str] | None = None,
    date: str | None = None,
    season: str | None = None,
    limit_per_league: int | None = None,
    include_summaries: bool = True,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
    lookahead_days: int = DEFAULT_LOOKAHEAD_DAYS,
    close_client: bool = False,
    keep_alive: bool = True,
) -> int:
    espn = EspnApiClient()
    selected_leagues = leagues or DEFAULT_LEAGUES

    total = 0

    try:
        for league in selected_leagues:
            total += sync_league(
                client_instance,
                espn,
                league=league,
                date=date,
                season=season,
                limit=limit_per_league,
                include_summaries=include_summaries,
                lookback_hours=lookback_hours,
                lookahead_days=lookahead_days,
            )

        print(f"done — {total} game(s)")

        if keep_alive:
            print(f"relay still running at {client_instance.url} (Ctrl-C to stop)")
            while True:
                time.sleep(3600)

    except KeyboardInterrupt:
        pass

    finally:
        if close_client:
            client_instance.close()

    return 0


if __name__ == "__main__":
    client = create_hyper_server(
        root=SPORTS_ROOT,
        data_path=create_default_storage_directory(),
    )

    sys.exit(
        run(
            client_instance=client,
            leagues=DEFAULT_LEAGUES,
            date=None,
            season=None,
            limit_per_league=25,
            include_summaries=True,
            lookback_hours=18,
            lookahead_days=7,
            close_client=True,
            keep_alive=True,
        )
    )