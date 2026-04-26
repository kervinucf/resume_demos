import random
import re
from typing import List
import collections
import statistics
from machine.m2.server.src._services.content_manager.segments._.earthquake.scenes.graphics.utils import (
    get_region_by_coords
)

# --- [I] DYNAMIC PHRASING POOLS ---
# Pools of interchangeable words and phrases to eliminate repetition.

HEADLINES_GLOBAL = ["SEISMIC SUMMARY", "GLOBAL ACTIVITY REPORT", "PLANETARY SEISMIC OVERVIEW"]
HEADLINES_STRONGEST = ["HEADLINE EVENT", "MOST POWERFUL QUAKE", "TOP SEISMIC EVENT"]
HEADLINES_HOTSPOT = ["ACTIVITY HOT SPOT", "REGIONAL FOCUS", "SEISMIC HOTBED"]

VERBS_LOGGED = ["logged", "cataloged", "recorded", "observed"]
VERBS_OCCURRED = ["was recorded", "was detected", "struck", "occurred", "rumbled near"]
VERBS_ACTIVE = ["was most active", "saw the most activity", "experienced the highest concentration of tremors"]

NOUNS_QUAKE = ["event", "quake", "tremor", "rumbling", "seismic event"]

# --- [II] UTILITY & FORMATTING HELPERS ---

_UNITS = {"km": "kilometers", "mi": "miles"}
_DIRECTIONS = {
    "N": "north", "S": "south", "E": "east", "W": "west",
    "NE": "northeast", "NW": "northwest",
    "SE": "southeast", "SW": "southwest",
}

def humanize_location(raw: str) -> str:
    """Makes USGS-style location strings more natural."""
    if not raw or " of " not in raw:
        return raw
    parts = raw.split(" of ", 1)
    if len(parts) != 2:
        return raw
    distance_part, place_part = parts
    match = re.match(r"(\d+\.?\d*)\s*([a-zA-Z]+)\s*([A-Z]{1,2})", distance_part.strip())
    if not match:
        return raw
    distance, unit_abbr, direction_abbr = match.groups()
    unit_full = _UNITS.get(unit_abbr, unit_abbr)
    direction_full = _DIRECTIONS.get(direction_abbr, direction_abbr)
    return f"{distance} {unit_full} {direction_full} of {place_part}"

def _get_magnitude_descriptor(mag: float) -> str:
    """Returns a descriptive phrase for a given magnitude."""
    if mag >= 7.0: return f"a major M{mag:.1f}"
    if mag >= 6.0: return f"a strong M{mag:.1f}"
    if mag >= 5.0: return f"a moderate M{mag:.1f}"
    if mag >= 4.0: return f"a light M{mag:.1f}"
    return f"a minor M{mag:.1f}"



# --- [III] DYNAMIC STORY BLOCKS (NOW WITH RANDOMIZED PHRASING) ---

def _block_global_summary(events: list) -> str:
    return (f"{random.choice(HEADLINES_GLOBAL)}: {len(events)} total {random.choice(NOUNS_QUAKE)}s "
            f"{random.choice(VERBS_LOGGED)}.")

def _block_strongest_event(summary: dict) -> str:
    event = summary.get("global_max_event", {})
    mag_desc = _get_magnitude_descriptor(summary.get("global_max_magnitude", 0))
    location = humanize_location(event.get("location", "an unknown location"))
    return f"{random.choice(HEADLINES_STRONGEST)}: {mag_desc} {random.choice(NOUNS_QUAKE)} {random.choice(VERBS_OCCURRED)} {location}."

def _block_regional_hotspot(summary: dict) -> str:
    region_name = summary.get("most_active_region")
    count = summary.get("region_event_count")
    if not region_name or count <= 1:
        return None
    return f"{random.choice(HEADLINES_HOTSPOT)}: The {region_name} region {random.choice(VERBS_ACTIVE)} with {count} tremors."

def _block_depth_focus(summary: dict) -> str:
    depth = summary.get("global_max_event", {}).get("depth_km", 0)
    if not depth or depth <= 0:
        return None
    depth_desc = "a very shallow" if depth < 30 else "a relatively deep"
    return f"The main event was {depth_desc} {random.choice(NOUNS_QUAKE)}, originating {depth:.0f} km below the surface."

def _block_significance_count(summary: dict) -> str:
    count = summary.get("interesting", {}).get("global_significant_quakes", 0)
    if count == 0:
        return "No major quakes (M6.0+) were detected in this period."
    plural = 's' if count > 1 else ''
    return f"CLASSIFICATION: {count} {random.choice(NOUNS_QUAKE)}{plural} registered as significant (M6.0+)."

def _block_magnitude_comparison(summary: dict) -> str:
    max_mag = summary.get("global_max_magnitude", 0)
    avg_mag = summary.get("global_average_magnitude", 0)
    if not max_mag or not avg_mag or max_mag - avg_mag < 1.0:
        return None
    intensity_multiplier = 10**(max_mag - avg_mag)
    return (f"The headline event registered over {intensity_multiplier:.0f} times the shaking intensity "
            f"of the daily average magnitude of M{avg_mag:.1f}.")

def _block_other_noteworthy_quakes(events: list, summary: dict) -> str:
    strongest_uid = summary.get("global_max_event", {}).get("uid")
    if not strongest_uid:
        return None
    events_sorted = sorted(events, key=lambda e: float(e.magnitude), reverse=True)
    other_quakes_text = []
    for event in events_sorted:
        if event.uid == strongest_uid:
            continue
        if len(other_quakes_text) < 2 and float(event.magnitude) > 3.0:
            desc = (f"a M{float(event.magnitude):.1f} {random.choice(NOUNS_QUAKE)} near "
                    f"{humanize_location(event.location)}")
            other_quakes_text.append(desc)
    if not other_quakes_text:
        return None
    return "OTHER NOTEWORTHY ACTIVITY: " + " and ".join(other_quakes_text) + "."

def _block_mild_tremors(summary: dict) -> str:
    count = summary.get("interesting", {}).get("mild_quakes_below_3", 0)
    if count < 5:
        return None
    return f"Alongside these events, {count} minor tremors below M3.0 were also cataloged."

# --- [IV] NARRATIVE RECIPES (UNCHANGED) ---

def _recipe_standard_report(summary: dict, events: list) -> str:
    """Standard News: Global -> Strongest -> Comparison -> Regional -> Other Quakes -> Significance."""
    blocks = [
        _block_global_summary(events),
        _block_strongest_event(summary),
        _block_magnitude_comparison(summary),
        _block_regional_hotspot(summary),
        _block_other_noteworthy_quakes(events, summary),
        _block_significance_count(summary),
        _block_mild_tremors(summary)
    ]
    return " ••• ".join(filter(None, blocks))

def _recipe_lead_with_the_big_one(summary: dict, events: list) -> str:
    """Lead Story: Strongest -> Depth -> Comparison -> Other Quakes -> Global -> Regional."""
    blocks = [
        _block_strongest_event(summary),
        _block_depth_focus(summary),
        _block_magnitude_comparison(summary),
        _block_other_noteworthy_quakes(events, summary),
        _block_global_summary(events),
        _block_regional_hotspot(summary),
        _block_mild_tremors(summary)
    ]
    return " ••• ".join(filter(None, blocks))

def summarize_earthquake_activity(events: List["EarthquakeEvent"]) -> dict:
    """Summarizes statistics for a list of earthquake events."""
    if not events: return {"error": "No earthquake data provided."}
    
    magnitudes = [float(ev.magnitude) for ev in events]
    max_event = max(events, key=lambda ev: float(ev.magnitude))
    
    region_counts = collections.defaultdict(list)
    for ev in events:
        region = get_region_by_coords(float(ev.latitude), float(ev.longitude))
        region_counts[region].append(ev)
    
    region_name, region_events = max(region_counts.items(), key=lambda kv: len(kv[1]))
    region_mags = [float(ev.magnitude) for ev in region_events]
    
    return {
        "global_average_magnitude": round(statistics.mean(magnitudes), 2),
        "global_max_magnitude": round(max(magnitudes), 2),
        "global_max_event": {
            "uid": max_event.uid, "location": max_event.location,
            "region": get_region_by_coords(max_event.latitude, max_event.longitude)
        },
        "most_active_region": region_name,
        "region_event_count": len(region_events),
        "region_average_magnitude": round(statistics.mean(region_mags), 2),
        "region_max_magnitude": round(max(region_mags), 2),
        "total_events": len(events)
    }
    
# --- [V] MAIN ORCHESTRATOR (UNCHANGED) ---

def create_ticker_text(events: List["EarthquakeEvent"]) -> str:
    summary = summarize_earthquake_activity(events)
    """
    Randomly selects a narrative recipe to build a varied, long-form ticker.
    """
    if not events:
        return "No seismic activity to report in the current window."

    recipes = [
        _recipe_standard_report,
        _recipe_lead_with_the_big_one,
    ]

    chosen_recipe = random.choice(recipes)
    return chosen_recipe(summary, events)