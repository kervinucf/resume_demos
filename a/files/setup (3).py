# -*- coding: utf-8 -*-
"""
Refactored earthquake visualization script with a clear separation of concerns.
- UTILS: Core data processing and calculation helpers.
- ELEMENTS: Functions to generate visual components.
- MOTIONS & SCENES: Functions to control the director's timeline and actions.
- STORY LOGIC & ORCHESTRATION: High-level narrative and segment execution.
"""

from machine.m2.pds.lib.helpers.utils import access_clock, random
from machine.m2.server.src._services.content_manager.segments._.utils.director import (
    Director, add_elements_action, clear_layer_action, define_dynamic_html_action,
    init_layer_action, log_action, pan_action, refresh_layer_action, remove_elements_action,
    set_assignments_action, set_layout_action, set_state_action, wait_action
)


# =============================================================================
#   PART 3: MOTIONS & SCENES
#   Functions that control the director's timeline, camera, and state.
# =============================================================================


def setup_scene(director: Director, rundown_stories: list) -> Director:
    """🎬 Adds all setup actions to the timeline."""
    return director \
        .add(log_action, message="Director: Starting News Segment (Focus Mode)") \
        .add(clear_layer_action, layer_name='points') \
        .add(clear_layer_action, layer_name='html') \
        .add(init_layer_action, layer_name='points',
             properties={'pointColor': "() => '#7dfc00'", 'pointAltitude': 0.01, 'pointRadius': 0.35}) \
        .add(init_layer_action, layer_name='html', properties={
        'htmlElement': "d => { const el=document.createElement('div'); el.innerHTML=d.html.trim(); return el.firstChild; }"}) \
        .add(set_state_action, key="rundownStories", value=rundown_stories) \
        .add(pan_action, lat=20, lng=-30, duration=2000, altitude=3.0) \
        .add(wait_action, duration=2.0)


