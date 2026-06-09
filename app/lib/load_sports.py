"""
Build the sports database from a scoreboard provider (root="sports").

Every game is one node at sports.games.<league>.<yyyy>.<mm>.<dd>.<game_id>, written
densely and indexed (browsable) by league, status (scoped by league), score_bug_mode
(scoped by league), start_day (scoped by league), and team. A latest pointer and
team/venue sidecars hang off each event.

Orchestration only. The provider is read inside the create_* verbs and named in exactly
one place (app/lib/helpers/sports/__init__.py); this file never touches ESPN JSON,
URLs, or the source module — switching providers doesn't touch it.
"""
from __future__ import annotations

import sys

from app.lib.helpers.sports import (
    HyperClient,
    apply_graph_operations,
    list_game_candidates,
    create_game_object,
    SportsGameObject,
)

__all__ = ["load_sports", "SportsGameObject"]

SPORTS_ROOT = "sports"
DEFAULT_LEAGUES = ["nba", "nfl", "nhl", "mlb", "eng.1"]
SELECT_LIMIT_PER_LEAGUE = 25


def load_sports(
        ROOT: str = SPORTS_ROOT,
        DATA_DIR: str = None,
        leagues: list[str] | None = None,
        date: str | None = None,
        season: str | None = None,
) -> int:
    leagues = leagues or DEFAULT_LEAGUES
    print(f"data dir: {DATA_DIR}", flush=True)
    print(f"leagues: {', '.join(leagues)}", flush=True)

    try:
        with HyperClient.open_sqlite_file(
                root_key=ROOT,
                reset=True,
                path=DATA_DIR,
        ) as data_store:
            written = 0
            latest_done: set[str] = set()
            for game_record, mode, candidate_proof in list_game_candidates(
                    leagues, date=date, season=season,
            ):
                if not candidate_proof:
                    print(f"  skip: out-of-window {game_record.get('matchup')} "
                          f"({game_record.get('league')})", flush=True)
                    continue

                game_object, object_proof = create_game_object(game_record, mode)
                if not game_object:
                    print("skipping", game_record.get("game_id"))
                    continue

                write_latest = game_object.league not in latest_done
                latest_done.add(game_object.league)

                apply_graph_operations(
                    game_object=game_object,
                    client_instance=data_store,
                    namespace=SPORTS_ROOT,
                    write_latest=write_latest,
                )

                written += 1
                print(f"  ok: {game_object.matchup} "
                      f"[{game_object.status_detail}] ({mode})", flush=True)

            print(f"done: built {data_store.count:,} games ({data_store.written:,} writes)", flush=True)

    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(load_sports())
