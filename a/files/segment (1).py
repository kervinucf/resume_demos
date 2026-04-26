from collections import defaultdict
from typing import Dict, List, Tuple, Any, Callable
import random
import itertools

from machine.m2.pds.lib.functions.resolvers.event_resolver import retrieve_from_event_db
from machine.m2.pds.lib.functions.resolvers.kb_resolver import retrieve_from_kb_db
from machine.m2.pds.lib.helpers.resolvers.dtos.events.news_event import NewsEvent

from machine.m2.server.src._services.content_manager.segments._.news.scenes.main import (
    ASSET_NAME_TO_EXCHANGE,
    EXCHANGE_LOCATIONS,
    make_marker_element,
    create_hex_element,
    create_ring_element
)
from machine.m2.server.src._services.content_manager.segments._.news.scenes.setup import setup_scene
from machine.m2.server.src._services.content_manager.segments._.news.scenes.teardown import teardown_scene
from machine.m2.server.src._services.content_manager.segments._.news.scenes.graphics.dashboards import \
    generate_news_broadcast_elements
from machine.m2.server.src._services.content_manager.segments._.news.scenes.graphics.chyron import \
    create_news_chyron
from machine.m2.server.src._services.content_manager.segments._.news.scenes.graphics.ticker import \
    create_news_ticker
from machine.m2.server.src._services.content_manager.segments._.utils.director import (
    Director,
    add_elements_action,
    pan_action,
    refresh_layer_action,
    remove_elements_action,
    set_state_action,
    wait_action,
    define_dynamic_html_action,
    set_layout_action,
    set_assignments_action
)

# Type alias for our declarative actions: (Function, kwargs)
ActionDef = Tuple[Callable, Dict[str, Any]]


# --- Pure / Utility Functions ---

def _trim(s: str, n: int) -> str:
    """Trims a string to n characters, adding an ellipsis if truncated."""
    return s if len(s) <= n else (s[:n - 1] + "…")


def get_unique_valid_events(events: List[NewsEvent]) -> List[NewsEvent]:
    """Filters events to unique titles and ensures they have valid coordinates."""
    unique_events = list({e.title: e for e in events}.values())
    return [e for e in unique_events if e.latitude and e.longitude]


def build_rundown_stories(events: List[NewsEvent]) -> List[Dict[str, str]]:
    """Maps events to their rundown story dictionaries."""
    return [
        {"id": e.uid, "title": _trim(e.title, 64), "subtitle": f"{e.source} • {e.region}"}
        for e in events
    ]


# --- Action Generators (Declarative Pipeline) ---

def determine_layout_actions(event: NewsEvent, graphics: Dict) -> List[ActionDef]:
    """Returns a declarative list of actions based on the event's data profile."""
    if len(event.summary) > 100:
        return [
            (set_layout_action, {"layout_mode": "TRIPLE_LEFT_HEAVY"}),
            (set_assignments_action, {"assignments": {
                "box1": {"type": "DynamicHTML", "resourceId": "summary_panel"},
                "box2": {"type": "Earth", "resourceId": "earth1"},
                "box3": {"type": "DynamicHTML", "resourceId": "resource_panel"},
            }}),
            (define_dynamic_html_action,
             {"resource_id": "summary_panel", "html_content": graphics['summary_graphic_qr']}),
            (define_dynamic_html_action, {"resource_id": "resource_panel", "html_content": graphics['image_graphic']}),
            (wait_action, {"duration": 5})
        ]

    has_valid_image = event.image and len(event.image) >= 5
    if has_valid_image:
        return [
            (set_layout_action, {"layout_mode": random.choice(["SPLIT_VERTICAL", "PIP_BOTTOM_RIGHT"])}),
            (set_assignments_action, {"assignments": {
                "box1": {"type": "DynamicHTML", "resourceId": "resource_panel"},
                "box2": {"type": "Earth", "resourceId": "earth1"},
            }}),
            (
            define_dynamic_html_action, {"resource_id": "resource_panel", "html_content": graphics['image_graphic_qr']})
        ]

    return [
        (set_layout_action, {"layout_mode": 'SINGLE'}),
        (set_assignments_action, {"assignments": {
            "box1": {"type": "Earth", "resourceId": "earth1"},
        }})
    ]


def build_event_actions(event: NewsEvent, idx: int, stoppage: float, minutes_left: int) -> List[ActionDef]:
    """Maps a single event into a sequence of Director actions."""
    lat, lng = float(event.latitude), float(event.longitude)
    graphics = generate_news_broadcast_elements(event, dark_mode=True)
    chyron_text = list(create_news_chyron([event]).values())[-1]

    # Compose the sequence of actions
    actions = [
        (set_state_action, {"key": "selectedStory", "value": {"id": event.uid, "data": {"lat": lat, "lng": lng}}})
    ]

    actions.extend(determine_layout_actions(event, graphics))

    actions.extend([
        (set_state_action, {"key": "chyronText", "value": chyron_text}),
        (pan_action,
         {"lat": lat, "lng": lng, "duration": random.randint(2000, 4200), "altitude": random.uniform(0.32, .66)}),
        (wait_action, {"duration": 5}),

        (add_elements_action, {"layer_name": 'points', "elements": [{'id': event.uid, 'lat': lat, 'lng': lng}]}),
        (add_elements_action, {"layer_name": 'html', "elements": [
            {'id': f"card_{event.uid}", 'lat': lat, 'lng': lng, 'html': make_marker_element(event, idx + 1)}]}),

        (refresh_layer_action, {"layer_name": 'points'}),
        (refresh_layer_action, {"layer_name": 'html'}),

        (wait_action, {"duration": 4}),
        (wait_action, {"duration": stoppage * minutes_left - 15})
    ])

    return actions


# --- Side-Effect Applicators ---

def apply_actions(director: Director, actions: List[ActionDef]) -> None:
    """Takes a declarative list of actions and mutates the director."""
    for action_func, kwargs in actions:
        director.add(action_func, **kwargs)


def render_elements(director, elements_dict):
    """Add visual elements across all layers and refresh."""
    for layer, items in elements_dict.items():
        if items:
            elements = items if isinstance(items, list) else [items]
            director.add(add_elements_action, layer_name=layer, elements=elements)
            director.add(refresh_layer_action, layer_name=layer)


def clear_elements(director, elements_dict):
    """Remove visual elements by their IDs."""
    for layer, ids in elements_dict.items():
        if ids:
            director.add(remove_elements_action, layer_name=layer, ids=ids if isinstance(ids, list) else [ids])


# --- Main Orchestrator ---

def get_news_graphics(ds, controller, event_showing=None, minutes_left=300):
    """Generates the finance graphics script, showing each asset individually."""

    # 1. Fetch Data
    raw_news_events = retrieve_from_event_db(
        data_adapter=ds(event=True), object_news=True, many=True, uid=event_showing
    )

    # 2. Process Data (Pure Pipeline)
    valid_events = get_unique_valid_events(raw_news_events)

    if not valid_events:
        return Director(controller.clear()).build()

    rundown_stories = build_rundown_stories(valid_events)
    stoppage = 1 / len(valid_events)

    # Declaratively map all events into a flat list of actions
    all_story_actions = list(itertools.chain.from_iterable(
        build_event_actions(event, idx, stoppage, minutes_left)
        for idx, event in enumerate(valid_events)
    ))

    # 3. Apply Side Effects (Director Mutation)
    director = Director(controller.clear())

    setup_scene(director, rundown_stories)
    director.add(set_state_action, key="hideSideBar", value=False)
    director.add(set_state_action, key="tickerText", value=create_news_ticker(events=valid_events))

    # Hydrate the director with our generated action sequence
    apply_actions(director, all_story_actions)

    last_uid = valid_events[-1].uid
    teardown_scene(director, last_uid)
    director.add(set_state_action, key="hideSideBar", value=True)

    return director.build()