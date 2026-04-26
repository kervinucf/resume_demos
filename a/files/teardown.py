# -*- coding: utf-8 -*-
"""
Refactored earthquake visualization script with a clear separation of concerns.
- UTILS: Core data processing and calculation helpers.
- ELEMENTS: Functions to generate visual components.
- MOTIONS & SCENES: Functions to control the director's timeline and actions.
- STORY LOGIC & ORCHESTRATION: High-level narrative and segment execution.
"""
from typing import Any, Dict, List

from machine.m2.server.src._services.content_manager.segments._.utils.director import (
    Director, add_elements_action, clear_layer_action, define_dynamic_html_action,
    init_layer_action, log_action, pan_action, refresh_layer_action, remove_elements_action,
    set_assignments_action, set_layout_action, set_state_action, wait_action
)

def teardown_scene(director: Director, all_hex_ids: List[str]=[], all_label_ids: List[str]=[]) -> Director:
    """🧹 Adds the final overview and full scene cleanup."""
    return director \
        .add(set_state_action, key="chyronText", value="GLOBAL MARKETS TREND OVERVIEW") \
        .add(pan_action, lat=38.9, lng=-77.03, duration=6000, altitude=1.8) \
        .add(wait_action, duration=15.0) \
        .add(log_action, message="Director: Market Trend Report Concluded. Cleaning up.") \
        .add(remove_elements_action, layer_name='hex', ids=all_hex_ids, id_key='id') \
        .add(refresh_layer_action, layer_name='hex') \
        .add(remove_elements_action, layer_name='labels', ids=all_label_ids, id_key='id') \
        .add(refresh_layer_action, layer_name='labels') \
        .add(set_state_action, key="chyronText", value="") \
        .add(set_state_action, key="tickerText", value="")