import datetime
import re
from dataclasses import dataclass
from typing import Dict, Optional
from urllib.parse import quote_plus


# --- DTO Definition ---
@dataclass
class NewsEvent:
    uid: str
    published_utc: datetime.datetime
    date_published: str
    region: str
    title: str
    source: str
    link: str
    summary: Optional[str] = None
    author: Optional[str] = None
    credit: Optional[str] = None
    image: Optional[str] = None
    video: Optional[str] = None
    latitude: Optional[str] = None
    longitude: Optional[str] = None


# --- Helper Functions ---
def _clean_html(raw_html: str) -> str:
    if not raw_html: return ""
    return re.sub(re.compile('<.*?>'), '', raw_html)


def _format_timestamp(dt: datetime.datetime) -> str:
    return dt.strftime("%H:%M %Z").upper()


# --- THE PRESTIGE BROADCAST ENGINE ---

def _get_broadcast_css() -> str:
    return '''
    @import url('https://fonts.googleapis.com/css2?family=Chakra+Petch:wght@500;700&family=Roboto:wght@400;500;900&display=swap');

    :root {
        --bg-slate: #080808;
        --text-main: #ffffff;
        --text-muted: #94a3b8;
        --accent-live: #ef4444; /* Broadcast Red */
        --accent-gold: #eab308; /* Breaking News Gold */
        --border-color: #333333;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }

    body, .root {
        width: 100%; height: 100%;
        background-color: var(--bg-slate);
        color: var(--text-main);
        font-family: 'Roboto', sans-serif;
        overflow: hidden; /* STRICT OVERFLOW CONTROL */
        position: relative;
        container-type: inline-size; /* Enables CQI units */
        display: flex; flex-direction: column;
    }

    /* BROADCAST BACKGROUND TEXTURE */
    .root::before {
        content: ""; position: absolute; inset: 0; pointer-events: none; z-index: 0;
        background: radial-gradient(circle at top right, rgba(255,255,255,0.05), transparent 60%),
                    linear-gradient(to bottom, #080808 0%, #111 100%);
    }

    /* THE "LOWER THIRD" / HEADER BAR */
    .broadcast-header {
        position: relative; z-index: 10;
        padding: 3cqi 4cqi; /* Responsive padding */
        border-bottom: 2px solid var(--accent-gold);
        background: linear-gradient(90deg, #000 0%, rgba(0,0,0,0.8) 100%);
        display: flex; justify-content: space-between; align-items: center;
        flex-shrink: 0; /* Prevent crushing */
    }

    .slug-line {
        font-family: 'Chakra Petch', sans-serif;
        font-weight: 700; text-transform: uppercase; letter-spacing: 0.1em;
        color: var(--accent-gold); font-size: 3cqi;
        display: flex; align-items: center; gap: 1em;
    }

    .live-indicator {
        background: var(--accent-live); color: white;
        padding: 0.2em 0.6em; border-radius: 2px; font-size: 0.8em;
    }

    .timestamp {
        font-family: 'Chakra Petch', sans-serif;
        color: var(--text-muted); font-size: 2.5cqi;
    }

    /* CONTENT AREA */
    .content-area {
        position: relative; z-index: 1;
        flex: 1; /* Fill remaining space */
        padding: 4cqi 5cqi;
        display: flex; flex-direction: column; justify-content: center;
        overflow: hidden;
    }
    '''


# --- GRAPHIC GENERATORS ---

def _generate_headline_graphic(event: NewsEvent, dark_mode: bool) -> str:
    # Intelligent Font Scaling Logic
    # We map title length to 'cqi' (Container Query Inline) units.
    # This guarantees the text fits the width of the container perfectly.
    chars = len(event.title)
    if chars < 20:
        font_size = "10cqi"
    elif chars < 40:
        font_size = "7.5cqi"
    elif chars < 60:
        font_size = "6cqi"
    elif chars < 80:
        font_size = "5cqi"
    elif chars < 120:
        font_size = "4cqi"
    else:
        font_size = "3.2cqi"

    return f'''
    <style>
        {_get_broadcast_css()}
        .breaking-tag {{
            font-family: 'Chakra Petch', sans-serif;
            font-size: 2.5cqi; font-weight: 700; letter-spacing: 0.2em;
            color: var(--text-muted); text-transform: uppercase;
            margin-bottom: 1.5cqi;
            border-left: 4px solid var(--accent-live);
            padding-left: 1.5cqi;
        }}
        .headline-text {{
            font-family: 'Roboto', sans-serif;
            font-weight: 900;
            font-size: {font_size}; /* Computed Size */
            line-height: 1.1;
            color: #fff;
            text-transform: uppercase;
            letter-spacing: -0.02em;
            /* Text Shadow for Readability */
            text-shadow: 0 2px 4px rgba(0,0,0,0.5);
        }}
        .source-line {{
            margin-top: 3cqi;
            border-top: 1px solid var(--border-color);
            padding-top: 2cqi;
            font-size: 2.5cqi; color: var(--text-muted);
            font-weight: 500; text-transform: uppercase;
            display: flex; align-items: center; gap: 1em;
        }}
    </style>
    <div class="root">
        <div class="broadcast-header">
            <div class="slug-line">
                <span class="live-indicator">LIVE</span>
                <span>{event.region.upper()}</span>
            </div>
            <div class="timestamp">{_format_timestamp(event.published_utc)}</div>
        </div>
        <div class="content-area">
            <div class="breaking-tag">Developing Story</div>
            <div class="headline-text">{event.title}</div>
            <div class="source-line">
                <span>SOURCE: {event.source}</span>
            </div>
        </div>
    </div>
    '''


def _generate_summary_graphic(event: NewsEvent, dark_mode: bool) -> str:
    clean_summary = _clean_html(event.summary or "No briefing available.")

    # Truncate summary if massive to prevent overflow
    if len(clean_summary) > 450:
        clean_summary = clean_summary[:447] + "..."

    return f'''
    <style>
        {_get_broadcast_css()}
        .briefing-container {{
            background: rgba(255,255,255,0.03);
            border: 1px solid var(--border-color);
            padding: 3cqi;
            height: 100%;
            display: flex; flex-direction: column;
        }}
        .briefing-title {{
            font-family: 'Chakra Petch', sans-serif;
            font-size: 3cqi; color: var(--accent-gold);
            text-transform: uppercase; font-weight: 700;
            margin-bottom: 2cqi; padding-bottom: 2cqi;
            border-bottom: 1px solid var(--border-color);
        }}
        .briefing-text {{
            font-size: 3.5cqi; line-height: 1.5;
            color: #e2e8f0; font-weight: 400;
            flex: 1;
            /* Elegant typography */
            text-align: justify;
        }}
        .author-tag {{
            font-size: 2cqi; color: var(--text-muted);
            text-transform: uppercase; margin-top: 2cqi; text-align: right;
        }}
    </style>
    <div class="root">
        <div class="broadcast-header">
            <div class="slug-line">HAPPENING NOW</div>
            <div class="timestamp">{event.date_published}</div>
        </div>
        <div class="content-area">
            <div class="briefing-container">
                <div class="briefing-title">{event.title[:60]}...</div>
                <div class="briefing-text">{clean_summary}</div>
                <div class="author-tag">Filed By: {event.author or 'WIRE SERVICES'}</div>
            </div>
        </div>
    </div>
    '''


def _generate_map_graphic(event: NewsEvent, dark_mode: bool) -> Optional[str]:
    if not event.latitude or not event.longitude: return None

    # Satellite Targeting aesthetic
    return f'''
    <style>
        {_get_broadcast_css()}
        .map-layer {{
            position: absolute; inset: 0;
            background: #050505;
            background-image: url('https://upload.wikimedia.org/wikipedia/commons/8/88/World_map_-_low_resolution.svg');
            background-size: cover; background-position: center;
            filter: grayscale(1) invert(0) contrast(1.2) brightness(0.6);
            opacity: 0.5;
        }}
        .grid-overlay {{
            position: absolute; inset: 0;
            background-image: linear-gradient(rgba(255,255,255,0.05) 1px, transparent 1px),
            linear-gradient(90deg, rgba(255,255,255,0.05) 1px, transparent 1px);
            background-size: 4cqi 4cqi;
            z-index: 2;
        }}
        .coords-box {{
            position: absolute; bottom: 4cqi; left: 4cqi;
            background: rgba(0,0,0,0.8); border: 1px solid var(--accent-gold);
            padding: 1cqi 2cqi;
            font-family: 'Chakra Petch', monospace; color: var(--accent-gold);
            font-size: 3cqi; z-index: 5;
        }}
        .reticle {{
            position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
            width: 20cqi; height: 20cqi;
            border: 2px solid var(--accent-live); border-radius: 50%;
            z-index: 4;
            box-shadow: 0 0 20px rgba(239, 68, 68, 0.4);
        }}
        .reticle::after {{
            content: ''; position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
            width: 1cqi; height: 1cqi; background: #fff;
        }}
    </style>
    <div class="root">
        <div class="broadcast-header" style="background:transparent; z-index:10;">
            <div class="slug-line">GEOLOCATION DATA</div>
        </div>
        <div class="map-layer"></div>
        <div class="grid-overlay"></div>
        <div class="reticle"></div>
        <div class="coords-box">
            LAT: {event.latitude} <br> LON: {event.longitude}
        </div>
    </div>
    '''


def _generate_source_graphic(event: NewsEvent, dark_mode: bool) -> str:
    # "Wire Data" aesthetic
    return f'''
    <style>
        {_get_broadcast_css()}
        .data-row {{
            display: flex; align-items: baseline;
            border-bottom: 1px solid rgba(255,255,255,0.1);
            padding: 2cqi 0;
        }}
        .data-label {{
            width: 25%; font-family: 'Chakra Petch', sans-serif;
            color: var(--text-muted); font-size: 2.5cqi; text-transform: uppercase;
        }}
        .data-val {{
            font-size: 3.5cqi; font-weight: 700; color: #fff;
            flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
        }}
        .data-val.highlight {{ color: var(--accent-gold); }}
    </style>
    <div class="root">
        <div class="broadcast-header">
            <div class="slug-line">METADATA WIRE</div>
        </div>
        <div class="content-area" style="justify-content: flex-start;">
            <div class="data-row">
                <span class="data-label">PROVIDER</span>
                <span class="data-val highlight">{event.source}</span>
            </div>
            <div class="data-row">
                <span class="data-label">DATE</span>
                <span class="data-val">{event.date_published}</span>
            </div>
            <div class="data-row">
                <span class="data-label">REGION</span>
                <span class="data-val">{event.region.upper()}</span>
            </div>
            <div class="data-row">
                <span class="data-label">UID HASH</span>
                <span class="data-val" style="font-family:monospace; font-size:2cqi;">{event.uid}</span>
            </div>
        </div>
    </div>
    '''


def _generate_image_graphic(event: NewsEvent, dark_mode: bool) -> Optional[str]:
    if not event.image: return None

    # "Picture-in-Picture" / Broadcast Frame aesthetic
    return f'''
    <style>
        {_get_broadcast_css()}
        .image-stage {{
            width: 100%; height: 100%; position: absolute; top: 0; left: 0;
            background: #000;
        }}
        .bg-blur {{
            position: absolute; inset: 0;
            background-image: url('{event.image}');
            background-size: cover; background-position: center;
            filter: blur(20px) brightness(0.3); opacity: 0.5;
        }}
        .main-img {{
            position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
            max-width: 95%; max-height: 85%;
            box-shadow: 0 10px 30px rgba(0,0,0,0.8);
            border: 1px solid rgba(255,255,255,0.2);
        }}
        .credit-badge {{
            position: absolute; bottom: 2cqi; right: 2cqi;
            background: rgba(0,0,0,0.7); color: #fff;
            padding: 0.5cqi 1cqi; font-size: 2cqi;
            text-transform: uppercase; font-family: 'Chakra Petch', sans-serif;
        }}
    </style>
    <div class="root">
        <div class="image-stage">
            <div class="bg-blur"></div>
            <img class="main-img" src="{event.image}" alt="News Image">
            <div class="credit-badge">IMG: {event.credit or event.source}</div>
        </div>
        <div class="broadcast-header" style="position:absolute; top:0; left:0; right:0; background:transparent; border:none;">
            <div class="slug-line" style="text-shadow:0 2px 4px #000;">VISUAL FEED</div>
        </div>
    </div>
    '''


def generate_news_broadcast_elements(event: NewsEvent, dark_mode: bool = True) -> Dict[str, Optional[str]]:
    # Generate base graphics
    graphics = {
        'headline_graphic': _generate_headline_graphic(event, dark_mode),
        'summary_graphic': _generate_summary_graphic(event, dark_mode),
        'source_graphic': _generate_source_graphic(event, dark_mode),
        'map_graphic': _generate_map_graphic(event, dark_mode),
        'image_graphic': _generate_image_graphic(event, dark_mode),
    }

    # QR Code Integration (Prestige Style)
    qr_api_url = f"https://api.qrserver.com/v1/create-qr-code/?size=100x100&data={quote_plus(event.link)}&qzone=1"
    qr_style = f'''
        <style>
            .qr-box {{
                position: absolute; bottom: 3cqi; right: 3cqi;
                background: #fff; padding: 0.5cqi;
                border-radius: 2px; z-index: 100;
                box-shadow: 0 4px 12px rgba(0,0,0,0.5);
            }}
            .qr-box img {{ display: block; width: 10cqi; height: 10cqi; }}
        </style>
    '''
    qr_div = f'<div class="qr-box"><img src="{qr_api_url}"></div>'

    output = {}
    output.update(graphics)

    # Safe injection logic
    for name, html in graphics.items():
        if html:
            # Inject Style
            injected_html = html.replace('</style>', f'{qr_style}</style>')
            # Inject Div at the end of the root container
            if '</div>' in injected_html:
                last_div_index = injected_html.rfind('</div>')
                output[f"{name}_qr"] = injected_html[:last_div_index] + qr_div + injected_html[last_div_index:]
            else:
                output[f"{name}_qr"] = injected_html + qr_div

    return output


# --- MAIN EXECUTION BLOCK ---
if __name__ == "__main__":
    # Sample Data
    example_event = NewsEvent(
        uid="12345", published_utc=datetime.datetime(2025, 10, 22, 14, 30),
        date_published='2025-10-22', region='North America',
        title="Global Markets Rally as Tech Sector Surges to New Record Highs Amidst Earnings Reports",
        source='Bloomberg', link='https://example.com',
        summary='Major indices hit record highs today as technology stocks outperformed expectations. Analysts cite strong AI adoption and favorable interest rate guidance as key drivers.',
        author='Jane Doe', credit='Getty Images',
        image='https://upload.wikimedia.org/wikipedia/commons/thumb/8/8d/President_Barack_Obama.jpg/1200px-President_Barack_Obama.jpg',
        latitude='40.7128', longitude='-74.0060'
    )

    print("Generating Broadcast Graphics...")
    graphics = generate_news_broadcast_elements(example_event)

    for name, html in graphics.items():
        if html:
            filename = f"broadcast_{name}.html"
            with open(filename, "w", encoding="utf-8") as f: f.write(html)
            print(f"-> {filename}")