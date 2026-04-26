import json
import textwrap
from lxml import etree as ET


def _ce(tag, class_name=None, parent=None, text=None, style=None):
    e = ET.Element(tag)
    if class_name:
        e.set('class', class_name)
    if text:
        e.text = text
    if parent is not None:
        parent.append(e)
    if style:
        style_str = '; '.join([f'{key}: {value}' for key, value in style.items()])
        e.set('style', style_str)
    return e


def create_continent_marker(marker):
    el_style = {
        'position': 'relative',
        'pointer-events': 'none',
        'transition': 'opacity 0.5s ease'
    }

    estimated_info_text_width = len(marker.get('infoText', '')) * 8

    base_class = f"globe-marker marker-type-{marker.get('type', 'default')}"
    el_class = f"{base_class} {'continent-marker' if marker.get('isContinent') else 'city-marker'}"

    el = _ce('div', class_name=el_class, style=el_style)
    el.set('style', el.get('style') + '; opacity: 1;')

    container = _ce('div', 'marker-container', el)
    _ce('div', 'location-name', container, marker.get('name'))
    badge = _ce('div', 'marker-badge continent-badge', container)

    _ce('div', 'broadcast-pulse', badge)

    _ce('span', 'marker-emoji', badge, marker.get('emoji'))

    ticker_wrapper_style = {'width': f'{estimated_info_text_width}px'}
    ticker_wrapper = _ce('div', 'ticker-wrapper', badge, style=ticker_wrapper_style)

    ticker_item = _ce('div', 'ticker-item current', ticker_wrapper)
    _ce('span', 'info-text', ticker_item, marker.get('infoText', ''), style={'color': '#ffffff'})

    return ET.tostring(el, pretty_print=True, method='html', encoding='unicode')


from lxml import etree as ET


def _ce(tag, class_name=None, parent=None, text=None, style=None):
    e = ET.Element(tag)
    if class_name:
        e.set('class', class_name)
    if text:
        e.text = text
    if parent is not None:
        parent.append(e)
    if style:
        style_str = '; '.join([f'{key}: {value}' for key, value in style.items()])
        e.set('style', style_str)
    return e


def create_city_marker(marker):
    el_style = {
        'position': 'relative',
        'pointer-events': 'none',
        'transition': 'opacity 0.5s ease'
    }

    base_class = f"globe-marker marker-type-{marker.get('type', 'default')}"
    el_class = f"{base_class} {'continent-marker' if marker.get('isContinent') else 'city-marker'}"

    el = _ce('div', class_name=el_class, style=el_style)
    el.set('style', el.get('style') + '; opacity: 1;')

    container = _ce('div', 'marker-container', el)

    badge_style = {
        'flex-direction': 'column',
        'align-items': 'center'
    }
    badge = _ce('div', 'marker-badge city-badge', container, style=badge_style)

    emoji_style = {
        'margin-right': '0',
        'margin-bottom': '4px'
    }
    _ce('span', 'marker-emoji', badge, marker.get('emoji'), style=emoji_style)

    ticker_wrapper_style = {
        'width': '100%',
        'text-align': 'center'
    }
    ticker_wrapper = _ce('div', 'ticker-wrapper', badge, style=ticker_wrapper_style)

    ticker_item_style = {
        'width': '100%',
        'justify-content': 'center',
        'left': '0',
        'right': '0'
    }
    ticker_item = _ce('div', 'ticker-item current', ticker_wrapper, style=ticker_item_style)

    text_style = {
        'display': 'block',
        'text-align': 'center',
        'color': '#ffffff'
    }
    _ce('span', 'info-text', ticker_item, marker.get('infoText', ''), style=text_style)

    _ce('div', 'location-name', container, marker.get('name'))

    return ET.tostring(el, pretty_print=True, method='html', encoding='unicode')

def create_weather_marker(marker):
    el_style = {
        'position': 'relative',
        'pointer-events': 'none',
        'transition': 'opacity 0.5s ease'
    }

    base_class = f"globe-marker marker-type-{marker.get('type', 'default')}"
    el_class = f"{base_class} {'continent-marker' if marker.get('isContinent') else 'city-marker'}"

    el = _ce('div', class_name=el_class, style=el_style)
    el.set('style', el.get('style') + '; opacity: 1;')

    container = _ce('div', 'marker-container', el)

    badge_style = {
        'flex-direction': 'column',
        'align-items': 'center'
    }
    badge = _ce('div', 'marker-badge city-badge', container, style=badge_style)

    emoji_style = {
        'margin-right': '0',
        'margin-bottom': '4px'
    }
    _ce('span', 'marker-emoji', badge, marker.get('emoji'), style=emoji_style)

    ticker_wrapper_style = {
        'width': '100%',
        'text-align': 'center'
    }
    ticker_wrapper = _ce('div', 'ticker-wrapper', badge, style=ticker_wrapper_style)

    ticker_item_style = {
        'width': '100%',
        'justify-content': 'center',
        'left': '0',
        'right': '0'
    }
    ticker_item = _ce('div', 'ticker-item current', ticker_wrapper, style=ticker_item_style)

    text_style = {
        'display': 'block',
        'text-align': 'center',
        'color': '#ffffff'
    }
    _ce('span', 'info-text', ticker_item, marker.get('infoText', ''), style=text_style)

    _ce('div', 'location-name', container, marker.get('name'))

    return ET.tostring(el, pretty_print=True, method='html', encoding='unicode')
