# -*- coding: utf-8 -*-
import json
import textwrap
import datetime
from typing import List, Dict, Optional
from dataclasses import dataclass
from lxml import etree as ET

# ---------------------------------------------------------------------------
# FrontendController (with hex/hexBin alias fixes + refresh_layer)
# ---------------------------------------------------------------------------
class FrontendController:
    def __init__(self):
        self.commands = []

    def build(self) -> str:
        return "\n".join(self.commands)

    # --- CORE & UTILITY METHODS ---
    def set_sleep(self, duration: float):
        self.commands.append(f"await new Promise(r => setTimeout(r, {int(duration * 1000)}));")
        return self

    def set_state(self, key, value):
        self.commands.append(f"state.{key} = {json.dumps(value)};")
        return self

    def log_message(self, message: str):
        self.commands.append(f"console.log({json.dumps(message)});")
        return self

    # --- LAYER INITIALIZATION ---
    def initialize_layer(self, layer_name: str, properties: dict):
        prop_chain = []
        for key, value in properties.items():
            val_str = value if (isinstance(value, str) and ('=>' in value or value.strip().startswith('('))) \
                     else json.dumps(value)
            prop_chain.append(f".{key}({val_str})")

        method_map = {
            'html':   'htmlElements',
            'hex':    'hexBinPoints',
            'hexBin': 'hexBinPoints',
            'labels': 'labels',
            'rings':  'rings',
            'arcs':   'arcs',
            'paths':  'paths'
        }
        method_base = method_map.get(layer_name, layer_name)
        method_name = f"{method_base}Data"

        data_source = self._get_data_array_name(layer_name)
        js_code = f"if (globe) {{ globe{''.join(prop_chain)}.{method_name}({data_source}); }}"
        self.commands.append(js_code)
        return self

    # --- REBIND/REFRESH A LAYER'S DATA SETTER ---
    def refresh_layer(self, layer_name: str):
        method_map = {
            'html':   'htmlElements',
            'hex':    'hexBinPoints',
            'hexBin': 'hexBinPoints',
            'labels': 'labels',
            'rings':  'rings',
            'arcs':   'arcs',
            'paths':  'paths'
        }
        layer_map = {
            'html':   'htmlData',
            'hex':    'hexBinData',
            'hexBin': 'hexBinData',
            'labels': 'labelsData',
            'rings':  'ringsData',
            'arcs':   'arcsData',
            'paths':  'pathsData'
        }
        method_base = method_map.get(layer_name, layer_name)
        method_name = f"{method_base}Data"
        data_source = f"state.{layer_map.get(layer_name, f'{layer_name}Data')}"
        self.commands.append(f"if (globe) {{ globe.{method_name}({data_source}); }}")
        return self

    # --- DATA ARRAY NAME RESOLUTION ---
    def _get_data_array_name(self, layer_name: str) -> str:
        layer_map = {
            'html':   'htmlData',
            'hex':    'hexBinData',
            'hexBin': 'hexBinData',
            'labels': 'labelsData',
            'rings':  'ringsData',
            'arcs':   'arcsData',
            'paths':  'pathsData'
        }
        if layer_name in layer_map:
            return f"state.{layer_map[layer_name]}"
        return f"state.{layer_name}Data"

    # --- GRANULAR DATA MANIPULATION ---
    def add_elements(self, layer_name: str, elements: list):
        array_name = self._get_data_array_name(layer_name)
        elements_json = json.dumps(elements)
        self.commands.append(f"{array_name} = [...{array_name}, ...{elements_json}];")
        return self

    def update_elements(self, layer_name: str, updates: list, id_key: str = 'id'):
        array_name = self._get_data_array_name(layer_name)
        updates_json = json.dumps(updates)
        id_key_json = json.dumps(id_key)
        js_code = f"""
const updatesMap = new Map({updates_json}.map(u => [u[{id_key_json}], u]));
{array_name} = {array_name}.map(existing => {{
    const update = updatesMap.get(existing[{id_key_json}]);
    return update ? {{ ...existing, ...update }} : existing;
}});
"""
        self.commands.append(textwrap.dedent(js_code))
        return self

    def remove_elements(self, layer_name: str, ids_to_remove: list, id_key: str = 'id'):
        array_name = self._get_data_array_name(layer_name)
        ids_json = json.dumps(ids_to_remove)
        id_key_json = json.dumps(id_key)
        self.commands.append(
            f"{{ const idsToRemove = new Set({ids_json}); {array_name} = {array_name}.filter(el => !idsToRemove.has(el[{id_key_json}])); }}")
        return self

    def clear_layer(self, layer_name: str):
        array_name = self._get_data_array_name(layer_name)
        self.commands.append(f"{array_name} = [];")
        return self

    def update_with_function(self, layer_name: str, map_function: str, filter_function: str = '() => true'):
        array_name = self._get_data_array_name(layer_name)
        js_code = f"""
{array_name} = {array_name}.map(el => {{
    if (({filter_function})(el)) {{
        return ({map_function})(el);
    }}
    return el;
}});
"""
        self.commands.append(textwrap.dedent(js_code))
        return self

    # --- CAMERA ---
    def pan_to_globe_location(self, lat, lng, duration, altitude=1.4):
        pov_str = f"{{lat: {json.dumps(lat)}, lng: {json.dumps(lng)}, altitude: {json.dumps(altitude)}}}"
        self.commands.append(f"if (globe) {{ globe.pointOfView({pov_str}, {duration}); }}")
        return self


# ---------------------------------------------------------------------------
# DATA: EXCHANGES & EVENT MODEL
# ---------------------------------------------------------------------------
EXCHANGE_LOCATIONS: Dict[str, Dict[str, object]] = {
    'NYSE':   {'name': 'New York Stock Exchange',  'city': 'New York',  'region': 'North America', 'lat': 40.7069, 'lng': -74.0113},
    'NASDAQ': {'name': 'NASDAQ MarketSite',        'city': 'New York',  'region': 'North America', 'lat': 40.7567, 'lng': -73.9857},
    'LSE':    {'name': 'London Stock Exchange',    'city': 'London',    'region': 'Europe',        'lat': 51.5150, 'lng': -0.0984},
    'FWB':    {'name': 'Frankfurt Stock Exchange', 'city': 'Frankfurt', 'region': 'Europe',        'lat': 50.1155, 'lng': 8.6797},
    'TSE':    {'name': 'Tokyo Stock Exchange',     'city': 'Tokyo',     'region': 'Asia',          'lat': 35.6797, 'lng': 139.7788},
    'HKEX':   {'name': 'Hong Kong Stock Exchange', 'city': 'Hong Kong', 'region': 'Asia',          'lat': 22.2833, 'lng': 114.1583},
}

@dataclass
class FinancialEvent:
    _DEFAULT_COLLECTION = "finance"
    timestamp_utc: str
    date: str
    asset_name: str
    asset_ticker: str
    asset_type: str
    price: float
    term: str
    performance: Optional[Dict[str, Optional[str]]] = None
    uid: Optional[str] = None

    def __post_init__(self):
        parts = [self.asset_name, self.asset_type, self.term, self.timestamp_utc]
        self.uid = ":".join([str(p).strip().lower().replace(" ", "_") for p in parts if p])

# ---------------------------------------------------------------------------
# PLACEHOLDER DATA (replace with live feed as needed)
# ---------------------------------------------------------------------------
def get_placeholder_indices(exchange_code: str) -> List[FinancialEvent]:
    now = datetime.datetime.utcnow()
    data = {
        'NYSE': [
            FinancialEvent(timestamp_utc=now.isoformat(), date=now.strftime("%Y-%m-%d"),
                           asset_name='DOW JONES', asset_ticker='^DJI', asset_type='index',
                           price=39888.15, term='spot', performance={'day': '+0.21%'}),
            FinancialEvent(timestamp_utc=now.isoformat(), date=now.strftime("%Y-%m-%d"),
                           asset_name='S&P 500', asset_ticker='^GSPC', asset_type='index',
                           price=5315.70, term='spot', performance={'day': '+0.35%'})
        ],
        'NASDAQ': [
            FinancialEvent(timestamp_utc=now.isoformat(), date=now.strftime("%Y-%m-%d"),
                           asset_name='NASDAQ 100', asset_ticker='^NDX', asset_type='index',
                           price=18750.40, term='spot', performance={'day': '-0.11%'})
        ],
        'LSE': [
            FinancialEvent(timestamp_utc=now.isoformat(), date=now.strftime("%Y-%m-%d"),
                           asset_name='FTSE 100', asset_ticker='^FTSE', asset_type='index',
                           price=8235.50, term='spot', performance={'day': '+0.52%'})
        ],
        'FWB': [
            FinancialEvent(timestamp_utc=now.isoformat(), date=now.strftime("%Y-%m-%d"),
                           asset_name='DAX', asset_ticker='^GDAXI', asset_type='index',
                           price=18710.22, term='spot', performance={'day': '+0.88%'})
        ],
        'TSE': [
            FinancialEvent(timestamp_utc=now.isoformat(), date=now.strftime("%Y-%m-%d"),
                           asset_name='Nikkei 225', asset_ticker='^N225', asset_type='index',
                           price=39015.87, term='spot', performance={'day': '-0.15%'})
        ],
        'HKEX': [
            FinancialEvent(timestamp_utc=now.isoformat(), date=now.strftime("%Y-%m-%d"),
                           asset_name='Hang Seng', asset_ticker='^HSI', asset_type='index',
                           price=18511.01, term='spot', performance={'day': '+1.23%'})
        ]
    }
    return data.get(exchange_code, [])

# ---------------------------------------------------------------------------
# HTML MARKERS & TICKER
# ---------------------------------------------------------------------------
def _ce(tag, class_name=None, parent=None, text=None):
    e = ET.Element(tag)
    if class_name: e.set('class', class_name)
    if text: e.text = text
    if parent is not None: parent.append(e)
    return e

def create_exchange_html_marker(exchange_name: str, event: FinancialEvent):
    day_perf = event.performance.get('day', '0.0%')
    color = '#33ff99' if '+' in day_perf else '#ff4d4d'
    el = _ce('div', 'globe-marker exchange-marker')
    _ce('div', 'exchange-name', el, text=exchange_name)
    _ce('div', 'index-name', el, text=event.asset_name)
    price_container = _ce('div', 'price-container', el)
    _ce('span', 'asset-price', price_container, text=f"{event.price:,.2f}")
    perf_el = _ce('span', 'asset-perf', price_container, text=day_perf)
    perf_el.set('style', f'color: {color};')
    return ET.tostring(el, method='html', encoding='unicode')

def format_events_for_ticker(events: List[FinancialEvent]) -> str:
    parts = []
    for event in events:
        perf_indicator = "▲" if '+' in event.performance.get('day', '') else "▼"
        parts.append(f"{event.asset_name.upper()} {event.price:,.2f} {perf_indicator} {event.performance.get('day', '0.0%')}")
    return "   |   ".join(parts)

# ---------------------------------------------------------------------------
# HEX & LABEL HELPERS (non-interactive, broadcast-visible)
# ---------------------------------------------------------------------------
def _pct_str_to_float(pct: str) -> float:
    s = (pct or "0").strip().replace('%', '')
    try:
        return float(s) / 100.0
    except ValueError:
        return 0.0

def _global_max_abs_move() -> float:
    maxv = 0.0
    for code in EXCHANGE_LOCATIONS.keys():
        evs = get_placeholder_indices(code)
        if not evs: continue
        mv = abs(_pct_str_to_float(evs[0].performance.get('day', '0%')))
        if mv > maxv: maxv = mv
    return maxv or 1.0

def _hex_point_for_exchange(code: str, norm_den: float) -> dict:
    ex = EXCHANGE_LOCATIONS[code]
    ev = get_placeholder_indices(code)[0]
    day = _pct_str_to_float(ev.performance.get('day', '0%'))
    weight = min(abs(day) / norm_den, 1.0)  # 0..1
    sign = 1 if day >= 0 else -1
    arrow = "▲" if sign > 0 else "▼"
    pct_str = ev.performance.get('day', '0%')
    return {
        'id': f'hex_{code}',
        'lat': ex['lat'],
        'lng': ex['lng'],
        'weight': weight,  # drives height
        'sign': sign,      # drives color
        'label': f"{ex['name']} — {ev.asset_name}: {arrow} {pct_str} • {ev.price:,.2f}"
    }

def _label_for_exchange(code: str, norm_den: float) -> dict:
    """
    Label sits at the TOP of the hex and includes market name/info.
    hexAltitude = max(weight * 0.6, 0.012)
    labelAltitude = hexAltitude + offset
    """
    ex = EXCHANGE_LOCATIONS[code]
    ev = get_placeholder_indices(code)[0]
    day = _pct_str_to_float(ev.performance.get('day', '0%'))
    weight = min(abs(day) / norm_den, 1.0)
    sign = 1 if day >= 0 else -1
    arrow = "▲" if sign > 0 else "▼"
    pct_str = ev.performance.get('day', '0%')
    color = '#33ff99' if sign > 0 else '#ff4d4d'

    # Mirror JS hexAltitude formula from the layer init
    hex_altitude = max(weight * 0.6, 0.012)
    label_offset = 0.02
    label_altitude = hex_altitude + label_offset

    # Include market name + index + price + % in the visible label
    text = f"{ex['name']} — {ev.asset_name}: {arrow} {pct_str} • {ev.price:,.2f}"

    return {
        'id': f'label_{code}',
        'lat': ex['lat'],
        'lng': ex['lng'],
        'text': text,
        'color': color,
        'size': 0.8,                 # slightly smaller to fit longer text
        'altitude': label_altitude,  # sits above hex top
        'includeDot': False
    }

# ---------------------------------------------------------------------------
# DIRECTOR SCRIPT
# ---------------------------------------------------------------------------
def run_high_fidelity_market_report():
    controller = FrontendController()

    tour_plan = [
        {'city': 'Tokyo & Hong Kong', 'exchanges': ['TSE', 'HKEX'], 'region_alt': 1.8},
        {'city': 'London & Frankfurt', 'exchanges': ['LSE', 'FWB'], 'region_alt': 1.9},
        {'city': 'New York', 'exchanges': ['NYSE', 'NASDAQ'], 'region_alt': 2.2}
    ]

    # Will track IDs so we can clean up at the very end
    all_hex_ids = []
    all_label_ids = []

    # --- SETUP ---
    controller.log_message("Director AI: Rolling High-Fidelity Market Report.") \
        .set_state("chyronText", "GLOBAL MARKETS: IN FOCUS") \
        .set_state("tickerText", f"Live Report: {datetime.datetime.now().strftime('%A, %B %d, %Y, %I:%M %p EDT')}") \
        .set_state("htmlData", []) \
        .set_state("ringsData", []) \
        .set_state("hexBinData", []) \
        .set_state("labelsData", []) \
        .initialize_layer('html', {
            'htmlElement': "d => { const el = document.createElement('div'); el.innerHTML = d.html.trim(); return el.firstChild; }",
            'htmlTransitionDuration': 300
        }) \
        .initialize_layer('rings', {
            'ringColor': "() => '#00aaff'",
            'ringMaxRadius': 5,
            'ringPropagationSpeed': 2,
            'ringRepeatPeriod': 1200
        }) \
        .initialize_layer('hex', {
            'hexBinPointLat':    "(p) => p.lat",
            'hexBinPointLng':    "(p) => p.lng",
            'hexBinPointWeight': "(p) => p.weight",     # 0..1
            'hexBinResolution':  4,
            'hexMargin':         0.15,
            'hexAltitude':       "(d) => Math.max(d.sumWeight * 0.6, 0.012)",
            'hexTopColor':       "(d) => ((d.points?.[0]?.sign ?? 1) > 0 ? '#33ff99' : '#ff4d4d')",
            'hexSideColor':      "(d) => ((d.points?.[0]?.sign ?? 1) > 0 ? 'rgba(51,255,153,0.55)' : 'rgba(255,77,77,0.55)')",
            'hexLabel':          "(d) => d.points?.[0]?.label ?? ''",
            'hexBinMerge':       True
        }) \
        .initialize_layer('labels', {
            'labelText': 'text',
            'labelLat': 'lat',
            'labelLng': 'lng',
            'labelColor': 'color',
            'labelSize': 'size',
            'labelAltitude': 'altitude',
            'labelIncludeDot': 'includeDot',
            'labelsTransitionDuration': 300
        }) \
        .pan_to_globe_location(25, 15, 2000, 1.5) \
        .set_sleep(4)

    # Global normalization so all candles are comparable
    norm_den = _global_max_abs_move()

    # --- TOUR ---
    for city_tour in tour_plan:
        first_exchange = EXCHANGE_LOCATIONS[city_tour['exchanges'][0]]

        controller.set_state("chyronText", f"MARKET ACTIVITY: {first_exchange['region'].upper()}") \
            .pan_to_globe_location(first_exchange['lat'], first_exchange['lng'], 4000, city_tour['region_alt']) \
            .add_elements('rings', [{'id': f"ring_{first_exchange['city']}", 'lat': first_exchange['lat'], 'lng': first_exchange['lng']}]) \
            .refresh_layer('rings') \
            .set_sleep(3)

        for code in city_tour['exchanges']:
            ex = EXCHANGE_LOCATIONS[code]
            indices = get_placeholder_indices(code)

            controller.set_state("chyronText", ex['name']) \
                .pan_to_globe_location(ex['lat'], ex['lng'], 2500, 0.58) \
                .set_sleep(2)

            # HTML overlay + ticker
            marker_id = f"marker_{code}"
            marker_html = create_exchange_html_marker(ex['name'], indices[0])
            ticker_content = format_events_for_ticker(indices)

            controller.add_elements('html', [{
                'id': marker_id, 'lat': ex['lat'], 'lng': ex['lng'], 'html': marker_html
            }]).refresh_layer('html').set_state("tickerText", ticker_content).set_sleep(1)

            # Add (and keep) hex candle + top label until the very end
            hex_pt = _hex_point_for_exchange(code, norm_den)
            lbl_pt = _label_for_exchange(code, norm_den)

            controller.add_elements('hex', [hex_pt]).refresh_layer('hex') \
                      .add_elements('labels', [lbl_pt]).refresh_layer('labels')

            all_hex_ids.append(hex_pt['id'])
            all_label_ids.append(lbl_pt['id'])

            # Remove only the HTML marker as we move on
            controller.remove_elements('html', [marker_id]).refresh_layer('html') \
                      .set_sleep(1)

        controller.remove_elements('rings', [f"ring_{first_exchange['city']}"]).refresh_layer('rings').set_sleep(1)

    # --- CLEAN UP ALL HEXES/LABELS ONCE (end of sequence) ---
    if all_hex_ids:
        controller.remove_elements('hex', all_hex_ids).refresh_layer('hex')
    if all_label_ids:
        controller.remove_elements('labels', all_label_ids).refresh_layer('labels')

    # --- OUTRO ---
    controller.set_state("chyronText", "CLOSING BELL SUMMARY") \
        .set_state("tickerText", "Global markets show mixed results as investors await key economic data.") \
        .pan_to_globe_location(38.90, -77.03, 6000, 1.8) \
        .set_sleep(8) \
        .set_state("chyronText", "") \
        .set_state("tickerText", "") \
        .log_message("Director AI: High-Fidelity Market Report Concluded.")

    return controller.build()

# ---------------------------------------------------------------------------
# EXECUTION (broadcast-friendly; zero pointer interaction)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("Generating broadcast script for the High-Fidelity Market Report (Hex Candles w/ Top Labels incl. Market Info)...")
    script_to_send = run_high_fidelity_market_report()

    try:
        from machine.sdk.pds.lib.components.utils.connection_manager import ConnectionManager
        ConnectionManager().broadcast(script_content=script_to_send)
    except Exception as e:
        print(f"[warn] Broadcast not executed here: {e}")

    print("\n--- GENERATED SCRIPT (Hex Candles) ---")
    print(script_to_send)
    print("\n--- SCRIPT END ---")
