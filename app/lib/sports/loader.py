"""
Pull ESPN scoreboards into the hypergraph.

After a run, the `sports` namespace looks like:

    sports/
      games/
        nba/
          2026/04/21/401585632        (canonical game record)
        nfl/
          2026/09/14/401772001
      latest/
        nba                            (pointer → most recently-seen game)
      index/
        by/
          league/
            nba/
              401585632
          sport/
            basketball/
              401585632
          status/
            in/
              401585632
            post/
              ...
        scoped/
          league/
            nba/
              status/
                post/
                  ...
      _meta/
        memberships/
          <sha1 of each record path>

Each team referenced by a game also gains a back-ref:

    sports.teams.<league>.<team_id>.refs.games.<matchup_key>
      → sports.games.<league>.<yyyy>.<mm>.<dd>.<game_id>

Usage
-----
    # Today's NBA scoreboard
    python load_sports.py --league nba

    # A specific date (YYYYMMDD per ESPN's convention)
    python load_sports.py --league nfl --date 20260914

    # A bundle of leagues
    python load_sports.py --all
"""
from __future__ import annotations

import argparse
import sys

from app.utils.server import create_hyper_server
from app.utils.storage import create_default_storage_directory
from app.utils.dtos.SportsEvent import SportsGame
from app.utils.clients.espn import EspnApiClient, EspnApiError
from app.lib.sports.helpers import games_from_scoreboard, now_utc
from HyperCoreSDK.python.client import HyperClient
from HyperCoreSDK.python.indexes import (
    ScopeSpec,
    ValueIndexSpec,
    upsert_with_indexes,
)


DEFAULT_LEAGUES: list[str] = ["nba", "nfl", "nhl", "mlb", "eng.1"]


# ---------------------------------------------------------------------------
# Index specs
#
#   "Index by league, slugified — `index/by/league/nba/` holds every NBA
#    game across all dates."
#
#   "Index by sport — `index/by/sport/basketball/` holds NBA + WNBA +
#    college games together."
#
#   "Index by status globally — quick lookup for 'what's live right now'."
#
#   "Scope status under league for per-league live boards."
# ---------------------------------------------------------------------------

SPORTS_INDEXES: list[ValueIndexSpec] = [
    ValueIndexSpec(
        name="league",
        path="league",
        normalize="slug",
        link_projections={
            "sport": "sport",
            "start_time": "start_time",
            "status": "status",
            "status_detail": "status_detail",
        },
    ),
    ValueIndexSpec(
        name="sport",
        path="sport",
        normalize="slug",
        link_projections={
            "league": "league",
            "start_time": "start_time",
            "status": "status",
            "status_detail": "status_detail",
        },
    ),
    ValueIndexSpec(
        name="status",
        path="status",
        normalize="slug",
        link_projections={
            "league": "league",
            "sport": "sport",
            "start_time": "start_time",
            "status_detail": "status_detail",
        },
    ),
    ValueIndexSpec(
        name="status",
        path="status",
        normalize="slug",
        scopes=[ScopeSpec(path="league", normalize="slug")],
        link_projections={
            "start_time": "start_time",
            "status_detail": "status_detail",
        },
    ),
]


# ---------------------------------------------------------------------------
# Per-record write
# ---------------------------------------------------------------------------

def write_game(hc: HyperClient, game: SportsGame) -> str:
    game_rel = f"games/{game.record_key()}"
    game_abs = f"sports.{game_rel.replace('/', '.')}"
    latest_abs = f"sports.latest.{game.latest_key()}"

    # Canonical game record + indexes
    upsert_with_indexes(
        hc,
        record_path=game_rel,
        record_data=game.to_dict(),
        index_specs=SPORTS_INDEXES,
        ref_key=game.game_id,
        ref_payload=game.ref_payload(),
    )

    # "Latest" pointer per league
    hc.write(latest_abs, {
        "data": game.latest_dict(game_abs),
        "links": {"target": game_abs},
    })

    # Per-team back-refs so a team page can list its games cheaply
    matchup = game.matchup_key()
    for team in (game.home, game.away):
        team_path = f"sports.teams.{game.league}.{team.team_id}"
        hc.write(f"{team_path}.refs.games.{matchup}", {
            "data": {
                "kind": "sports-game",
                "target": game_abs,
                "role": "home" if team.is_home else "away",
                **game.ref_payload(),
            },
            "links": {"target": game_abs},
        })

    return game_abs


# ---------------------------------------------------------------------------
# Per-league sync
# ---------------------------------------------------------------------------

def sync_league(
    hc: HyperClient,
    espn: EspnApiClient,
    *,
    league: str,
    date: str | None = None,
    season: str | None = None,
    limit: int | None = None,
) -> int:
    print(f"league: {league}" + (f" — date {date}" if date else ""))

    try:
        sport = espn.sport_for_league(league)
    except KeyError as exc:
        print(f"  {exc}")
        return 0

    try:
        payload = espn.get_scoreboard(league=league, date=date)
    except EspnApiError as exc:
        print(f"  fetch failed: {exc}")
        return 0

    # Best-effort season: ESPN echoes it in the payload
    season_guess = season or str(
        (payload.get("season") or {}).get("year")
        or (payload.get("leagues") or [{}])[0].get("season", {}).get("year")
        or now_utc().year
    )

    fetched_at = now_utc().isoformat()
    games = games_from_scoreboard(
        payload,
        league=league,
        sport=sport,
        season=season_guess,
        fetched_at=fetched_at,
    )

    written = 0
    for game in games:
        if limit is not None and written >= limit:
            break
        try:
            write_game(hc, game)
            written += 1
            score = f"{game.away.abbreviation} {game.away.score} @ {game.home.abbreviation} {game.home.score}"
            print(f"  ok: {score}  [{game.status_detail}]")
        except Exception as exc:
            print(f"  write failed for {game.game_id}: {type(exc).__name__}: {exc}")

    return written


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(client_instance: HyperClient, argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="load_sports.py")
    p.add_argument("--league", help="single league, e.g. 'nba'")
    p.add_argument("--all", action="store_true", help="use DEFAULT_LEAGUES")
    p.add_argument("--date", default=None, help="YYYYMMDD (ESPN convention)")
    p.add_argument("--season", default=None)
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args(argv[1:])

    if not args.all and not args.league:
        p.error("pass --league LEAGUE or --all")

    espn = EspnApiClient()
    leagues = DEFAULT_LEAGUES if args.all else [args.league]

    try:
        total = 0
        for league in leagues:
            total += sync_league(
                client_instance,
                espn,
                league=league,
                date=args.date,
                season=args.season,
                limit=args.limit,
            )
        print(f"done — {total} game(s)")

    finally:
        if client_instance.owns_relay():
            print(f"relay still running at {client_instance.url} (Ctrl-C to stop)")
            try:
                client_instance._owned.process.wait()
            except KeyboardInterrupt:
                pass
            finally:
                client_instance.close()
        else:
            client_instance.close()

    return 0


if __name__ == "__main__":
    sys.exit(
        main(
            client_instance=create_hyper_server(
                root="sports",
                data_path=create_default_storage_directory(),
            ),
            argv=sys.argv,
        )
    )