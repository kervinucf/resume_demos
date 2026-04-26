from typing import Any, Dict, List
from lxml import etree as ET

from machine.m2.pds.lib.helpers.resolvers.dtos.geo.country import Country
from machine.m2.server.src._services.content_manager.segments._.finance.scenes.graphics.elements import (
    create_country_marker,
    create_hex_element,
    create_ring_element,
)

EXCHANGE_LOCATIONS: Dict[str, Dict[str, object]] = {
    'NYSE': {'name': 'New York Stock Exchange', 'city': 'New York', 'region': 'North America', 'lat': 40.7069,
             'lng': -74.0113, 'alt': 2.2},
    'NASDAQ': {'name': 'NASDAQ MarketSite', 'city': 'New York', 'region': 'North America', 'lat': 40.7567,
               'lng': -73.9857, 'alt': 2.2},
    'TSX': {'name': 'Toronto Stock Exchange', 'city': 'Toronto', 'region': 'North America', 'lat': 43.648,
            'lng': -79.387, 'alt': 2.2},
    'LSE': {'name': 'London Stock Exchange', 'city': 'London', 'region': 'Europe', 'lat': 51.5150, 'lng': -0.0984,
            'alt': 1.9},
    'FWB': {'name': 'Frankfurt Stock Exchange', 'city': 'Frankfurt', 'region': 'Europe', 'lat': 50.1155, 'lng': 8.6797,
            'alt': 1.9},
    'Euronext': {'name': 'Euronext Paris', 'city': 'Paris', 'region': 'Europe', 'lat': 48.868, 'lng': 2.341,
                 'alt': 1.9},
    'TSE': {'name': 'Tokyo Stock Exchange', 'city': 'Tokyo', 'region': 'Asia', 'lat': 35.6797, 'lng': 139.7788,
            'alt': 1.8},
    'HKEX': {'name': 'Hong Kong Stock Exchange', 'city': 'Hong Kong', 'region': 'Asia', 'lat': 22.2833, 'lng': 114.1583,
             'alt': 1.8},
    'SSE': {'name': 'Shanghai Stock Exchange', 'city': 'Shanghai', 'region': 'Asia', 'lat': 31.238, 'lng': 121.505,
            'alt': 1.8},
    'ASX': {'name': 'Australian Securities Exchange', 'city': 'Sydney', 'region': 'Oceania', 'lat': -33.8688,
            'lng': 151.2093, 'alt': 2.0},
}
ASSET_NAME_TO_EXCHANGE = {
    'USA (S&P 500)': 'NYSE', 'USA (NASDAQ)': 'NASDAQ', 'CANADA (S&P/TSX)': 'TSX',
    'UK (FTSE 100)': 'LSE', 'GERMANY (DAX)': 'FWB', 'FRANCE (CAC 40)': 'Euronext', 'EUROZONE (EURO STOXX 50)': 'FWB',
    'JAPAN (NIKKEI 225)': 'TSE', 'HONG KONG (HANG SENG)': 'HKEX', 'CHINA (SHANGHAI COMPOSITE)': 'SSE',
    'AUSTRALIA (ASX 200)': 'ASX',
}


# ---------------------------------------------------------------------------
# VISUALIZATION HELPERS
# ---------------------------------------------------------------------------
def _ce(tag, cls=None, p=None, txt=None):
    e = ET.Element(tag)
    if cls: e.set('class', cls)
    if txt: e.text = txt
    if p is not None: p.append(e)
    return e


def _pct(p: str) -> float:  # ... implementation
    try:
        return float((p or "0").strip().replace('%', '')) / 100.0
    except ValueError:
        return 0.0



def make_marker_element(event=None, idx="05"):

    if idx < 10:
        idx = f"0{idx}"

    """
    Generate a self-contained map marker div with inline styling.
    Designed for broadcast-quality overlays or web map integration.

    Args:
        idx (str): The marker idx to display above the pulse.

    Returns:
        str: HTML <div> markup for the marker with inline styles.
    """
    return f"""
                             <div style="
                               position: relative;
                               width: max-content;
                               display: inline-flex;
                               flex-direction: column;
                               align-items: center;
                               font-family: 'Inter', sans-serif;
                             ">
                               <div style="
                                 font-size: 12px;
                                 font-weight: 700;
                                 color: #e0e6eb;
                                 letter-spacing: -1px;
                                 transform: translateY(0);
                                 text-shadow:
                                   0 1px 0 #ffffff0a,
                                   0 2px 1px rgba(0,0,0,0.25),
                                   0 4px 4px rgba(0,0,0,0.3);
                               ">
                                 {idx}
                               </div>

                               <div style="
                                 height: 7px;
                                 width: 7px;
                                 z-index: 1;
                                 opacity: 0;
                                 border: 3px solid rgba(0,156,255,0.5);
                                 border-radius: 50%;
                                 animation: flash 2s ease-out infinite;
                               "></div>

                               <div style="
                                 width: 7px;
                                 height: 7px;
                                 border: 3px solid rgba(91,186,245,0.6);
                                 border-radius: 50%;
                                 background: #0078c8;
                                 box-shadow: 0 0 12px rgba(0,156,255,0.8);
                                 z-index: 5;
                               "></div>
                             </div>

                             <style>
                             @keyframes flash {{
                               0% {{transform: scale(0.2); opacity: 0.0;}}
                               25% {{opacity: 0.3;}}
                               50% {{transform: scale(1); opacity: 0.5;}}
                               100% {{transform: scale(1.4); opacity: 0;}}
                             }}
                             </style>
                             """.strip()
