import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


# ==============================================================================
# TIME + NARRATIVE FUNCTIONS
# ==============================================================================

def daily_narrative(current_utc):
    """Returns the regional narrative code based on UTC hour."""
    narrative_order = {
        0: "AS-1", 1: "EU-1", 2: "NA-1", 3: "AF-1",
        4: "SA-1", 5: "OC-1", 6: "AS-2", 7: "EU-2",
        8: "NA-2", 9: "AF-2", 10: "SA-2", 11: "OC-2",
        12: "AS-3", 13: "EU-3", 14: "NA-3", 15: "AF-3",
        16: "SA-3", 17: "OC-3", 18: "AS-4", 19: "EU-4",
        20: "NA-4", 21: "AF-4", 22: "SA-4", 23: "OC-4"
    }
    return narrative_order.get(current_utc.hour, "NA-1")


def hourly_narrative():
    """Returns the standard list of hourly broadcast segments."""
    return [
        "global_headlines", "regional_headlines", "regional_weather",
        "global_weather", "sports_events", "global_finances",
        "regional_finances", "earthquakes", "sun_rises", "sun_sets"
    ]


def get_current_time(local_tz="America/Chicago"):
    """Gets current UTC time."""
    return datetime.now(ZoneInfo(local_tz)).astimezone(ZoneInfo("UTC"))


def build_current_narrative(current_utc_time):
    """Builds narrative for current time."""
    return daily_narrative(current_utc_time), hourly_narrative()


def create_hourly_schedule(segments):
    """Builds 60-minute schedule of segments."""
    num_segments = len(segments)
    if num_segments == 0: return []
    base_duration = 60 // num_segments
    remainder = 60 % num_segments
    schedule = []
    current_minute = 0
    for i, seg in enumerate(segments):
        duration = base_duration + (1 if i < remainder else 0)
        schedule.append({
            'segment': seg,
            'duration': duration,
            'start_minute': current_minute,
            'end_minute': current_minute + duration
        })
        current_minute += duration
    return schedule


def get_current_segment_info(schedule):
    """Returns info about the active segment and progress."""
    now = get_current_time()
    current_minute, current_second = now.minute, now.second
    for s in schedule:
        if s["start_minute"] <= current_minute < s["end_minute"]:
            elapsed_seconds = (current_minute - s["start_minute"]) * 60 + current_second
            total_seconds = s["duration"] * 60
            progress = elapsed_seconds / total_seconds if total_seconds > 0 else 0
            return {
                "current_segment": s["segment"],
                "duration": s["duration"],
                "progress": progress,
                "time_remaining": (1 - progress) * s["duration"],
            }
    return None


def get_current_broadcast_status():
    """Computes region, segment, progress."""
    current_utc = get_current_time()
    region, segments = build_current_narrative(current_utc)
    schedule = create_hourly_schedule(segments)
    cur = get_current_segment_info(schedule)
    if not cur:
        return "Error", "N/A", "N/A", "Error", 0, 0

    current_idx = next((i for i, s in enumerate(schedule) if s["segment"] == cur["current_segment"]), -1)
    if current_idx == -1:
        return region, "N/A", "N/A", "Error", 0, 0

    next_idx = (current_idx + 1) % len(schedule)
    next_seg = schedule[next_idx]["segment"]
    next_region = region if next_idx != 0 else daily_narrative(current_utc + timedelta(hours=1))

    return region, cur["current_segment"], next_seg, next_region, cur["time_remaining"], cur["progress"]


# ==============================================================================
# IMPORT BROADCAST SYSTEM
# ==============================================================================

from machine.m2.pds.lib.helpers.fe.controller import FrontendController
from machine.sdk.pds.lib.components.utils.connection_manager import ConnectionManager
from machine.m2.server.src.dependencies import get_database_adapter
from machine.m2.pds.lib.helpers.utils import access_clock
from machine.m2.server.src._services.content_manager.data.service import process_content_update
from machine.m2.server.src._services.content_manager.segments.service import construct_content_script
from machine.m2.pds.lib.functions.resolvers.narrative_resolver import retrieve_from_narrative_db


# ==============================================================================
# CLI IMPLEMENTATION
# ==============================================================================

class NarrativeCLI:
    def __init__(self):
        self.conn_mgr = ConnectionManager()
        self.fe_ctrl = FrontendController()
        self.running = True
        self.lock = threading.Lock()
        self.status_thread = threading.Thread(target=self._auto_status, daemon=True)

    # -------------------------------------------------------
    # UTILITIES
    # -------------------------------------------------------

    def _print_status(self):
        region, cur_seg, next_seg, next_region, mins_left, progress = get_current_broadcast_status()
        print("-" * 60)
        print(f"Region        : {region}")
        print(f"Current (Auto): {cur_seg} ({progress:.2%})")
        print(f"Next (Auto)   : {next_seg} (in {mins_left:.2f} mins)")
        print(f"Next Region   : {next_region}")
        print("-" * 60)

    def _auto_status(self):
        while self.running:
            time.sleep(15)
            with self.lock:
                print("\n[Auto-Refresh Status]")
                self._print_status()

    # -------------------------------------------------------
    # COMMAND HANDLERS
    # -------------------------------------------------------

    def cmd_status(self):
        self._print_status()

    def cmd_update(self, args):
        """Updates content for a specified segment, or the next scheduled one."""
        region, cur_seg, next_seg, next_region, mins_left, progress = get_current_broadcast_status()

        target_segment = ""
        if args:
            segment_arg = args[0]
            if segment_arg in hourly_narrative():
                target_segment = segment_arg
                print(f">>> Manually targeting '{target_segment}' for content update...")
            else:
                print(f"!!! Invalid segment '{segment_arg}'. Please choose from: {hourly_narrative()}")
                return
        else:
            target_segment = next_seg
            print(f">>> Targeting next scheduled segment '{target_segment}' for content update...")

        # For manual updates, we'll use the upcoming region context.
        target_region = next_region

        print(">>> Performing content update...")
        process_content_update(
            next_region=target_region,
            next_segment=target_segment,
            progress=0,  # Not relevant for manual update
            minutes_left=0,  # Not relevant for manual update
            database_adapter=get_database_adapter
        )
        print(f">>> Content update for '{target_segment}' completed.")

    def cmd_send(self, args):
        """Fetches, constructs, and sends a broadcast for a specific segment."""
        if not args:
            print("!!! Usage: send <segment_name>")
            print(f"    Available segments: {hourly_narrative()}")
            return

        segment_name = args[0]
        if segment_name not in hourly_narrative():
            print(f"!!! Invalid segment '{segment_name}'.")
            print(f"    Available segments: {hourly_narrative()}")
            return

        try:
            print(f">>> Generating broadcast for segment '{segment_name}'...")
            broadcast = retrieve_from_narrative_db(
                object_segment=True,
                data_adapter=get_database_adapter(narrative=True),
                date=access_clock(return_date=True),
                source=segment_name,
            )
            if broadcast:
                # Explicitly set the frontend mode to prevent UI bugs
                self.fe_ctrl.clear()
                self.fe_ctrl.set_state("selectedStory", None)
                mode_map = {
                    "global_headlines": "RUNDOWN", "regional_headlines": "RUNDOWN",
                    "sports_events": "SPORTS", "global_finances": "FINANCE",
                }
                mode = mode_map.get(segment_name, "DEFAULT")
                self.fe_ctrl.set_state("currentMode", mode)

                script = construct_content_script(
                    broadcast=broadcast,
                    current_segment=segment_name,
                    controller=self.fe_ctrl,
                    database_adapter=get_database_adapter,
                )
                self.conn_mgr.broadcast(script_content=script)
                print(f">>> Broadcast for '{segment_name}' sent successfully.")
            else:
                print(f">>> No broadcast data found for '{segment_name}'. Try running 'update {segment_name}' first.")
        except Exception as e:
            print(f"!!! Error sending broadcast for '{segment_name}': {e}")

    def cmd_interrupt(self):
        """Immediately clear all visuals and reset frontend."""
        try:
            print(">>> Sending INTERRUPT script...")
            self.fe_ctrl.clear()
            for layer in ['html', 'labels', 'rings', 'paths', 'points', 'arcs', 'hex', 'polygons', 'heatmaps']:
                self.fe_ctrl.clear_layer(layer)
            self.fe_ctrl.set_state("chyronText", "")
            self.fe_ctrl.set_state("tickerText", "STANDBY...")
            self.fe_ctrl.set_state("rundownStories", [])
            self.fe_ctrl.set_state("selectedStory", None)
            self.fe_ctrl.pan_to_globe_location(lat=20, lng=0, duration=1500, altitude=3.5)
            interrupt_script = self.fe_ctrl.build()
            self.conn_mgr.broadcast(script_content=interrupt_script)
            print(">>> Frontend cleared to standby.")
        except Exception as e:
            print(f"!!! Error sending interrupt: {e}")

    # -------------------------------------------------------
    # CLI LOOP
    # -------------------------------------------------------

    def run(self):
        print("=== Narrative Broadcast CLI ===")
        print("Commands: status | update [segment] | send <segment> | interrupt | exit")
        self.status_thread.start()

        while self.running:
            try:
                raw_input = input("\n> ").strip().lower()
                if not raw_input:
                    continue

                parts = raw_input.split()
                cmd = parts[0]
                args = parts[1:]

                if cmd == "status":
                    self.cmd_status()
                elif cmd == "update":
                    self.cmd_update(args)
                elif cmd == "send":
                    self.cmd_send(args)
                elif cmd == "interrupt":
                    self.cmd_interrupt()
                elif cmd in ("exit", "quit"):
                    self.running = False
                    break
                else:
                    print("Unknown command. Try: status, update, send, interrupt, exit")
            except KeyboardInterrupt:
                print("\nShutting down by user request...")
                self.running = False
                break
            except Exception as e:
                print(f"An unexpected error occurred: {e}")

        print("CLI terminated.")


# ==============================================================================
# ENTRY POINT
# ==============================================================================

if __name__ == "__main__":
    cli = NarrativeCLI()
    cli.run()
