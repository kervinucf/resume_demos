# -*- coding: utf-8 -*-
"""
Director: Global News Activation (Focus Mode)

This version modifies the sequence to show only one story at a time.
As the camera moves to a new location, the visual elements (point, card, ring)
from the previous location are removed.
"""
import json
import datetime
from dataclasses import dataclass
from typing import Optional, List


# ---------------- DTO (Data Transfer Object) ----------------
@dataclass
class NewsEvent:
    """A dataclass to represent a single news event or location."""
    uid: str
    region: str
    title: str
    source: str
    latitude: str
    longitude: str
    published_utc: Optional[str] = None

    def __post_init__(self):
        """Generate a timestamp if one isn't provided."""
        if not self.published_utc:
            self.published_utc = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------- Frontend Controller ----------------
class FrontendController:
    """Builds a sequence of JavaScript commands for the frontend globe."""

    def __init__(self):
        self.commands = []

    def build(self) -> str:
        return "\n".join(self.commands)

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
        base_map = {'html': 'htmlElements', 'labels': 'labels', 'rings': 'rings', 'points': 'points'}
        base = base_map.get(layer_name, layer_name)
        method = f"{base}Data"
        src = self._arr(layer_name)
        self.commands.append(f"if (globe) {{ globe{''.join(chain)}.{method}({src}); }}")
        return self

    def _arr(self, layer_name: str) -> str:
        arr_map = {'html': 'htmlData', 'labels': 'labelsData', 'rings': 'ringsData', 'points': 'pointsData'}
        return f"state.{arr_map.get(layer_name, f'{layer_name}Data')}"

    def add_elements(self, layer_name: str, elements: list):
        a = self._arr(layer_name)
        self.commands.append(f"{a} = [...{a}, ...{json.dumps(elements)}];")
        return self

    def remove_elements(self, layer_name: str, ids: list, id_key: str = 'id'):
        a = self._arr(layer_name)
        idsj = json.dumps(ids)
        kj = json.dumps(id_key)
        self.commands.append(f"{{ const idsToRemove=new Set({idsj}); {a}={a}.filter(e=>!idsToRemove.has(e[{kj}])); }}")
        return self

    def pan_to_globe_location(self, lat, lng, duration_ms, altitude=1.4):
        pov = f"{{lat:{json.dumps(lat)},lng:{json.dumps(lng)},altitude:{json.dumps(altitude)}}}"
        self.commands.append(f"if (globe) {{ globe.pointOfView({pov}, {int(duration_ms)}); }}")
        return self


# ---------------- Helpers: Time, Strings, and HTML ----------------
def _time_ago_short(iso_utc: str) -> str:
    """Returns a short, human-readable 'time ago' string."""
    try:
        dt = datetime.datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
        delta = datetime.datetime.now(datetime.timezone.utc) - dt
        mins = int(delta.total_seconds() // 60)
        if mins < 1: return "now"
        if mins < 60: return f"{mins}m"
        hours, rem = divmod(mins, 60)
        return f"{hours}h {rem}m" if rem else f"{hours}h"
    except (ValueError, TypeError):
        return ""


def _trim(s: str, n: int) -> str:
    """Trims a string to n characters, adding an ellipsis if truncated."""
    return s if len(s) <= n else (s[:n - 1] + "…")


def _chip_html(title: str, source: str, ago: str) -> str:
    """Generates the HTML for a story info chip."""
    return f"""
<div style="color:#eef6ff; font: 600 12px Inter, system-ui, sans-serif; padding:10px 14px; border-radius:14px; max-width: 380px; backdrop-filter: blur(8px) saturate(140%); border:1px solid rgba(255,255,255,.12); background: linear-gradient(180deg, rgba(255,255,255,.06), rgba(255,255,255,.025));">
  <div style="display:flex; align-items:center; gap:10px; margin-bottom:8px; opacity:.9;">
    <div style="width:9px; height:9px; border-radius:50%; background:radial-gradient(circle at 30% 30%, #ffd166, #ff9f1a 60%, rgba(0,0,0,0) 70%);"></div>
    <div style="letter-spacing:.2px;">{source} • {ago}</div>
  </div>
  <div style="opacity:.98; font-weight:700; line-height:1.28; font-size:13px;">{title}</div>
</div>
""".strip()


# ---------------- Main Director Logic ----------------
def get_activation_sequence() -> List[NewsEvent]:
    """Provides the list of locations/stories for the sequence."""
    return [
        NewsEvent('iad', 'North America', 'Network Command Initiated', 'PDS Control', '38.9072', '-77.0369'),
        NewsEvent('lhr', 'Europe', 'Transatlantic Hub Online', 'London Station', '51.5074', '-0.1278'),
        NewsEvent('ank', 'Asia', 'Strategic Comms Relay Activated', 'Ankara Relay', '39.9334', '32.8597'),
        NewsEvent('del', 'Asia', 'South Asia Node Secured', 'New Delhi Hub', '28.6139', '77.2090'),
        NewsEvent('hnd', 'Asia', 'Pacific Economic Monitor Live', 'Tokyo Center', '35.6762', '139.6503'),
        NewsEvent('mex', 'North America', 'Americas Connection Finalized', 'Mexico City Link', '19.4326', '-99.1332')
    ]


def sample_news_graphics():
    """
    Simulates a global activation, showing one story at a time by cleaning up
    the previous story's elements before showing the next.
    """
    controller = FrontendController()
    locations = get_activation_sequence()
    prev_loc = None  # **NEW**: To keep track of the previous location

    # 1. INITIALIZE SCENE & LAYERS
    controller.log_message("Starting Global News Activation (Focus Mode)...") \
        .set_state("chyronText", "STANDBY: GLOBAL ACTIVATION") \
        .initialize_layer('points', {'pointColor': "() => '#7dfc00'", 'pointAltitude': 0.01, 'pointRadius': 0.35}) \
        .initialize_layer('rings', {'ringColor': "() => '#ff2d00'", 'ringMaxRadius': 4, 'ringPropagationSpeed': 2}) \
        .initialize_layer('html', {
        'htmlElement': "d => { const el=document.createElement('div'); el.innerHTML=d.html.trim(); return el.firstChild; }"})

    # Pre-populate the rundown list
    rundown_stories = [{
        "id": loc.uid,
        "title": _trim(loc.title, 64),
        "subtitle": f"{loc.source} • {loc.region}",
        "data": {"lat": float(loc.latitude), "lng": float(loc.longitude)}
    } for loc in locations]
    controller.set_state("rundownStories", rundown_stories)

    # Initial pan
    controller.pan_to_globe_location(20, -30, 2000, 3.0).set_sleep(2)

    # 2. ITERATE AND BUILD THE SCENE
    for loc in locations:
        lat, lng = float(loc.latitude), float(loc.longitude)

        # --- Pan camera and update Chyron ---
        controller.set_state("chyronText", f"CONNECTING: {loc.region.upper()}") \
            .pan_to_globe_location(lat, lng, 2500, 1.5)

        # --- After pan starts, update selection and ticker. ---
        controller.set_state("selectedStory", {"id": loc.uid, "data": {"lat": lat, "lng": lng}}) \
            .set_state("tickerText", loc.title) \
            .set_sleep(2.5)  # Wait for pan to finish

        # --- **NEW**: Remove the previous location's elements ---
        if prev_loc:
            controller.remove_elements('points', [prev_loc.uid])
            controller.remove_elements('html', [f"card_{prev_loc.uid}"])
            if prev_loc.uid in ['ank', 'hnd']:  # Also remove the ring if it was a critical node
                controller.remove_elements('rings', [f"ring_{prev_loc.uid}"])
            controller.set_sleep(0.5)  # Short pause for the removal animation

        # --- Add visual elements for the CURRENT location ---
        controller.add_elements('points', [{'id': loc.uid, 'lat': lat, 'lng': lng}])

        ago = _time_ago_short(loc.published_utc)
        card_html = _chip_html(loc.title, loc.source, ago)
        controller.add_elements('html', [{'id': f"card_{loc.uid}", 'lat': lat, 'lng': lng, 'html': card_html}]) \
            .set_sleep(1.5)

        if loc.uid in ['ank', 'hnd']:
            controller.set_state("tickerText", f"CRITICAL NODE SECURED: {loc.source}") \
                .add_elements('rings', [{'id': f"ring_{loc.uid}", 'lat': lat, 'lng': lng}]) \
                .set_sleep(2)

        # --- **NEW**: Update the tracker for the next iteration ---
        prev_loc = loc
        controller.set_sleep(3)  # Hold on the location to read the card

    # 3. FINALIZE THE SEQUENCE
    controller.set_state("selectedStory", None) \
        .set_state("chyronText", "GLOBAL NETWORK FULLY ACTIVATED") \
        .set_state("tickerText", "All nodes online. Monitoring global activity.") \
        .pan_to_globe_location(25, 80, 5000, 3.5) \
        .set_sleep(5)

    # **NEW**: Clean up the VERY LAST story's elements
    if prev_loc:
        controller.remove_elements('points', [prev_loc.uid])
        controller.remove_elements('html', [f"card_{prev_loc.uid}"])
        if prev_loc.uid in ['ank', 'hnd']:
            controller.remove_elements('rings', [f"ring_{prev_loc.uid}"])

    return controller.build()


# ---------------- Run locally (optional) ----------------
if __name__ == "__main__":
    print("Generating Global News Activation script (Focus Mode)...")
    script = run_global_news_activation()
    # Optional: Broadcast if SDK is available
    try:
        from machine.sdk.pds.lib.components.utils.connection_manager import ConnectionManager

        ConnectionManager().broadcast(script_content=script)
    except Exception as e:
        print(f"[warn] Broadcast not executed: {e}")

    print("\n--- GENERATED SCRIPT ---")
    print(script)
    print("\n--- SCRIPT END ---")