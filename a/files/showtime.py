import time
from datetime import datetime, timedelta

# ASSUMPTION: The code you provided is saved as 'narrative_cli.py'
# If it is named something else, please adjust the import below.
from machine.m2.server.src._services.content_manager.b2 import (
    ConnectionManager,
    FrontendController,
    process_content_update,
    construct_content_script,
    retrieve_from_narrative_db,
    get_database_adapter,
    access_clock,
    get_current_time,
    daily_narrative
)


class AutomatedBroadcaster:
    def __init__(self):
        print(">>> Initializing Automated Broadcast System...")
        self.conn_mgr = ConnectionManager()
        self.fe_ctrl = FrontendController()

        # The specific playlist requested
        self.playlist = [
            "global_finances",
            "sports_events",
            "global_headlines",
            "earthquakes"
        ]

        # Mode mapping for the frontend controller (copied from your CLI logic)
        self.mode_map = {
            "global_headlines": "RUNDOWN",
            "regional_headlines": "RUNDOWN",
            "sports_events": "SPORTS",
            "global_finances": "FINANCE",
            "earthquakes": "DEFAULT"  # Assuming default for earthquakes
        }

    def _update_segment_data(self, segment_name):
        """Triggers the backend data update for a specific segment."""
        try:
            print(f"    [Update] Processing data update for '{segment_name}'...")

            # Determine region context
            current_utc = get_current_time()
            # We look ahead slightly to ensure we are generating for the current/upcoming block
            region = daily_narrative(current_utc)

            process_content_update(
                next_region=region,
                next_segment=segment_name,
                progress=0,
                minutes_left=0,
                database_adapter=get_database_adapter
            )
            print(f"    [Update] Success: '{segment_name}' data refreshed.")
            return True
        except Exception as e:
            print(f"    [Update] Error refreshing '{segment_name}': {e}")
            return False

    def _display_segment(self, segment_name):
        """Fetches the script and sends it to the frontend."""
        try:
            print(f"    [Display] Constructing broadcast for '{segment_name}'...")

            # 1. Fetch from DB
            broadcast = retrieve_from_narrative_db(
                object_segment=True,
                data_adapter=get_database_adapter(narrative=True),
                date=access_clock(return_date=True),
                source=segment_name,
            )

            if not broadcast:
                print(f"    [Display] Warning: No broadcast object found for '{segment_name}'.")
                return False

            # 2. Prepare Frontend State
            # We explicitly clear and set modes to ensure the UI handles the data type correctly
            self.fe_ctrl.clear()
            self.fe_ctrl.set_state("selectedStory", None)

            mode = self.mode_map.get(segment_name, "DEFAULT")
            self.fe_ctrl.set_state("currentMode", mode)

            # 3. Build Script
            script = construct_content_script(
                broadcast=broadcast,
                current_segment=segment_name,
                controller=self.fe_ctrl,
                database_adapter=get_database_adapter,
            )

            # 4. Broadcast
            self.conn_mgr.broadcast(script_content=script)
            print(f"    [Display] Success: '{segment_name}' is now live.")
            return True

        except Exception as e:
            print(f"    [Display] Error broadcasting '{segment_name}': {e}")
            return False

    def run_hourly_loop(self):
        """
        Runs the playlist endlessly.
        4 items in playlist = 15 minutes per item to fill the hour.
        """
        print("=== Starting Hourly Loop ===")
        print(f"Playlist: {self.playlist}")

        # Calculate hold time to fill an hour evenly
        seconds_in_hour = 3600
        hold_time = seconds_in_hour // len(self.playlist)

        while True:
            cycle_start_time = datetime.now()
            print(f"\n>>> Starting Cycle at {cycle_start_time.strftime('%H:%M:%S')}")

            for i, segment in enumerate(self.playlist):
                segment_start = time.time()
                print(f"\n--- Segment {i + 1}/{len(self.playlist)}: {segment} ---")

                # 1. Update Data
                self._update_segment_data(segment)

                # 2. Display Data
                success = self._display_segment(segment)

                # 3. Wait Duration
                # We calculate sleep time to ensure we stay on the 15-minute marks
                # regardless of how long the update/display logic took.
                elapsed = time.time() - segment_start
                sleep_time = max(0, hold_time - elapsed)

                if success:
                    print(f"    [Wait] Showing '{segment}' for {sleep_time / 60:.1f} minutes...")
                else:
                    print(f"    [Wait] Failed to show '{segment}'. Holding anyway for sync...")

                time.sleep(sleep_time)


if __name__ == "__main__":
    runner = AutomatedBroadcaster()
    try:
        runner.run_hourly_loop()
    except KeyboardInterrupt:
        print("\n>>> Automated Broadcast Loop Stopped by User.")