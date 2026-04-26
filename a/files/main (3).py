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



def make_currency_location_elements(ev: Country) -> Dict[str, Dict]:
    """Create all display elements for a single earthquake event."""
    return { "marker": { "id": ev.uid, "lat": float(ev.center.get('latitude')), "lng": float(ev.center.get('longitude')),
                         "html": create_country_marker(ev), },
             "hex": create_hex_element(ev), "ring": create_ring_element(ev), }

