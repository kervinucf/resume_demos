# -*- coding: utf-8 -*-
"""
Refactored earthquake visualization script with a clear separation of concerns.
- UTILS: Core data processing and calculation helpers.
- ELEMENTS: Functions to generate visual components.
- MOTIONS & SCENES: Functions to control the director's timeline and actions.
- STORY LOGIC & ORCHESTRATION: High-level narrative and segment execution.
"""
from typing import Any, Dict, List, Tuple

import numpy as np
from lxml import etree as ET

# --- Mock imports for standalone execution ---
from machine.m2.pds.lib.helpers.resolvers.dtos.geo.country import Country
from machine.m2.server.src._services.content_manager.segments._.earthquake.scenes.graphics.utils import (
    _ce,
    determine_severity_color
)

# =============================================================================
#   PART 2: ELEMENTS
#   Functions dedicated to creating visual components (HTML markers, etc.).
# =============================================================================

def create_country_marker(event: Country) -> str:
    """Generates HTML for a single earthquake event marker."""

    glow_style = { 'width': '44px', 'height': '44px'}


    return ET.tostring(
    _ce('span', parent=_ce('div', style=glow_style), text=f'{event.flag}')
    , method='html', encoding='unicode')

def create_hex_element(event: Country) -> dict:
    """Returns a hex visualization element for an earthquake event."""
    return {"id": f"hex_{event.uid}", "lat": float(event.center.get('latitude')), "lng": float(event.center.get('longitude')), "weight": .024, "sign": 1, "label": ""}

def create_ring_element(event: Country) -> dict:
    """Creates a data structure for a propagating ring pulse."""
    return {"id": f"ring_{event.uid}", "lat": float(event.center.get('latitude')), "lng": float(event.center.get('longitude')), "color": 'yellow'}

