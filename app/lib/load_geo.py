from __future__ import annotations

import sys

from app.lib.helpers.geo import (
    HyperClient,
    list_location_candidates,
    create_location_object,
    get_currency_information,
    apply_graph_operations,
)
from app.lib.helpers.common import get_loader_state, mark_ran


GEO_ROOT = "geo"
MIN_POPULATION = 1_000_000

# Keep while testing. Set to None for full geo load.
TEST_GEO_LIMIT: int | None = 20


def load_geo(
    force: bool = False,
    ROOT: str = GEO_ROOT,
    DATA_DIR: str | None = None,
) -> int:
    print("[geo] checking location load state", flush=True)

    state = get_loader_state(
        name="geo",
        DATA_DIR=DATA_DIR,
    )

    if not force and state.get("loaded"):
        print("[geo] skipped: locations already loaded", flush=True)
        return 0

    currencies = get_currency_information()

    print(f"[geo] data dir: {DATA_DIR}", flush=True)
    print(f"[geo] countryInfo loaded: {len(currencies):,} countries", flush=True)
    print("[geo] loading locations", flush=True)

    records_written = 0

    try:
        with HyperClient.open_sqlite_file(
            root_key=ROOT,
            reset=True,
            path=DATA_DIR,
        ) as data_store:

            for location_candidate, candidate_proof in list_location_candidates():
                location_object, object_proof = create_location_object(
                    location_candidate,
                    currencies,
                )

                if not location_object:
                    print(
                        "[geo] skipping invalid location candidate",
                        location_candidate,
                        flush=True,
                    )
                    continue

                if location_object.population <= MIN_POPULATION:
                    continue

                apply_graph_operations(
                    client_instance=data_store,
                    location_object=location_object,
                    namespace=GEO_ROOT,
                )

                records_written += 1
                print(f"[geo] saved {location_object.name}", flush=True)

                if TEST_GEO_LIMIT and records_written >= TEST_GEO_LIMIT:
                    print(
                        f"[geo] stopping early at test limit={TEST_GEO_LIMIT}",
                        flush=True,
                    )
                    break

            print(
                f"[geo] done: built {records_written:,} locations "
                f"({getattr(data_store, 'written', 0):,} writes)",
                flush=True,
            )

    except FileNotFoundError as exc:
        print(f"[geo] error: {exc}", file=sys.stderr)
        return 1

    print("[geo] marking locations as loaded", flush=True)

    mark_ran(
        name="geo",
        DATA_DIR=DATA_DIR,
        loaded=True,
        records_written=records_written,
    )

    print("[geo] marked locations as loaded", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(load_geo())