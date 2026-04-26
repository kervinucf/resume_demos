# -*- coding: utf-8 -*-
"""
Director: GLOBAL SPORTS (5-Minute Segment)

This version uses a more compact HTML card for better visual presentation on the globe.

- The '_sports_card_html' function has been updated with smaller dimensions.
- All other logic and pacing remains the same.
"""
import json
import datetime
from dataclasses import dataclass
from typing import List, Dict


# ---------------- DTO (As provided by user) ----------------
@dataclass
class SportsEvent:
    _DEFAULT_COLLECTION = "sports"
    timestamp_utc: str
    sport: str
    league: str
    track_id: str
    name: str
    short_name: str
    scheduled: str
    status: str
    venue_uid: str = ""
    home_uid: str = ""
    home_score: str = "0"
    away_uid: str = ""
    away_score: str = "0"
    uid: str = ""

    def __post_init__(self):
        parts = [self.sport, self.league, self.track_id]
        self.uid = ":".join([str(p).strip().lower().replace(" ", "_") for p in parts if p])


# ---------------- Frontend Controller (Unchanged) ----------------
class FrontendController:
    """Builds a sequence of JavaScript commands for the frontend globe."""

    def __init__(self): self.commands = []

    def build(self) -> str: return "\n".join(self.commands)

    def set_sleep(self, s: float):
        self.commands.append(f"await new Promise(r => setTimeout(r, {int(max(s, 0) * 1000)}));")
        return self

    def set_state(self, key, value):
        self.commands.append(f"state.{key} = {json.dumps(value)};")
        return self

    def log_message(self, msg: str):
        self.commands.append(f"console.log({json.dumps(msg)});")
        return self

    def initialize_layer(self, layer_name: str, properties: dict):
        chain = []
        for k, v in properties.items():
            val = v if (isinstance(v, str) and ('=>' in v or v.strip().startswith('('))) else json.dumps(v)
            chain.append(f".{k}({val})")
        base_map = {'html': 'htmlElements', 'labels': 'labels', 'rings': 'rings'}
        base = base_map.get(layer_name, layer_name)
        method = f"{base}Data"
        src = self._arr(layer_name)
        self.commands.append(f"if (globe) {{ globe{''.join(chain)}.{method}({src}); }}")
        return self

    def _arr(self, layer_name: str) -> str:
        arr_map = {'html': 'htmlData', 'labels': 'labelsData', 'rings': 'ringsData'}
        return f"state.{arr_map.get(layer_name, f'{layer_name}Data')}"

    def add_elements(self, layer_name: str, elements: list):
        a = self._arr(layer_name)
        self.commands.append(f"{a} = [...{a}, ...{json.dumps(elements)}];")
        return self

    def pan_to_globe_location(self, lat, lng, duration_ms, altitude=1.4):
        pov = f"{{lat:{json.dumps(lat)},lng:{json.dumps(lng)},altitude:{json.dumps(altitude)}}}"
        self.commands.append(f"if (globe) {{ globe.pointOfView({pov}, {int(duration_ms)}); }}")
        return self


# ---------------- Sports-Specific Helpers ----------------
def _sports_card_html(event: SportsEvent) -> str:
    """
    Generates HTML for a sports scoreboard card.
    **MODIFIED** for a more compact design.
    """
    home, away = event.short_name.split(' vs ')
    status_map = {
        "final": "FINAL",
        "in_progress": "LIVE",
        "scheduled": datetime.datetime.fromisoformat(event.scheduled).strftime('%H:%M UTC')
    }
    status_text = status_map.get(event.status, "SCHEDULED")

    score_display = f"""
    <div style="font-size: 16px; font-weight: 700;">{event.home_score}</div>
    <div style="font-size: 10px; margin: 0 6px;">-</div>
    <div style="font-size: 16px; font-weight: 700;">{event.away_score}</div>
    """
    if event.status == 'scheduled':
        score_display = ""

    return f"""
    <div style="font-family: Inter, system-ui, sans-serif; background: rgba(20,20,20,0.75); color: white; border-radius: 6px; padding: 8px; width: 130px; backdrop-filter: blur(6px); border: 1px solid rgba(255,255,255,0.15); box-shadow: 0 4px 12px rgba(0,0,0,0.4);">
        <div style="font-size: 8px; font-weight: 500; color: #bbb; margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.5px;">{event.league}</div>
        <div style="display: flex; justify-content: space-between; align-items: center; font-size: 11px; font-weight: 600; text-transform: uppercase;">
            <span>{home}</span>
            <span>{away}</span>
        </div>
        <div style="display: flex; justify-content: center; align-items: center; margin: 4px 0;">
            {score_display}
        </div>
        <div style="font-size: 10px; font-weight: 600; color: #ffc107; text-align: center; letter-spacing: 0.5px;">{status_text}</div>
    </div>
    """.strip()


# ---------------- Data Generation ----------------
def get_sports_venues() -> Dict[str, dict]:
    """Returns a mapping of venue_uid to location data."""
    return {
        "wembley": {"name": "Wembley Stadium", "city": "London", "lat": 51.556, "lon": -0.279},
        "maracana": {"name": "Maracanã Stadium", "city": "Rio de Janeiro", "lat": -22.912, "lon": -43.230},
        "tokyo_dome": {"name": "Tokyo Dome", "city": "Tokyo", "lat": 35.705, "lon": 139.751},
        "lambeau": {"name": "Lambeau Field", "city": "Green Bay", "lat": 44.501, "lon": -88.062},
        "scg": {"name": "Sydney Cricket Ground", "city": "Sydney", "lat": -33.891, "lon": 151.224},
    }


def get_sports_events() -> List[SportsEvent]:
    """Generates a sample list of sports events across different statuses."""
    now = datetime.datetime.now(datetime.timezone.utc)
    return [
        # Final Scores
        SportsEvent(now.isoformat(), "Soccer", "Premier League", "epl_234", "Chelsea vs Arsenal", "CHE vs ARS",
                    (now - datetime.timedelta(hours=3)).isoformat(), "final", "wembley", "che", "1", "ars", "2"),
        # In-Progress
        SportsEvent(now.isoformat(), "Cricket", "T20 World Cup", "t20_101", "Australia vs India", "AUS vs IND",
                    (now - datetime.timedelta(minutes=45)).isoformat(), "in_progress", "scg", "aus", "154", "ind",
                    "122"),
        SportsEvent(now.isoformat(), "Soccer", "Copa Libertadores", "lib_56", "Flamengo vs River Plate", "FLA vs RIV",
                    (now - datetime.timedelta(minutes=20)).isoformat(), "in_progress", "maracana", "fla", "0", "riv",
                    "0"),
        # Scheduled
        SportsEvent(now.isoformat(), "Baseball", "NPB", "npb_88", "Yomiuri Giants vs Hanshin Tigers", "YGI vs HAN",
                    (now + datetime.timedelta(hours=2)).isoformat(), "scheduled", "tokyo_dome"),
        SportsEvent(now.isoformat(), "American Football", "NFL", "nfl_92", "Green Bay Packers vs Chicago Bears",
                    "GBP vs CHI", (now + datetime.timedelta(hours=4)).isoformat(), "scheduled", "lambeau"),
    ]


# ---------------- Main Director Logic ----------------
def run_sports_broadcast_5min():
    """Creates a 5-minute, 3-act sports broadcast script."""
    controller = FrontendController()
    venues = get_sports_venues()
    events = get_sports_events()

    # === TIMING & PACING (TARGET: 300 SECONDS) ===
    PAN_TO_VENUE_MS = 6000
    HOLD_PER_EVENT_S = 18.0
    ACT_TRANSITION_S = 10.0

    # 1. INITIALIZE
    controller.log_message("Starting Global Sports Broadcast (5-Minute Segment)...") \
        .set_state("chyronText", "GLOBAL SPORTS WRAP") \
        .initialize_layer('html', {
        'htmlElement': "d => { const el = document.createElement('div'); el.className='globe-marker'; el.innerHTML=d.html.trim(); return el; }"}) \
        .initialize_layer('labels', {'labelText': 'text', 'labelLat': 'lat', 'labelLng': 'lng', 'labelSize': 0.6,
                                     'labelColor': "() => 'rgba(255, 255, 255, 0.75)'"}) \
        .initialize_layer('rings', {'ringColor': "() => '#ff2d00'", 'ringMaxRadius': 4, 'ringPropagationSpeed': 1.5,
                                    'ringRepeatPeriod': 800})

    controller.pan_to_globe_location(20, 0, 5000, 3.5).set_sleep(5)

    # === ACT 1: FINAL SCORES ===
    controller.set_state("chyronText", "SPORTS: FINAL RESULTS").set_state("tickerText",
                                                                          "Reviewing results from games recently concluded...")
    controller.set_sleep(ACT_TRANSITION_S / 2)

    for event in [e for e in events if e.status == 'final']:
        venue = venues.get(event.venue_uid)
        if not venue: continue

        controller.pan_to_globe_location(venue['lat'], venue['lon'], PAN_TO_VENUE_MS, 1.8).set_sleep(
            PAN_TO_VENUE_MS / 1000)
        card_html = _sports_card_html(event)
        controller.add_elements('html',
                                [{'id': event.uid, 'lat': venue['lat'], 'lng': venue['lon'], 'html': card_html}])
        controller.add_elements('labels', [
            {'id': f"label_{event.uid}", 'lat': venue['lat'], 'lng': venue['lon'], 'text': venue['city']}])
        controller.set_state("tickerText", f"Final score in the {event.league}: {event.name}.")
        controller.set_sleep(HOLD_PER_EVENT_S)

    # === ACT 2: LIVE ACTION ===
    controller.set_state("chyronText", "SPORTS: LIVE ACTION").set_state("tickerText",
                                                                        "Checking in on games currently in-progress...")
    controller.pan_to_globe_location(10, -30, ACT_TRANSITION_S * 1000, 2.5).set_sleep(ACT_TRANSITION_S)

    for event in [e for e in events if e.status == 'in_progress']:
        venue = venues.get(event.venue_uid)
        if not venue: continue

        controller.pan_to_globe_location(venue['lat'], venue['lon'], PAN_TO_VENUE_MS, 1.8).set_sleep(
            PAN_TO_VENUE_MS / 1000)
        # Add a pulsing ring to signify LIVE action
        controller.add_elements('rings', [{'id': f"ring_{event.uid}", 'lat': venue['lat'], 'lng': venue['lon']}])
        card_html = _sports_card_html(event)
        controller.add_elements('html',
                                [{'id': event.uid, 'lat': venue['lat'], 'lng': venue['lon'], 'html': card_html}])
        controller.add_elements('labels', [
            {'id': f"label_{event.uid}", 'lat': venue['lat'], 'lng': venue['lon'], 'text': venue['city']}])
        controller.set_state("tickerText",
                             f"Live from {venue['city']}: {event.name}, score is {event.home_score}-{event.away_score}.")
        controller.set_sleep(HOLD_PER_EVENT_S)

    # === ACT 3: UP NEXT ===
    controller.set_state("chyronText", "SPORTS: UP NEXT").set_state("tickerText",
                                                                    "A look at major matchups scheduled to begin soon...")
    controller.pan_to_globe_location(40, -90, ACT_TRANSITION_S * 1000, 2.5).set_sleep(ACT_TRANSITION_S)

    for event in [e for e in events if e.status == 'scheduled']:
        venue = venues.get(event.venue_uid)
        if not venue: continue

        controller.pan_to_globe_location(venue['lat'], venue['lon'], PAN_TO_VENUE_MS, 1.8).set_sleep(
            PAN_TO_VENUE_MS / 1000)
        card_html = _sports_card_html(event)
        controller.add_elements('html',
                                [{'id': event.uid, 'lat': venue['lat'], 'lng': venue['lon'], 'html': card_html}])
        controller.add_elements('labels', [
            {'id': f"label_{event.uid}", 'lat': venue['lat'], 'lng': venue['lon'], 'text': venue['city']}])
        controller.set_state("tickerText", f"Coming up in the {event.league}: {event.name}.")
        controller.set_sleep(HOLD_PER_EVENT_S)

    # 4. FINALIZE
    controller.set_state("chyronText", "GLOBAL SPORTS WRAP").set_state("tickerText",
                                                                       "That's the latest from the world of sports.")
    controller.pan_to_globe_location(25, 0, 8000, 4.0).set_sleep(15)

    return controller.build()


# ---------------- Run locally (optional) ----------------
if __name__ == "__main__":
    print("Generating 5-Minute Global Sports Broadcast script...")
    script = run_sports_broadcast_5min()
    try:
        from machine.sdk.pds.lib.components.utils.connection_manager import ConnectionManager

        ConnectionManager().broadcast(script_content=script)
    except Exception as e:
        print(f"[warn] Broadcast not executed: {e}")

    print("\n--- GENERATED SCRIPT ---")
    print(script)
    print("\n--- SCRIPT END ---")