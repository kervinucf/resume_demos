from collections import defaultdict
from typing import Dict, List, Tuple, Any, Callable
import random
import itertools

from machine.m2.pds.lib.functions.resolvers.event_resolver import retrieve_from_event_db
from machine.m2.pds.lib.functions.resolvers.kb_resolver import retrieve_from_kb_db
from machine.m2.pds.lib.helpers.resolvers.dtos.events.sports_event import SportsEvent

from machine.m2.server.src._services.content_manager.segments._.sports.scenes.main import (
    ASSET_NAME_TO_EXCHANGE,
    EXCHANGE_LOCATIONS,
    make_currency_location_elements,
    create_hex_element,
    create_ring_element
)
from machine.m2.server.src._services.content_manager.segments._.sports.scenes.setup import setup_scene
from machine.m2.server.src._services.content_manager.segments._.sports.scenes.teardown import teardown_scene
from machine.m2.server.src._services.content_manager.segments._.sports.scenes.graphics.dashboards import \
    generate_sports_dashboards
from machine.m2.server.src._services.content_manager.segments._.sports.scenes.graphics.chyron import \
    create_sports_chyron
from machine.m2.server.src._services.content_manager.segments._.sports.scenes.graphics.ticker import \
    create_sports_ticker
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

# Type alias for declarative actions
ActionDef = Tuple[Callable, Dict[str, Any]]

# --- Constants ---

LEAGUE_METADATA = {
    'in_progress': {'name': 'In-Progress Games', 'logo': '🔴'},
    'completed': {'name': 'Completed Games', 'logo': '🏁'},
    'scheduled': {'name': 'Scheduled Games', 'logo': '🗓️'},
    'nhl': {'name': 'National Hockey League', 'logo': '🏒'},
    'nba': {'name': 'National Basketball Association', 'logo': '🏀'},
    'mlb': {'name': 'Major League Baseball', 'logo': '⚾'},
    'nfl': {'name': 'National Football League', 'logo': '🏈'},
    'college-football': {'name': 'NCAAM Football', 'logo': '🏈'},
    'mens-college-basketball': {'name': 'NCAAM Basketball', 'logo': '🏀'},
    'usa.1': {'name': 'Major League Soccer', 'logo': '⚽'},
    'mex.1': {'name': 'Liga MX', 'logo': '⚽'},
    'eng.1': {'name': 'Premier League', 'logo': '⚽'},
    'esp.1': {'name': 'La Liga', 'logo': '⚽'},
    'ita.1': {'name': 'Serie A', 'logo': '⚽'},
    'ger.1': {'name': 'Bundesliga', 'logo': '⚽'},
    'fra.1': {'name': 'Ligue 1', 'logo': '⚽'},
    'uefa.champions': {'name': 'UEFA Champions League', 'logo': '⚽'},
    'uefa.europa': {'name': 'UEFA Europa League', 'logo': '⚽'},
    'ksa.1': {'name': 'Saudi Pro League', 'logo': '⚽'},
    'jpn.1': {'name': 'J1 League', 'logo': '⚽'},
    'aus.1': {'name': 'A-League', 'logo': '⚽'},
    'usa.nwsl': {'name': 'NWSL', 'logo': '⚽'},
    'por.1': {'name': 'Primeira Liga', 'logo': '⚽'},
    'ned.1': {'name': 'Eredivisie', 'logo': '⚽'},
    'default': {'name': 'Sports Center', 'logo': '🏅'}
}


# --- Pure / Utility Functions ---

def _trim(s: str, n: int) -> str:
    """Trims a string to n characters, adding an ellipsis if truncated."""
    return s if len(s) <= n else (s[:n - 1] + "…")


def build_league_map(events: List[SportsEvent]) -> Dict[str, List[SportsEvent]]:
    """Maps sports events to their respective leagues and statuses purely."""
    league_map = defaultdict(list)
    for event in events:
        if 'final' in event.status or 'full_time' in event.status:
            league_map['completed'].append(event)
        elif 'scheduled' in event.status:
            league_map['scheduled'].append(event)
        else:
            league_map['in_progress'].append(event)

        league_map[event.league].append(event)
    return dict(league_map)


def build_rundown_stories(topics: List[str]) -> List[Dict[str, str]]:
    """Maps topics to rundown metadata dictionaries."""
    return [
        {
            "id": topic,
            "title": f"{LEAGUE_METADATA.get(topic.replace('league_', ''), LEAGUE_METADATA['default'])['logo']}   {LEAGUE_METADATA.get(topic.replace('league_', ''), LEAGUE_METADATA['default'])['name']}",
            "subtitle": ''
        }
        for topic in topics
    ]


def has_valid_coordinates(venues: List[Any]) -> bool:
    """Checks if any venue in the list has valid lat/lng coordinates."""
    valid_venues = [v for v in venues if v and getattr(v, 'latitude', None) and getattr(v, 'longitude', None)]
    return len(valid_venues) > 0


# --- Action Generators (Declarative Pipeline) ---

def determine_layout_actions(has_coords: bool) -> List[ActionDef]:
    """Returns declarative layout actions based on data availability."""
    if has_coords:
        # Note: Added fallback for when valid coordinates exist (adjust if you have specific multi-box logic)
        return [
            (set_layout_action, {"layout_mode": random.choice(["SPLIT_VERTICAL", "PIP_BOTTOM_RIGHT"])}),
            (set_assignments_action, {"assignments": {
                "box1": {"type": "DynamicHTML", "resourceId": "scoreboard_panel"},
                "box2": {"type": "DynamicHTML", "resourceId": "scoreboard_panel2"},
            }})
        ]

    # Fallback to single if venues are missing or invalid
    return [
        (set_layout_action, {"layout_mode": 'SINGLE'}),
        (set_assignments_action, {"assignments": {
            "main": {"type": "DynamicHTML", "resourceId": "scoreboard_panel"},
        }})
    ]


def build_topic_actions(topic: str, graphic_html: str, events: List[SportsEvent], venues: List[Any], stoppage: float,
                        minutes_left: int) -> List[ActionDef]:
    """Maps a single sports topic into a sequence of Director actions."""
    actions = [
        (set_state_action, {"key": "selectedStory", "value": {"id": topic, "data": {"lat": 0.00, "lng": 0.00}}})
    ]

    # Determine layout declaratively based on venue data
    actions.extend(determine_layout_actions(has_valid_coordinates(venues)))

    # Add graphic assignments and timings
    actions.extend([
        (define_dynamic_html_action, {
            "resource_id": "scoreboard_panel",
            "html_content": graphic_html,
            "scroll_duration": stoppage * minutes_left * 1.3 * 1000
        }),
        (wait_action, {"duration": 5}),
        (wait_action, {"duration": stoppage * minutes_left * 0.9})
    ])

    return actions


# --- Side-Effect Applicators ---

def apply_actions(director: Director, actions: List[ActionDef]) -> None:
    """Takes a declarative list of actions and mutates the director."""
    for action_func, kwargs in actions:
        director.add(action_func, **kwargs)


# --- Main Orchestrator ---

def get_sports_graphics(ds, controller, event_showing=None, minutes_left=600):
    """Generates the sports graphics script, showing leagues and dashboards."""

    # 1. Fetch Core Data
    sports_events: List[SportsEvent] = retrieve_from_event_db(
        data_adapter=ds(event=True), object_sports=True, many=True, uid=event_showing
    )

    # 2. Process Core Data (Pure Pipelines)
    sports_graphics = generate_sports_dashboards(sports_events)
    topics = list(sports_graphics.keys())
    print(f"Generated sports graphics for {len(topics)} topics.")

    if not topics:
        return Director(controller.clear()).build()

    league_map = build_league_map(sports_events)
    rundown_stories = build_rundown_stories(topics)
    stoppage = 1 / len(topics)

    # 3. Pre-fetch Venue Data (Isolating external I/O)
    # We fetch all unique venues up front rather than looping hitting the DB
    all_venue_uids = list(set([e.venue_uid for e in sports_events if e.venue_uid]))
    all_venues = retrieve_from_kb_db(data_adapter=ds(kb=True), object_establishment=True, many=True, uid=all_venue_uids)
    venue_lookup = {v.uid: v for v in all_venues if v}

    # 4. Generate Action Sequence
    all_story_actions = []
    for topic in topics:
        events = league_map.get(topic.replace('league_', ''), [])
        venues_for_topic = [venue_lookup.get(e.venue_uid) for e in events if e.venue_uid in venue_lookup]

        topic_actions = build_topic_actions(
            topic=topic,
            graphic_html=sports_graphics[topic],
            events=events,
            venues=venues_for_topic,
            stoppage=stoppage,
            minutes_left=minutes_left
        )
        all_story_actions.extend(topic_actions)

    # 5. Apply Side Effects (Director Mutation)
    director = Director(controller.clear())

    setup_scene(director, rundown_stories)
    director.add(set_state_action, key="hideChyron", value=True)
    director.add(set_state_action, key="hideSideBar", value=False)
    director.add(set_state_action, key="tickerText", value=create_sports_ticker(events=sports_events))

    # Hydrate the director with the generated actions
    apply_actions(director, all_story_actions)

    # Use the actual last topic as the teardown ID (Fixing the prev_uid bug)
    last_topic_uid = topics[-1]
    teardown_scene(director, last_topic_uid)

    director.add(set_state_action, key="hideSideBar", value=True)

    return director.build()