from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from HyperCoreSDK.python.client import HyperClient
from HyperCoreSDK.python.helpers.server import create_hyper_server
from HyperCoreSDK.python.helpers.storage import create_default_storage_directory
from HyperCoreSDK.python.helpers.indexes import (
    ScopeSpec,
    ValueIndexSpec,
    plan_upsert,
)
from app.lib.geo.helpers import location_from_row


ROOT = "geo"
FINANCE_ROOT = "finance"

USE_DIRECT = True
BATCH_OPS = 9_000
PROGRESS_EVERY = 50_000


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
    country_name = parts[4].strip()
    continent = parts[8].strip().upper()
    currency_code = parts[10].strip().upper()
    currency_name = parts[11].strip()

    if not country_code:
        return None

    return CountryCurrency(
        country_code=country_code,
        country_name=country_name,
        continent=continent,
        currency_code=currency_code,
        currency_name=currency_name,
    )


def load_country_currency(path: Path) -> dict[str, CountryCurrency]:
    out: dict[str, CountryCurrency] = {}

    if not path.exists():
        print(f"warning: countryInfo.txt not found at {path}; continuing without currency enrichment")
        return out

    with path.open("r", encoding="utf-8") as f:
        for line in f:
            item = _country_currency_from_line(line)
            if item is not None:
                out[item.country_code] = item

    return out


LOCATION_INDEXES: list[ValueIndexSpec] = [
    ValueIndexSpec(
        name="country_code",
        path="country_code",
        normalize="upper",
        link_projections={
            "name": "name",
            "country_flag_emoji": "country_flag_emoji",
            "timezone": "timezone",
            "lat": "lat",
            "lon": "lon",
            "country_name": "country_name",
            "continent": "continent",
            "currency_code": "currency_code",
            "currency_name": "currency_name",
            "currency_path": "currency_path",
        },
    ),
    ValueIndexSpec(
        name="timezone",
        path="timezone",
        normalize="slug",
        scopes=[ScopeSpec(path="country_code", normalize="upper")],
        link_projections={
            "name": "name",
            "country_code": "country_code",
            "country_flag_emoji": "country_flag_emoji",
            "lat": "lat",
            "lon": "lon",
            "country_name": "country_name",
            "continent": "continent",
            "currency_code": "currency_code",
            "currency_name": "currency_name",
            "currency_path": "currency_path",
        },
    ),
    ValueIndexSpec(
        name="population_band",
        path="population_band",
        normalize="none",
        link_projections={
            "name": "name",
            "country_code": "country_code",
            "country_flag_emoji": "country_flag_emoji",
            "lat": "lat",
            "lon": "lon",
            "population": "population",
            "country_name": "country_name",
            "continent": "continent",
            "currency_code": "currency_code",
            "currency_name": "currency_name",
            "currency_path": "currency_path",
        },
    ),
]


def make_writer():
    if USE_DIRECT:
        return HyperClient.direct_writer(
            data_dir=create_default_storage_directory(),
            root=ROOT,
            reset=True,
            bulk=True,
            batch_ops=BATCH_OPS,
            flush_every_rows=1_000_000,
            write_outbox=False,
            skip_memberships=True,

            # Critical with patched direct.py flush using:
            # ON CONFLICT(parent_id, name) DO UPDATE
            #
            # That conflict target requires the unique index:
            # idx_nodes_parent_name ON nodes(parent_id, name)
            drop_parent_lookup_index=False,
        )

    client = create_hyper_server(
        root=ROOT,
        data_path=create_default_storage_directory(),
    )

    return client.writer(batch_ops=BATCH_OPS)


def location_body(
    loc,
    country_currency: dict[str, CountryCurrency],
) -> dict[str, Any]:
    data = loc.to_dict()

    country_code = str(loc.country_code or "").strip().upper()
    cc = country_currency.get(country_code)

    if cc is not None:
        data["country_name"] = cc.country_name
        data["continent"] = cc.continent
        data["currency_code"] = cc.currency_code
        data["currency_name"] = cc.currency_name

        if cc.currency_code:
            data["currency_path"] = f"{FINANCE_ROOT}.currencies.{cc.currency_code}"
            data["fx_latest_usd_path"] = f"{FINANCE_ROOT}.latest.fx.USD.{cc.currency_code}"

    return data


def location_query(
    root: str,
    record_path: str,
    loc,
    country_currency: dict[str, CountryCurrency],
) -> dict[str, Any]:
    record_dot = f"{root}.{record_path.replace('/', '.')}"
    country_code = str(loc.country_code or "").strip().upper()
    cc = country_currency.get(country_code)

    currency_path = (
        f"{FINANCE_ROOT}.currencies.{cc.currency_code}"
        if cc is not None and cc.currency_code
        else None
    )

    fx_latest_usd_path = (
        f"{FINANCE_ROOT}.latest.fx.USD.{cc.currency_code}"
        if cc is not None and cc.currency_code
        else None
    )

    refs: dict[str, str] = {}

    if currency_path:
        refs["currency"] = currency_path

    if fx_latest_usd_path:
        refs["fx_latest_usd"] = fx_latest_usd_path

    tokens = [
        loc.name,
        loc.country_code,
        getattr(loc, "country_flag_emoji", None),
        getattr(loc, "timezone", None),
        loc.record_key(),
        cc.country_name if cc else None,
        cc.continent if cc else None,
        cc.currency_code if cc else None,
        cc.currency_name if cc else None,
    ]

    return {
        "entity_id": record_dot,
        "entity_type": "location",
        "canonical_path": record_dot,
        "display": loc.name,
        "text": " ".join(str(x) for x in tokens if x),
        "facets": {
            "country_code": loc.country_code,
            "timezone": getattr(loc, "timezone", None),
            "population_band": getattr(loc, "population_band", None),
            "country_name": cc.country_name if cc else None,
            "continent": cc.continent if cc else None,
            "currency_code": cc.currency_code if cc else None,
            "currency_name": cc.currency_name if cc else None,
        },
        "numbers": {
            "lat": float(loc.lat),
            "lon": float(loc.lon),
            "population": float(getattr(loc, "population", 0) or 0),
        },
        "refs": refs,
        "tokens": tokens,
    }


def ops_for_location(
    root: str,
    loc,
    country_currency: dict[str, CountryCurrency],
) -> list[dict[str, Any]]:
    record_path = f"locations/{loc.record_key()}"
    data = location_body(loc, country_currency)

    return plan_upsert(
        root=root,
        record_path=record_path,
        record_data={
            **data,
            "query": location_query(root, record_path, loc, country_currency),
        },
        index_specs=LOCATION_INDEXES,
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
        prior_paths=(),
    )


def main() -> int:
    data_dir = Path(__file__).parent
    data_file_path = data_dir / "allCountries.txt"
    country_info_path = data_dir / "countryInfo.txt"

    country_currency = load_country_currency(country_info_path)
    print(f"countryInfo loaded: {len(country_currency):,} countries")

    count = 0

    with make_writer() as writer:
        with data_file_path.open("r", encoding="utf-8") as f:
            for line in f:
                loc = location_from_row(line.rstrip("\n").split("\t"))
                if loc is None:
                    continue

                writer.write_ops(ops_for_location(writer.root, loc, country_currency))
                count += 1

                if count % PROGRESS_EVERY == 0:
                    print(f"  loaded {count:,}… ({writer.written:,} writes)")

        print(f"done: loaded {count:,} locations ({writer.written:,} writes)")

    return 0


if __name__ == "__main__":
    sys.exit(main())