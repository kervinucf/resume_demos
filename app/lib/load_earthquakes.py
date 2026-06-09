"""
Build the earthquake database from a USGS GeoJSON provider (root="earthquakes").

Every event is one node at earthquakes.events.<period>-<event_day>-<usgs_id>,
written densely and indexed (browsable) by period, event_day, magnitude_band,
status, and alert (each scoped by period where useful). A latest pointer tracks
the newest event per feed period.

Orchestration only. The provider is read inside the create_* verbs and named in
exactly one place (app/lib/helpers/earthquakes/__init__.py); this file never
touches GeoJSON shapes, URLs, or the source module — switching providers doesn't
touch it.

Geo cross-linking (nearby-location backrefs) is deferred: it needs a live relay
spatial query, not the cold offline build used here.
"""
from __future__ import annotations

import sys

from app.lib.helpers.earthquakes import (
    HyperClient,
    apply_graph_operations,
    list_event_candidates,
    create_event_object,
    EarthquakeEventObject,
)

__all__ = ["load_earthquakes", "EarthquakeEventObject"]

EARTHQUAKE_ROOT = "earthquakes"
DEFAULT_PERIODS = ["hour", "day"]


def load_earthquakes(
        ROOT: str = EARTHQUAKE_ROOT,
        DATA_DIR: str = None,
        periods: list[str] | None = None,
) -> int:
    periods = periods or DEFAULT_PERIODS
    print(f"data dir: {DATA_DIR}", flush=True)
    print(f"periods: {', '.join(periods)}", flush=True)

    try:
        with HyperClient.open_sqlite_file(
                root_key=ROOT,
                reset=True,
                path=DATA_DIR,
        ) as data_store:
            written = 0
            latest_done: set[str] = set()
            for event_record, candidate_proof in list_event_candidates(periods):
                if not candidate_proof:
                    continue

                event_object, object_proof = create_event_object(event_record)
                if not event_object:
                    print("skipping", event_record.get("event_id"))
                    continue

                period_key = event_object.latest_key()
                write_latest = period_key not in latest_done
                latest_done.add(period_key)

                apply_graph_operations(
                    event_object=event_object,
                    client_instance=data_store,
                    namespace=EARTHQUAKE_ROOT,
                    write_latest=write_latest,
                )

                written += 1
                print(f"  ok: M{event_object.magnitude:g} {event_object.place} "
                      f"({event_object.period})", flush=True)

            print(f"done: built {data_store.count:,} events ({data_store.written:,} writes)", flush=True)

    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(load_earthquakes())
