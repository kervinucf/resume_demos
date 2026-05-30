# app/lib/memory/loader.py
"""
Bulk-import GeoNames locations into root="memory".

Every location is one record. Browsable indexes are built for country_code,
timezone (scoped by country), and population band. Search is inferred by the
relay from the record's data and `kind` — there is no hand-written query.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from HyperCoreSDK.python.helpers.loader import BulkLoader, projection
from HyperCoreSDK.python.helpers.indexes import ScopeSpec, ValueIndexSpec
from app.lib.geo.helpers import location_from_row


ROOT = "memory"
FINANCE_ROOT = "finance"


# Fields each index entry projects for cheap rendering without hydrating the record.
PROJECT = projection(
    "name", "country_flag_emoji", "timezone", "lat", "lon",
    "country_name", "continent", "currency_code", "currency_name", "currency_path",
)

LOCATION_INDEXES: list[ValueIndexSpec] = [
    ValueIndexSpec("country_code", "country_code", normalize="upper", link_projections=PROJECT),
    ValueIndexSpec(
        "timezone", "timezone", normalize="slug",
        scopes=[ScopeSpec("country_code", normalize="upper")],
        link_projections=PROJECT,
    ),
    ValueIndexSpec(
        "population_band", "population_band", normalize="none",
        link_projections={**PROJECT, "population": "population"},
    ),
]


@dataclass(frozen=True)
class CountryCurrency:
    country_code: str
    country_name: str
    continent: str
    currency_code: str
    currency_name: str


def _country_currency_from_line(line: str) -> CountryCurrency | None:
    line = line.rstrip("\n")
    if not line or line.startswith("#"):
        return None

    parts = line.split("\t")
    if len(parts) < 12:
        return None

    country_code = parts[0].strip().upper()
    if not country_code:
        return None

    return CountryCurrency(
        country_code=country_code,
        country_name=parts[4].strip(),
        continent=parts[8].strip().upper(),
        currency_code=parts[10].strip().upper(),
        currency_name=parts[11].strip(),
    )


def load_country_currency(path: Path) -> dict[str, CountryCurrency]:
    if not path.exists():
        print(f"warning: countryInfo.txt not found at {path}; continuing without currency enrichment")
        return {}

    out: dict[str, CountryCurrency] = {}
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            item = _country_currency_from_line(line)
            if item is not None:
                out[item.country_code] = item
    return out


def location_data(loc, currencies: dict[str, CountryCurrency]) -> dict[str, Any]:
    """One dict that is both the stored record and the thing search is inferred from."""
    data: dict[str, Any] = {"kind": "location", **loc.to_dict()}

    cc = currencies.get(str(loc.country_code or "").strip().upper())
    if cc is not None:
        data["country_name"] = cc.country_name
        data["continent"] = cc.continent
        data["currency_code"] = cc.currency_code
        data["currency_name"] = cc.currency_name
        if cc.currency_code:
            data["currency_path"] = f"{FINANCE_ROOT}.currencies.{cc.currency_code}"
            data["fx_latest_usd_path"] = f"{FINANCE_ROOT}.latest.fx.USD.{cc.currency_code}"

    return data


def main() -> int:
    here = Path(__file__).parent
    currencies = load_country_currency(here / "countryInfo.txt")
    print(f"countryInfo loaded: {len(currencies):,} countries")

    with BulkLoader(ROOT, indexes=LOCATION_INDEXES, reset=True) as memory:
        with (here / "allCountries.txt").open("r", encoding="utf-8") as f:
            for line in f:
                loc = location_from_row(line.rstrip("\n").split("\t"))
                if loc is None:
                    continue

                data = location_data(loc, currencies)
                memory.record(
                    f"locations/{loc.record_key()}",
                    data,
                    ref_key=str(loc.geoname_id),
                    ref_payload={
                        **loc.ref_payload(),
                        "country_name": data.get("country_name"),
                        "continent": data.get("continent"),
                        "currency_code": data.get("currency_code"),
                        "currency_name": data.get("currency_name"),
                        "currency_path": data.get("currency_path"),
                        "fx_latest_usd_path": data.get("fx_latest_usd_path"),
                    },
                )

        print(f"done: loaded {memory.count:,} locations ({memory.written:,} writes)")

    return 0


if __name__ == "__main__":
    sys.exit(main())