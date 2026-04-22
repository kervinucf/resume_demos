from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from ___.HyperCoreSDK import HyperClient
from ___.HyperCoreSDK import HyperCoreNode

def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None

    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _read_value(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _bool_value(value: Any) -> bool:
    return bool(value)


def _event_sub_paths(
    *,
    sport: str,
    league: str,
    scoreboard_date: str,
    event_id: str,
) -> list[str]:
    return [
        "scoreboard",
        str(sport or "").strip().lower(),
        str(league or "").strip().lower(),
        str(scoreboard_date or "").strip(),
        str(event_id or "").strip(),
    ]


def _state_sub_paths(
    *,
    sport: str,
    league: str,
    scoreboard_date: str,
) -> list[str]:
    return [
        "state",
        "scoreboard",
        str(sport or "").strip().lower(),
        str(league or "").strip().lower(),
        str(scoreboard_date or "").strip(),
    ]


@dataclass(frozen=True)
class ScoreboardEvent(HyperCoreNode):
    id: str
    sport: str
    league: str
    scoreboard_date: str
    name: str
    short_name: str
    game_date: str
    status: str

    venue_name: str | None = None
    venue_city: str | None = None
    venue_state: str | None = None

    home_team_id: str | None = None
    home_team_name: str | None = None
    home_team_abbr: str | None = None
    home_team_logo: str | None = None
    home_team_score: str | None = None
    home_team_winner: bool = False

    away_team_id: str | None = None
    away_team_name: str | None = None
    away_team_abbr: str | None = None
    away_team_logo: str | None = None
    away_team_score: str | None = None
    away_team_winner: bool = False

    leaders: dict[str, Any] | None = None
    fetched_at: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", str(self.id or "").strip())
        object.__setattr__(self, "sport", str(self.sport or "").strip().lower())
        object.__setattr__(self, "league", str(self.league or "").strip().lower())
        object.__setattr__(self, "scoreboard_date", str(self.scoreboard_date or "").strip())
        object.__setattr__(self, "name", str(self.name or "").strip())
        object.__setattr__(self, "short_name", str(self.short_name or "").strip())
        object.__setattr__(self, "game_date", str(self.game_date or "").strip())
        object.__setattr__(self, "status", str(self.status or "").strip().upper())

        object.__setattr__(self, "venue_name", _clean_text(self.venue_name))
        object.__setattr__(self, "venue_city", _clean_text(self.venue_city))
        object.__setattr__(self, "venue_state", _clean_text(self.venue_state))

        object.__setattr__(self, "home_team_id", _clean_text(self.home_team_id))
        object.__setattr__(self, "home_team_name", _clean_text(self.home_team_name))
        object.__setattr__(self, "home_team_abbr", _clean_text(self.home_team_abbr))
        object.__setattr__(self, "home_team_logo", _clean_text(self.home_team_logo))
        object.__setattr__(self, "home_team_score", _clean_text(self.home_team_score))
        object.__setattr__(self, "home_team_winner", _bool_value(self.home_team_winner))

        object.__setattr__(self, "away_team_id", _clean_text(self.away_team_id))
        object.__setattr__(self, "away_team_name", _clean_text(self.away_team_name))
        object.__setattr__(self, "away_team_abbr", _clean_text(self.away_team_abbr))
        object.__setattr__(self, "away_team_logo", _clean_text(self.away_team_logo))
        object.__setattr__(self, "away_team_score", _clean_text(self.away_team_score))
        object.__setattr__(self, "away_team_winner", _bool_value(self.away_team_winner))

        object.__setattr__(self, "leaders", dict(self.leaders or {}))
        object.__setattr__(self, "fetched_at", str(self.fetched_at or "").strip())

    def to_kv(self, *, omit_empty: bool = False) -> dict[str, Any]:
        data = asdict(self)
        if not omit_empty:
            return data

        out: dict[str, Any] = {}
        for key, value in data.items():
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            if isinstance(value, dict) and not value:
                continue
            out[key] = value
        return out

    def as_record(self, **extra: Any) -> dict[str, Any]:
        data = self.to_kv()
        data.update(extra)
        return data

    def as_reference(self, *, path: str, **extra: Any) -> dict[str, Any]:
        data = {
            "id": self.id,
            "sport": self.sport,
            "league": self.league,
            "scoreboard_date": self.scoreboard_date,
            "name": self.name,
            "short_name": self.short_name,
            "status": self.status,
            "path": path,
        }
        data.update(extra)
        return data

    @classmethod
    def properties(cls) -> list[str]:
        return [field.name for field in fields(cls)]

    @classmethod
    def from_mapping(
        cls,
        source: Mapping[str, Any] | Any,
        *,
        field_map: Mapping[str, str] | None = None,
        transforms: Mapping[str, Callable[[Any, Any], Any]] | None = None,
        defaults: Mapping[str, Any] | None = None,
        strict: bool = False,
    ) -> "ScoreboardEvent":
        mapped: dict[str, Any] = {}
        field_map = dict(field_map or {})
        transforms = dict(transforms or {})
        defaults = dict(defaults or {})

        for field_name in cls.properties():
            source_key = field_map.get(field_name, field_name)
            value = _read_value(source, source_key, defaults.get(field_name))

            if field_name in transforms:
                value = transforms[field_name](value, source)

            if value is None and field_name in {"id", "sport", "league", "scoreboard_date", "name", "short_name", "game_date", "status"} and strict:
                raise ValueError(f"Missing required field '{field_name}'")

            mapped[field_name] = value

        return cls(**mapped)

    @classmethod
    def from_source(
        cls,
        source: Any,
        *,
        mapper: Callable[[Any], Mapping[str, Any]],
    ) -> "ScoreboardEvent":
        return cls.from_mapping(mapper(source))

    def storage_key(self) -> str:
        return self.id

    def save_to(
        self,
        hyper_client: HyperClient,
        *,
        root: str = "sports",
        sub_paths: list[str] | None = None,
    ) -> None:
        old_root = hyper_client.root
        try:
            hyper_client.root = root
            effective_sub_paths = sub_paths or _event_sub_paths(
                sport=self.sport,
                league=self.league,
                scoreboard_date=self.scoreboard_date,
                event_id=self.id,
            )
            self.commit(
                hyper_client,
                sub_paths=effective_sub_paths,
            )
        finally:
            hyper_client.root = old_root

    @classmethod
    def load_from(
        cls,
        hyper_client: HyperClient,
        *,
        sport: str,
        league: str,
        scoreboard_date: str,
        event_id: str,
        root: str = "sports",
        sub_paths: list[str] | None = None,
    ) -> "ScoreboardEvent | None":
        old_root = hyper_client.root
        try:
            hyper_client.root = root
            effective_sub_paths = sub_paths or _event_sub_paths(
                sport=sport,
                league=league,
                scoreboard_date=scoreboard_date,
                event_id=event_id,
            )
            clean_data, metadata = cls.read_out(
                hyper_client,
                sub_paths=effective_sub_paths,
            )
        finally:
            hyper_client.root = old_root

        if not clean_data:
            return None

        try:
            instance = cls.from_mapping(clean_data, strict=True)
            object.__setattr__(instance, "_metadata", metadata)
            return instance
        except (TypeError, ValueError, KeyError):
            return None


@dataclass(frozen=True)
class ScoreboardState(HyperCoreNode):
    sport: str
    league: str
    scoreboard_date: str
    last_checked_at: str
    refreshed_at: str | None
    event_count: int
    ok: bool
    error: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "sport", str(self.sport or "").strip().lower())
        object.__setattr__(self, "league", str(self.league or "").strip().lower())
        object.__setattr__(self, "scoreboard_date", str(self.scoreboard_date or "").strip())
        object.__setattr__(self, "last_checked_at", str(self.last_checked_at or "").strip())
        object.__setattr__(self, "refreshed_at", _clean_text(self.refreshed_at))
        object.__setattr__(self, "event_count", int(self.event_count))
        object.__setattr__(self, "ok", _bool_value(self.ok))
        object.__setattr__(self, "error", _clean_text(self.error))

    def to_kv(self, *, omit_empty: bool = False) -> dict[str, Any]:
        data = asdict(self)
        if not omit_empty:
            return data

        out: dict[str, Any] = {}
        for key, value in data.items():
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            out[key] = value
        return out

    def as_record(self, **extra: Any) -> dict[str, Any]:
        data = self.to_kv()
        data.update(extra)
        return data

    @classmethod
    def properties(cls) -> list[str]:
        return [field.name for field in fields(cls)]

    @classmethod
    def from_mapping(
        cls,
        source: Mapping[str, Any] | Any,
        *,
        field_map: Mapping[str, str] | None = None,
        transforms: Mapping[str, Callable[[Any, Any], Any]] | None = None,
        defaults: Mapping[str, Any] | None = None,
        strict: bool = False,
    ) -> "ScoreboardState":
        mapped: dict[str, Any] = {}
        field_map = dict(field_map or {})
        transforms = dict(transforms or {})
        defaults = dict(defaults or {})

        for field_name in cls.properties():
            source_key = field_map.get(field_name, field_name)
            value = _read_value(source, source_key, defaults.get(field_name))

            if field_name in transforms:
                value = transforms[field_name](value, source)

            if value is None and field_name in {"sport", "league", "scoreboard_date", "last_checked_at", "event_count", "ok"} and strict:
                raise ValueError(f"Missing required field '{field_name}'")

            mapped[field_name] = value

        return cls(**mapped)

    @classmethod
    def from_source(
        cls,
        source: Any,
        *,
        mapper: Callable[[Any], Mapping[str, Any]],
    ) -> "ScoreboardState":
        return cls.from_mapping(mapper(source))

    def save_to(
        self,
        hyper_client: HyperClient,
        *,
        root: str = "sports",
        sub_paths: list[str] | None = None,
    ) -> None:
        old_root = hyper_client.root
        try:
            hyper_client.root = root
            effective_sub_paths = sub_paths or _state_sub_paths(
                sport=self.sport,
                league=self.league,
                scoreboard_date=self.scoreboard_date,
            )
            self.commit(
                hyper_client,
                sub_paths=effective_sub_paths,
            )
        finally:
            hyper_client.root = old_root

    @classmethod
    def load_from(
        cls,
        hyper_client: HyperClient,
        *,
        sport: str,
        league: str,
        scoreboard_date: str,
        max_age_minutes: int = 15,
        root: str = "sports",
        sub_paths: list[str] | None = None,
    ) -> tuple["ScoreboardState | None", bool]:
        old_root = hyper_client.root
        try:
            hyper_client.root = root
            effective_sub_paths = sub_paths or _state_sub_paths(
                sport=sport,
                league=league,
                scoreboard_date=scoreboard_date,
            )
            clean_data, metadata = cls.read_out(
                hyper_client,
                sub_paths=effective_sub_paths,
            )
        finally:
            hyper_client.root = old_root

        if not clean_data:
            return None, True

        try:
            instance = cls.from_mapping(clean_data, strict=True)
            object.__setattr__(instance, "_metadata", metadata)

            basis = _parse_iso_datetime(instance.refreshed_at) or _parse_iso_datetime(
                instance.last_checked_at
            )
            if basis is None:
                return instance, True

            update_required = (
                datetime.now(timezone.utc) - basis
            ) >= timedelta(minutes=max_age_minutes)

            return instance, update_required
        except (TypeError, ValueError, KeyError):
            return None, True