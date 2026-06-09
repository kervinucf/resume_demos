"""
Geo factory — the typed objects + index specs for the geo database.

Mirrors app/lib/helpers/weather/factory.py:
    WeatherEvent      <->  LocationObject       (frozen dataclass, the stored body)
    WeatherObserver   <->  LocationFactory      (builds typed objects from NORMALIZED fields)
    PROJECT / indexing <-> PROJECT / LOCATION_INDEXES

Nothing here knows how any provider encodes its data — LocationFactory takes plain
named fields, exactly like WeatherObserver.create_weather_object takes temp/wind/etc.
Provider decoding (column layout, file formats) lives in app/lib/sources/.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from HyperCoreSDK.python.dtos.object import HyperObject

_SLUG_RE = re.compile(r"[^a-zA-Z0-9]+")
_DASHES_RE = re.compile(r"-{2,}")


def slugify(value: str, *, fallback: str = "location") -> str:
    text = _SLUG_RE.sub("-", str(value or "").strip().lower())
    text = _DASHES_RE.sub("-", text).strip("-")
    return text or fallback


def country_code_to_flag(code: str) -> str:
    code = (code or "").strip().upper()
    if len(code) != 2 or not code.isalpha():
        return ""
    base = 0x1F1E6 - ord("A")
    return chr(base + ord(code[0])) + chr(base + ord(code[1]))


# ---------------------------------------------------------------------------
# Location object  (analog of WeatherEvent)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LocationObject:
    geoname_id: str
    name: str
    lat: float
    lon: float
    country_code: str
    population: int
    timezone: str
    country_flag_emoji: str = ""
    elevation: int | None = None

    country_name: str = ""
    continent: str = ""
    currency_code: str = ""
    currency_name: str = ""

    _finance_root: str = "finance"
    _geo_root: str = "geo"


# ---------------------------------------------------------------------------
# Country / currency enrichment  (neutral shapes; parsing lives in the source)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CountryCurrency:
    country_code: str
    country_name: str
    continent: str
    currency_code: str
    currency_name: str


# ---------------------------------------------------------------------------
# Location builder  (analog of WeatherObserver)
# ---------------------------------------------------------------------------
class LocationFactory(HyperObject):
    """Builds typed LocationObjects from already-normalized provider fields."""

    @staticmethod
    def _from_record(record_dict: dict) -> LocationObject:
        # The reader hands us a clean record body; build straight from it.
        return LocationObject(**record_dict)

    @classmethod
    def create_location_object(
            cls,
            *,
            geoname_id: str = None,
            name: str = None,
            lat: float = 0,
            lon: float = 0,
            country_code: str = None,
            population: int = None,
            timezone: str = None,
            elevation: int | None = None,
            from_record: dict[str, "LocationObject"] = None,
    ) -> LocationObject:

        if from_record:
            return cls._from_record(record_dict=from_record)

        cc = str(country_code or "").strip().upper()
        return LocationObject(
            geoname_id=str(geoname_id or "").strip(),
            name=str(name or "").strip(),
            lat=float(lat),
            lon=float(lon),
            country_code=cc,
            country_flag_emoji=country_code_to_flag(cc),
            timezone=str(timezone or "").strip(),
            population=int(population or 0),
            elevation=elevation,
        )

    @classmethod
    def add_currency_information(
            cls,
            base_location_object: LocationObject,
            currencies: dict[str, "CountryCurrency"]
    ) -> LocationObject:
        """
        Return a copy enriched with country/currency info, or self unchanged when the
        country isn't in the map. Frozen-safe (uses dataclasses.replace). This is the
        geo analog of the per-field enrichment WeatherObserver does at build time.
        """
        cc = currencies.get(str(base_location_object.country_code or "").strip().upper())

        if cc is None:
            print("can not add currency information -- not available")
            return base_location_object

        return replace(
            base_location_object,
            country_name=cc.country_name,
            continent=cc.continent,
            currency_code=cc.currency_code,
            currency_name=cc.currency_name,
        )