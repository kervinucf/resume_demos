# HyperCoreSDK/python/load_locations.py
"""
Load GeoNames populated-places into the hypergraph.

Reads allCountries.txt (tab-separated, from https://download.geonames.org/export/dump/),
writes one Location node per populated place at `geo.locations.<slug>-<geoname_id>`,
and maintains country_code and timezone indexes so downstream consumers
(weather, news, map viewers) can navigate by region.

After a run, the `geo` namespace looks like:

    geo/
      locations/
        new-york-city-5128581       (your record)
        paris-2988507
        ...
      index/
        by/
          country_code/
            US/
              5128581               (ref → geo.locations.new-york-city-5128581)
              ...
            FR/
              2988507
          timezone/
            america-new-york/
              5128581
            europe-paris/
              2988507
      _meta/
        memberships/
          <sha1 of each record path>

Every index entry carries projected fields (name, country flag, lat/lon, ...)
so UIs can render listings without a second round-trip to hydrate the source
record. Follow `_links.record` on any entry to reach the canonical node.

Usage
-----
    # Auto-spawn a relay if one isn't already running
    python load_locations.py ./cities5000.txt

    # Use an existing relay
    HYPER_URL=http://127.0.0.1:8765 python load_locations.py ./allCountries.txt

    # Limit for development
    python load_locations.py ./allCountries.txt --limit 50000
"""
from __future__ import annotations

import sys
from pathlib import Path
from app.utils.server import create_hyper_server
from app.utils.storage import create_default_storage_directory
from app.lib.geo.helpers import location_from_row, Location
from HyperCoreSDK.python.client import HyperClient
from HyperCoreSDK.python.indexes import (
    ScopeSpec,
    ValueIndexSpec,
    upsert_with_indexes,
)

# ---------------------------------------------------------------------------
# Index specs
#
# Reads like sentences:
#
#   "Index by country_code, uppercase. Project name, flag, tz, lat, lon
#    onto each entry's links."
#
#   "Index by timezone, slugified. Also nest under country_code so that
#    `index/scoped/country_code/US/timezone/america-new-york/` contains only
#    the US entries in that zone."
# ---------------------------------------------------------------------------

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
        },
    ),
]

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(client_instance: HyperClient) -> int:
    print(
        client_instance.read(
            "geo.locations"
        )
    )

    try:
        data_file_path = Path(__file__).parent / "allCountries.txt"
        path = Path(data_file_path)
        count = 0

        with path.open("r", encoding="utf-8") as f:
            for line in f:
                loc = location_from_row(line.rstrip("\n").split("\t"))
                if loc is None:
                    continue

                upsert_with_indexes(
                    client_instance,
                    record_path=f"locations/{loc.record_key()}",
                    record_data=loc.to_dict(),
                    index_specs=LOCATION_INDEXES,
                    ref_key=loc.geoname_id,
                    ref_payload=loc.ref_payload(),
                )

                count += 1
                if 5000 > 0 and count % 5000 == 0:
                    print(f"  loaded {count:,}…")

    finally:
        # A spawned relay stays up after loading so you can poke at the data
        # or start consumers. Ctrl-C to stop.
        if client_instance.owns_relay():
            print(f"relay still running at {client_instance.url} (Ctrl-C to stop)")
            try:
                client_instance._owned.process.wait()
            except KeyboardInterrupt:
                pass
            finally:
                client_instance.close()
        else:
            client_instance.close()

    return 0


if __name__ == "__main__":
    sys.exit(
        main(
            client_instance=create_hyper_server(
                root="geo",
                data_path=create_default_storage_directory(),
            )
        )
    )
