from __future__ import annotations

import os
import time
from pathlib import Path

from HyperCoreSDK.python.client import create_default_storage_directory

from app.lib.load_geo import load_geo
from app.lib.load_weather import load_weather


DATA_DIR = str(
    Path(os.getenv("HYPER_DATA_DIR", create_default_storage_directory()))
    .expanduser()
    .resolve()
)

LOOP_SLEEP_SECONDS = 60


def main() -> int:
    print("[loop] starting social data loader", flush=True)
    print(f"[loop] data dir: {DATA_DIR}", flush=True)

    print("[loop] step 1: load locations once", flush=True)
    load_geo(DATA_DIR=DATA_DIR)

    print("[loop] entering update cycle", flush=True)

    while True:
        print("[loop] step 2: update weather if due", flush=True)

        weather_results = load_weather(
            DATA_DIR=DATA_DIR,
            limit=12,
        )

        print(
            f"[loop] step 2b: load social from "
            f"{len(weather_results)} new weather events",
            flush=True,
        )

        # load_social(
        #     DATA_DIR=DATA_DIR,
        #     weather_events=weather_results,
        # )

        for result in weather_results:
            print(
                f"[loop] new weather event: {result.event}",
                flush=True,
            )

        print(
            f"[loop] cycle complete; sleeping {LOOP_SLEEP_SECONDS} seconds",
            flush=True,
        )

        time.sleep(LOOP_SLEEP_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())