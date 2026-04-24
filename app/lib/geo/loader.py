from __future__ import annotations

import sys
from pathlib import Path

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
USE_DIRECT = True
BATCH_OPS = 9_000
PROGRESS_EVERY = 50_000


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
        )

    client = create_hyper_server(
        root=ROOT,
        data_path=create_default_storage_directory(),
    )

    return client.writer(batch_ops=BATCH_OPS)


def ops_for_location(root: str, loc) -> list[dict]:
    return plan_upsert(
        root=root,
        record_path=f"locations/{loc.record_key()}",
        record_data=loc.to_dict(),
        index_specs=LOCATION_INDEXES,
        ref_key=str(loc.geoname_id),
        ref_payload=loc.ref_payload(),
        prior_paths=(),
    )


def main() -> int:
    data_file_path = Path(__file__).parent / "allCountries.txt"

    count = 0

    with make_writer() as writer:
        with data_file_path.open("r", encoding="utf-8") as f:
            for line in f:
                loc = location_from_row(line.rstrip("\n").split("\t"))
                if loc is None:
                    continue

                writer.write_ops(ops_for_location(writer.root, loc))
                count += 1

                if count % PROGRESS_EVERY == 0:
                    print(f"  loaded {count:,}… ({writer.written:,} writes)")

        print(f"done: loaded {count:,} locations ({writer.written:,} writes)")

    return 0


if __name__ == "__main__":
    sys.exit(main())