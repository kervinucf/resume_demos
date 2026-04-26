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

def teardown_scene(director: Director, final_loc_uid: str) -> Director:
    """🧹 Adds all teardown and cleanup actions to the timeline."""
    return director \
        .add(log_action, message="Director: News sequence complete. Cleaning up.") \
        .add(set_state_action, key="chyronText", value="") \
        .add(set_state_action, key="tickerText", value="Monitoring for new events.") \
        .add(remove_elements_action, layer_name='points', ids=[final_loc_uid]) \
        .add(remove_elements_action, layer_name='html', ids=[f"card_{final_loc_uid}"]) \
        .add(refresh_layer_action, layer_name='points') \
        .add(refresh_layer_action, layer_name='html') \
        .add(set_state_action, key="rundownStories", value=[]) \
        .add(set_state_action, key="selectedStory", value=None) \
        .add(pan_action, lat=25, lng=80, duration=5000, altitude=3.5) \
        .add(wait_action, duration=5.0)

