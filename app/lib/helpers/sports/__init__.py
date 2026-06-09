"""
Sports operations — the verbs the main loader orchestrates.

Mirrors app/lib/helpers/geo/__init__.py and weather/__init__.py: the provider is
read inside the create_* functions, so the loader only ever imports:

    from app.lib.helpers.sports import (apply_graph_operations,
                                        list_game_candidates,
                                        create_game_object,
                                        SportsGameObject)

This module is the ONE place that names the concrete provider. To switch providers,
change the `app.lib.sources.espn` import below to another module exposing the same
source callables — nothing else changes.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Iterator

from HyperCoreSDK.python.client import HyperClient, projection, ScopeSpec, ValueIndexSpec
# --- provider boundary: swap this single import to change source ------------
from app.lib.sources.espn import iter_game_candidates, _parse_iso, _now_utc
# ---------------------------------------------------------------------------
from app.lib.helpers.sports.factory import SportsFactory, SportsGameObject

sports_factory = SportsFactory()

__all__ = [
    "HyperClient",
    "list_game_candidates",
    "create_game_object",
    "apply_graph_operations",
    "SportsGameObject",
]

DEFAULT_LOOKBACK_HOURS = 18
DEFAULT_LOOKAHEAD_DAYS = 7

# Fields each index entry carries forward for cheap rendering (information density).
PROJECT = projection(
    "league", "sport", "start_time", "start_day",
    "status", "status_detail", "score_bug_mode", "matchup",
)
GAME_INDEXES: list[ValueIndexSpec] = [
    ValueIndexSpec("league", "league", normalize="slug", link_projections=PROJECT),
    ValueIndexSpec(
        "status", "status", normalize="slug",
        scopes=[ScopeSpec("league", normalize="slug")],
        link_projections=PROJECT,
    ),
    ValueIndexSpec(
        "score_bug_mode", "score_bug_mode", normalize="slug",
        scopes=[ScopeSpec("league", normalize="slug")],
        link_projections=PROJECT,
    ),
    ValueIndexSpec(
        "start_day", "start_day", normalize="slug",
        scopes=[ScopeSpec("league", normalize="slug")],
        link_projections=PROJECT,
    ),
    ValueIndexSpec(
        "team", "team_keys", normalize="slug", multi=True,
        link_projections=PROJECT,
    ),
]


# ---------------------------------------------------------------------------
# Score-bug window selection (the candidate proof gate)
# ---------------------------------------------------------------------------
def _score_bug_mode(
        record: dict[str, Any], *, explicit_date: bool,
        lookback_hours: int, lookahead_days: int,
) -> str | None:
    now = _now_utc()
    start = _parse_iso(record.get("start_time"))
    from datetime import datetime as _dt
    start = _dt.fromisoformat(start)
    status = str(record.get("status") or "").lower()

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


def list_game_candidates(
        leagues: list[str],
        *,
        date: str | None = None,
        season: str | None = None,
        lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
        lookahead_days: int = DEFAULT_LOOKAHEAD_DAYS,
) -> Iterator[tuple[dict[str, Any], str, bool]]:
    """Yield (game_record, score_bug_mode, proof). proof=False -> out of window."""
    for record in iter_game_candidates(leagues, date=date, season=season):
        try:
            mode = _score_bug_mode(
                record, explicit_date=bool(date),
                lookback_hours=lookback_hours, lookahead_days=lookahead_days,
            )
        except (TypeError, ValueError) as exc:
            print(f"  skip: bad game record ({exc})", flush=True)
            continue

        proof = mode is not None
        yield record, (mode or ""), proof


def create_game_object(
        game_record: dict[str, Any],
        score_bug_mode: str = "",
) -> tuple[SportsGameObject | None, bool]:
    try:
        game_object = sports_factory.create_game_object(
            game_record=game_record, score_bug_mode=score_bug_mode,
        )
    except (TypeError, ValueError) as exc:
        print(f"  skip: bad game record ({exc})", flush=True)
        return None, False

    proof = bool(game_object.game_id) and bool(game_object.start_time)
    if not proof:
        print(f"  skip: incomplete game object ({game_record.get('game_id')})", flush=True)
    return game_object, proof


def apply_graph_operations(
        game_object: SportsGameObject,
        client_instance: HyperClient,
        namespace,
        write_latest: bool = True,
) -> dict[str, str]:
    league = game_object.league
    # Flat leaf like geo's locations/<name>-<geoname_id> — no deep shared ancestors,
    # so 16 same-day siblings don't collide on (parent_id, name) during bulk flush.
    record_key = f"{league}-{game_object.start_day}-{game_object.game_id}"
    loc = record_key

    event_path = f"games/{record_key}"
    latest_path = f"latest/{game_object.latest_key()}"

    event_dot = f"{namespace}.{event_path.replace('/', '.')}"
    latest_dot = f"{namespace}.{latest_path.replace('/', '.')}"
    home_dot = f"{namespace}.teams.{league}.{game_object.home_team_key}"
    away_dot = f"{namespace}.teams.{league}.{game_object.away_team_key}"
    venue_dot = f"{namespace}.venues.{game_object.venue_key}"

    # 1) RECORD — indexed, dense
    n1 = client_instance.save_record(
        path=event_path,
        data=game_object.__dict__,
        indexes=GAME_INDEXES,
        root=namespace,
    )

    # 2) POINTER sports.latest.<league> -> event. Written ONCE per league (the first
    #    game seen). Writing it per-game inserts the same (parent=latest, name=league)
    #    node N times -> UNIQUE collision at bulk flush.
    n2 = 0
    if write_latest:
        n2 = client_instance.write_ops([{
            "path": latest_dot,
            "data": {"data": {"tag": "sports_latest", "origin": loc,
                              "score_bug_mode": game_object.score_bug_mode},
                     "links": {"event": event_dot}},
        }], root=namespace)

    # 3) SIDECARS — flat unique leaves (team_refs/<league>-<tkey>-<id>,
    #    venue_refs/<vkey>-<id>) so games never re-insert shared ancestor nodes.
    n3 = client_instance.write_ops([
        {"path": f"{namespace}.team_refs.{league}-{game_object.home_team_key}-{game_object.game_id}",
         "data": {"data": {"tag": "ref", "rel": "game", "role": "home", "matchup": game_object.matchup},
                  "links": {"event": event_dot, "team": home_dot}}},
        {"path": f"{namespace}.team_refs.{league}-{game_object.away_team_key}-{game_object.game_id}",
         "data": {"data": {"tag": "ref", "rel": "game", "role": "away", "matchup": game_object.matchup},
                  "links": {"event": event_dot, "team": away_dot}}},
        {"path": f"{namespace}.venue_refs.{game_object.venue_key}-{game_object.game_id}",
         "data": {"data": {"tag": "ref", "rel": "game", "matchup": game_object.matchup},
                  "links": {"event": event_dot, "venue": venue_dot}}},
    ], root=namespace)

    # EV: four numbers, one line — any zero count flags the broken kind (latest=0 is
    # expected for the 2nd+ game of a league)
    print(f"[sports] {event_dot} record={n1} latest={n2} sidecars={n3}", flush=True)
    return {"event": event_dot, "latest": latest_dot}