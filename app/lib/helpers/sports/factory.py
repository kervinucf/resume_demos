"""
Sports factory — the typed object + builder for the sports database.

Mirrors app/lib/helpers/weather/factory.py and geo/factory.py:
    WeatherEventObject  <->  SportsGameObject   (frozen dataclass, the stored body)
    WeatherFactory      <->  SportsFactory      (builds typed objects from NORMALIZED fields)

Nothing here knows how ESPN encodes data — SportsFactory takes plain named fields.
Provider decoding (JSON layout, league->sport) lives in app/lib/sources/espn.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from HyperCoreSDK.python.dtos.object import HyperObject


# ---------------------------------------------------------------------------
# Game object  (analog of WeatherEventObject)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SportsGameObject:
    game_id: str
    league: str
    sport: str
    season: str
    start_time: str
    start_day: str
    status: str
    status_detail: str

    matchup: str
    home_team_key: str
    away_team_key: str
    home_score: int | None
    away_score: int | None
    venue_key: str
    venue_name: str
    venue_city: str
    venue_country: str
    fetched_at: str

    # selection mode set loader-side, like weather's proof-gated fields
    score_bug_mode: str = ""
    team_keys: list[str] = field(default_factory=list)

    # nested team snapshots (kept for sidecar writes)
    home: dict[str, Any] = field(default_factory=dict)
    away: dict[str, Any] = field(default_factory=dict)

    _sports_root: str = "sports"

    def record_key(self) -> str:
        """Time-partitioned id: <league>/<yyyy>/<mm>/<dd>/<game_id>."""
        dt = datetime.fromisoformat(self.start_time)
        return f"{self.league}/{dt.year:04d}/{dt.month:02d}/{dt.day:02d}/{self.game_id}"

    def latest_key(self) -> str:
        return self.league

    def compact_ts(self) -> str:
        """Sortable event stamp (lexical == chronological)."""
        return datetime.fromisoformat(self.start_time).strftime("%Y%m%dT%H%M%S")


# ---------------------------------------------------------------------------
# Game builder  (analog of WeatherFactory / LocationFactory)
# ---------------------------------------------------------------------------
class SportsFactory(HyperObject):
    """Builds typed SportsGameObjects from already-normalized provider fields."""

    @classmethod
    def create_game_object(
            cls,
            *,
            game_record: dict[str, Any],
            score_bug_mode: str = "",
    ) -> SportsGameObject:
        home = game_record.get("home") or {}
        away = game_record.get("away") or {}
        return SportsGameObject(
            game_id=str(game_record.get("game_id") or "").strip(),
            league=str(game_record.get("league") or "").strip(),
            sport=str(game_record.get("sport") or "").strip(),
            season=str(game_record.get("season") or "").strip(),
            start_time=str(game_record.get("start_time") or "").strip(),
            start_day=str(game_record.get("start_day") or "").strip(),
            status=str(game_record.get("status") or "").strip(),
            status_detail=str(game_record.get("status_detail") or "").strip(),
            matchup=str(game_record.get("matchup") or ""),
            home_team_key=str(home.get("team_key") or ""),
            away_team_key=str(away.get("team_key") or ""),
            home_score=home.get("score"),
            away_score=away.get("score"),
            venue_key=str(game_record.get("venue_key") or ""),
            venue_name=str(game_record.get("venue_name") or ""),
            venue_city=str(game_record.get("venue_city") or ""),
            venue_country=str(game_record.get("venue_country") or ""),
            fetched_at=str(game_record.get("fetched_at") or ""),
            score_bug_mode=score_bug_mode,
            team_keys=list(game_record.get("team_keys") or []),
            home=home,
            away=away,
        )