from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

_SLUG_RE = re.compile(r"[^a-zA-Z0-9]+")
_DASHES_RE = re.compile(r"-{2,}")


def slugify(value: str, *, fallback: str = "game") -> str:
    text = _SLUG_RE.sub("-", str(value or "").strip().lower())
    text = _DASHES_RE.sub("-", text).strip("-")
    return text or fallback


@dataclass(frozen=True)
class GameTeam:
    team_id: str
    abbreviation: str
    display_name: str
    score: int | None
    is_home: bool
    is_winner: bool | None


@dataclass(frozen=True)
class SportsGame:
    game_id: str                 # ESPN event id, e.g. "401585632"
    league: str                  # "nba", "nfl", "eng.1"
    sport: str                   # "basketball", "football", "soccer"
    season: str                  # "2026"
    start_time: str              # ISO-8601 UTC
    status: str                  # "pre", "in", "post"
    status_detail: str           # "Q3 4:32", "Final", "Scheduled 7:30 PM"
    venue_name: str
    venue_city: str
    venue_country: str
    home: GameTeam
    away: GameTeam
    fetched_at: str              # ISO-8601 UTC

    def record_key(self) -> str:
        """
        Stable, time-partitioned id:
            <league>/<yyyy>/<mm>/<dd>/<game_id>
        """
        dt = datetime.fromisoformat(self.start_time)
        return (
            f"{self.league}/"
            f"{dt.year:04d}/{dt.month:02d}/{dt.day:02d}/"
            f"{self.game_id}"
        )

    def latest_key(self) -> str:
        """Per-league pointer to the most recently fetched game."""
        return self.league

    def matchup_key(self) -> str:
        """Per-league/per-day/per-matchup slug useful for back-refs on teams."""
        dt = datetime.fromisoformat(self.start_time)
        return (
            f"{dt.year:04d}.{dt.month:02d}.{dt.day:02d}."
            f"{self.away.abbreviation.lower()}-at-{self.home.abbreviation.lower()}."
            f"{self.game_id}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {"model": "sports-game", **asdict(self)}

    def latest_dict(self, canonical_path: str) -> dict[str, Any]:
        d = self.to_dict()
        d["model"] = "sports-game-latest"
        d["target"] = canonical_path
        return d

    def ref_payload(self) -> dict[str, Any]:
        """
        Compact snapshot merged into index entries and per-team back-refs —
        lets listings render scoreboards without hydrating full records.
        """
        return {
            "league": self.league,
            "sport": self.sport,
            "start_time": self.start_time,
            "status": self.status,
            "status_detail": self.status_detail,
            "home_abbr": self.home.abbreviation,
            "home_name": self.home.display_name,
            "home_score": self.home.score,
            "away_abbr": self.away.abbreviation,
            "away_name": self.away.display_name,
            "away_score": self.away.score,
            "venue_city": self.venue_city,
            "venue_country": self.venue_country,
        }