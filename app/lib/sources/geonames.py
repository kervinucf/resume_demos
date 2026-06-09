"""
GeoNames source — the provider-specific read layer for the geo loader.

Analog of app/lib/sources/open_metro.py: everything that knows *how this provider
encodes data* lives here and nowhere else. To swap providers, write another module
in app/lib/sources/ that exposes the same three callables and re-point the import in
app/lib/helpers/geo/__init__.py:

    source_available(data_dir)      -> (bool, Path)       # is the gazetteer present
    iter_geonames_list(...)         -> Iterator[dict]     # one normalized record per populated place
    iter_country_currency(...)      -> Iterator[dict]     # one normalized record per country

Records are plain dicts with neutral keys — no app.* / typed imports here, so a new
provider only has to match the key names, not our object model.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterator

# Where the GeoNames dumps live. Provider config stays here, not in the loader.
# Defaults to this module's directory: app/lib/sources/
_DEFAULT_DIR = Path(
    os.getenv("GEONAMES_DIR", str(Path(__file__).parent))
).expanduser().resolve()

_ALL_COUNTRIES = "allCountries.txt"
_COUNTRY_INFO = "countryInfo.txt"


def _resolve_dir(data_dir: str | Path | None = None) -> Path:
    if data_dir is None:
        return _DEFAULT_DIR
    return Path(data_dir).expanduser().resolve()


def _parse_int(value: Any) -> int | None:
    value = str(value or "").strip()
    if not value:
        return None

    try:
        return int(value)
    except ValueError:
        return None


def source_available(data_dir: str | Path | None = None) -> tuple[bool, Path]:
    """Whether the primary gazetteer file is present, and the path we looked at."""
    path = _resolve_dir(data_dir) / _ALL_COUNTRIES
    return path.exists(), path


def iter_geonames_list(data_dir: str | Path | None = None) -> Iterator[dict[str, Any]]:
    """
    Stream populated places from GeoNames allCountries.txt as normalized records.

    GeoNames column layout provider-specific knowledge:
      0 geonameid, 1 name, 4 latitude, 5 longitude, 6 feature_class,
      8 country_code, 14 population, 15 elevation, 16 dem, 17 timezone.

    Rows that aren't populated places ("P"), or that fail basic coercion, are dropped.
    """
    path = _resolve_dir(data_dir) / _ALL_COUNTRIES
    if not path.exists():
        raise FileNotFoundError(f"allCountries.txt not found at {path}")

    with path.open("r", encoding="utf-8") as f:
        for line in f:
            record = _record_from_row(line.rstrip("\n").split("\t"))
            if record is not None:
                yield record


def _record_from_row(parts: list[str]) -> dict[str, Any] | None:
    if len(parts) < 18:
        return None

    if parts[6].strip() != "P":
        return None

    country_code = parts[8].strip().upper()
    if not country_code:
        return None

    try:
        lat = float(parts[4])
        lon = float(parts[5])
        population = int(parts[14])
    except (ValueError, IndexError):
        return None

    return {
        "geoname_id": parts[0].strip(),
        "name": parts[1].strip(),
        "lat": lat,
        "lon": lon,
        "country_code": country_code,
        "population": population,
        "timezone": parts[17].strip(),
        "elevation": _parse_int(parts[15]) or _parse_int(parts[16]),
    }


def iter_country_currency(data_dir: str | Path | None = None) -> Iterator[dict[str, Any]]:
    """
    Stream per-country currency/continent records from GeoNames countryInfo.txt.

    Missing file is non-fatal — currency enrichment is optional — so we warn and
    yield nothing, mirroring the old load_country_currency behavior.
    """
    path = _resolve_dir(data_dir) / _COUNTRY_INFO
    if not path.exists():
        print(
            f"warning: countryInfo.txt not found at {path}; "
            "continuing without currency enrichment",
            flush=True,
        )
        return

    with path.open("r", encoding="utf-8") as f:
        for line in f:
            record = _country_currency_from_line(line)
            if record is not None:
                yield record


def _country_currency_from_line(line: str) -> dict[str, Any] | None:
    line = line.rstrip("\n")
    if not line or line.startswith("#"):
        return None

    parts = line.split("\t")
    if len(parts) < 12:
        return None

    country_code = parts[0].strip().upper()
    if not country_code:
        return None

    return {
        "country_code": country_code,
        "country_name": parts[4].strip(),
        "continent": parts[8].strip().upper(),
        "currency_code": parts[10].strip().upper(),
        "currency_name": parts[11].strip(),
    }