import random
from decimal import Decimal
from typing import List, Dict
import datetime

# --- [I] DTO AND HELPERS (Unchanged) ---
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


def _get_region_flag(name: str) -> str:
    flags = {
        'USA': '🇺🇸', 'Canada': '🇨🇦', 'Mexico': '🇲🇽', 'UK': '🇬🇧', 'Germany': '🇩🇪', 'France': '🇫🇷',
        'Eurozone': '🇪🇺', 'Japan': '🇯🇵', 'China': '🇨🇳', 'Hong Kong': '🇭🇰', 'India': '🇮🇳',
        'South Korea': '🇰🇷', 'Australia': '🇦🇺', 'New Zealand': '🇳🇿', 'South Africa': '🇿🇦',
        'Brazil': '🇧🇷', 'Argentina': '🇦🇷', 'Switzerland': '🇨🇭', 'Singapore': '🇸🇬'
    }
    for country, flag in flags.items():
        if country in name: return flag
    if 'United Kingdom' in name: return '🇬🇧'
    return '🌐'


# --- [II] PHRASING POOLS AND TEMPLATES (Unchanged from V3) ---
CHYRON_TEMPLATES = {
    "gainer": [
        "TOP GAINER ({term}): {ticker} {region} ▲ {gain:+.2f}% to {price:,.2f}",
        "MARKET LEADER ({term}): {name} posts a {gain:+.2f}% gain, closing at {price:,.2f}.",
        "STRONG MOVER ({term}): A {gain:+.2f}% surge for {ticker} {region}, now trading at {price:,.2f}."
    ],
    "loser": [
        "TOP LOSER ({term}): {ticker} {region} ▼ {gain:+.2f}% to {price:,.2f}",
        "LAGGARD ({term}): {name} sees a {gain:+.2f}% decline, finishing at {price:,.2f}.",
        "WEAKEST PERFORMER ({term}): {ticker} {region} drops {gain:+.2f}% to {price:,.2f}."
    ],
    "currency_strong": [
        "FX STRONG: {ticker} {region} leads against the USD at {price:.4f}.",
        "CURRENCY LEADER: {name} ({ticker}) shows most strength vs. USD.",
    ],
    "currency_weak": [
        "FX WEAK: {ticker} {region} at {price:.2f} per USD.",
        "CURRENCY LAGGARD: 1 USD now buys {price:.2f} {name} ({ticker})."
    ]
}
HEADLINES_MARKET = ["MARKET SUMMARY", "FINANCIAL WRAP", "GLOBAL MARKETS OVERVIEW", "PERFORMANCE SNAPSHOT"]
VERBS_GAIN = ["climbed", "rose", "advanced", "gained", "surged"]
VERBS_LOSE = ["fell", "dropped", "declined", "retreated", "slid"]
DESCRIPTORS_STRONG = ["The standout performer", "The leading gainer", "The top-performing asset"]
DESCRIPTORS_WEAK = ["The biggest laggard", "The top decliner", "The weakest performer"]


# --- [III] DATA PROCESSING AND SUMMARIZATION (Unchanged from V3) ---
def _process_financial_events(events: List[FinancialEvent], term: str) -> Dict:
    assets = {}
    for event in events:
        name = event.asset_name
        if name not in assets:
            assets[name] = {'name': name, 'ticker': event.asset_ticker, 'region': _get_region_flag(name),
                            'type': event.asset_type, 'terms_prices': {}, 'performance': {}}
        if event.performance: assets[name]['performance'].update(event.performance)
        if event.price is not None and event.term: assets[name]['terms_prices'][event.term] = float(event.price)

    performance_assets = [a for a in list(assets.values()) if
                          a.get('type') != 'Currency' and term in a.get('performance', {})]
    for asset in performance_assets:
        try:
            gain_pct = float(str(asset['performance'][term]).replace('%', '').replace('+', ''))
            asset['gain'] = gain_pct
            asset['last_price'] = asset.get('terms_prices', {}).get('Last')
            if asset['last_price'] is not None and asset.get('type') == 'Index':
                asset['point_change'] = asset['last_price'] - (asset['last_price'] / (1 + (gain_pct / 100)))
        except (ValueError, TypeError, KeyError):
            asset['gain'] = None

    return {
        "performance_assets": [a for a in performance_assets if a['gain'] is not None],
        "currency_assets": [a for a in list(assets.values()) if
                            a.get('type') == 'Currency' and 'Last' in a.get('terms_prices', {})]
    }


def summarize_financial_activity(processed_data: Dict, term: str) -> Dict:
    perf_assets = processed_data["performance_assets"]
    if not perf_assets: return {"error": "No performance data."}

    gainers = sorted([a for a in perf_assets if a['gain'] >= 0], key=lambda x: x['gain'], reverse=True)
    losers = sorted([a for a in perf_assets if a['gain'] < 0], key=lambda x: x['gain'])

    return {
        "term": term, "top_gainer": gainers[0] if gainers else None,
        "top_loser": losers[0] if losers else None, "gainer_count": len(gainers),
        "loser_count": len(losers), "most_volatile": max(perf_assets, key=lambda x: abs(x['gain']), default=None),
        "strongest_currency": min(processed_data["currency_assets"], key=lambda x: x['terms_prices']['Last'],
                                  default=None),
        "weakest_currency": max(processed_data["currency_assets"], key=lambda x: x['terms_prices']['Last'],
                                default=None)
    }


# --- [IV & V] V3 NARRATIVE BLOCKS & RECIPES (Kept for narrative option) ---
# ... All _block_ and _recipe_ functions from V3 remain here ...
def _block_top_performer(s: dict) -> str:
    gainer = s.get("top_gainer")
    if not gainer: return None
    price_info = f" to close at {gainer['last_price']:,.2f}" if gainer.get('last_price') is not None else ""
    if gainer.get('point_change') is not None:
        return f"{gainer['name']} {gainer['region']} led the gains, climbing {gainer['gain']:+.2f}%, adding over {gainer['point_change']:.0f} points{price_info}."
    return f"{random.choice(DESCRIPTORS_STRONG)} was {gainer['name']} {gainer['region']}, which {random.choice(VERBS_GAIN)} {gainer['gain']:+.2f}%{price_info}."


def _block_top_laggard(s: dict) -> str:
    loser = s.get("top_loser")
    if not loser: return None
    price_info = f" to {loser['last_price']:,.2f}" if loser.get('last_price') is not None else ""
    return f"On the downside, {random.choice(DESCRIPTORS_WEAK)} was {loser['name']} {loser['region']}, which {random.choice(VERBS_LOSE)} {loser['gain']:+.2f}%{price_info}."


def _block_market_breadth(s: dict) -> str:
    if s['gainer_count'] == 0 and s['loser_count'] == 0: return None
    sentiment = "positive" if s['gainer_count'] > s['loser_count'] else "negative" if s['loser_count'] > s[
        'gainer_count'] else "mixed"
    return f"Market breadth was {sentiment} for the {s['term']}, with {s['gainer_count']} assets rising against {s['loser_count']} falling."


def _block_volatility_focus(s: dict) -> str:
    volatile = s.get("most_volatile")
    if not volatile: return None
    return f"The most significant move was seen in {volatile['name']} {volatile['region']}, which shifted by {volatile['gain']:+.2f}%."


def _block_currency_focus(s: dict) -> str:
    strong, weak = s.get("strongest_currency"), s.get("weakest_currency")
    if not strong or not weak: return None
    return f"IN FOREX MARKETS: The {strong['name']} ({strong['ticker']}) strengthened, while the {weak['name']} ({weak['ticker']}) weakened, with 1 USD now buying {weak['terms_prices']['Last']:.2f} {weak['ticker']}."


def _recipe_classic_broadcast(s: dict) -> str:
    return " ••• ".join(filter(None, [_block_top_performer(s), _block_top_laggard(s), _block_market_breadth(s),
                                      _block_currency_focus(s)]))


def _recipe_breadth_first(s: dict) -> str:
    return " ••• ".join(filter(None,
                               [_block_market_breadth(s), _block_top_performer(s), _block_volatility_focus(s),
                                _block_currency_focus(s)]))


def _recipe_volatility_lead(s: dict) -> str:
    return " ••• ".join(filter(None,
                               [_block_volatility_focus(s), _block_top_performer(s), _block_top_laggard(s),
                                _block_currency_focus(s)]))


# --- [VI] PUBLIC-FACING FUNCTIONS (V3 Narrative + V4 Sequential) ---

def create_financial_chyron(events: List[FinancialEvent], term: str = '1Y') -> Dict[str, str]:
    # ... (Same as before)
    processed = _process_financial_events(events, term)
    summary = summarize_financial_activity(processed, term)
    output = {}
    if gainer := summary.get('top_gainer'):
        template = random.choice(CHYRON_TEMPLATES["gainer"])
        output['gainer_board'] = template.format(term=term, **gainer, price=gainer.get('last_price', 0))
    if loser := summary.get('top_loser'):
        template = random.choice(CHYRON_TEMPLATES["loser"])
        output['loser_board'] = template.format(term=term, **loser, price=loser.get('last_price', 0))
    if strong := summary.get('strongest_currency'):
        template = random.choice(CHYRON_TEMPLATES["currency_strong"])
        output['currency_board_best'] = template.format(**strong, price=strong['terms_prices']['Last'])
    if weak := summary.get('weakest_currency'):
        template = random.choice(CHYRON_TEMPLATES["currency_weak"])
        output['currency_board_worst'] = template.format(**weak, price=weak['terms_prices']['Last'])
    return output


def create_narrative_ticker(events: List[FinancialEvent], term: str = '1Y') -> str:
    # Renamed from create_financial_ticker for clarity
    processed = _process_financial_events(events, term)
    summary = summarize_financial_activity(processed, term)
    if "error" in summary: return summary["error"]

    recipes = [_recipe_classic_broadcast, _recipe_breadth_first, _recipe_volatility_lead]
    return random.choice(recipes)(summary)


# --- NEW V4 SEQUENTIAL TICKER ---
def create_sequential_ticker(events: List[FinancialEvent], term: str = '1Y') -> str:
    """
    Generates a scrolling, data-feed-style ticker that iterates through all
    available assets and currencies.
    """
    processed = _process_financial_events(events, term)
    perf_assets = processed["performance_assets"]
    currencies = processed["currency_assets"]

    ticker_segments = []

    flags = {
        'USA': '🇺🇸', 'Canada': '🇨🇦', 'Mexico': '🇲🇽', 'United Kingdom': '🇬🇧',
        'Germany': '🇩🇪', 'France': '🇫🇷', 'Eurozone': '🇪🇺', 'Japan': '🇯🇵',
        'China': '🇨🇳', 'Hong Kong': '🇭🇰', 'India': '🇮🇳', 'South Korea': '🇰🇷',
        'Australia': '🇦🇺', 'New Zealand': '🇳🇿', 'South Africa': '🇿🇦',
        'Brazil': '🇧🇷', 'Argentina': '🇦🇷', 'Switzerland': '🇨🇭', 'Norway': '🇳🇴',
        'Sweden': '🇸🇪', 'Denmark': '🇩🇰', 'Poland': '🇵🇱', 'Czechia': '🇨🇿',
        'Hungary': '🇭🇺', 'Romania': '🇷🇴', 'Bulgaria': '🇧🇬', 'Albania': '🇦🇱',
        'North Macedonia': '🇲🇰', 'Serbia': '🇷🇸', 'Nigeria': '🇳🇬', 'Kenya': '🇰🇪',
        'Egypt': '🇪🇬', 'Algeria': '🇩🇿', 'Morocco': '🇲🇦', 'Ghana': '🇬🇭',
        'Tanzania': '🇹🇿', 'Uganda': '🇺🇬', 'Namibia': '🇳🇦', 'Malawi': '🇲🇼',
        'Mozambique': '🇲🇿', 'Eswatini': '🇸🇿', 'Libya': '🇱🇾', 'Lesotho': '🇱🇸',
        'Singapore': '🇸🇬', 'Malaysia': '🇲🇾', 'Thailand': '🇹🇭', 'Indonesia': '🇮🇩',
        'Philippines': '🇵🇭', 'Pakistan': '🇵🇰', 'Bangladesh': '🇧🇩', 'Viet Nam': '🇻🇳',
        'Taiwan': '🇹🇼', 'United Arab Emirates': '🇦🇪', 'Saudi Arabia': '🇸🇦',
        'Israel': '🇮🇱', 'Türkiye': '🇹🇷', 'Chile': '🇨🇱', 'Colombia': '🇨🇴',
        'Peru': '🇵🇪', 'Uruguay': '🇺🇾', 'Paraguay': '🇵🇾', 'Bolivia': '🇧🇴',
        'Guyana': '🇬🇾', 'Suriname': '🇸🇷', 'Panama': '🇵🇦', 'Honduras': '🇭🇳',
        'Guatemala': '🇬🇹', 'Costa Rica': '🇨🇷', 'Nicaragua': '🇳🇮', 'Cuba': '🇨🇺',
        'Bahamas': '🇧🇸', 'Trinidad and Tobago': '🇹🇹', 'Jamaica': '🇯🇲',
        'Dominican Republic': '🇩🇴', 'Haiti': '🇭🇹', 'Belize': '🇧🇿',
        'Papua New Guinea': '🇵🇬', 'Samoa': '🇼🇸', 'Tonga': '🇹🇴', 'Vanuatu': '🇻🇺',
    }

    # Process stocks and indices
    for asset in sorted(perf_assets, key=lambda x: x['ticker']):
        arrow = "▲" if asset['gain'] >= 0 else "▼"
        price_str = f"{asset.get('last_price', ''):,.2f}" if asset.get('last_price') else ""
        segment = f"{asset['ticker']} {asset['region']} {arrow} {asset['gain']:+.2f}% {price_str}".strip()
        ticker_segments.append(segment)

    # Process currencies
    for curr in sorted(currencies, key=lambda x: x['ticker']):
        price = curr.get('terms_prices', {}).get('Last')
        if price is not None:
            # For currencies, the price is the focus
            segment = f"{flags.get(curr.get('name'), '🌐')} {curr['ticker']}/USD {price:.4f}"
            if price != 0.0000 and curr.get('ticker') != 'USD':
                ticker_segments.append(segment)

    if not ticker_segments:
        return "No market data available to generate ticker."

    return " | ".join(ticker_segments)
#


# --- DEMONSTRATION ---
if __name__ == "__main__":
    your_events_list = [FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 55, 9, 255000), date='2025-10-20', asset_name='Wheat', asset_ticker='Wheat', asset_type='Commodity', price=Decimal('491.7703733440'), term='Day % (Intraday)', performance={'Hour %': '-0.81%', 'Last Price': '-0.20%', 'Day % (Intraday)': '-0.36%', 'Day % (vs Close)': '+1.34%'}, uid='wheat:commodity:day_%_(intraday):2025-10-20t15:55:09.255000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 55, 9, 238000), date='2025-10-20', asset_name='Corn', asset_ticker='Corn', asset_type='Commodity', price=Decimal('422.9994967287'), term='Day % (Intraday)', performance={'Hour %': '-0.59%', 'Last Price': '+0.06%', 'Day % (Intraday)': '-0.65%', 'Day % (vs Close)': '+2.31%'}, uid='corn:commodity:day_%_(intraday):2025-10-20t15:55:09.238000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 55, 9, 222000), date='2025-10-20', asset_name='Natural Gas', asset_ticker='Natural Gas', asset_type='Commodity', price=Decimal('3.1539711364'), term='Day % (Intraday)', performance={'Hour %': '+4.60%', 'Last Price': '+0.67%', 'Day % (Intraday)': '+4.63%', 'Day % (vs Close)': '+5.91%'}, uid='natural_gas:commodity:day_%_(intraday):2025-10-20t15:55:09.222000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 55, 9, 207000), date='2025-10-20', asset_name='Copper', asset_ticker='Copper', asset_type='Commodity', price=Decimal('4.9821357682'), term='Day % (Intraday)', performance={'Hour %': '+0.76%', 'Last Price': '-0.13%', 'Day % (Intraday)': '+0.76%', 'Day % (vs Close)': '-2.73%'}, uid='copper:commodity:day_%_(intraday):2025-10-20t15:55:09.207000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 55, 9, 192000), date='2025-10-20', asset_name='Silver', asset_ticker='Silver', asset_type='Commodity', price=Decimal('50.3572477244'), term='Day % (Intraday)', performance={'Hour %': '+2.17%', 'Last Price': '+0.34%', 'Day % (Intraday)': '+2.17%', 'Day % (vs Close)': '+2.24%'}, uid='silver:commodity:day_%_(intraday):2025-10-20t15:55:09.192000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 55, 9, 175000), date='2025-10-20', asset_name='Gold', asset_ticker='Gold', asset_type='Commodity', price=Decimal('4250.0730069113'), term='Day % (Intraday)', performance={'Hour %': '+2.75%', 'Last Price': '+0.40%', 'Day % (Intraday)': '+2.73%', 'Day % (vs Close)': '+5.85%'}, uid='gold:commodity:day_%_(intraday):2025-10-20t15:55:09.175000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 55, 9, 156000), date='2025-10-20', asset_name='Crude Oil (WTI)', asset_ticker='Crude Oil (WTI)', asset_type='Commodity', price=Decimal('56.9483662767'), term='Day % (Intraday)', performance={'Hour %': '-0.84%', 'Last Price': '-0.18%', 'Day % (Intraday)': '-0.84%', 'Day % (vs Close)': '-5.09%'}, uid='crude_oil_(wti):commodity:day_%_(intraday):2025-10-20t15:55:09.156000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 55, 9, 135000), date='2025-10-20', asset_name='Bitcoin', asset_ticker='Bitcoin', asset_type='Commodity', price=Decimal('108668.3171206226'), term='Day % (Intraday)', performance={'Hour %': '+2.80%', 'Last Price': '+0.37%', 'Day % (Intraday)': '+2.80%', 'Day % (vs Close)': '-2.44%'}, uid='bitcoin:commodity:day_%_(intraday):2025-10-20t15:55:09.135000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 55, 7, 477000), date='2025-10-20', asset_name='Wheat', asset_ticker='Wheat', asset_type='Commodity', price=Decimal('491.7703733440'), term='Day % (Intraday)', performance={'Hour %': '-0.81%', 'Last Price': '-0.20%', 'Day % (Intraday)': '-0.36%', 'Day % (vs Close)': '+1.34%'}, uid='wheat:commodity:day_%_(intraday):2025-10-20t15:55:07.477000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 55, 7, 455000), date='2025-10-20', asset_name='Corn', asset_ticker='Corn', asset_type='Commodity', price=Decimal('423.0071550942'), term='Day % (Intraday)', performance={'Hour %': '-0.71%', 'Last Price': '-0.06%', 'Day % (Intraday)': '-0.77%', 'Day % (vs Close)': '+2.19%'}, uid='corn:commodity:day_%_(intraday):2025-10-20t15:55:07.455000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 55, 7, 435000), date='2025-10-20', asset_name='Natural Gas', asset_ticker='Natural Gas', asset_type='Commodity', price=Decimal('3.1530670743'), term='Day % (Intraday)', performance={'Hour %': '+4.63%', 'Last Price': '+0.70%', 'Day % (Intraday)': '+4.66%', 'Day % (vs Close)': '+5.94%'}, uid='natural_gas:commodity:day_%_(intraday):2025-10-20t15:55:07.435000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 55, 7, 417000), date='2025-10-20', asset_name='Copper', asset_ticker='Copper', asset_type='Commodity', price=Decimal('4.9821357682'), term='Day % (Intraday)', performance={'Hour %': '+0.76%', 'Last Price': '-0.13%', 'Day % (Intraday)': '+0.76%', 'Day % (vs Close)': '-2.73%'}, uid='copper:commodity:day_%_(intraday):2025-10-20t15:55:07.417000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 55, 7, 399000), date='2025-10-20', asset_name='Silver', asset_ticker='Silver', asset_type='Commodity', price=Decimal('50.3523884103'), term='Day % (Intraday)', performance={'Hour %': '+2.16%', 'Last Price': '+0.33%', 'Day % (Intraday)': '+2.16%', 'Day % (vs Close)': '+2.23%'}, uid='silver:commodity:day_%_(intraday):2025-10-20t15:55:07.399000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 55, 7, 377000), date='2025-10-20', asset_name='Gold', asset_ticker='Gold', asset_type='Commodity', price=Decimal('4249.8783218145'), term='Day % (Intraday)', performance={'Hour %': '+2.74%', 'Last Price': '+0.40%', 'Day % (Intraday)': '+2.73%', 'Day % (vs Close)': '+5.85%'}, uid='gold:commodity:day_%_(intraday):2025-10-20t15:55:07.377000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 55, 7, 326000), date='2025-10-20', asset_name='Crude Oil (WTI)', asset_ticker='Crude Oil (WTI)', asset_type='Commodity', price=Decimal('56.9483662767'), term='Day % (Intraday)', performance={'Hour %': '-0.84%', 'Last Price': '-0.18%', 'Day % (Intraday)': '-0.84%', 'Day % (vs Close)': '-5.09%'}, uid='crude_oil_(wti):commodity:day_%_(intraday):2025-10-20t15:55:07.326000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 55, 7, 303000), date='2025-10-20', asset_name='Bitcoin', asset_ticker='Bitcoin', asset_type='Commodity', price=Decimal('108668.3171206226'), term='Day % (Intraday)', performance={'Hour %': '+2.80%', 'Last Price': '+0.37%', 'Day % (Intraday)': '+2.80%', 'Day % (vs Close)': '-2.44%'}, uid='bitcoin:commodity:day_%_(intraday):2025-10-20t15:55:07.303000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 55, 5, 591000), date='2025-10-20', asset_name='Wheat', asset_ticker='Wheat', asset_type='Commodity', price=Decimal('491.7703733440'), term='Day % (Intraday)', performance={'Hour %': '-0.81%', 'Last Price': '-0.20%', 'Day % (Intraday)': '-0.36%', 'Day % (vs Close)': '+1.34%'}, uid='wheat:commodity:day_%_(intraday):2025-10-20t15:55:05.591000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 55, 5, 576000), date='2025-10-20', asset_name='Corn', asset_ticker='Corn', asset_type='Commodity', price=Decimal('423.0033235975'), term='Day % (Intraday)', performance={'Hour %': '-0.65%', 'Last Price': '+0.00%', 'Day % (Intraday)': '-0.71%', 'Day % (vs Close)': '+2.25%'}, uid='corn:commodity:day_%_(intraday):2025-10-20t15:55:05.576000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 55, 5, 563000), date='2025-10-20', asset_name='Natural Gas', asset_ticker='Natural Gas', asset_type='Commodity', price=Decimal('3.1530670743'), term='Day % (Intraday)', performance={'Hour %': '+4.63%', 'Last Price': '+0.70%', 'Day % (Intraday)': '+4.66%', 'Day % (vs Close)': '+5.94%'}, uid='natural_gas:commodity:day_%_(intraday):2025-10-20t15:55:05.563000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 55, 5, 548000), date='2025-10-20', asset_name='Copper', asset_ticker='Copper', asset_type='Commodity', price=Decimal('4.9821357682'), term='Day % (Intraday)', performance={'Hour %': '+0.76%', 'Last Price': '-0.13%', 'Day % (Intraday)': '+0.76%', 'Day % (vs Close)': '-2.73%'}, uid='copper:commodity:day_%_(intraday):2025-10-20t15:55:05.548000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 55, 5, 532000), date='2025-10-20', asset_name='Silver', asset_ticker='Silver', asset_type='Commodity', price=Decimal('50.3571778060'), term='Day % (Intraday)', performance={'Hour %': '+2.19%', 'Last Price': '+0.36%', 'Day % (Intraday)': '+2.19%', 'Day % (vs Close)': '+2.26%'}, uid='silver:commodity:day_%_(intraday):2025-10-20t15:55:05.532000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 55, 5, 517000), date='2025-10-20', asset_name='Gold', asset_ticker='Gold', asset_type='Commodity', price=Decimal('4249.8783218145'), term='Day % (Intraday)', performance={'Hour %': '+2.74%', 'Last Price': '+0.40%', 'Day % (Intraday)': '+2.73%', 'Day % (vs Close)': '+5.85%'}, uid='gold:commodity:day_%_(intraday):2025-10-20t15:55:05.517000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 55, 5, 497000), date='2025-10-20', asset_name='Crude Oil (WTI)', asset_ticker='Crude Oil (WTI)', asset_type='Commodity', price=Decimal('56.9483662767'), term='Day % (Intraday)', performance={'Hour %': '-0.84%', 'Last Price': '-0.18%', 'Day % (Intraday)': '-0.84%', 'Day % (vs Close)': '-5.09%'}, uid='crude_oil_(wti):commodity:day_%_(intraday):2025-10-20t15:55:05.497000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 55, 5, 473000), date='2025-10-20', asset_name='Bitcoin', asset_ticker='Bitcoin', asset_type='Commodity', price=Decimal('108668.3171206226'), term='Day % (Intraday)', performance={'Hour %': '+2.80%', 'Last Price': '+0.37%', 'Day % (Intraday)': '+2.80%', 'Day % (vs Close)': '-2.44%'}, uid='bitcoin:commodity:day_%_(intraday):2025-10-20t15:55:05.473000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 55, 3, 480000), date='2025-10-20', asset_name='Wheat', asset_ticker='Wheat', asset_type='Commodity', price=Decimal('491.7703733440'), term='Day % (Intraday)', performance={'Hour %': '-0.81%', 'Last Price': '-0.20%', 'Day % (Intraday)': '-0.36%', 'Day % (vs Close)': '+1.34%'}, uid='wheat:commodity:day_%_(intraday):2025-10-20t15:55:03.480000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 55, 3, 468000), date='2025-10-20', asset_name='Corn', asset_ticker='Corn', asset_type='Commodity', price=Decimal('423.0033235975'), term='Day % (Intraday)', performance={'Hour %': '-0.65%', 'Last Price': '+0.00%', 'Day % (Intraday)': '-0.71%', 'Day % (vs Close)': '+2.25%'}, uid='corn:commodity:day_%_(intraday):2025-10-20t15:55:03.468000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 55, 3, 452000), date='2025-10-20', asset_name='Natural Gas', asset_ticker='Natural Gas', asset_type='Commodity', price=Decimal('3.1530670743'), term='Day % (Intraday)', performance={'Hour %': '+4.63%', 'Last Price': '+0.70%', 'Day % (Intraday)': '+4.66%', 'Day % (vs Close)': '+5.94%'}, uid='natural_gas:commodity:day_%_(intraday):2025-10-20t15:55:03.452000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 55, 3, 439000), date='2025-10-20', asset_name='Copper', asset_ticker='Copper', asset_type='Commodity', price=Decimal('4.9905744618'), term='Day % (Intraday)', performance={'Hour %': '+0.79%', 'Last Price': '-0.10%', 'Day % (Intraday)': '+0.79%', 'Day % (vs Close)': '-2.70%'}, uid='copper:commodity:day_%_(intraday):2025-10-20t15:55:03.439000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 55, 3, 428000), date='2025-10-20', asset_name='Silver', asset_ticker='Silver', asset_type='Commodity', price=Decimal('50.3621060873'), term='Day % (Intraday)', performance={'Hour %': '+2.18%', 'Last Price': '+0.35%', 'Day % (Intraday)': '+2.18%', 'Day % (vs Close)': '+2.25%'}, uid='silver:commodity:day_%_(intraday):2025-10-20t15:55:03.428000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 55, 3, 413000), date='2025-10-20', asset_name='Gold', asset_ticker='Gold', asset_type='Commodity', price=Decimal('4250.0730069113'), term='Day % (Intraday)', performance={'Hour %': '+2.75%', 'Last Price': '+0.40%', 'Day % (Intraday)': '+2.73%', 'Day % (vs Close)': '+5.85%'}, uid='gold:commodity:day_%_(intraday):2025-10-20t15:55:03.413000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 55, 3, 395000), date='2025-10-20', asset_name='Crude Oil (WTI)', asset_ticker='Crude Oil (WTI)', asset_type='Commodity', price=Decimal('56.9483662767'), term='Day % (Intraday)', performance={'Hour %': '-0.84%', 'Last Price': '-0.18%', 'Day % (Intraday)': '-0.84%', 'Day % (vs Close)': '-5.09%'}, uid='crude_oil_(wti):commodity:day_%_(intraday):2025-10-20t15:55:03.395000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 55, 3, 375000), date='2025-10-20', asset_name='Bitcoin', asset_ticker='Bitcoin', asset_type='Commodity', price=Decimal('108668.3171206226'), term='Day % (Intraday)', performance={'Hour %': '+2.80%', 'Last Price': '+0.37%', 'Day % (Intraday)': '+2.80%', 'Day % (vs Close)': '-2.44%'}, uid='bitcoin:commodity:day_%_(intraday):2025-10-20t15:55:03.375000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 55, 1, 309000), date='2025-10-20', asset_name='Wheat', asset_ticker='Wheat', asset_type='Commodity', price=Decimal('491.7703733440'), term='Day % (Intraday)', performance={'Hour %': '-0.81%', 'Last Price': '-0.20%', 'Day % (Intraday)': '-0.36%', 'Day % (vs Close)': '+1.34%'}, uid='wheat:commodity:day_%_(intraday):2025-10-20t15:55:01.309000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 55, 1, 294000), date='2025-10-20', asset_name='Corn', asset_ticker='Corn', asset_type='Commodity', price=Decimal('423.0071550942'), term='Day % (Intraday)', performance={'Hour %': '-0.71%', 'Last Price': '-0.06%', 'Day % (Intraday)': '-0.77%', 'Day % (vs Close)': '+2.19%'}, uid='corn:commodity:day_%_(intraday):2025-10-20t15:55:01.294000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 55, 1, 281000), date='2025-10-20', asset_name='Natural Gas', asset_ticker='Natural Gas', asset_type='Commodity', price=Decimal('3.1530670743'), term='Day % (Intraday)', performance={'Hour %': '+4.63%', 'Last Price': '+0.70%', 'Day % (Intraday)': '+4.66%', 'Day % (vs Close)': '+5.94%'}, uid='natural_gas:commodity:day_%_(intraday):2025-10-20t15:55:01.281000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 55, 1, 267000), date='2025-10-20', asset_name='Copper', asset_ticker='Copper', asset_type='Commodity', price=Decimal('4.9905744618'), term='Day % (Intraday)', performance={'Hour %': '+0.79%', 'Last Price': '-0.10%', 'Day % (Intraday)': '+0.79%', 'Day % (vs Close)': '-2.70%'}, uid='copper:commodity:day_%_(intraday):2025-10-20t15:55:01.267000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 55, 1, 254000), date='2025-10-20', asset_name='Silver', asset_ticker='Silver', asset_type='Commodity', price=Decimal('50.3571778060'), term='Day % (Intraday)', performance={'Hour %': '+2.19%', 'Last Price': '+0.36%', 'Day % (Intraday)': '+2.19%', 'Day % (vs Close)': '+2.26%'}, uid='silver:commodity:day_%_(intraday):2025-10-20t15:55:01.254000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 55, 1, 241000), date='2025-10-20', asset_name='Gold', asset_ticker='Gold', asset_type='Commodity', price=Decimal('4249.8783218145'), term='Day % (Intraday)', performance={'Hour %': '+2.74%', 'Last Price': '+0.40%', 'Day % (Intraday)': '+2.73%', 'Day % (vs Close)': '+5.85%'}, uid='gold:commodity:day_%_(intraday):2025-10-20t15:55:01.241000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 55, 1, 223000), date='2025-10-20', asset_name='Crude Oil (WTI)', asset_ticker='Crude Oil (WTI)', asset_type='Commodity', price=Decimal('56.9497680048'), term='Day % (Intraday)', performance={'Hour %': '-0.86%', 'Last Price': '-0.19%', 'Day % (Intraday)': '-0.86%', 'Day % (vs Close)': '-5.11%'}, uid='crude_oil_(wti):commodity:day_%_(intraday):2025-10-20t15:55:01.223000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 55, 1, 200000), date='2025-10-20', asset_name='Bitcoin', asset_ticker='Bitcoin', asset_type='Commodity', price=Decimal('108668.3171206226'), term='Day % (Intraday)', performance={'Hour %': '+2.80%', 'Last Price': '+0.37%', 'Day % (Intraday)': '+2.80%', 'Day % (vs Close)': '-2.44%'}, uid='bitcoin:commodity:day_%_(intraday):2025-10-20t15:55:01.200000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 54, 58, 920000), date='2025-10-20', asset_name='Wheat', asset_ticker='Wheat', asset_type='Commodity', price=Decimal('491.7703733440'), term='Day % (Intraday)', performance={'Hour %': '-0.81%', 'Last Price': '-0.20%', 'Day % (Intraday)': '-0.36%', 'Day % (vs Close)': '+1.34%'}, uid='wheat:commodity:day_%_(intraday):2025-10-20t15:54:58.920000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 54, 58, 889000), date='2025-10-20', asset_name='Corn', asset_ticker='Corn', asset_type='Commodity', price=Decimal('423.0071550942'), term='Day % (Intraday)', performance={'Hour %': '-0.71%', 'Last Price': '-0.06%', 'Day % (Intraday)': '-0.77%', 'Day % (vs Close)': '+2.19%'}, uid='corn:commodity:day_%_(intraday):2025-10-20t15:54:58.889000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 54, 58, 863000), date='2025-10-20', asset_name='Natural Gas', asset_ticker='Natural Gas', asset_type='Commodity', price=Decimal('3.1530670743'), term='Day % (Intraday)', performance={'Hour %': '+4.63%', 'Last Price': '+0.70%', 'Day % (Intraday)': '+4.66%', 'Day % (vs Close)': '+5.94%'}, uid='natural_gas:commodity:day_%_(intraday):2025-10-20t15:54:58.863000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 54, 58, 830000), date='2025-10-20', asset_name='Copper', asset_ticker='Copper', asset_type='Commodity', price=Decimal('4.9816413615'), term='Day % (Intraday)', performance={'Hour %': '+0.77%', 'Last Price': '-0.12%', 'Day % (Intraday)': '+0.77%', 'Day % (vs Close)': '-2.72%'}, uid='copper:commodity:day_%_(intraday):2025-10-20t15:54:58.830000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 54, 58, 802000), date='2025-10-20', asset_name='Silver', asset_ticker='Silver', asset_type='Commodity', price=Decimal('50.3571778060'), term='Day % (Intraday)', performance={'Hour %': '+2.19%', 'Last Price': '+0.36%', 'Day % (Intraday)': '+2.19%', 'Day % (vs Close)': '+2.26%'}, uid='silver:commodity:day_%_(intraday):2025-10-20t15:54:58.802000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 54, 58, 758000), date='2025-10-20', asset_name='Gold', asset_ticker='Gold', asset_type='Commodity', price=Decimal('4249.8783218145'), term='Day % (Intraday)', performance={'Hour %': '+2.74%', 'Last Price': '+0.40%', 'Day % (Intraday)': '+2.73%', 'Day % (vs Close)': '+5.85%'}, uid='gold:commodity:day_%_(intraday):2025-10-20t15:54:58.758000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 54, 58, 740000), date='2025-10-20', asset_name='Crude Oil (WTI)', asset_ticker='Crude Oil (WTI)', asset_type='Commodity', price=Decimal('56.9483662767'), term='Day % (Intraday)', performance={'Hour %': '-0.84%', 'Last Price': '-0.18%', 'Day % (Intraday)': '-0.84%', 'Day % (vs Close)': '-5.09%'}, uid='crude_oil_(wti):commodity:day_%_(intraday):2025-10-20t15:54:58.740000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 15, 54, 58, 717000), date='2025-10-20', asset_name='Bitcoin', asset_ticker='Bitcoin', asset_type='Commodity', price=Decimal('108668.3171206226'), term='Day % (Intraday)', performance={'Hour %': '+2.80%', 'Last Price': '+0.37%', 'Day % (Intraday)': '+2.80%', 'Day % (vs Close)': '-2.44%'}, uid='bitcoin:commodity:day_%_(intraday):2025-10-20t15:54:58.717000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 13, 2, 849000), date='2025-10-20', asset_name='Wheat', asset_ticker='Wheat', asset_type='Commodity', price=Decimal('491.7458729365'), term='Day % (Intraday)', performance={'Hour %': '+0.00%', 'Last Price': '-0.05%', 'Day % (Intraday)': '-0.05%', 'Day % (vs Close)': '+1.76%'}, uid='wheat:commodity:day_%_(intraday):2025-10-20t00:13:02.849000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 13, 2, 832000), date='2025-10-20', asset_name='Corn', asset_ticker='Corn', asset_type='Commodity', price=Decimal('423.0076091310'), term='Day % (Intraday)', performance={'Hour %': '+0.00%', 'Last Price': '-0.12%', 'Day % (Intraday)': '-0.12%', 'Day % (vs Close)': '+2.30%'}, uid='corn:commodity:day_%_(intraday):2025-10-20t00:13:02.832000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 13, 2, 815000), date='2025-10-20', asset_name='Natural Gas', asset_ticker='Natural Gas', asset_type='Commodity', price=Decimal('3.1518911347'), term='Day % (Intraday)', performance={'Hour %': '-0.10%', 'Last Price': '-0.06%', 'Day % (Intraday)': '-0.06%', 'Day % (vs Close)': '+0.67%'}, uid='natural_gas:commodity:day_%_(intraday):2025-10-20t00:13:02.815000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 13, 2, 798000), date='2025-10-20', asset_name='Copper', asset_ticker='Copper', asset_type='Commodity', price=Decimal('4.9839871898'), term='Day % (Intraday)', performance={'Hour %': '-0.08%', 'Last Price': '-0.08%', 'Day % (Intraday)': '-0.08%', 'Day % (vs Close)': '-1.74%'}, uid='copper:commodity:day_%_(intraday):2025-10-20t00:13:02.798000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 13, 2, 781000), date='2025-10-20', asset_name='Silver', asset_ticker='Silver', asset_type='Commodity', price=Decimal('50.3599280144'), term='Day % (Intraday)', performance={'Hour %': '+0.02%', 'Last Price': '+0.02%', 'Day % (Intraday)': '+0.02%', 'Day % (vs Close)': '+3.43%'}, uid='silver:commodity:day_%_(intraday):2025-10-20t00:13:02.781000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 13, 2, 760000), date='2025-10-20', asset_name='Gold', asset_ticker='Gold', asset_type='Commodity', price=Decimal('4250.0999600160'), term='Day % (Intraday)', performance={'Hour %': '+0.06%', 'Last Price': '+0.04%', 'Day % (Intraday)': '+0.04%', 'Day % (vs Close)': '+4.69%'}, uid='gold:commodity:day_%_(intraday):2025-10-20t00:13:02.760000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 13, 2, 742000), date='2025-10-20', asset_name='Crude Oil (WTI)', asset_ticker='Crude Oil (WTI)', asset_type='Commodity', price=Decimal('56.9486102779'), term='Day % (Intraday)', performance={'Hour %': '+0.02%', 'Last Price': '+0.02%', 'Day % (Intraday)': '+0.02%', 'Day % (vs Close)': '-4.62%'}, uid='crude_oil_(wti):commodity:day_%_(intraday):2025-10-20t00:13:02.742000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 13, 2, 723000), date='2025-10-20', asset_name='Bitcoin', asset_ticker='Bitcoin', asset_type='Commodity', price=Decimal('108771.5360690278'), term='Day % (Intraday)', performance={'Hour %': '-0.24%', 'Last Price': '-0.33%', 'Day % (Intraday)': '-0.33%', 'Day % (vs Close)': '-6.04%'}, uid='bitcoin:commodity:day_%_(intraday):2025-10-20t00:13:02.723000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 13, 0, 452000), date='2025-10-20', asset_name='Wheat', asset_ticker='Wheat', asset_type='Commodity', price=Decimal('491.7458729365'), term='Day % (Intraday)', performance={'Hour %': '+0.00%', 'Last Price': '-0.05%', 'Day % (Intraday)': '-0.05%', 'Day % (vs Close)': '+1.76%'}, uid='wheat:commodity:day_%_(intraday):2025-10-20t00:13:00.452000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 13, 0, 428000), date='2025-10-20', asset_name='Corn', asset_ticker='Corn', asset_type='Commodity', price=Decimal('423.0076091310'), term='Day % (Intraday)', performance={'Hour %': '+0.00%', 'Last Price': '-0.12%', 'Day % (Intraday)': '-0.12%', 'Day % (vs Close)': '+2.30%'}, uid='corn:commodity:day_%_(intraday):2025-10-20t00:13:00.428000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 13, 0, 406000), date='2025-10-20', asset_name='Natural Gas', asset_ticker='Natural Gas', asset_type='Commodity', price=Decimal('3.1531531532'), term='Day % (Intraday)', performance={'Hour %': '-0.13%', 'Last Price': '-0.10%', 'Day % (Intraday)': '-0.10%', 'Day % (vs Close)': '+0.64%'}, uid='natural_gas:commodity:day_%_(intraday):2025-10-20t00:13:00.406000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 13, 0, 378000), date='2025-10-20', asset_name='Copper', asset_ticker='Copper', asset_type='Commodity', price=Decimal('4.9839871898'), term='Day % (Intraday)', performance={'Hour %': '-0.08%', 'Last Price': '-0.08%', 'Day % (Intraday)': '-0.08%', 'Day % (vs Close)': '-1.74%'}, uid='copper:commodity:day_%_(intraday):2025-10-20t00:13:00.378000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 13, 0, 357000), date='2025-10-20', asset_name='Silver', asset_ticker='Silver', asset_type='Commodity', price=Decimal('50.3497901259'), term='Day % (Intraday)', performance={'Hour %': '+0.06%', 'Last Price': '+0.06%', 'Day % (Intraday)': '+0.06%', 'Day % (vs Close)': '+3.47%'}, uid='silver:commodity:day_%_(intraday):2025-10-20t00:13:00.357000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 13, 0, 335000), date='2025-10-20', asset_name='Gold', asset_ticker='Gold', asset_type='Commodity', price=Decimal('4250.0999600160'), term='Day % (Intraday)', performance={'Hour %': '+0.06%', 'Last Price': '+0.04%', 'Day % (Intraday)': '+0.04%', 'Day % (vs Close)': '+4.69%'}, uid='gold:commodity:day_%_(intraday):2025-10-20t00:13:00.335000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 13, 0, 308000), date='2025-10-20', asset_name='Crude Oil (WTI)', asset_ticker='Crude Oil (WTI)', asset_type='Commodity', price=Decimal('56.9472211116'), term='Day % (Intraday)', performance={'Hour %': '+0.04%', 'Last Price': '+0.04%', 'Day % (Intraday)': '+0.04%', 'Day % (vs Close)': '-4.60%'}, uid='crude_oil_(wti):commodity:day_%_(intraday):2025-10-20t00:13:00.308000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 13, 0, 288000), date='2025-10-20', asset_name='Bitcoin', asset_ticker='Bitcoin', asset_type='Commodity', price=Decimal('108771.5360690278'), term='Day % (Intraday)', performance={'Hour %': '-0.24%', 'Last Price': '-0.33%', 'Day % (Intraday)': '-0.33%', 'Day % (vs Close)': '-6.04%'}, uid='bitcoin:commodity:day_%_(intraday):2025-10-20t00:13:00.288000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 12, 57, 602000), date='2025-10-20', asset_name='Wheat', asset_ticker='Wheat', asset_type='Commodity', price=Decimal('491.7458729365'), term='Day % (Intraday)', performance={'Hour %': '+0.00%', 'Last Price': '-0.05%', 'Day % (Intraday)': '-0.05%', 'Day % (vs Close)': '+1.76%'}, uid='wheat:commodity:day_%_(intraday):2025-10-20t00:12:57.602000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 12, 57, 588000), date='2025-10-20', asset_name='Corn', asset_ticker='Corn', asset_type='Commodity', price=Decimal('423.0076091310'), term='Day % (Intraday)', performance={'Hour %': '+0.00%', 'Last Price': '-0.12%', 'Day % (Intraday)': '-0.12%', 'Day % (vs Close)': '+2.30%'}, uid='corn:commodity:day_%_(intraday):2025-10-20t00:12:57.588000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 12, 57, 573000), date='2025-10-20', asset_name='Natural Gas', asset_ticker='Natural Gas', asset_type='Commodity', price=Decimal('3.1531531532'), term='Day % (Intraday)', performance={'Hour %': '-0.13%', 'Last Price': '-0.10%', 'Day % (Intraday)': '-0.10%', 'Day % (vs Close)': '+0.64%'}, uid='natural_gas:commodity:day_%_(intraday):2025-10-20t00:12:57.573000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 12, 57, 556000), date='2025-10-20', asset_name='Copper', asset_ticker='Copper', asset_type='Commodity', price=Decimal('4.9839871898'), term='Day % (Intraday)', performance={'Hour %': '-0.08%', 'Last Price': '-0.08%', 'Day % (Intraday)': '-0.08%', 'Day % (vs Close)': '-1.74%'}, uid='copper:commodity:day_%_(intraday):2025-10-20t00:12:57.556000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 12, 57, 529000), date='2025-10-20', asset_name='Silver', asset_ticker='Silver', asset_type='Commodity', price=Decimal('50.3497901259'), term='Day % (Intraday)', performance={'Hour %': '+0.06%', 'Last Price': '+0.06%', 'Day % (Intraday)': '+0.06%', 'Day % (vs Close)': '+3.47%'}, uid='silver:commodity:day_%_(intraday):2025-10-20t00:12:57.529000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 12, 57, 515000), date='2025-10-20', asset_name='Gold', asset_ticker='Gold', asset_type='Commodity', price=Decimal('4250.0999600160'), term='Day % (Intraday)', performance={'Hour %': '+0.06%', 'Last Price': '+0.04%', 'Day % (Intraday)': '+0.04%', 'Day % (vs Close)': '+4.69%'}, uid='gold:commodity:day_%_(intraday):2025-10-20t00:12:57.515000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 12, 57, 497000), date='2025-10-20', asset_name='Crude Oil (WTI)', asset_ticker='Crude Oil (WTI)', asset_type='Commodity', price=Decimal('56.9472211116'), term='Day % (Intraday)', performance={'Hour %': '+0.04%', 'Last Price': '+0.04%', 'Day % (Intraday)': '+0.04%', 'Day % (vs Close)': '-4.60%'}, uid='crude_oil_(wti):commodity:day_%_(intraday):2025-10-20t00:12:57.497000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 12, 57, 477000), date='2025-10-20', asset_name='Bitcoin', asset_ticker='Bitcoin', asset_type='Commodity', price=Decimal('108771.5360690278'), term='Day % (Intraday)', performance={'Hour %': '-0.24%', 'Last Price': '-0.33%', 'Day % (Intraday)': '-0.33%', 'Day % (vs Close)': '-6.04%'}, uid='bitcoin:commodity:day_%_(intraday):2025-10-20t00:12:57.477000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 12, 54, 895000), date='2025-10-20', asset_name='Wheat', asset_ticker='Wheat', asset_type='Commodity', price=Decimal('491.7458729365'), term='Day % (Intraday)', performance={'Hour %': '+0.00%', 'Last Price': '-0.05%', 'Day % (Intraday)': '-0.05%', 'Day % (vs Close)': '+1.76%'}, uid='wheat:commodity:day_%_(intraday):2025-10-20t00:12:54.895000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 12, 54, 870000), date='2025-10-20', asset_name='Corn', asset_ticker='Corn', asset_type='Commodity', price=Decimal('423.0076091310'), term='Day % (Intraday)', performance={'Hour %': '+0.00%', 'Last Price': '-0.12%', 'Day % (Intraday)': '-0.12%', 'Day % (vs Close)': '+2.30%'}, uid='corn:commodity:day_%_(intraday):2025-10-20t00:12:54.870000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 12, 54, 862000), date='2025-10-20', asset_name='Natural Gas', asset_ticker='Natural Gas', asset_type='Commodity', price=Decimal('3.1518911347'), term='Day % (Intraday)', performance={'Hour %': '-0.10%', 'Last Price': '-0.06%', 'Day % (Intraday)': '-0.06%', 'Day % (vs Close)': '+0.67%'}, uid='natural_gas:commodity:day_%_(intraday):2025-10-20t00:12:54.862000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 12, 54, 851000), date='2025-10-20', asset_name='Copper', asset_ticker='Copper', asset_type='Commodity', price=Decimal('4.9839871898'), term='Day % (Intraday)', performance={'Hour %': '-0.08%', 'Last Price': '-0.08%', 'Day % (Intraday)': '-0.08%', 'Day % (vs Close)': '-1.74%'}, uid='copper:commodity:day_%_(intraday):2025-10-20t00:12:54.851000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 12, 54, 840000), date='2025-10-20', asset_name='Silver', asset_ticker='Silver', asset_type='Commodity', price=Decimal('50.3497901259'), term='Day % (Intraday)', performance={'Hour %': '+0.06%', 'Last Price': '+0.06%', 'Day % (Intraday)': '+0.06%', 'Day % (vs Close)': '+3.47%'}, uid='silver:commodity:day_%_(intraday):2025-10-20t00:12:54.840000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 12, 54, 830000), date='2025-10-20', asset_name='Gold', asset_ticker='Gold', asset_type='Commodity', price=Decimal('4250.0999600160'), term='Day % (Intraday)', performance={'Hour %': '+0.06%', 'Last Price': '+0.04%', 'Day % (Intraday)': '+0.04%', 'Day % (vs Close)': '+4.69%'}, uid='gold:commodity:day_%_(intraday):2025-10-20t00:12:54.830000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 12, 54, 818000), date='2025-10-20', asset_name='Crude Oil (WTI)', asset_ticker='Crude Oil (WTI)', asset_type='Commodity', price=Decimal('56.9486102779'), term='Day % (Intraday)', performance={'Hour %': '+0.02%', 'Last Price': '+0.02%', 'Day % (Intraday)': '+0.02%', 'Day % (vs Close)': '-4.62%'}, uid='crude_oil_(wti):commodity:day_%_(intraday):2025-10-20t00:12:54.818000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 12, 54, 797000), date='2025-10-20', asset_name='Bitcoin', asset_ticker='Bitcoin', asset_type='Commodity', price=Decimal('108771.5360690278'), term='Day % (Intraday)', performance={'Hour %': '-0.24%', 'Last Price': '-0.33%', 'Day % (Intraday)': '-0.33%', 'Day % (vs Close)': '-6.04%'}, uid='bitcoin:commodity:day_%_(intraday):2025-10-20t00:12:54.797000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 12, 50, 881000), date='2025-10-20', asset_name='Wheat', asset_ticker='Wheat', asset_type='Commodity', price=Decimal('491.7458729365'), term='Day % (Intraday)', performance={'Hour %': '+0.00%', 'Last Price': '-0.05%', 'Day % (Intraday)': '-0.05%', 'Day % (vs Close)': '+1.76%'}, uid='wheat:commodity:day_%_(intraday):2025-10-20t00:12:50.881000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 12, 50, 862000), date='2025-10-20', asset_name='Corn', asset_ticker='Corn', asset_type='Commodity', price=Decimal('423.0076091310'), term='Day % (Intraday)', performance={'Hour %': '+0.00%', 'Last Price': '-0.12%', 'Day % (Intraday)': '-0.12%', 'Day % (vs Close)': '+2.30%'}, uid='corn:commodity:day_%_(intraday):2025-10-20t00:12:50.862000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 12, 50, 830000), date='2025-10-20', asset_name='Natural Gas', asset_ticker='Natural Gas', asset_type='Commodity', price=Decimal('3.1518911347'), term='Day % (Intraday)', performance={'Hour %': '-0.10%', 'Last Price': '-0.06%', 'Day % (Intraday)': '-0.06%', 'Day % (vs Close)': '+0.67%'}, uid='natural_gas:commodity:day_%_(intraday):2025-10-20t00:12:50.830000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 12, 50, 815000), date='2025-10-20', asset_name='Copper', asset_ticker='Copper', asset_type='Commodity', price=Decimal('4.9839871898'), term='Day % (Intraday)', performance={'Hour %': '-0.08%', 'Last Price': '-0.08%', 'Day % (Intraday)': '-0.08%', 'Day % (vs Close)': '-1.74%'}, uid='copper:commodity:day_%_(intraday):2025-10-20t00:12:50.815000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 12, 50, 805000), date='2025-10-20', asset_name='Silver', asset_ticker='Silver', asset_type='Commodity', price=Decimal('50.3547516738'), term='Day % (Intraday)', performance={'Hour %': '+0.07%', 'Last Price': '+0.07%', 'Day % (Intraday)': '+0.07%', 'Day % (vs Close)': '+3.48%'}, uid='silver:commodity:day_%_(intraday):2025-10-20t00:12:50.805000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 12, 50, 782000), date='2025-10-20', asset_name='Gold', asset_ticker='Gold', asset_type='Commodity', price=Decimal('4249.9750124938'), term='Day % (Intraday)', performance={'Hour %': '+0.06%', 'Last Price': '+0.05%', 'Day % (Intraday)': '+0.05%', 'Day % (vs Close)': '+4.69%'}, uid='gold:commodity:day_%_(intraday):2025-10-20t00:12:50.782000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 12, 50, 749000), date='2025-10-20', asset_name='Crude Oil (WTI)', asset_ticker='Crude Oil (WTI)', asset_type='Commodity', price=Decimal('56.9472211116'), term='Day % (Intraday)', performance={'Hour %': '+0.04%', 'Last Price': '+0.04%', 'Day % (Intraday)': '+0.04%', 'Day % (vs Close)': '-4.60%'}, uid='crude_oil_(wti):commodity:day_%_(intraday):2025-10-20t00:12:50.749000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 12, 50, 723000), date='2025-10-20', asset_name='Bitcoin', asset_ticker='Bitcoin', asset_type='Commodity', price=Decimal('108771.5360690278'), term='Day % (Intraday)', performance={'Hour %': '-0.24%', 'Last Price': '-0.33%', 'Day % (Intraday)': '-0.33%', 'Day % (vs Close)': '-6.04%'}, uid='bitcoin:commodity:day_%_(intraday):2025-10-20t00:12:50.723000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 12, 43, 85000), date='2025-10-20', asset_name='Wheat', asset_ticker='Wheat', asset_type='Commodity', price=Decimal('491.7458729365'), term='Day % (Intraday)', performance={'Hour %': '+0.00%', 'Last Price': '-0.05%', 'Day % (Intraday)': '-0.05%', 'Day % (vs Close)': '+1.76%'}, uid='wheat:commodity:day_%_(intraday):2025-10-20t00:12:43.085000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 12, 43, 70000), date='2025-10-20', asset_name='Corn', asset_ticker='Corn', asset_type='Commodity', price=Decimal('423.0076091310'), term='Day % (Intraday)', performance={'Hour %': '+0.00%', 'Last Price': '-0.12%', 'Day % (Intraday)': '-0.12%', 'Day % (vs Close)': '+2.30%'}, uid='corn:commodity:day_%_(intraday):2025-10-20t00:12:43.070000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 12, 43, 52000), date='2025-10-20', asset_name='Natural Gas', asset_ticker='Natural Gas', asset_type='Commodity', price=Decimal('3.1518911347'), term='Day % (Intraday)', performance={'Hour %': '-0.10%', 'Last Price': '-0.06%', 'Day % (Intraday)': '-0.06%', 'Day % (vs Close)': '+0.67%'}, uid='natural_gas:commodity:day_%_(intraday):2025-10-20t00:12:43.052000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 12, 43, 30000), date='2025-10-20', asset_name='Copper', asset_ticker='Copper', asset_type='Commodity', price=Decimal('4.9814944483'), term='Day % (Intraday)', performance={'Hour %': '-0.03%', 'Last Price': '-0.03%', 'Day % (Intraday)': '-0.03%', 'Day % (vs Close)': '-1.69%'}, uid='copper:commodity:day_%_(intraday):2025-10-20t00:12:43.030000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 12, 43, 17000), date='2025-10-20', asset_name='Silver', asset_ticker='Silver', asset_type='Commodity', price=Decimal('50.3547516738'), term='Day % (Intraday)', performance={'Hour %': '+0.07%', 'Last Price': '+0.07%', 'Day % (Intraday)': '+0.07%', 'Day % (vs Close)': '+3.48%'}, uid='silver:commodity:day_%_(intraday):2025-10-20t00:12:43.017000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 12, 42, 996000), date='2025-10-20', asset_name='Gold', asset_ticker='Gold', asset_type='Commodity', price=Decimal('4250.0499700180'), term='Day % (Intraday)', performance={'Hour %': '+0.08%', 'Last Price': '+0.06%', 'Day % (Intraday)': '+0.06%', 'Day % (vs Close)': '+4.71%'}, uid='gold:commodity:day_%_(intraday):2025-10-20t00:12:42.996000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 12, 42, 977000), date='2025-10-20', asset_name='Crude Oil (WTI)', asset_ticker='Crude Oil (WTI)', asset_type='Commodity', price=Decimal('56.9472211116'), term='Day % (Intraday)', performance={'Hour %': '+0.04%', 'Last Price': '+0.04%', 'Day % (Intraday)': '+0.04%', 'Day % (vs Close)': '-4.60%'}, uid='crude_oil_(wti):commodity:day_%_(intraday):2025-10-20t00:12:42.977000'), FinancialEvent(timestamp_utc=datetime.datetime(2025, 10, 20, 0, 12, 42, 950000), date='2025-10-20', asset_name='Bitcoin', asset_ticker='Bitcoin', asset_type='Commodity', price=Decimal('108771.5360690278'), term='Day % (Intraday)', performance={'Hour %': '-0.24%', 'Last Price': '-0.33%', 'Day % (Intraday)': '-0.33%', 'Day % (vs Close)': '-6.04%'}, uid='bitcoin:commodity:day_%_(intraday):2025-10-20t00:12:42.950000')]


    print("--- [V3] DYNAMIC CHYRON GENERATION (showing 3 runs) ---")
    for i in range(3):
        print(f"\n--- RUN #{i + 1} ---")
        chyron_outputs = create_financial_chyron(your_events_list, term='Day % (Intraday)')
        for key, value in chyron_outputs.items():
            print(f"[{key.upper()}]: {value}")

    print("\n" + "=" * 50 + "\n")

    print("--- [V3] DYNAMIC NARRATIVE TICKER (for comparison) ---")
    narrative_ticker = create_narrative_ticker(your_events_list, term='Day % (Intraday)')
    print(f"NARRATIVE: {narrative_ticker}\n")

    print("=" * 50 + "\n")

    print("--- [V4] COMPREHENSIVE SEQUENTIAL TICKER ---")
    sequential_ticker = create_sequential_ticker(your_events_list, term='Day % (Intraday)')
    print(f"DATA FEED: {sequential_ticker}")