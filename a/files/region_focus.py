# -*- coding: utf-8 -*-
"""
Earthquake Region Focus Visualization — Fixed & Readable
---------------------------------------------------------
Now fully functional with the correct Director.add() calls.
"""

from math import sqrt
from typing import Dict, List, Tuple
import numpy as np
import uuid

# --- External Imports ---
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

# =====================================================
# 1. UTILS — Core logic
# =====================================================

def geo_distance(ev1: EarthquakeEvent, ev2: EarthquakeEvent) -> float:
    """Compute approximate spatial distance between two events."""
    return sqrt(
        (float(ev1.latitude) - float(ev2.latitude)) ** 2
        + (float(ev1.longitude) - float(ev2.longitude)) ** 2
    )

def cluster_events(events: List[EarthquakeEvent], threshold: float = 80.0) -> List[List[EarthquakeEvent]]:
    """Group nearby earthquake events into spatial clusters."""
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
    """Return (mean_lat, mean_lng) for a cluster of events."""
    lats = [float(e.latitude) for e in cluster]
    lngs = [float(e.longitude) for e in cluster]
    return np.mean(lats), np.mean(lngs)


# =====================================================
# 2. ELEMENTS — Visual components
# =====================================================

def make_badge(cluster: List[EarthquakeEvent], lat: float, lng: float) -> Dict:
    """Create a summary badge for a large cluster."""
    return {
        "id": f"badge_{uuid.uuid4()}",
        "lat": lat,
        "lng": lng,
        "html": create_cluster_badge_marker(cluster),
    }

def make_marker(ev: EarthquakeEvent) -> Dict:
    """Create an individual event marker."""
    return {
        "id": ev.uid,
        "lat": float(ev.latitude),
        "lng": float(ev.longitude),
        "html": create_individual_marker(ev),
    }


# =====================================================
# 3. SCENES — Reusable action fragments
# =====================================================


def show_cluster_badge(cluster, center_lat, center_lng) -> List[Tuple]:
    """Return a list of (action, kwargs) for a large cluster scene."""
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
    """Return a list of (action, kwargs) for small cluster visualization."""
    actions = []
    for ev in cluster:
        marker = make_marker(ev)
        actions.extend([
            (add_elements_action, dict(layer_name="html", elements=[marker])),
            (add_elements_action, dict(layer_name="hex", elements=[create_hex_element(ev)])),
            (add_elements_action, dict(layer_name="rings", elements=[create_ring_element(ev)])),
            (wait_action, dict(duration=0.5)),
        ])
    # Refresh & pause
    actions.extend([
        (refresh_layer_action, dict(layer_name="html")),
        (refresh_layer_action, dict(layer_name="hex")),
        (refresh_layer_action, dict(layer_name="rings")),
        (wait_action, dict(duration=3.5)),
    ])
    # Remove elements
    for ev in cluster:
        actions.extend([
            (remove_elements_action, dict(layer_name="html", ids=[ev.uid])),
            (remove_elements_action, dict(layer_name="hex", ids=[f"hex_{ev.uid}"])),
            (remove_elements_action, dict(layer_name="rings", ids=[f"ring_{ev.uid}"])),
            (wait_action, dict(duration=0.25)),
        ])
    return actions


# =====================================================
# 4. STORY — High-level orchestration
# =====================================================

def region_focus(director: Director, registered_events: List[EarthquakeEvent]):
    """Narrative: camera moves through earthquake clusters region by region."""
    if not registered_events:
        return

    ticker_text = create_ticker_text(registered_events)
    director.add(set_state_action, key="tickerText", value=ticker_text)

    for cluster in cluster_events(registered_events):
        if not cluster:
            continue

        # Context setup
        director.add(
            set_state_action,
            key="chyronText",
            value=create_chyron_text(cluster, access_clock)
            )

        # Correct call signature
        center_lat, center_lng = compute_cluster_center(cluster)
        director.add(
            pan_action, 
            lat=center_lat,
            lng=center_lng,
            duration=3500,
            altitude=random.uniform(0.42, 1.1),
        )

        # Scene branching
        for action, kwargs in (
            show_cluster_badge(cluster, center_lat, center_lng)
            if len(cluster) >= 3
            else show_individual_markers(cluster)
        ):
            director.add(action, **kwargs)

        for action, kwargs in [
            (refresh_layer_action, dict(layer_name="html")),
            (refresh_layer_action, dict(layer_name="hex")),
            (refresh_layer_action, dict(layer_name="rings")),
            (wait_action, dict(duration=1.0)),
        ]:
            director.add(action, **kwargs)
