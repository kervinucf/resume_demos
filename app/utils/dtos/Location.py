from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any, Callable, Mapping

from ___.HyperCoreSDK import HyperClient
from ___.HyperCoreSDK import HyperCoreNode

def country_code_to_flag_emoji(country_code: str) -> str:
    code = (country_code or "").strip().upper()
    if len(code) != 2 or not code.isalpha():
        return ""

    base = 127397
    return chr(base + ord(code[0])) + chr(base + ord(code[1]))


def _slugify(value: str) -> str:
    text = str(value or "").strip().lower()
    out: list[str] = []
    last_dash = False

    for ch in text:
        if ch.isalnum():
            out.append(ch)
            last_dash = False
        else:
            if not last_dash:
                out.append("-")
                last_dash = True

    return "".join(out).strip("-") or "location"


def _read_value(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


@dataclass(frozen=True)
class Location(HyperCoreNode):
    lat: float
    lon: float
    name: str
    country_code: str
    country_flag_emoji: str = ""
    elevation: int | None = None
    timezone: str = ""

    def __post_init__(self) -> None:
        clean_name = str(self.name or "").strip()
        clean_country_code = str(self.country_code or "").strip().upper()
        clean_timezone = str(self.timezone or "").strip()
        clean_lat = float(self.lat)
        clean_lon = float(self.lon)
        clean_elevation = None if self.elevation is None else int(self.elevation)
        clean_flag = str(self.country_flag_emoji or "").strip() or country_code_to_flag_emoji(clean_country_code)

        object.__setattr__(self, "name", clean_name)
        object.__setattr__(self, "country_code", clean_country_code)
        object.__setattr__(self, "timezone", clean_timezone)
        object.__setattr__(self, "lat", clean_lat)
        object.__setattr__(self, "lon", clean_lon)
        object.__setattr__(self, "elevation", clean_elevation)
        object.__setattr__(self, "country_flag_emoji", clean_flag)

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
        data = self.to_kv(omit_empty=True)
        data["path"] = path
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
    ) -> "Location":
        mapped: dict[str, Any] = {}
        field_map = dict(field_map or {})
        transforms = dict(transforms or {})
        defaults = dict(defaults or {})

        for field_name in cls.properties():
            source_key = field_map.get(field_name, field_name)
            value = _read_value(source, source_key, defaults.get(field_name))

            if field_name in transforms:
                value = transforms[field_name](value, source)

            if value is None and field_name in {"lat", "lon", "name", "country_code"} and strict:
                raise ValueError(f"Missing required field '{field_name}'")

            mapped[field_name] = value

        return cls(**mapped)

    @classmethod
    def from_source(
        cls,
        source: Any,
        *,
        mapper: Callable[[Any], Mapping[str, Any]],
    ) -> "Location":
        return cls.from_mapping(mapper(source))

    def storage_key(self, *, suffix: str | None = None) -> str:
        base = _slugify(self.name)
        if suffix is None or not str(suffix).strip():
            return base
        return f"{base}-{str(suffix).strip()}"

    def save_to(
        self,
        hyper_client: HyperClient,
        *,
        root: str = "geo",
        sub_paths: list[str] | None = None,
        collection: str = "locations",
        item_id: str | None = None,
    ) -> None:
        old_root = hyper_client.root
        try:
            hyper_client.root = root
            if sub_paths is None:
                sub_paths = [collection, item_id or self.storage_key()]
            self.commit(
                hyper_client,
                sub_paths=sub_paths,
            )
        finally:
            hyper_client.root = old_root

    @classmethod
    def load_from(
        cls,
        hyper_client: HyperClient,
        *,
        root: str = "geo",
        sub_paths: list[str] | None = None,
        collection: str = "locations",
        location_name: str | None = None,
    ) -> "Location | None":
        old_root = hyper_client.root
        try:
            hyper_client.root = root
            if sub_paths is None:
                if not location_name:
                    return None
                sub_paths = [collection, location_name]

            clean_data, metadata = cls.read_out(
                hyper_client,
                sub_paths=sub_paths,
            )
        finally:
            hyper_client.root = old_root

        if not clean_data:
            return None

        try:
            instance = cls.from_mapping(clean_data)
            object.__setattr__(instance, "_metadata", metadata)
            return instance
        except (TypeError, ValueError, KeyError):
            return None