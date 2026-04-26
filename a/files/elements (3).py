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
from machine.m2.pds.lib.helpers.resolvers.dtos.events.earthquake_event import EarthquakeEvent
from machine.m2.server.src._services.content_manager.segments._.earthquake.scenes.graphics.utils import (
    _ce,
    determine_severity_color
)

# =============================================================================
#   PART 2: ELEMENTS
#   Functions dedicated to creating visual components (HTML markers, etc.).
# =============================================================================

def create_cluster_badge_marker(cell_events: List[EarthquakeEvent]) -> str:
    """Generates HTML for a badge summarizing a cluster of events."""
    event_count = len(cell_events)
    max_magnitude = max(float(e.magnitude) for e in cell_events)
    color_theme = determine_severity_color(max_magnitude)
    
    badge_style = {'width': '80px', 'height': '80px', 'display': 'flex', 'flex-direction': 'column', 'align-items': 'center', 'justify-content': 'center', 'transform': 'translateX(-50%) translateY(-50%)', 'pointer-events': 'none'}
    count_style = {'font-size': '24px', 'font-weight': '700', 'color': color_theme['primary'], 'line-height': '1'}
    label_style = {'font-size': '10px', 'font-weight': '600', 'color': 'rgba(255, 255, 255, 0.8)', 'margin-top': '4px', 'text-transform': 'uppercase'}
    mag_style = {'font-size': '12px', 'font-weight': '600', 'color': color_theme['primary'], 'margin-top': '2px'}
    
    el = _ce('div', style=badge_style)
    _ce('div', style=count_style, parent=el, text=str(event_count))
    _ce('div', style=label_style, parent=el, text='EVENTS')
    _ce('div', style=mag_style, parent=el, text=f'Max M{max_magnitude:.1f}')
    return ET.tostring(el, method='html', encoding='unicode')

def create_individual_marker(event: EarthquakeEvent) -> str:
    """Generates HTML for a single earthquake event marker."""
    magnitude = float(event.magnitude)
    color_theme = determine_severity_color(magnitude)
    mag_scale = 1.0 + np.clip((magnitude - 4.5) / 3.5, 0, 0.5)
    
    marker_style = {'display': 'flex', 'align-items': 'center', 'gap': '6px', 'width': '140px', 'transform': f'translateX(-50%) scale({mag_scale:.2f})', 'pointer-events': 'none'}
    glow_style = {'position': 'absolute', 'width': '44px', 'height': '44px', 'animation': 'pulse 2.5s ease-in-out infinite', 'opacity': '0.8'}
    readout_style = {'display': 'flex', 'align-items': 'center', 'gap': '8px', 'padding': '4px 10px'}
    label_style = {'font-size': '10px', 'font-weight': '600', 'color': 'rgba(255, 255, 255, 0.7)', 'text-transform': 'uppercase'}
    value_style = {'font-size': '18px', 'font-weight': '800', 'color': color_theme['primary'], 'line-height': '1'}
    
    el = _ce('div', style=marker_style)
    _ce('div', style=glow_style, parent=el)
    readout = _ce('div', style=readout_style, parent=el)
    _ce('span', style=label_style, parent=readout, text='MAG')
    _ce('span', style=value_style, parent=readout, text=f'{magnitude:.1f}')
    return ET.tostring(el, method='html', encoding='unicode')

def create_hex_element(event: EarthquakeEvent) -> dict:
    """Returns a hex visualization element for an earthquake event."""
    return {"id": f"hex_{event.uid}", "lat": float(event.latitude), "lng": float(event.longitude), "weight": .024, "sign": 1, "label": float(event.magnitude)}

def create_ring_element(event: EarthquakeEvent) -> dict:
    """Creates a data structure for a propagating ring pulse."""
    color_theme = determine_severity_color(float(event.magnitude))
    return {"id": f"ring_{event.uid}", "lat": float(event.latitude), "lng": float(event.longitude), "color": color_theme["glow"]}

