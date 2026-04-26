# -*- coding: utf-8 -*-
"""
Financial Market Visualization Narrative — functional, declarative flow.
"""
from collections import defaultdict
from typing import Dict, List, Tuple, Any, Callable
import random
import itertools

from machine.m2.pds.lib.functions.resolvers.event_resolver import retrieve_from_event_db
from machine.m2.pds.lib.functions.resolvers.kb_resolver import retrieve_from_kb_db
from machine.m2.pds.lib.helpers.resolvers.dtos.events.financial_event import FinancialEvent

from machine.m2.server.src._services.content_manager.segments._.finance.scenes.main import (
    ASSET_NAME_TO_EXCHANGE,
    EXCHANGE_LOCATIONS,
    make_currency_location_elements,
    create_hex_element,
    create_ring_element
)
from machine.m2.server.src._services.content_manager.segments._.finance.scenes.setup import setup_scene
from machine.m2.server.src._services.content_manager.segments._.finance.scenes.teardown import teardown_scene
from machine.m2.server.src._services.content_manager.segments._.finance.scenes.graphics.dashboards import \
    generate_financial_dashboard, CURRENCIES_BY_CONTINENT
from machine.m2.server.src._services.content_manager.segments._.finance.scenes.graphics.chyron import \
    create_financial_chyron
from machine.m2.server.src._services.content_manager.segments._.finance.scenes.graphics.ticker import \
    create_narrative_ticker, create_sequential_ticker
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

CONTINENT_NAMES = {
    'NA': 'North America',
    'SA': 'South America',
    'EU': 'Europe',
    'AF': 'Africa',
    'AS': 'Asia',
    'OC': 'Oceania'
}


# --- Pure / Utility Functions ---

def build_currency_map(currency_events: List[FinancialEvent]) -> Dict[str, List[str]]:
    """Pure function mapping currency assets to their continent codes."""
    currency_map = defaultdict(list)
    for event in currency_events:
        for continent_code, currency_codes in CURRENCIES_BY_CONTINENT.items():
            if event.asset_ticker in currency_codes:
                currency_map[continent_code].append(event.asset_name)
    return dict(currency_map)


# --- Declarative Layer Helpers ---

def build_render_layer_actions(elements_dict: Dict[str, List[Any]]) -> List[ActionDef]:
    """Generates actions to add visual elements and refresh their respective layers."""
    actions = []
    for layer, items in elements_dict.items():
        if items:
            actions.append(
                (add_elements_action, {"layer_name": layer, "elements": items if isinstance(items, list) else [items]}))
    for layer in elements_dict.keys():
        actions.append((refresh_layer_action, {"layer_name": layer}))
    return actions


def build_clear_layer_actions(elements_dict: Dict[str, List[Any]]) -> List[ActionDef]:
    """Generates actions to remove elements by ID and refresh layers."""
    actions = []
    for layer, ids in elements_dict.items():
        if ids:
            actions.append(
                (remove_elements_action, {"layer_name": layer, "ids": ids if isinstance(ids, list) else [ids]}))
    for layer in elements_dict.keys():
        actions.append((refresh_layer_action, {"layer_name": layer}))
    return actions


# --- Action Generators (Declarative Pipeline) ---

def build_overview_segment_actions(idx: int, commodity_html: Dict, commodity_chyron: Dict, stock_market_html: Dict,
                                   stock_market_chyron: Dict, minutes_left: float) -> List[ActionDef]:
    """Maps an index to its specific overview segment action sequence."""
    duration = minutes_left * 0.166
    scroll_duration = duration * 0.9 * 1000

    if idx == 0:
        return [
            (define_dynamic_html_action,
             {"resource_id": "overview_panel", "html_content": commodity_html['leaderboard'],
              "scroll_duration": scroll_duration}),
            (set_state_action, {"key": "chyronText", "value": commodity_chyron['gainer_board']}),
            (wait_action, {"duration": duration})
        ]
    elif idx == 1:
        return [
            (set_state_action, {"key": "chyronText", "value": commodity_chyron['loser_board']}),
            (wait_action, {"duration": duration})
        ]
    elif idx == 2:
        return [
            (define_dynamic_html_action,
             {"resource_id": "overview_panel", "html_content": commodity_html['comparison_chart']}),
            (wait_action, {"duration": duration})
        ]
    elif idx == 3:
        return [
            (define_dynamic_html_action,
             {"resource_id": "overview_panel", "html_content": stock_market_html['gainer_board'],
              "scroll_duration": scroll_duration}),
            (set_state_action, {"key": "chyronText", "value": stock_market_chyron['gainer_board']}),
            (wait_action, {"duration": duration})
        ]
    elif idx == 4:
        return [
            (define_dynamic_html_action,
             {"resource_id": "overview_panel", "html_content": stock_market_html['loser_board'],
              "scroll_duration": scroll_duration}),
            (set_state_action, {"key": "chyronText", "value": stock_market_chyron['loser_board']}),
            (wait_action, {"duration": duration})
        ]
    elif idx == 5:
        return [
            (set_state_action, {"key": "hideChyron", "value": True}),
            (define_dynamic_html_action,
             {"resource_id": "overview_panel", "html_content": stock_market_html['comparison_chart'],
              "scroll_duration": scroll_duration}),
            (wait_action, {"duration": duration})
        ]
    return []


def build_continent_cycle_actions(
        continent_code: str,
        continent_obj: Any,
        locations: List[Any],
        currency_graphic: str,
        overview_actions: List[ActionDef],
        minutes_left: float
) -> List[ActionDef]:
    """Builds the complete declarative sequence for a single continent loop."""
    actions = []

    # Render Continent Graphics
    actions.append((define_dynamic_html_action, {"resource_id": "currency_panel", "html_content": currency_graphic,
                                                 "scroll_duration": minutes_left * 0.166 * 0.85 * 1000}))

    # Pan to Continent
    lat, lng = continent_obj.center.get('latitude'), continent_obj.center.get('longitude')
    actions.append((pan_action, {"lat": lat, "lng": lng, "duration": 4000, "altitude": random.uniform(0.24, 0.61)}))

    # Prepare map elements purely
    elements = [make_currency_location_elements(loc)["marker"] for loc in locations]
    hexes = [create_hex_element(loc) for loc in locations]
    rings = [create_ring_element(loc) for loc in locations]

    actions.append((wait_action, {"duration": 4.0}))

    # Render map elements
    actions.extend(build_render_layer_actions({"html": elements, "hex": hexes, "rings": rings}))

    # Insert the respective overview segment (Commodity or Stock Market)
    actions.extend(overview_actions)

    # Clear map elements and refresh
    actions.extend(build_clear_layer_actions({
        "html": [e["id"] for e in elements],
        "hex": [h["id"] for h in hexes],
        "rings": [r["id"] for r in rings],
    }))

    actions.append((wait_action, {"duration": 2.0}))

    return actions


# --- Side-Effect Applicators ---

def apply_actions(director: Director, actions: List[ActionDef]) -> None:
    """Takes a declarative list of actions and mutates the director."""
    for action_func, kwargs in actions:
        director.add(action_func, **kwargs)


# --- Main Orchestrator ---

def get_finance_graphics(ds, controller, event_showing=None, minutes_left=300):
    """Generates the finance graphics script, showing each asset individually."""

    # ---------------------------------------------------------
    # 1. Gather Data (All I/O pushed to the top)
    # ---------------------------------------------------------
    currency_events = retrieve_from_event_db(data_adapter=ds(event=True), object_finance=True, many=True,
                                             uid=event_showing, asset_type="Currency")
    commodity_events = retrieve_from_event_db(data_adapter=ds(event=True), object_finance=True, many=True,
                                              uid=event_showing, asset_type="Commodity")
    stock_market_events = retrieve_from_event_db(data_adapter=ds(event=True), object_finance=True, many=True,
                                                 uid=event_showing, asset_type="Stock Exchange")

    currency_map = build_currency_map(currency_events)

    # Pre-fetch Geographical Data to prevent N+1 DB queries in the loop
    all_continent_names = list(CONTINENT_NAMES.values())
    fetched_continents = retrieve_from_kb_db(data_adapter=ds(kb=True), object_continent=True, many=True,
                                             name=all_continent_names)
    continent_lookup = {c.name: c for c in fetched_continents if c}

    # Pre-fetch Country Data per continent code
    country_lookup = {}
    for code in CONTINENT_NAMES.keys():
        country_names = currency_map.get(code, [])
        if country_names:
            country_lookup[code] = retrieve_from_kb_db(data_adapter=ds(kb=True), object_country=True, many=True,
                                                       name=country_names)
        else:
            country_lookup[code] = []

    # ---------------------------------------------------------
    # 2. Process Dashboards & Data Assets (Pure)
    # ---------------------------------------------------------
    stock_market_html = generate_financial_dashboard(events=stock_market_events, term='Day % (Intraday)',
                                                     dark_mode=True)
    stock_market_chyron = create_financial_chyron(events=stock_market_events, term='Day % (Intraday)')
    stock_market_ticker = create_sequential_ticker(events=stock_market_events, term='Day % (Intraday)')

    commodity_html = generate_financial_dashboard(events=commodity_events, term='Day % (Intraday)', dark_mode=True)
    commodity_chyron = create_financial_chyron(events=commodity_events, term='Day % (Intraday)')
    commodity_ticker = create_sequential_ticker(events=commodity_events, term='Day % (Intraday)')

    currency_html = generate_financial_dashboard(events=currency_events, term='Day % (Intraday)', dark_mode=True)
    currency_ticker = create_sequential_ticker(events=currency_events, term='Day % (Intraday)')

    # ---------------------------------------------------------
    # 3. Build Action Sequence (Declarative Pipeline)
    # ---------------------------------------------------------
    all_actions = [
        (set_state_action, {"key": "hideSideBar", "value": True}),
        (set_layout_action, {"layout_mode": "TRIPLE_LEFT_HEAVY"}),
        (set_assignments_action, {"assignments": {
            "box1": {"type": "DynamicHTML", "resourceId": "overview_panel"},
            "box2": {"type": "Earth", "resourceId": "earth1"},
            "box3": {"type": "DynamicHTML", "resourceId": "currency_panel"},
        }}),
        (set_state_action, {"key": "tickerText",
                            "value": f"{currency_ticker}     ||     {commodity_ticker}     ||     {stock_market_ticker}"})
    ]

    for idx, (continent_code, continent_name) in enumerate(CONTINENT_NAMES.items()):
        continent_obj = continent_lookup.get(continent_name)
        if not continent_obj:
            continue

        locations = country_lookup.get(continent_code, [])
        currency_graphic = currency_html.get(f'currency_board_{continent_code.lower()}')

        # Build the specific segment (commodity or stock) for this loop iteration
        overview_actions = build_overview_segment_actions(idx, commodity_html, commodity_chyron, stock_market_html,
                                                          stock_market_chyron, minutes_left)

        # Build the entire loop's sequence and append it to our master timeline
        cycle_actions = build_continent_cycle_actions(continent_code, continent_obj, locations, currency_graphic,
                                                      overview_actions, minutes_left)
        all_actions.extend(cycle_actions)

    # ---------------------------------------------------------
    # 4. Apply Side Effects (Director Mutation)
    # ---------------------------------------------------------
    director = Director(controller.clear())
    setup_scene(director)

    apply_actions(director, all_actions)

    teardown_scene(director)
    return director.build()