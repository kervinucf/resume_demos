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


def _event_sub_paths(*, location_key: str) -> list[str]:
    return [
        "current",
        str(location_key or "").strip(),
    ]


def _state_sub_paths(*, location_key: str) -> list[str]:
    return [
        "state",
        "current",
        str(location_key or "").strip(),
    ]


@dataclass(frozen=True)
class WeatherEvent(HyperCoreNode):
    lat: float
    lon: float
    name: str
    country_code: str | None
    condition: str
    temperature: float
    wind_speed: float | None
    precipitation: float | None
    rain: float | None
    showers: float | None
    snowfall: float | None
    weather_code: int | None
    local_time: str
    fetched_at: str
    location_path: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "lat", float(self.lat))
        object.__setattr__(self, "lon", float(self.lon))
        object.__setattr__(self, "name", str(self.name or "").strip())
        object.__setattr__(self, "country_code", _clean_text(self.country_code))
        object.__setattr__(self, "condition", str(self.condition or "").strip())
        object.__setattr__(self, "temperature", float(self.temperature))
        object.__setattr__(self, "wind_speed", None if self.wind_speed is None else float(self.wind_speed))
        object.__setattr__(self, "precipitation", None if self.precipitation is None else float(self.precipitation))
        object.__setattr__(self, "rain", None if self.rain is None else float(self.rain))
        object.__setattr__(self, "showers", None if self.showers is None else float(self.showers))
        object.__setattr__(self, "snowfall", None if self.snowfall is None else float(self.snowfall))
        object.__setattr__(self, "weather_code", None if self.weather_code is None else int(self.weather_code))
        object.__setattr__(self, "local_time", str(self.local_time or "").strip())
        object.__setattr__(self, "fetched_at", str(self.fetched_at or "").strip())
        object.__setattr__(self, "location_path", _clean_text(self.location_path))

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

    def as_reference(self, *, path: str, **extra: Any) -> dict[str, Any]:
        data = {
            "name": self.name,
            "country_code": self.country_code,
            "condition": self.condition,
            "temperature": self.temperature,
            "local_time": self.local_time,
            "path": path,
        }
        if self.location_path:
            data["location_path"] = self.location_path
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
    ) -> "WeatherEvent":
        mapped: dict[str, Any] = {}
        field_map = dict(field_map or {})
        transforms = dict(transforms or {})
        defaults = dict(defaults or {})

        for field_name in cls.properties():
            source_key = field_map.get(field_name, field_name)
            value = _read_value(source, source_key, defaults.get(field_name))

            if field_name in transforms:
                value = transforms[field_name](value, source)

            if value is None and field_name in {"lat", "lon", "name", "condition", "temperature", "local_time", "fetched_at"} and strict:
                raise ValueError(f"Missing required field '{field_name}'")

            mapped[field_name] = value

        return cls(**mapped)

    @classmethod
    def from_source(
        cls,
        source: Any,
        *,
        mapper: Callable[[Any], Mapping[str, Any]],
    ) -> "WeatherEvent":
        return cls.from_mapping(mapper(source))

    def storage_key(self) -> str:
        return self.name

    def save_to(
        self,
        hyper_client: HyperClient,
        *,
        root: str = "weather",
        location_key: str | None = None,
        sub_paths: list[str] | None = None,
    ) -> None:
        old_root = hyper_client.root
        try:
            hyper_client.root = root
            effective_sub_paths = sub_paths or _event_sub_paths(
                location_key=location_key or self.storage_key(),
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
        location_key: str,
        root: str = "weather",
        sub_paths: list[str] | None = None,
    ) -> "WeatherEvent | None":
        old_root = hyper_client.root
        try:
            hyper_client.root = root
            effective_sub_paths = sub_paths or _event_sub_paths(location_key=location_key)
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
class WeatherState(HyperCoreNode):
    location_name: str
    country_code: str | None
    last_checked_at: str
    refreshed_at: str | None
    local_time: str | None
    ok: bool
    error: str | None
    location_path: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "location_name", str(self.location_name or "").strip())
        object.__setattr__(self, "country_code", _clean_text(self.country_code))
        object.__setattr__(self, "last_checked_at", str(self.last_checked_at or "").strip())
        object.__setattr__(self, "refreshed_at", _clean_text(self.refreshed_at))
        object.__setattr__(self, "local_time", _clean_text(self.local_time))
        object.__setattr__(self, "ok", bool(self.ok))
        object.__setattr__(self, "error", _clean_text(self.error))
        object.__setattr__(self, "location_path", _clean_text(self.location_path))

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
    ) -> "WeatherState":
        mapped: dict[str, Any] = {}
        field_map = dict(field_map or {})
        transforms = dict(transforms or {})
        defaults = dict(defaults or {})

        for field_name in cls.properties():
            source_key = field_map.get(field_name, field_name)
            value = _read_value(source, source_key, defaults.get(field_name))

            if field_name in transforms:
                value = transforms[field_name](value, source)

            if value is None and field_name in {"location_name", "last_checked_at", "ok"} and strict:
                raise ValueError(f"Missing required field '{field_name}'")

            mapped[field_name] = value

        return cls(**mapped)

    @classmethod
    def from_source(
        cls,
        source: Any,
        *,
        mapper: Callable[[Any], Mapping[str, Any]],
    ) -> "WeatherState":
        return cls.from_mapping(mapper(source))

    def save_to(
        self,
        hyper_client: HyperClient,
        *,
        root: str = "weather",
        location_key: str | None = None,
        sub_paths: list[str] | None = None,
    ) -> None:
        old_root = hyper_client.root
        try:
            hyper_client.root = root
            effective_sub_paths = sub_paths or _state_sub_paths(
                location_key=location_key or self.location_name,
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
        location_key: str,
        max_age_hours: int = 4,
        root: str = "weather",
        sub_paths: list[str] | None = None,
    ) -> tuple["WeatherState | None", bool]:
        old_root = hyper_client.root
        try:
            hyper_client.root = root
            effective_sub_paths = sub_paths or _state_sub_paths(location_key=location_key)
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
            ) >= timedelta(hours=max_age_hours)

            return instance, update_required
        except (TypeError, ValueError, KeyError):
            return None, True