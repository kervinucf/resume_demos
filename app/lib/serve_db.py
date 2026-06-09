from __future__ import annotations

import sys
from typing import Any

from HyperCoreSDK.python.helpers.loader import Database
#
from app.lib.helpers.weather import (create_weather_candidate,
                                     create_weather_event_object,
                                     apply_graph_operations)
from app.lib.helpers.geo import geo_factory   # add this (or extend the existing geo import)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def serve_database(path_to_data_file: str) -> int:
    print("observing weather for geo locations…", flush=True)
    written = 0

    with Database() as data_store:
        data_store.serve(
            sqlite_file_location=path_to_data_file,
        )

    return 0


if __name__ == "__main__":
    sys.exit(serve_database(""))
