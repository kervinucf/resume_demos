# -*- coding: utf-8 -*-
"""
Refactored earthquake visualization script with a clear separation of concerns.
- UTILS: Core data processing and calculation helpers.
- ELEMENTS: Functions to generate visual components.
- MOTIONS & SCENES: Functions to control the director's timeline and actions.
- STORY LOGIC & ORCHESTRATION: High-level narrative and segment execution.
"""

from typing import Any, Dict, List, Tuple

from lxml import etree as ET


# =============================================================================
#   PART 1: UTILS
#   Helper functions for data manipulation, calculations, and classification.
# =============================================================================

def _ce(tag, class_name=None, parent=None, text=None, style=None):
    """A utility to create an lxml etree element."""
    e = ET.Element(tag)
    if class_name:
        e.set('class', class_name)
    if text:
        e.text = text
    if parent is not None:
        parent.append(e)
    if style:
        e.set('style', '; '.join([f'{k}: {v}' for k, v in style.items()]))
    return e

def determine_severity_color(magnitude: float) -> Dict[str, str]:
    """Determines a color theme based on earthquake magnitude."""
    if magnitude >= 6.5:
        return {'primary': '#d90429', 'glow': 'rgba(217, 4, 41, 0.85)', 'secondary': 'rgba(217, 4, 41, 0.2)'}
    elif magnitude >= 5.0:
        return {'primary': '#fca311', 'glow': 'rgba(252, 163, 17, 0.75)', 'secondary': 'rgba(252, 163, 17, 0.15)'}
    else:
        return {'primary': "#fe9240", 'glow': 'rgba(254, 228, 64, 0.65)', 'secondary': 'rgba(254, 228, 64, 0.1)'}

def get_region_by_coords(lat: float, lon: float) -> str:
    """Categorizes geographic coordinates into a named region."""
    if 8 <= lat <= 85 and -168 <= lon <= -52: return "North America"
    if -56 <= lat < 8 and -81 <= lon <= -34: return "South America"
    if 36 <= lat <= 71 and -24 <= lon <= 65: return "Europe"
    if 0 <= lat <= 77 and 65 < lon <= 180: return "Asia"
    if -35 <= lat < 36 and -18 <= lon <= 51: return "Africa"
    if -47 <= lat < 0 and 113 <= lon <= 180: return "Oceania"
    return "The Pacific Ring of Fire"

def split_into_n(lst, n):
    """Splits a list into n roughly equal-sized parts."""
    k, m = divmod(len(lst), n)
    return [lst[i * k + min(i, m):(i + 1) * k + min(i + 1, m)] for i in range(n)]
