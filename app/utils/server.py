from __future__ import annotations

import os
import signal
import sys
import threading
from pathlib import Path

from HyperCoreSDK.python.client import HyperClient, DEFAULT_URL


SOURCE_ROOT = os.getenv("HYPER_SOURCE_ROOT", "geo")
DERIVED_ROOT = os.getenv("HYPER_DERIVED_ROOT", "derived")
URL = os.getenv("HYPER_URL", DEFAULT_URL)
DATA_DIR = str(Path(os.getenv("HYPER_DATA_DIR", Path.cwd() / ".hyper-data")).expanduser().resolve())

def _define_relay_script() -> None:
    project_folder = Path.cwd().parent.parent.parent
    os.environ["HYPER_RELAY_SCRIPT"] = f"{project_folder}/HyperCoreSDK/src/relay.js"


def create_hyper_server(url=URL, root=SOURCE_ROOT, data_path=DATA_DIR) -> HyperClient:
    if HyperClient._health_ok(url):
        return HyperClient.attach(url, root=root)

    _define_relay_script()
    return HyperClient.spawn(data_dir=data_path, root=root)


def react(server_process, ev) -> None:
    prefix = SOURCE_ROOT + "."
    if not ev.path.startswith(prefix):
        return
    rel = ev.path[len(prefix):]
    if not rel:
        return

    target = f"{DERIVED_ROOT}.{rel}"

    if ev.kind == "del":
        try:
            server_process.delete(target)
        except Exception as exc:
            print(exc, file=sys.stderr)
        return

    payload = ev.data if isinstance(ev.data, dict) else {"value": ev.data}
    derived = {
        "source_path": ev.path,
        "source_fields": sorted(payload.keys()),
        "derived_at_seq": ev.commit_seq,
        "derived_at_ms": ev.updated_at,
    }

    try:
        server_process.write(target, {"data": derived})
    except Exception as exc:
        print(exc, file=sys.stderr)


def main() -> int:
    if SOURCE_ROOT == DERIVED_ROOT:
        return 2

    server_process = create_hyper_server()
    stop = threading.Event()

    def shutdown(*_):
        stop.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    handle = server_process.watch(
        SOURCE_ROOT,
        lambda ev: react(server_process, ev),
        scope="subtree",
        on_error=lambda e: print(e, file=sys.stderr),
    )

    try:
        while not stop.is_set():
            stop.wait(1.0)
    finally:
        handle.stop()
        server_process.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())