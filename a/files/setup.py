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
    """🎬 Adds all setup actions to the timeline."""
    return director \
        .add(log_action, message=f"Director: Rolling Market Trend Report.") \
        .add(clear_layer_action, layer_name='html').add(clear_layer_action, layer_name='rings') \
        .add(clear_layer_action, layer_name='hex').add(clear_layer_action, layer_name='labels') \
        .add(init_layer_action, layer_name='html', properties={
        'htmlElement': "d=>{const e=document.createElement('div');e.innerHTML=d.html.trim();return e.firstChild;}"}) \
        .add(init_layer_action, layer_name='rings',
             properties={'ringColor': "d=>d.color", 'ringMaxRadius': 1, 'ringPropagationSpeed': 1,
                         'ringRepeatPeriod': 1800}) \
        .add(init_layer_action, layer_name='hex',
             properties={'hexBinPointLat': "p=>p.lat", 'hexBinPointLng': "p=>p.lng", 'hexBinPointWeight': "p=>p.weight",
                         'hexBinResolution': 4, 'hexMargin': 0.15, 'hexAltitude': "d=>Math.max(d.sumWeight*0.6,0.012)",
                         'hexTopColor': "d=>(d.points&&d.points[0]&&d.points[0].sign>0?'#33ff99':'#ff4d4d')",
                         'hexSideColor': "d=>(d.points&&d.points[0]&&d.points[0].sign>0?'rgba(51,255,153,0.55)':'rgba(255,77,77,0.55)')"}) \
        .add(init_layer_action, layer_name='labels',
             properties={'labelText': 'text', 'labelLat': 'lat', 'labelLng': 'lng', 'labelColor': 'color',
                         'labelSize': 'size', 'labelAltitude': 'altitude', 'labelIncludeDot': 'includeDot'}) \
        .add(set_state_action, key="chyronText", value="GLOBAL MARKETS: TREND ANALYSIS") \
        .add(set_state_action, key="tickerText", value="Analyzing 1-Month market momentum...") \
        .add(pan_action, lat=25, lng=15, duration=2000, altitude=2.5) \
        .add(wait_action, duration=4.0)


