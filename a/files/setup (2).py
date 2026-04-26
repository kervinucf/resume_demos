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

def setup_scene(director: Director) -> Director:
    """Initializes the scene with layers, default text, and an opening pan."""
    director.add(log_action, message="[DIRECTOR] Opening: Global seismic overview.")
    director.add(set_state_action, key="hideChyron", value=False)
    director.add(set_state_action, key="hideSideBar", value=True)
    director.add(clear_layer_action, layer_name='html')
    director.add(init_layer_action, layer_name='html', properties={'htmlElement': "d => { const el = document.createElement('div'); el.innerHTML = d.html.trim(); return el.firstChild; }", 'htmlTransitionDuration': 1200})
    director.add(init_layer_action, layer_name='hex', properties={'hexBinPointLat': "p=>p.lat", 'hexBinPointLng': "p=>p.lng", 'hexBinPointWeight': "p=>p.weight", 'hexBinResolution': 4, 'hexMargin': 0.15, 'hexAltitude': "d=>Math.max(d.sumWeight*0.6,0.012)", 'hexTopColor': "d=>'#33ff99'", 'hexSideColor': "d=>'rgba(51,255,153,0.55)'"})
    director.add(init_layer_action, layer_name='rings', properties={'ringColor': "d => d.color", 'ringMaxRadius': .75, 'ringPropagationSpeed': .15, 'ringRepeatPeriod': 1600})
    director.add(set_state_action, key="chyronText", value="GLOBAL SEISMIC ACTIVITY")
    director.add(set_state_action, key="tickerText", value="Real-time planetary monitoring.")
    director.add(pan_action, lat=15, lng=160, duration=12000, altitude=random.uniform(.42, 2.1))
    director.add(wait_action, duration=12.0)
    return director
