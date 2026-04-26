from typing import List, Dict, Tuple
from math import sqrt
import uuid
import numpy as np

from machine.m2.pds.lib.helpers.resolvers.dtos.events.earthquake_event import EarthquakeEvent
from machine.m2.pds.lib.helpers.utils import access_clock, random
from machine.m2.server.src._services.content_manager.segments._.earthquake.scenes.graphics.chyron import create_chyron_text
from machine.m2.server.src._services.content_manager.segments._.earthquake.scenes.graphics.ticker import create_ticker_text
from machine.m2.server.src._services.content_manager.segments._.earthquake.scenes.graphics.elements import (
    create_individual_marker,
    create_cluster_badge_marker,
    create_hex_element,
    create_ring_element,
)
from machine.m2.server.src._services.content_manager.segments._.utils.director import (
    Director,
    add_elements_action,
    remove_elements_action,
    refresh_layer_action,
    set_state_action,
    wait_action,
    log_action,
    pan_action,
)


def format_event_age(timestamp_utc: float) -> str:
    time_since = access_clock(time_since=timestamp_utc)
    if time_since is None:
        return "unknown"
    if time_since < 1:
        return f"{time_since * 60:.0f} min ago"
    return f"{int(time_since)} hr ago"


def random_altitude() -> float:
    return random.uniform(0.42, 1.9)


def geo_distance(ev1: EarthquakeEvent, ev2: EarthquakeEvent) -> float:
    return sqrt(
        (float(ev1.latitude) - float(ev2.latitude)) ** 2
        + (float(ev1.longitude) - float(ev2.longitude)) ** 2
    )


def cluster_events(events: List[EarthquakeEvent], threshold: float = 80.0) -> List[List[EarthquakeEvent]]:
    if not events:
        return []
    clusters, current = [], [events[0]]
    for ev in events[1:]:
        if geo_distance(ev, current[-1]) <= threshold:
            current.append(ev)
        else:
            clusters.append(current)
            current = [ev]
    clusters.append(current)
    return clusters


def compute_cluster_center(cluster: List[EarthquakeEvent]) -> Tuple[float, float]:
    lats = [float(e.latitude) for e in cluster]
    lngs = [float(e.longitude) for e in cluster]
    return np.mean(lats), np.mean(lngs)


def make_badge(cluster: List[EarthquakeEvent], lat: float, lng: float) -> Dict:
    return {
        "id": f"badge_{uuid.uuid4()}",
        "lat": lat,
        "lng": lng,
        "html": create_cluster_badge_marker(cluster),
    }


def make_marker(ev: EarthquakeEvent) -> Dict:
    return {
        "id": ev.uid,
        "lat": float(ev.latitude),
        "lng": float(ev.longitude),
        "html": create_individual_marker(ev),
    }

def make_event_elements(ev: EarthquakeEvent) -> Dict[str, Dict]:
    """Create all display elements for a single earthquake event."""
    return { "marker": { "id": ev.uid, "lat": float(ev.latitude), "lng": float(ev.longitude), "html": create_individual_marker(ev), }, "hex": create_hex_element(ev), "ring": create_ring_element(ev), }


def show_cluster_badge(cluster, center_lat, center_lng) -> List[Tuple]:
    badge = make_badge(cluster, center_lat, center_lng)
    hexes = [create_hex_element(e) for e in cluster]
    rings = [create_ring_element(e) for e in cluster]

    return [
        (add_elements_action, dict(layer_name="html", elements=[badge])),
        (add_elements_action, dict(layer_name="hex", elements=hexes)),
        (add_elements_action, dict(layer_name="rings", elements=rings)),
        (refresh_layer_action, dict(layer_name="html")),
        (refresh_layer_action, dict(layer_name="hex")),
        (refresh_layer_action, dict(layer_name="rings")),
        (wait_action, dict(duration=4.0)),
        (remove_elements_action, dict(layer_name="html", ids=[badge["id"]])),
        (remove_elements_action, dict(layer_name="hex", ids=[h["id"] for h in hexes])),
        (remove_elements_action, dict(layer_name="rings", ids=[r["id"] for r in rings])),
    ]


def show_individual_markers(cluster: List[EarthquakeEvent]) -> List[Tuple]:
    actions = []
    for ev in cluster:
        marker = make_marker(ev)
        actions.extend([
            (add_elements_action, dict(layer_name="html", elements=[marker])),
            (add_elements_action, dict(layer_name="hex", elements=[create_hex_element(ev)])),
            (add_elements_action, dict(layer_name="rings", elements=[create_ring_element(ev)])),
            (wait_action, dict(duration=0.5)),
        ])
    actions.extend([
        (refresh_layer_action, dict(layer_name="html")),
        (refresh_layer_action, dict(layer_name="hex")),
        (refresh_layer_action, dict(layer_name="rings")),
        (wait_action, dict(duration=3.5)),
    ])
    for ev in cluster:
        actions.extend([
            (remove_elements_action, dict(layer_name="html", ids=[ev.uid])),
            (remove_elements_action, dict(layer_name="hex", ids=[f"hex_{ev.uid}"])),
            (remove_elements_action, dict(layer_name="rings", ids=[f"ring_{ev.uid}"])),
            (wait_action, dict(duration=0.25)),
        ])
    return actions
