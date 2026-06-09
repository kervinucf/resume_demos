from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from HyperCoreSDK.python.client import HyperClient, create_default_storage_directory
from app.lib.helpers.geo import slugify

GEO_ROOT = "geo"
WEATHER_ROOT = "weather"

DATA_DIR = str(
    Path(os.getenv("HYPER_DATA_DIR", create_default_storage_directory()))
    .expanduser()
    .resolve()
)


def main(condition: str = "clear", limit: int = 20) -> int:

    print(f"locations with condition={condition!r} (data dir: {DATA_DIR})", flush=True)


    with HyperClient(root_key=WEATHER_ROOT, data_dir=DATA_DIR) as db:
        seen_locations: set[str] = set()
        count = 0

        for weather_record in db.find(
            namespace=WEATHER_ROOT,
            read_path="events",
            where=lambda _weather_record: slugify(str(_weather_record.get("condition") or "")) == condition
        ):
            print(weather_record)
            location_key = str(weather_record.get("location") or "").strip()
            if not location_key or location_key in seen_locations:
                continue

            seen_locations.add(location_key)

            location = db.get_record(
                root=GEO_ROOT,
                path=f"{GEO_ROOT}/locations/{location_key}",
            )

            if not location:
                continue

            print(location)
            count += 1

            if count >= limit:
                break

    print(f"done: {count} location(s) with {condition!r} weather", flush=True)
    return 0


if __name__ == "__main__":
    argv = sys.argv[1:]
    sys.exit(main(argv[0] if argv else "clear"))