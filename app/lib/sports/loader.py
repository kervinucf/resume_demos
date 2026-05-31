# app/lib/sports/loader.py
"""
Pull ESPN scoreboards into root="sports".

    sports.games.<league>.<yyyy>.<mm>.<dd>.<game_id>     game record (+ indexes)
    sports.latest.<league>                               newest game per league
    sports.teams.<league>.<team>                         team, with refs.games / refs.athletes
    sports.athletes.<league>.<athlete>                   athlete, with refs.games
    sports.venues.<venue>                                venue, with refs.games
    sports.leaderboards.<league>.<day>.<category>        leaderboard (+ indexes)

Every node is one plain dict. Search/facets/numbers/refs are inferred by the
relay from the data, its `kind`, and its links — no hand-written query payloads.
This is a score-bug / hypermedia loader, not a full ESPN mirror.
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from HyperCoreSDK.python.helpers.loader import Loader, projection
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
        "league", "league", normalize="slug",
        link_projections=projection("sport", "start_time", "status", "status_detail", "score_bug_mode", "matchup"),
    ),
    ValueIndexSpec(
        "status", "status", normalize="slug",
        scopes=[ScopeSpec("league", normalize="slug")],
        link_projections=projection("sport", "start_time", "status_detail", "score_bug_mode", "matchup"),
    ),
    ValueIndexSpec(
        "score_bug_mode", "score_bug_mode", normalize="slug",
        scopes=[ScopeSpec("league", normalize="slug")],
        link_projections=projection("sport", "start_time", "status", "status_detail", "matchup"),
    ),
    ValueIndexSpec(
        "start_day", "start_day", normalize="slug",
        scopes=[ScopeSpec("league", normalize="slug")],
        link_projections=projection("sport", "start_time", "status", "status_detail", "score_bug_mode", "matchup"),
    ),
    ValueIndexSpec(
        "team", "team_keys", normalize="slug", multi=True,
        link_projections=projection("league", "sport", "start_time", "status", "status_detail", "matchup"),
    ),
]

LEADERBOARD_INDEXES: list[ValueIndexSpec] = [
    ValueIndexSpec(
        "league", "league", normalize="slug",
        link_projections=projection("sport", "date", "category", "leader_count"),
    ),
    ValueIndexSpec(
        "date", "date", normalize="slug",
        scopes=[ScopeSpec("league", normalize="slug")],
        link_projections=projection("sport", "category", "leader_count"),
    ),
    ValueIndexSpec(
        "category", "category", normalize="slug",
        scopes=[ScopeSpec("league", normalize="slug"), ScopeSpec("date", normalize="slug")],
        link_projections=projection("sport", "category_display", "leader_count"),
    ),
]


# ---------------------------------------------------------------------------
# Small primitives
# ---------------------------------------------------------------------------

def slug(value: Any, fallback: str = "unknown") -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", str(value or "").strip().lower())
    return re.sub(r"-{2,}", "-", text).strip("-") or fallback


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
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def day(value: str) -> str:
    return parse_dt(value).strftime("%Y-%m-%d")


def stream_link(path: str) -> str:
    return f"{path}?stream=true"


def changes_link(path: str) -> str:
    return f"{path}/api/changes-since"


def team_key(team: Any) -> str:
    return key(getattr(team, "display_name", None) or getattr(team, "abbreviation", None),
               getattr(team, "team_id", None), "team")


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
    return key("-".join(str(x) for x in [game.venue_name, game.venue_city, game.venue_country] if x), "", "venue")


def venue_path(game: SportsGame) -> str:
    return f"{SPORTS_ROOT}.venues.{venue_key(game)}"


def game_path(game: SportsGame) -> str:
    return f"{SPORTS_ROOT}.games.{game.record_key().replace('/', '.')}"


def leaderboards_day_path(game: SportsGame) -> str:
    return f"{SPORTS_ROOT}.leaderboards.{game.league}.{day(game.start_time)}"


# ---------------------------------------------------------------------------
# Score-bug selection
# ---------------------------------------------------------------------------

def score_bug_mode(game, *, now, explicit_date, lookback_hours, lookahead_days) -> str | None:
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
# ESPN summary extraction (unchanged parsing logic)
# ---------------------------------------------------------------------------

def link_map(items: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        href, rels = item.get("href"), item.get("rel")
        if not href or not isinstance(rels, list):
            continue
        for rel in rels:
            out[str(rel)] = str(href)
    return out


def headshot(value: Any) -> str | None:
    if isinstance(value, dict):
        return value.get("href")
    return value if isinstance(value, str) else None


def position(value: Any) -> str | None:
    if isinstance(value, dict):
        return value.get("abbreviation") or value.get("displayName") or value.get("name")
    return str(value) if value else None


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
                display_name = athlete.get("displayName") or athlete.get("fullName") or athlete.get("shortName")
                if not athlete_id and not display_name:
                    continue
                akey = athlete_key_from_values(display_name or athlete.get("shortName"), athlete_id)
                out.append({
                    "category": category_key, "category_display": category_display,
                    "rank": rank, "value": item.get("value"), "display_value": item.get("displayValue"),
                    "athlete_id": athlete_id, "athlete_key": akey,
                    "display_name": display_name or athlete_id, "short_name": athlete.get("shortName"),
                    "headshot": headshot(athlete.get("headshot")), "position": position(athlete.get("position")),
                    "team_id": team_id, "team_key": tkey, "team_display_name": team_name,
                    "team_abbreviation": team_abbr, "espn_links": link_map(athlete.get("links")),
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
                display_name = (athlete.get("displayName") or athlete.get("fullName")
                                or athlete.get("shortName") or athlete_id)
                akey = athlete_key_from_values(display_name, athlete_id)
                stats = entry.get("stats") or []
                stat_map = ({str(k): v for k, v in zip(stat_names, stats)}
                            if isinstance(stat_names, list) and isinstance(stats, list) else {})
                old = athletes.get(athlete_id, {})
                old_groups = old.get("stat_groups") if isinstance(old.get("stat_groups"), dict) else {}
                athletes[athlete_id] = {
                    **old, "athlete_id": athlete_id, "athlete_key": akey,
                    "display_name": display_name, "short_name": athlete.get("shortName"),
                    "headshot": headshot(athlete.get("headshot")), "jersey": athlete.get("jersey"),
                    "position": position(athlete.get("position")), "team_id": team_id, "team_key": tkey,
                    "team_abbreviation": team_abbr, "espn_links": link_map(athlete.get("links")),
                    "stat_groups": {**old_groups, stat_group_name: stat_map},
                }
    return list(athletes.values())


def extract_athletes(summary: dict[str, Any]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for row in extract_leaders(summary):
        athlete_id = str(row.get("athlete_id") or "")
        if not athlete_id:
            continue
        by_id[athlete_id] = {
            **by_id.get(athlete_id, {}), "athlete_id": athlete_id, "athlete_key": row.get("athlete_key"),
            "display_name": row.get("display_name"), "short_name": row.get("short_name"),
            "headshot": row.get("headshot"), "position": row.get("position"),
            "team_id": row.get("team_id"), "team_key": row.get("team_key"),
            "team_abbreviation": row.get("team_abbreviation"), "espn_links": row.get("espn_links") or {},
        }
    for row in extract_boxscore_athletes(summary):
        athlete_id = str(row.get("athlete_id") or "")
        if not athlete_id:
            continue
        old = by_id.get(athlete_id, {})
        merged = {**old, **row}
        if old.get("stat_groups") and row.get("stat_groups"):
            merged["stat_groups"] = {**old.get("stat_groups", {}), **row.get("stat_groups", {})}
        by_id[athlete_id] = merged
    return list(by_id.values())


def extract_plays(summary: dict[str, Any], limit: int = 25) -> list[dict[str, Any]]:
    plays = summary.get("plays")
    if not isinstance(plays, list):
        return []
    out = []
    for play in plays[-limit:]:
        if not isinstance(play, dict):
            continue
        period = play.get("period")
        out.append({
            "id": play.get("id"), "sequence_number": play.get("sequenceNumber"),
            "text": play.get("text"), "short_text": play.get("shortText"),
            "score_value": play.get("scoreValue"), "scoring_play": play.get("scoringPlay"),
            "home_score": play.get("homeScore"), "away_score": play.get("awayScore"),
            "period": period.get("number") if isinstance(period, dict) else period,
        })
    return out


def summary_bits(summary: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(summary, dict):
        return {"leaders": [], "athletes": [], "plays": [], "news": [], "odds": None,
                "predictor": None, "winprobability": []}
    news = summary.get("news")
    articles = news.get("articles") if isinstance(news, dict) else []
    wp = summary.get("winprobability")
    return {
        "leaders": extract_leaders(summary), "athletes": extract_athletes(summary),
        "plays": extract_plays(summary), "news": articles if isinstance(articles, list) else [],
        "odds": summary.get("odds"), "predictor": summary.get("predictor"),
        "winprobability": wp if isinstance(wp, list) else [],
    }


# ---------------------------------------------------------------------------
# Node data + links
# ---------------------------------------------------------------------------

def game_links(game: SportsGame) -> dict[str, str]:
    path = game_path(game)
    league = slug(game.league)
    return {
        "home_team": team_path(game, game.home),
        "away_team": team_path(game, game.away),
        "venue": venue_path(game),
        "leaderboards": leaderboards_day_path(game),
        "latest": f"{SPORTS_ROOT}.latest.{game.latest_key()}",
        "league_games": f"{SPORTS_ROOT}.index.by.league.{league}",
        "day_games": f"{SPORTS_ROOT}.index.scoped.league.{league}.start_day.{slug(day(game.start_time))}",
        "status_games": f"{SPORTS_ROOT}.index.scoped.league.{league}.status.{slug(game.status)}",
        "mode_games": f"{SPORTS_ROOT}.index.scoped.league.{league}.score_bug_mode",
        "stream": stream_link(path),
        "changes_since": changes_link(path),
    }


def game_data(game: SportsGame, *, mode: str, summary: dict[str, Any] | None) -> dict[str, Any]:
    bits = summary_bits(summary)
    data = {"kind": "sports_game", **game.to_dict()}
    data.update({
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
        data["recent_plays"] = bits["plays"]
    if bits["news"]:
        data["news"] = bits["news"][:5]
    if bits["odds"]:
        data["odds"] = bits["odds"]
    if bits["predictor"]:
        data["predictor"] = bits["predictor"]
    if bits["winprobability"]:
        data["winprobability_tail"] = bits["winprobability"][-25:]
    return data


def game_ref_payload(game: SportsGame, *, mode: str) -> dict[str, Any]:
    return {
        "league": game.league, "sport": game.sport, "start_time": game.start_time,
        "start_day": day(game.start_time), "status": game.status, "status_detail": game.status_detail,
        "score_bug_mode": mode, "matchup": f"{game.away.abbreviation} @ {game.home.abbreviation}",
        "team_keys": [team_key(game.home), team_key(game.away)], "venue_key": venue_key(game),
    }


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def write_team(lo: Loader, game: SportsGame, team: Any, game_abs: str) -> str:
    path = team_path(game, team)
    tkey = team_key(team)
    lo.thing(
        path=path, kind="sports_team", name=team.display_name, target=game_abs,
        content={
            "model": "sports-team", "team_key": tkey, "team_id": team.team_id, "espn_id": team.team_id,
            "abbreviation": team.abbreviation, "display_name": team.display_name,
            "league": game.league, "sport": game.sport, "latest_game": game_abs,
        },
        links={
            "latest_game": game_abs, "games": f"{path}.refs.games", "athletes": f"{path}.refs.athletes",
            "league": f"{SPORTS_ROOT}.index.by.league.{slug(game.league)}",
            "stream": stream_link(path), "changes_since": changes_link(path),
        },
    )
    return path


def write_venue(lo: Loader, game: SportsGame, game_abs: str) -> str:
    path = venue_path(game)
    vkey = venue_key(game)
    lo.thing(
        path=path, kind="sports_venue", name=game.venue_name or vkey, target=game_abs,
        content={
            "model": "sports-venue", "venue_key": vkey, "name": game.venue_name,
            "city": game.venue_city, "country": game.venue_country, "latest_game": game_abs,
        },
        links={
            "latest_game": game_abs, "games": f"{path}.refs.games",
            "stream": stream_link(path), "changes_since": changes_link(path),
        },
    )
    return path


def write_athletes(lo: Loader, *, game: SportsGame, game_abs: str, athletes: list[dict[str, Any]]) -> int:
    count = 0
    for athlete in athletes:
        athlete_id = str(athlete.get("athlete_id") or "")
        if not athlete_id:
            continue
        akey = athlete.get("athlete_key") or athlete_key_from_values(athlete.get("display_name"), athlete_id)
        path = athlete_path_from_key(game.league, akey)
        tkey = athlete.get("team_key") or team_key_from_values(athlete.get("team_abbreviation"), athlete.get("team_id"))
        tpath = team_path_from_key(game.league, tkey)

        lo.thing(
            path=path, kind="sports_athlete",
            name=athlete.get("display_name") or athlete_id, target=game_abs,
            content={"model": "sports-athlete", **athlete, "athlete_key": akey,
                  "league": game.league, "sport": game.sport, "latest_game": game_abs},
            links={"latest_game": game_abs, "team": tpath, "games": f"{path}.refs.games",
                   "stream": stream_link(path), "changes_since": changes_link(path)},
        )
        lo.link(
            source=path, rel=f"games.{game.matchup_key()}", target=game_abs,
            kind="sports-game", links={"team": tpath},
        )
        count += 1
    return count


def write_team_athlete_refs(lo: Loader, *, game: SportsGame, game_abs: str, athletes: list[dict[str, Any]]) -> None:
    for athlete in athletes:
        athlete_id = str(athlete.get("athlete_id") or "")
        if not athlete_id:
            continue
        akey = athlete.get("athlete_key") or athlete_key_from_values(athlete.get("display_name"), athlete_id)
        target = athlete_path_from_key(game.league, akey)
        tkey = athlete.get("team_key") or team_key_from_values(athlete.get("team_abbreviation"), athlete.get("team_id"))
        tpath = team_path_from_key(game.league, tkey)
        lo.link(
            source=tpath, rel=f"athletes.{akey}", target=target,
            kind="athlete-ref", name=athlete.get("display_name"),
            content={"athlete_id": athlete_id, "athlete_key": akey,
                  "display_name": athlete.get("display_name"), "short_name": athlete.get("short_name"),
                  "headshot": athlete.get("headshot"), "position": athlete.get("position"),
                  "team_id": athlete.get("team_id"), "team_key": tkey,
                  "team_abbreviation": athlete.get("team_abbreviation")},
            links={"athlete": target, "team": tpath},
        )


def write_leaderboards(lo: Loader, *, game: SportsGame, leaders: list[dict[str, Any]], fetched_at: str) -> int:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in leaders:
        grouped.setdefault(slug(row.get("category"), "leader"), []).append(row)

    start_day = day(game.start_time)

    for category, rows in grouped.items():
        rel = f"leaderboards/{game.league}/{start_day}/{category}"
        path = f"{SPORTS_ROOT}.{rel.replace('/', '.')}"
        category_display = str(rows[0].get("category_display") or category)

        clean_rows = []
        for row in rows:
            akey = row.get("athlete_key") or athlete_key_from_values(row.get("display_name"), row.get("athlete_id"))
            tkey = row.get("team_key") or team_key_from_values(row.get("team_abbreviation"), row.get("team_id"))
            clean_rows.append({
                "rank": row.get("rank"), "value": row.get("value"), "display_value": row.get("display_value"),
                "display_name": row.get("display_name"), "athlete_id": row.get("athlete_id"), "athlete_key": akey,
                "team_id": row.get("team_id"), "team_key": tkey, "team_abbreviation": row.get("team_abbreviation"),
                "athlete_path": athlete_path_from_key(game.league, akey),
                "team_path": team_path_from_key(game.league, tkey), "game_path": game_path(game),
            })
        clean_rows.sort(key=lambda r: int(r.get("rank") or 999999))

        data = {
            "kind": "sports_leaderboard", "model": "sports-leaderboard",
            "league": game.league, "sport": game.sport, "date": start_day,
            "category": category, "category_display": category_display, "leader_count": len(clean_rows),
            "updated_from_game_id": game.game_id, "updated_from_game_path": game_path(game),
            "fetched_at": fetched_at, "leaders": clean_rows,
        }

        # Leaderboard record + indexes + links, in one write.
        lo.record(
            rel, data, indexes=LEADERBOARD_INDEXES,
            ref_key=f"{game.league}-{start_day}-{category}", ref_payload=data,
            links={
                "updated_from_game": game_path(game), "day": leaderboards_day_path(game),
                "league": f"{SPORTS_ROOT}.leaderboards.{game.league}",
                "stream": stream_link(path), "changes_since": changes_link(path),
            },
        )

        for idx, row in enumerate(clean_rows, start=1):
            lo.link(
                source=path, rel=f"{idx:02d}-{row['athlete_key']}", target=row.get("athlete_path"),
                kind="leaderboard-entry",
                content={"rank": row.get("rank"), "display_name": row.get("display_name"),
                      "display_value": row.get("display_value"), "athlete_path": row.get("athlete_path"),
                      "team_path": row.get("team_path"), "game_path": row.get("game_path")},
                links={"athlete": row.get("athlete_path"), "team": row.get("team_path"), "game": row.get("game_path")},
            )

    return sum(len(v) for v in grouped.values())


def write_game(lo: Loader, game: SportsGame, *, fetched_at: str, summary: dict[str, Any] | None, mode: str) -> str:
    path = game_path(game)
    matchup = game.matchup_key()
    data = game_data(game, mode=mode, summary=summary)

    # 1. Canonical, searchable game + indexes + hypermedia links, in one write.
    lo.record(
        f"games/{game.record_key()}", data, indexes=GAME_INDEXES,
        ref_key=game.game_id, ref_payload=game_ref_payload(game, mode=mode),
        links=game_links(game),
    )

    # 2. Latest pointer for the league.
    lo.thing(
        path=f"{SPORTS_ROOT}.latest.{game.latest_key()}", kind="sports_game",
        name=f"{game.away.abbreviation} @ {game.home.abbreviation}", target=path,
        content={**game.latest_dict(path), "score_bug_mode": mode},
        links={"home_team": team_path(game, game.home), "away_team": team_path(game, game.away),
               "venue": venue_path(game), "leaderboards": leaderboards_day_path(game)},
    )

    # 3. Game -> its anchors.
    for rel, target in (("home_team", team_path(game, game.home)), ("away_team", team_path(game, game.away)),
                        ("venue", venue_path(game)), ("leaderboards", leaderboards_day_path(game)),
                        ("latest", f"{SPORTS_ROOT}.latest.{game.latest_key()}")):
        lo.link(source=path, rel=rel, target=target, kind="ref")

    # 4. Teams (+ their game backref).
    for team in (game.home, game.away):
        tpath = write_team(lo, game, team, path)
        lo.link(
            source=tpath, rel=f"games.{matchup}", target=path, kind="sports-game",
            content={"role": "home" if team.is_home else "away",
                  "matchup": data["matchup"], "status": game.status,
                  "status_detail": game.status_detail, "start_time": game.start_time},
            links={"opponent": team_path(game, game.away if team.is_home else game.home), "venue": venue_path(game)},
        )

    # 5. Venue (+ its game backref).
    vpath = write_venue(lo, game, path)
    lo.link(
        source=vpath, rel=f"games.{matchup}", target=path, kind="sports-game",
        content={"matchup": data["matchup"], "status": game.status,
              "status_detail": game.status_detail, "start_time": game.start_time},
        links={"home_team": team_path(game, game.home), "away_team": team_path(game, game.away)},
    )

    # 6. Athletes + leaderboards from the box score.
    bits = summary_bits(summary)
    athlete_count = write_athletes(lo, game=game, game_abs=path, athletes=bits["athletes"])
    write_team_athlete_refs(lo, game=game, game_abs=path, athletes=bits["athletes"])
    leader_count = write_leaderboards(lo, game=game, leaders=bits["leaders"], fetched_at=fetched_at)

    if athlete_count or leader_count:
        print(f"    enriched: athletes={athlete_count}, leaderboard_rows={leader_count}")

    return path


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def sync_league(
    lo: Loader,
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

    games = games_from_scoreboard(payload, league=league, sport=sport, season=season_guess, fetched_at=fetched_at)

    selected = [
        (game, mode)
        for game in games
        if (mode := score_bug_mode(game, now=now, explicit_date=bool(date),
                                   lookback_hours=lookback_hours, lookahead_days=lookahead_days))
    ]

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
            write_game(lo, game, fetched_at=fetched_at, summary=summary, mode=mode)
            written += 1
            print(f"  ok: {game.away.abbreviation} {game.away.score} @ "
                  f"{game.home.abbreviation} {game.home.score} [{game.status_detail}] ({mode})")
        except Exception as exc:
            print(f"  write failed for {game.game_id}: {type(exc).__name__}: {exc}")

    return written


def run(
    lo: Loader,
    *,
    leagues: list[str] | None = None,
    date: str | None = None,
    season: str | None = None,
    limit_per_league: int | None = None,
    include_summaries: bool = True,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
    lookahead_days: int = DEFAULT_LOOKAHEAD_DAYS,
    keep_alive: bool = True,
) -> int:
    espn = EspnApiClient()
    total = sum(
        sync_league(lo, espn, league=league, date=date, season=season, limit=limit_per_league,
                    include_summaries=include_summaries, lookback_hours=lookback_hours, lookahead_days=lookahead_days)
        for league in (leagues or DEFAULT_LEAGUES)
    )
    print(f"done — {total} game(s)")

    if keep_alive:
        lo.serve()
    else:
        lo.close()
    return 0


def main() -> int:
    return run(Loader(SPORTS_ROOT), leagues=DEFAULT_LEAGUES, limit_per_league=25)


if __name__ == "__main__":
    sys.exit(main())