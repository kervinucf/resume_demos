import time
import threading
from typing import Tuple, Any
from machine.m2.pds.lib.helpers.configs.world import get_current_broadcast_status
from machine.m2.pds.lib.helpers.fe.controller import FrontendController
from machine.m2.server.src.dependencies import get_database_adapter
from machine.m2.pds.lib.functions.resolvers.narrative_resolver import retrieve_from_narrative_db
from machine.m2.pds.lib.functions.resolvers.event_resolver import retrieve_from_event_db
from machine.sdk.pds.lib.components.utils.connection_manager import ConnectionManager
from machine.m2.pds.lib.helpers.utils import access_clock
from machine.m2.server.src._services.content_manager.data.service import process_content_update
from machine.m2.server.src._services.content_manager.segments.service import construct_content_script

PROGRESS_EPSILON = 0.0005
POLL_INTERVAL = 0.05
COARSE_INTERVAL = 0.2
CUTOFF_SECONDS = 4

# Shared flag to indicate when preview input is allowed
allow_preview = threading.Event()


def generate_interrupt_script(controller: FrontendController) -> str:
    print(">>> Generating INTERRUPT script to clear frontend.")
    controller.clear()
    all_layers = ['html', 'labels', 'rings', 'paths', 'points', 'arcs', 'hex', 'polygons', 'heatmaps']
    for layer in all_layers:
        controller.clear_layer(layer)
        controller.refresh_layer(layer)
    controller.set_state("chyronText", "")
    controller.set_state("tickerText", "STANDBY...")
    controller.set_state("rundownStories", [])
    controller.set_state("selectedStory", None)
    controller.pan_to_globe_location(lat=20, lng=0, duration=1500, altitude=3.5)
    return controller.build()


def _print_status(status: Tuple[Any, ...]):
    (region, cur_seg, next_seg, next_region, minutes_left, progress) = status
    print("-" * 58)
    print(f"Region        : {region}")
    print(f"Current       : {cur_seg} ({progress:.2%})")
    print(f"Next          : {next_seg} (in {minutes_left:.2f} mins)")
    print("-" * 58)


def _wait_for_segment_boundary() -> Tuple[Any, ...]:
    print(">>> Watching for next segment boundary... (press 'P' to preview)")
    allow_preview.set()  # Enable preview input
    while True:
        status = get_current_broadcast_status()
        progress = status[5]
        if progress < PROGRESS_EPSILON:
            print(">>> Segment boundary detected!")
            allow_preview.clear()  # Disable preview once boundary hit
            return status
        time.sleep(POLL_INTERVAL)


def _wait_for_duration(duration_minutes: float):
    end_time = time.monotonic() + duration_minutes * 60
    while (remaining := end_time - time.monotonic()) > 0:
        sleep_time = min(remaining, COARSE_INTERVAL)
        time.sleep(sleep_time)


def send_preview(conn_mgr, fe_ctrl):
    """Triggered when user presses 'P' during waiting state."""
    try:
        print(">>> PREVIEW requested. Generating preview script...")
        region, cur_seg, next_seg, next_region, minutes_left, progress = get_current_broadcast_status()
        preview_broadcast = retrieve_from_narrative_db(
            object_segment=True,
            data_adapter=get_database_adapter(narrative=True),
            date=access_clock(return_date=True),
            source=next_seg,
        )
        if preview_broadcast:
            preview_script = construct_content_script(
                broadcast=preview_broadcast,
                current_segment=next_seg,
                controller=fe_ctrl,
                database_adapter=get_database_adapter
            )
            conn_mgr.broadcast(script_content=preview_script)
            print(f">>> Preview for '{next_seg}' broadcasted successfully.")
        else:
            print(f">>> No preview data found for '{next_seg}'.")
    except Exception as e:
        print(f"!!! Error during preview generation: {e}")


def input_listener(conn_mgr, fe_ctrl):
    """Background thread to handle 'P' key input."""
    while True:
        user_input = input().strip().lower()
        if user_input == 'p':
            if allow_preview.is_set():
                send_preview(conn_mgr, fe_ctrl)
            else:
                print(">>> Preview not available right now (wait for boundary phase).")


def run_narrative_monitor():
    _region, cur_seg, next_seg, next_region, minutes_left, progress = get_current_broadcast_status()
    conn_mgr = ConnectionManager()
    fe_ctrl = FrontendController()

    process_content_update(
        next_region=next_region,
        next_segment=next_seg,
        progress=progress,
        minutes_left=minutes_left,
        database_adapter=get_database_adapter
    )

    print("--- Narrative Monitor Service Started ---")
    _print_status(get_current_broadcast_status())

    threading.Thread(target=input_listener, args=(conn_mgr, fe_ctrl), daemon=True).start()

    try:
        while True:
            status = _wait_for_segment_boundary()
            _print_status(status)

            _region, cur_seg, next_seg, next_region, minutes_left, progress = status
            broadcast = retrieve_from_narrative_db(
                object_segment=True,
                data_adapter=get_database_adapter(narrative=True),
                date=access_clock(return_date=True),
                source=cur_seg,
            )

            if broadcast:
                print(f">>> Airing '{cur_seg}' for {minutes_left:.2f} minutes...")
                main_script = construct_content_script(
                    broadcast=broadcast,
                    current_segment=cur_seg,
                    controller=fe_ctrl,
                    database_adapter=get_database_adapter
                )
                conn_mgr.broadcast(script_content=main_script)
                sleep_duration = minutes_left * 60 - CUTOFF_SECONDS

                if sleep_duration > 0:
                    print(f">>> Main script running. Will interrupt in {sleep_duration:.2f} seconds.")
                    time.sleep(sleep_duration)
                    interrupt_script = generate_interrupt_script(fe_ctrl)
                    conn_mgr.broadcast(script_content=interrupt_script)
            else:
                print(f">>> '{cur_seg}' not found. Moving on...")
                process_content_update(
                    next_region=next_region,
                    next_segment=next_seg,
                    progress=progress,
                    minutes_left=minutes_left,
                    database_adapter=get_database_adapter
                )

    except KeyboardInterrupt:
        print("\nMonitoring stopped by user. Graceful shutdown.")
    except Exception as e:
        print(f"\nAn unexpected error occurred: {e}")


#

if __name__ == "__main__":
    run_narrative_monitor()
