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
    population: int
    timezone: str
    country_flag_emoji: str = ""
    elevation: int | None = None

    @property
    def population_band(self) -> str:
        p = int(self.population)

        if p < 0:
            raise ValueError("population must be >= 0")
        elif p <= 49:
            return "0-49"
        elif p <= 199:
            return "50-199"
        elif p <= 999:
            return "200-999"
        elif p <= 2_499:
            return "1k-2_4k"
        elif p <= 4_999:
            return "2_5k-4_9k"
        elif p <= 9_999:
            return "5k-9_9k"
        elif p <= 19_999:
            return "10k-19_9k"
        elif p <= 49_999:
            return "20k-49_9k"
        elif p <= 99_999:
            return "50k-99_9k"
        elif p <= 249_999:
            return "100k-249_9k"
        elif p <= 499_999:
            return "250k-499_9k"
        elif p <= 999_999:
            return "500k-999_9k"
        elif p <= 2_499_999:
            return "1M-2_4M"
        elif p <= 4_999_999:
            return "2_5M-4_9M"
        elif p <= 9_999_999:
            return "5M-9_9M"
        elif p <= 19_999_999:
            return "10M-19_9M"
        else:
            return "20M+"


    def record_key(self) -> str:
        """Stable, human-readable per-record id: slug + geoname_id."""
        return f"{slugify(self.name)}-{self.geoname_id}"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["population_band"] = self.population_band
        return data

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
