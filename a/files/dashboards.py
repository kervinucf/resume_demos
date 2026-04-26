from decimal import Decimal
from typing import List, Dict, Optional
from datetime import timezone, timedelta
import time
import datetime
from datetime import datetime as dt
import math


# -------------------------------------------------------------------------
# DATA TRANSFER OBJECT
# -------------------------------------------------------------------------
class FinancialEvent:
    def __init__(self, timestamp_utc, date, asset_name, asset_ticker, asset_type, price, term, performance, uid):
        self.timestamp_utc = timestamp_utc
        self.date = date
        self.asset_name = asset_name
        self.asset_ticker = asset_ticker
        self.asset_type = asset_type
        self.price = price
        self.term = term
        self.performance = performance
        self.uid = uid


# -------------------------------------------------------------------------
# CONFIGURATION
# -------------------------------------------------------------------------
CURRENCIES_BY_CONTINENT = {
    'NA': ['CAD', 'MXN', 'USD', 'BZD', 'HTG', 'DOP', 'JMD', 'TTD', 'BSD', 'CUP', 'NIO', 'CRC', 'GTQ', 'HNL', 'PAB'],
    'SA': ['BRL', 'ARS', 'CLP', 'COP', 'PEN', 'UYU', 'PYG', 'BOB', 'GYD', 'SRD'],
    'EU': ['EUR', 'GBP', 'CHF', 'NOK', 'SEK', 'DKK', 'PLN', 'CZK', 'HUF', 'RON', 'BGN', 'ALL', 'MKD', 'RSD'],
    'AF': ['ZAR', 'NGN', 'KES', 'EGP', 'MAD', 'GHS', 'TZS', 'UGX', 'DZD', 'NAD', 'MWK', 'MZN', 'SZL', 'LYD'],
    'AS': ['JPY', 'CNY', 'INR', 'KRW', 'HKD', 'SGD', 'MYR', 'THB', 'IDR', 'PHP', 'PKR', 'BDT', 'VND', 'TWD', 'AED',
           'SAR', 'ILS', 'TRY'],
    'OC': ['AUD', 'NZD', 'PGK', 'WST', 'TOP', 'VUV']
}


def _get_region_flag(name: str) -> str:
    """Map asset name to region flag emoji for visual scanning."""
    flags = {
        'USA': '🇺🇸', 'United States': '🇺🇸', 'Canada': '🇨🇦', 'Mexico': '🇲🇽', 'UK': '🇬🇧',
        'Germany': '🇩🇪', 'France': '🇫🇷', 'Eurozone': '🇪🇺', 'Japan': '🇯🇵',
        'China': '🇨🇳', 'Hong Kong': '🇭🇰', 'India': '🇮🇳', 'South Korea': '🇰🇷',
        'Australia': '🇦🇺', 'Brazil': '🇧🇷', 'Switzerland': '🇨🇭', 'Russia': '🇷🇺',
        'Saudi Arabia': '🇸🇦', 'Singapore': '🇸🇬', 'South Africa': '🇿🇦',
        'Turkey': '🇹🇷', 'Türkiye': '🇹🇷', 'Italy': '🇮🇹', 'Spain': '🇪🇸'
    }
    for k, v in flags.items():
        if k in name: return v
    return '🌐'


# -------------------------------------------------------------------------
# STYLING ENGINE: "THE TERMINAL"
# -------------------------------------------------------------------------
def _get_terminal_styles(dark_mode: bool) -> str:
    """
    CSS optimized for high-density information display.
    References: Bloomberg Terminal, Eikon, Fox Business Ticker.
    """
    # Color Palette: Professional, High Contrast
    bg = '#000000' if dark_mode else '#ffffff'
    fg = '#e0e0e0' if dark_mode else '#121212'
    header_bg = '#1a1a1a' if dark_mode else '#00264d'  # Dark Grey or Navy Blue
    header_fg = '#fefefe' if dark_mode else '#ffffff'  # Bloomberg Amber or White

    grid = '#333333' if dark_mode else '#cccccc'

    # Financial Colors (Standardized)
    pos_bg = '#003300' if dark_mode else '#e6ffe6'
    pos_fg = '#00ff00' if dark_mode else '#006600'
    neg_bg = '#330000' if dark_mode else '#ffe6e6'
    neg_fg = '#ff3333' if dark_mode else '#cc0000'

    return f'''
    @import url('https://fonts.googleapis.com/css2?family=Roboto+Condensed:wght@700&family=JetBrains+Mono:wght@400;700&display=swap');

    :root {{
        --bg: {bg};
        --fg: {fg};
        --header-bg: {header_bg};
        --header-fg: {header_fg};
        --grid: {grid};
        --pos-fg: {pos_fg}; --pos-bg: {pos_bg};
        --neg-fg: {neg_fg}; --neg-bg: {neg_bg};
    }}

    * {{ box-sizing: border-box; }}

    body, .root-container {{
        background-color: var(--bg);
        color: var(--fg);
        font-family: 'JetBrains Mono', monospace; /* Data is code */
        font-size: 12px;
        margin: 0; padding: 0;
        height: 100%; width: 100%;
        overflow: hidden;
        display: flex; flex-direction: column;
    }}

    /* HEADER: The "Chyron" Look */
    .header {{
        background-color: var(--header-bg);
        color: var(--header-fg);
        padding: 8px 12px;
        border-bottom: 2px solid var(--header-fg);
        display: flex; justify-content: space-between; align-items: baseline;
    }}

    .title {{
        font-family: 'Roboto Condensed', sans-serif;
        text-transform: uppercase;
        font-size: 18px;
        letter-spacing: 0.5px;
    }}

    .meta {{ font-size: 10px; opacity: 0.8; }}

    /* DATA GRID */
    .content {{ flex: 1; overflow-y: auto; }}
    table {{ 
        width: 100%; 
        border-collapse: collapse; 
        table-layout: fixed; /* Strict layout for alignment */
    }}

    th {{
        text-align: left;
        font-family: 'Roboto Condensed', sans-serif;
        font-size: 11px;
        text-transform: uppercase;
        background: #222; /* Darker header within content */
        color: #888;
        padding: 4px 8px;
        border-bottom: 1px solid var(--grid);
        position: sticky; top: 0;
        z-index: 2;
    }}

    td {{
        padding: 4px 8px;
        border-bottom: 1px solid var(--grid);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }}

    .row-even {{ background-color: rgba(255,255,255,0.02); }}

    /* UTILITIES */
    .text-right {{ text-align: right; }}
    .ticker {{ font-weight: 700; color: #fff; }}
    .name {{ color: white; font-size: 11px; }}
    .val {{ font-weight: 700; }}

    .pos {{ color: var(--pos-fg); background-color: var(--pos-bg); }}
    .neg {{ color: var(--neg-fg); background-color: var(--neg-bg); }}
    .neu {{ color: #888; }}

    /* CHART BARS */
    .bar-container {{
        display: flex; align-items: center; height: 100%; width: 100%;
    }}
    .bar-wrapper {{
        flex: 1; background: #222; height: 14px; position: relative;
    }}
    .bar-fill {{
        height: 100%; display: block;
    }}
    '''


# -------------------------------------------------------------------------
# GENERATORS
# -------------------------------------------------------------------------

def _fmt_price(val) -> str:
    if val is None: return ""
    v = float(val)
    if v == 0: return "0.00"
    if v < 1: return f"{v:.5f}"
    if v > 1000: return f"{v:,.2f}"
    return f"{v:,.4f}"


def _generate_board_html(data: List[Dict], term: str, dark_mode: bool, title_suffix: str = "") -> str:
    rows = []
    for idx, item in enumerate(data):
        # Format Gain
        gain = item.get('gain', 0)
        gain_str = f"{gain:+.2f}%"
        if gain > 0:
            cls = "pos"
        elif gain < 0:
            cls = "neg"
        else:
            cls = "neu"

        # Format Price
        price = _fmt_price(item['terms_prices'].get('Last'))

        flag = item['region']
        row_cls = "row-even" if idx % 2 == 0 else ""

        if item['ticker'] != item['name']:
            name_html = f"<span class='name'>{item['ticker']} - {item['name']}</span>"
        else:
            name_html = f"<span class='name'>{item['name']}</span>"

        rows.append(f'''
            <tr class="{row_cls}">
                <td style="width: 35%">
                    <span style="font-size:14px; margin-right:4px;">{flag}</span>
                    {name_html}
                </td>
                <td class="text-right val" style="width: 20%">{price}</td>
                <td class="text-right val {cls}" style="width: 20%">{gain_str}</td>
            </tr>
        ''')

    return f'''
    <div class="root-container">
        <style>{_get_terminal_styles(dark_mode)}</style>
        <div class="header">
            <span class="title">MARKET DATA // {title_suffix}</span>
            <span class="meta">{term.upper()} • {len(data)} ITEMS</span>
        </div>
        <div class="content">
            <table>
                <thead>
                    <tr><th>ASSET NAME</th><th class="text-right">PRICE</th><th class="text-right">CHG %</th></tr>
                </thead>
                <tbody>{''.join(rows)}</tbody>
            </table>
        </div>
    </div>
    '''


def _generate_performance_bar_chart(data: List[Dict], term: str, chart_type: str, dark_mode: bool) -> str:
    """Horizontal Bar Chart - Preferred for Professional Scanning"""
    filtered = []
    for x in data:
        try:
            val = float(str(x['performance'].get(term, 0)).replace('%', '').replace('+', ''))
            if (chart_type == 'Gainers' and val > 0) or (chart_type == 'Losers' and val < 0):
                filtered.append({**x, 'val': val})
        except:
            continue

    filtered.sort(key=lambda x: abs(x['val']), reverse=True)
    filtered = filtered[:15]  # Strict limit for screen density

    if not filtered: return f'<div class="root-container"><div style="padding:20px">NO DATA</div></div>'

    max_val = max([abs(x['val']) for x in filtered]) if filtered else 1

    rows = []
    for idx, item in enumerate(filtered):
        pct = (abs(item['val']) / max_val) * 100
        color = "var(--pos-fg)" if item['val'] > 0 else "var(--neg-fg)"
        row_cls = "row-even" if idx % 2 == 0 else ""

        rows.append(f'''
            <tr class="{row_cls}">
                <td style="width: 15%"><span class="ticker">{item['ticker']}</span></td>
                <td style="width: 15%"><span class="val" style="color:{color}">{item['val']:+.2f}%</span></td>
                <td style="width: 70%">
                    <div class="bar-container">
                        <div class="bar-wrapper">
                            <div class="bar-fill" style="width: {pct}%; background-color: {color};"></div>
                        </div>
                    </div>
                </td>
            </tr>
        ''')

    return f'''
    <div class="root-container">
        <style>{_get_terminal_styles(dark_mode)}</style>
        <div class="header">
            <span class="title">MOMENTUM // {chart_type.upper()}</span>
            <span class="meta">{term} INTERVAL</span>
        </div>
        <div class="content">
            <table>
                <tbody>{''.join(rows)}</tbody>
            </table>
        </div>
    </div>
    '''


def _generate_comparison_chart(data: List[Dict], dark_mode: bool) -> str:
    """Heatmap Matrix Style"""
    rows = []
    terms = ['1M', '3M', '6M', '1Y']

    for idx, item in enumerate(data[:20]):  # Top 20 relevant assets only
        cells = []
        for t in terms:
            try:
                val = float(str(item['performance'].get(t, 0)).replace('%', '').replace('+', ''))
                if val > 0:
                    cls = "pos"
                elif val < 0:
                    cls = "neg"
                else:
                    cls = "neu"
                cells.append(f'<td class="text-right val {cls}">{val:+.1f}%</td>')
            except:
                cells.append('<td class="text-right neu">--</td>')

        row_cls = "row-even" if idx % 2 == 0 else ""
        rows.append(f'''
            <tr class="{row_cls}">
                <td class="ticker">{item['ticker']}</td>
                {''.join(cells)}
            </tr>
        ''')

    return f'''
    <div class="root-container">
        <style>{_get_terminal_styles(dark_mode)}</style>
        <div class="header">
            <span class="title">TERM MATRIX // CROSS-ASSET</span>
        </div>
        <div class="content">
            <table>
                <thead>
                    <tr><th>ASSET</th>{''.join(f'<th class="text-right">{t}</th>' for t in terms)}</tr>
                </thead>
                <tbody>{''.join(rows)}</tbody>
            </table>
        </div>
    </div>
    '''


def _generate_currency_board_html(data: List[Dict], dark_mode: bool, title: str) -> str:
    """Specialized View for FX"""
    return _generate_board_html(data, "Last", dark_mode, title_suffix=title.upper())


# -------------------------------------------------------------------------
# MAIN ORCHESTRATOR
# -------------------------------------------------------------------------
def generate_financial_dashboard(events: List, term: str = '1Y', dark_mode: bool = False) -> Dict[str, str]:
    # 1. Parse Data into uniform Dictionary structure
    assets = {}
    for event in events:
        # Assuming event object based on class definition provided
        name = event.asset_name
        if name not in assets:
            assets[name] = {
                'name': name, 'ticker': event.asset_ticker,
                'region': _get_region_flag(name),
                'type': event.asset_type,
                'terms_prices': {}, 'performance': {}
            }
        if event.performance:
            assets[name]['performance'].update(event.performance)
        if event.price:
            assets[name]['terms_prices'][event.term] = float(event.price)

    data_list = list(assets.values())

    # 2. Compute Sort Key (Gain)
    for asset in data_list:
        try:
            val_str = asset['performance'].get(term)
            asset['gain'] = float(str(val_str).replace('%', '').replace('+', '')) if val_str else 0.0
        except:
            asset['gain'] = 0.0

    # 3. Segregate Lists
    currencies = [x for x in data_list if x['type'] == 'Currency']
    # If no type data, treat all as generic
    if not currencies and data_list: currencies = []

    non_currencies = [x for x in data_list if x['type'] != 'Currency']

    # Sorts
    all_sorted = sorted(data_list, key=lambda x: x['gain'], reverse=True)
    curr_sorted = sorted(currencies, key=lambda x: x['gain'], reverse=True)

    # 4. Generate Output Dict
    output = {}

    # Boards (Tables)
    output['leaderboard'] = _generate_board_html(all_sorted, term, dark_mode, "OVERVIEW")
    output['gainer_board'] = _generate_board_html(all_sorted[:20], term, dark_mode, "TOP PERFORMERS")
    output['loser_board'] = _generate_board_html(sorted(all_sorted, key=lambda x: x['gain'])[:20], term, dark_mode,
                                                 "LAGGARDS")

    # Charts (Visuals)
    output['gainer_chart'] = _generate_performance_bar_chart(data_list, term, 'Gainers', dark_mode)
    output['loser_chart'] = _generate_performance_bar_chart(data_list, term, 'Losers', dark_mode)
    output['comparison_chart'] = _generate_comparison_chart(data_list, dark_mode)

    # FX Specific
    output['currency_board_best'] = _generate_currency_board_html(curr_sorted[:15], dark_mode, "FX: Strong")
    output['currency_board_worst'] = _generate_currency_board_html(sorted(currencies, key=lambda x: x['gain'])[:15],
                                                                   dark_mode, "FX: Weak")

    # Regional
    for code, tickers in CURRENCIES_BY_CONTINENT.items():
        subset = [c for c in currencies if c['ticker'] in tickers]
        output[f"currency_board_{code.lower()}"] = _generate_currency_board_html(subset, dark_mode, f"Region: {code}")

    return output


# -------------------------------------------------------------------------
# EXECUTION TEST
# -------------------------------------------------------------------------
if __name__ == "__main__":
    test_events = [
        FinancialEvent(dt.now(), '2025-10-18', 'Bitcoin', 'BTC', 'Crypto', Decimal('95000'), 'Last', {'1Y': '+120.5%'},
                       '1'),
        FinancialEvent(dt.now(), '2025-10-18', 'Euro', 'EUR', 'Currency', Decimal('1.05'), 'Last', {'1Y': '-2.1%'},
                       '2'),
        FinancialEvent(dt.now(), '2025-10-18', 'NVIDIA Corp', 'NVDA', 'Stock', Decimal('145.20'), 'Last',
                       {'1Y': '+210.0%'}, '3'),
        FinancialEvent(dt.now(), '2025-10-18', 'Gold', 'XAU', 'Commodity', Decimal('2650.00'), 'Last', {'1Y': '+15.2%'},
                       '4'),
    ]

    dashboards = generate_financial_dashboard(test_events, term='1Y', dark_mode=True)

    # Verify Interface
    print(f"Generated Keys: {list(dashboards.keys())}")

    with open("terminal_leaderboard.html", "w", encoding="utf-8") as f:
        f.write(dashboards['leaderboard'])
    print("Saved terminal_leaderboard.html")