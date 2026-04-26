# -*- coding: utf-8 -*-
"""
Earthquake Visualization Narrative — semantic, readable flow.
"""

from datetime import timezone
from typing import Any, Dict, List

from machine.m2.pds.lib.helpers.utils import access_clock, random
from machine.m2.pds.lib.functions.resolvers.event_resolver import retrieve_from_event_db
from machine.m2.pds.lib.helpers.resolvers.dtos.events.earthquake_event import EarthquakeEvent

from machine.m2.server.src._services.content_manager.segments._.earthquake.scenes.setup import setup_scene
from machine.m2.server.src._services.content_manager.segments._.earthquake.scenes.main import (
    format_event_age,
    make_event_elements,
    random_altitude,
    create_ticker_text,
    compute_cluster_center,
    create_chyron_text,
    cluster_events,
    make_badge,
    make_marker,
    create_hex_element,
    create_ring_element,
)
from machine.m2.server.src._services.content_manager.segments._.earthquake.scenes.teardown import teardown_scene

from machine.m2.server.src._services.content_manager.segments._.utils.director import (
    Director,
    set_state_action,
    pan_action,
    wait_action,
    add_elements_action,
    remove_elements_action,
    refresh_layer_action,
)


def create_content_storyline(earthquakes: List[EarthquakeEvent]) -> Dict[str, Any]:
    events: Dict[str, EarthquakeEvent] = {}
    significant: List[str] = []
    recent: List[str] = []
    chronological: List[str] = []

    earthquakes_sorted = sorted(earthquakes, key=lambda e: e.timestamp_utc)

    for event in earthquakes_sorted:
        if event.timestamp_utc.tzinfo is None:
            event.timestamp_utc = event.timestamp_utc.replace(tzinfo=timezone.utc)

        hours_since = access_clock(time_since=event.timestamp_utc)
        if hours_since is None or hours_since > 24:
            continue

        events[event.uid] = event
        chronological.append(event.uid)
        if hours_since <= 1:
            recent.append(event.uid)
        if float(event.magnitude) > 7.4:
            significant.append(event.uid)

    return {
        "registered_events": events,
        "content": {
            "significant": significant,
            "recent": recent,
            "chronological": chronological,
        },
    }


def render_elements(director, elements_dict):
    """Add visual elements across all layers and refresh."""
    for layer, items in elements_dict.items():
        if items:
            director.add(add_elements_action, layer_name=layer, elements=items if isinstance(items, list) else [items])
    
    for layer in elements_dict.keys():
        director.add(refresh_layer_action, layer_name=layer)


def clear_elements(director, elements_dict):
    """Remove visual elements by their IDs."""
    for layer, ids in elements_dict.items():
        if ids:
            director.add(remove_elements_action, layer_name=layer, ids=ids if isinstance(ids, list) else [ids])


def set_scene_context(director, chyron_text, location, duration, altitude):
    """Set up the scene context with chyron and camera position."""
    director.add(set_state_action, key="chyronText", value=chyron_text)
    director.add(
        pan_action,
        lat=location[0],
        lng=location[1],
        duration=duration,
        altitude=altitude,
    )
    if duration > 10:
        duration = duration % 1000
    director.add(wait_action, duration=duration)

def play_event_frame(director: Director, events):
    if not events:
        return

    # Single event: show one earthquake with detailed view
    if len(events) == 1:
        ev = events[0]
        elements = make_event_elements(ev)
        
        set_scene_context(
            director,
            chyron_text=f"{ev.location} {format_event_age(ev.timestamp_utc)}",
            location=(float(ev.latitude), float(ev.longitude)),
            duration=3000,
            altitude=random.uniform(0.42, 1.1)
        )
        
        render_elements(director, {
            "html": elements["marker"],
            "hex": elements["hex"],
            "rings": elements["ring"],
        })
        
        director.add(wait_action, duration=7.5)
        
        clear_elements(director, {
            "html": [elements["marker"]["id"]],
            "hex": [elements["hex"]["id"]],
            "rings": [elements["ring"]["id"]],
        })
        
        director.add(refresh_layer_action, layer_name="hex")
        director.add(refresh_layer_action, layer_name="rings")
        director.add(wait_action, duration=1.0)

    # Multiple events: show as cluster or individual markers
    else:
        center_lat, center_lng = compute_cluster_center(events)
        
        set_scene_context(
            director,
            chyron_text=create_chyron_text(events, access_clock),
            duration=3000,
            location=(center_lat, center_lng),
            altitude=random.uniform(0.42, 1.1)
        )

        # Large cluster: show unified badge
        if len(events) >= 3:
            badge = make_badge(events, center_lat, center_lng)
            hexes = [create_hex_element(e) for e in events]
            rings = [create_ring_element(e) for e in events]
            
            render_elements(director, {"html": badge, "hex": hexes, "rings": rings})
            director.add(wait_action, duration=4.0)
            clear_elements(director, {
                "html": [badge["id"]],
                "hex": [h["id"] for h in hexes],
                "rings": [r["id"] for r in rings],
            })

        # Small cluster: show individual markers sequentially
        else:
            for ev in events:
                render_elements(director, {
                    "html": make_marker(ev),
                    "hex": create_hex_element(ev),
                    "rings": create_ring_element(ev),
                })
                director.add(wait_action, duration=0.5)
            
            director.add(refresh_layer_action, layer_name="html")
            director.add(refresh_layer_action, layer_name="hex")
            director.add(refresh_layer_action, layer_name="rings")
            director.add(wait_action, duration=3.5)
            
            for ev in events:
                clear_elements(director, {
                    "html": [ev.uid],
                    "hex": [f"hex_{ev.uid}"],
                    "rings": [f"ring_{ev.uid}"],
                })
                director.add(wait_action, duration=0.25)

        # Final pause before next events
        director.add(refresh_layer_action, layer_name="html")
        director.add(refresh_layer_action, layer_name="hex")
        director.add(refresh_layer_action, layer_name="rings")
        director.add(wait_action, duration=1.0)


def get_earthquake_graphics(ds, controller, event_showing=None):
    earthquake_data: List[EarthquakeEvent]  = retrieve_from_event_db(
        data_adapter=ds(event=True),
        object_earthquake=True,
        many=True,
        uid=event_showing or [],
    )

    if not earthquake_data:
        return controller.log_message("No seismic data available.").build()

    director = Director(controller.clear())
    setup_scene(director)

    storyline = create_content_storyline(earthquakes=earthquake_data)
    segment_map = {
        "significant": storyline["content"]["significant"],
        "recent": storyline["content"]["recent"],
        "chronological": storyline["content"]["chronological"],
    }

    for _, event_ids in segment_map.items():
        if not event_ids:
            continue

        segment_events = [storyline["registered_events"][uid] for uid in event_ids]
        director.add(set_state_action, key="tickerText", value=create_ticker_text(segment_events))

        [
            play_event_frame(director, events=earthquake_events)
            for earthquake_events in (
                [[ev] for ev in segment_events]
                if len(segment_events) < 5
                else list(cluster_events(segment_events))
            )
        ]

    teardown_scene(director)
    return director.build()
