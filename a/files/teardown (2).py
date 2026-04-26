# -*- coding: utf-8 -*-
"""
Refactored earthquake visualization script with a clear separation of concerns.
- UTILS: Core data processing and calculation helpers.
- ELEMENTS: Functions to generate visual components.
- MOTIONS & SCENES: Functions to control the director's timeline and actions.
- STORY LOGIC & ORCHESTRATION: High-level narrative and segment execution.
"""

from machine.m2.server.src._services.content_manager.segments._.utils.director import (
    Director, add_elements_action, clear_layer_action, define_dynamic_html_action,
    init_layer_action, log_action, pan_action, refresh_layer_action, remove_elements_action,
    set_assignments_action, set_layout_action, set_state_action, wait_action
)

def teardown_scene(director: Director) -> Director:
    """Resets the scene to a global view and logs completion."""
    director.add(log_action, message="[DIRECTOR] Closing: Return to global perspective.")
    director.add(set_state_action, key="chyronText", value="GLOBAL SEISMIC ACTIVITY")
    director.add(set_state_action, key="tickerText", value="Monitoring continues worldwide.")
    director.add(pan_action, lat=0, lng=0, duration=15000, altitude=3.0)
    director.add(wait_action, duration=10.0)
    director.add(clear_layer_action, layer_name='html')
    director.add(refresh_layer_action, layer_name='html')
    director.add(log_action, message="[DIRECTOR] End transmission.")
    return director