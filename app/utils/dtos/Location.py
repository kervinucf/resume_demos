import re
from dataclasses import asdict, dataclass
from typing import Any

_SLUG_RE = re.compile(r"[^a-zA-Z0-9]+")
_DASHES_RE = re.compile(r"-{2,}")


def slugify(value: str, *, fallback: str = "location") -> str:
    text = _SLUG_RE.sub("-", str(value or "").strip().lower())
    text = _DASHES_RE.sub("-", text).strip("-")
    return text or fallback



# ---------------------------------------------------------------------------
# Location record
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Location:
    geoname_id: str
    name: str
    lat: float
    lon: float
    country_code: str
    timezone: str
    country_flag_emoji: str = ""
    elevation: int | None = None

    def record_key(self) -> str:
        """Stable, human-readable per-record id: slug + geoname_id."""
        return f"{slugify(self.name)}-{self.geoname_id}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def ref_payload(self) -> dict[str, Any]:
        """
        Compact snapshot merged into each index entry's `data`. Lets a UI
        render listings (city name, flag, coords) without hydrating the
        source record.
        """
        return {
            "name": self.name,
            "country_code": self.country_code,
            "country_flag_emoji": self.country_flag_emoji,
            "timezone": self.timezone,
            "lat": self.lat,
            "lon": self.lon,
        }
