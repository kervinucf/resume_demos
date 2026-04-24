from __future__ import annotations

import sys

from HyperCoreSDK.python.helpers.server import create_hyper_server
from HyperCoreSDK.python.helpers.storage import create_default_storage_directory
from HyperCoreSDK.python.client import HyperClient


def main(client_instance: HyperClient) -> int:
    try:
        print(f"geo relay running at {client_instance.url}")
        print("Press Ctrl-C to stop.")

        if client_instance.owns_relay():
            try:
                client_instance._owned.process.wait()
            except KeyboardInterrupt:
                pass
        else:
            print("attached to existing relay")

    finally:
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