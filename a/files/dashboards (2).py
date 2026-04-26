from typing import List, Dict
from datetime import datetime as dt, timezone, timedelta


class SportsEvent:
    def __init__(self, timestamp_utc, sport, league, track_id, name, short_name, scheduled, status, venue_uid,
                 home_uid, home_score, away_uid, away_score, uid):
        self.timestamp_utc = timestamp_utc
        self.sport = sport
        self.league = league
        self.track_id = track_id
        self.name = name
        self.short_name = short_name
        self.scheduled = scheduled
        self.status = status
        self.venue_uid = venue_uid
        self.home_uid = home_uid
        self.home_score = home_score
        self.away_uid = away_uid
        self.away_score = away_score
        self.uid = uid


# -------------------------------------------------------------------------
# BROADCAST ASSETS
# -------------------------------------------------------------------------

LEAGUE_META = {
    'in_progress': {'name': 'LIVE FEED', 'accent': '#ef4444'},
    'completed': {'name': 'FINAL', 'accent': '#94a3b8'},
    'scheduled': {'name': 'UP NEXT', 'accent': '#3b82f6'},

    'nhl': {'name': 'NHL', 'accent': '#0ea5e9'},
    'nba': {'name': 'NBA', 'accent': '#f97316'},
    'mlb': {'name': 'MLB', 'accent': '#22c55e'},
    'eng.1': {'name': 'PREMIER LG', 'accent': '#a855f7'},

    # Converted from icon → accent (chosen logically per sport/region)
    'mens-college-basketball': {'name': 'NCAAM Basketball', 'accent': '#f97316'},
    'usa.1': {'name': 'Major League Soccer', 'accent': '#0ea5e9'},
    'mex.1': {'name': 'Liga MX', 'accent': '#ef4444'},
    'esp.1': {'name': 'La Liga', 'accent': '#f97316'},
    'ita.1': {'name': 'Serie A', 'accent': '#22c55e'},
    'ger.1': {'name': 'Bundesliga', 'accent': '#ef4444'},
    'fra.1': {'name': 'Ligue 1', 'accent': '#0ea5e9'},
    'uefa.champions': {'name': 'UEFA Champions League', 'accent': '#3b82f6'},
    'uefa.europa': {'name': 'UEFA Europa League', 'accent': '#f97316'},
    'ksa.1': {'name': 'Saudi Pro League', 'accent': '#22c55e'},
    'jpn.1': {'name': 'J1 League', 'accent': '#ef4444'},
    'aus.1': {'name': 'A-League', 'accent': '#3b82f6'},
    'usa.nwsl': {'name': 'NWSL', 'accent': '#e11d48'},
    'por.1': {'name': 'Primeira Liga', 'accent': '#22c55e'},
    'ned.1': {'name': 'Eredivisie', 'accent': '#f97316'},

    'default': {'name': 'SPORTS', 'accent': '#3b82f6'}
}



# -------------------------------------------------------------------------
# GRAPHICS ENGINE (CSS)
# -------------------------------------------------------------------------

def _styles() -> str:
    return '''
    @import url('https://fonts.googleapis.com/css2?family=Anton&family=Roboto+Condensed:wght@700&display=swap');

    :root {
        --glass-shine: linear-gradient(180deg, rgba(255,255,255,0.15) 0%, rgba(255,255,255,0) 100%);
        --panel-bg: #1e293b;
    }

    body {
        margin: 0;
        padding: 0;
        background-color: #050505;
        background-image: 
            linear-gradient(rgba(18, 16, 16, 0) 50%, rgba(0, 0, 0, 0.25) 50%), 
            linear-gradient(90deg, rgba(255, 0, 0, 0.06), rgba(0, 255, 0, 0.02), rgba(0, 0, 255, 0.06));
        background-size: 100% 2px, 3px 100%; /* TV Scanlines */
        color: #fff;
        font-family: 'Roboto Condensed', sans-serif;
        -webkit-font-smoothing: antialiased;
        min-height: 100vh;
    }

    /* HEADER GRAPHIC */
    .broadcast-header {
        display: flex;
        align-items: center;
        padding: 2rem 3rem;
        background: linear-gradient(90deg, rgba(0,0,0,0.9) 0%, rgba(0,0,0,0.4) 100%);
        border-bottom: 4px solid var(--accent);
        box-shadow: 0 10px 30px rgba(0,0,0,0.8);
        position: sticky;
        top: 0;
        z-index: 10;
        margin-bottom: 2rem;
    }

    .header-pill {
        background: var(--accent);
        color: white;
        padding: 0.25rem 1rem;
        font-family: 'Anton', sans-serif;
        font-size: 1.5rem;
        letter-spacing: 1px;
        transform: skew(-10deg);
        margin-right: 1.5rem;
        box-shadow: 0 0 15px var(--accent);
    }

    .header-pill span { display: block; transform: skew(10deg); }

    .header-title {
        font-family: 'Anton', sans-serif;
        font-size: 4rem;
        text-transform: uppercase;
        line-height: 1;
        text-shadow: 0 4px 4px rgba(0,0,0,0.5);
    }

    /* GRID CONTAINER */
    .feed-container {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(600px, 1fr)); /* Responsive, wide cards */
        gap: 2rem;
        padding: 0 3rem 3rem 3rem;
    }

    /* CARD GRAPHIC */
    .score-card {
        background: var(--panel-bg);
        border: 1px solid #334155;
        border-top: 1px solid #64748b; /* Highlight bevel */
        border-radius: 4px;
        display: flex;
        height: 180px; /* Fixed height for uniformity */
        box-shadow: 0 20px 40px rgba(0,0,0,0.6);
        position: relative;
        overflow: hidden;
    }

    /* Left Status Bar */
    .status-column {
        width: 130px;
        background: #0f172a;
        display: flex;
        flex-direction: column;
        justify-content: center;
        align-items: center;
        border-right: 1px solid #334155;
        z-index: 2;
        padding: 1rem;
        text-align: center;
    }

    .status-main {
        font-family: 'Anton', sans-serif;
        font-size: 2rem;
        line-height: 1;
        margin-bottom: 0.5rem;
    }

    .status-sub {
        font-size: 1rem;
        color: #94a3b8;
        text-transform: uppercase;
        font-weight: 700;
    }

    .status-live { color: #ef4444; text-shadow: 0 0 20px rgba(239, 68, 68, 0.6); }
    .status-time { color: #fbbf24; }

    /* Match Content */
    .match-content {
        flex: 1;
        display: flex;
        flex-direction: column;
    }

    /* Team Row */
    .team-row {
        flex: 1;
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 0 2rem;
        position: relative;
        overflow: hidden; /* Ensures gradient stays in bounds */
    }

    /* The "Safe Text" Gradient.
       This sits ON TOP of the team background color.
       It creates a dark fade from left-to-right so white text is always readable.
    */
    .team-row::before {
        content: "";
        position: absolute;
        top: 0; left: 0; right: 0; bottom: 0;
        background: linear-gradient(90deg, rgba(15,23,42,0.95) 0%, rgba(15,23,42,0.85) 35%, rgba(15,23,42,0.2) 100%);
        z-index: 1;
    }

    /* Top Shine Effect */
    .team-row::after {
        content: "";
        position: absolute;
        top: 0; left: 0; right: 0; height: 50%;
        background: var(--glass-shine);
        opacity: 0.3;
        z-index: 2;
        pointer-events: none;
    }

    .team-info-group {
        display: flex;
        align-items: center;
        gap: 1rem;
        z-index: 3;
        flex: 1; /* Allow text to take space */
        min-width: 0; /* Enable truncation if absolutely necessary */
    }

    .team-name {
        font-family: 'Anton', sans-serif;
        font-size: clamp(1.5rem, 2.5vw, 2.5rem); /* Responsive typography */
        color: white;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        white-space: nowrap;
        text-shadow: 2px 2px 0px rgba(0,0,0,0.8);
    }

    .team-score {
        font-family: 'Anton', sans-serif;
        font-size: 3.5rem;
        color: white;
        z-index: 3;
        text-shadow: 3px 3px 0px rgba(0,0,0,0.8);
        padding-left: 1rem;
    }

    .score-dim { opacity: 0.3; }

    /* Winner Styling (Gold Accent) */
    .team-row.winner .team-name { color: #fbbf24; }
    .team-row.winner .team-score { color: #fbbf24; }

    '''


# -------------------------------------------------------------------------
# HELPERS
# -------------------------------------------------------------------------

def _parse_team(uid: str) -> Dict:
    p = uid.split(':')
    return {
        'abbr': p[1].upper() if len(p) > 1 else 'TBD',
        'name': p[2].replace('_', ' ').upper() if len(p) > 2 else 'TEAM',
        'color': f"#{p[3]}" if len(p) > 3 else '#333333',
    }


def _time_display(scheduled: dt, tz_offset: int = -5) -> str:
    local = scheduled + timedelta(hours=tz_offset)
    return local.strftime('%-I:%M %p')


def _render_card(e: Dict) -> str:
    h, a = e['home'], e['away']
    status = e['status']

    # Status Logic
    if status == 'live':
        status_col = '''
            <div class="status-main status-live">LIVE</div>
            <div class="status-sub">In Progress</div>
        '''
    elif status == 'final':
        status_col = '''
            <div class="status-main">FINAL</div>
        '''
    else:
        status_col = f'''
            <div class="status-main status-time">{_time_display(e['scheduled'])}</div>
            <div class="status-sub">ET (US)</div>
        '''

    # Scores
    if status == 'scheduled':
        h_score = '<span class="team-score score-dim">0</span>'
        a_score = '<span class="team-score score-dim">0</span>'
    else:
        h_score = f'<span class="team-score">{e["home_score"]}</span>'
        a_score = f'<span class="team-score">{e["away_score"]}</span>'

    # Winner Logic
    h_cls, a_cls = "", ""
    if status == 'final':
        try:
            if int(e['home_score']) > int(e['away_score']):
                h_cls = "winner"
            else:
                a_cls = "winner"
        except:
            pass

    # Inlines styles used for dynamic team colors
    return f'''
    <div class="score-card">
        <div class="status-column">
            {status_col}
        </div>
        <div class="match-content">
            <div class="team-row {a_cls}" style="background-color: {a['color']};">
                <div class="team-info-group">
                    <span class="team-name">{a['name']}</span>
                </div>
                {a_score}
            </div>
            <div class="team-row {h_cls}" style="background-color: {h['color']};">
                <div class="team-info-group">
                    <span class="team-name">{h['name']}</span>
                </div>
                {h_score}
            </div>
        </div>
    </div>
    '''


def _generate_html(title: str, content: str, meta: Dict) -> str:
    accent = meta.get('accent', '#2563eb')
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{_styles()}</style>
</head>
<body style="--accent: {accent};">
    <div class="broadcast-header">
        <div class="header-pill">
            <span>{meta.get('name', 'SPORTS')}</span>
        </div>
    </div>
    <div class="feed-container">
        {content}
    </div>
</body>
</html>'''


# -------------------------------------------------------------------------
# GENERATOR LOGIC
# -------------------------------------------------------------------------

def generate_sports_dashboards(events: List[SportsEvent]) -> Dict[str, str]:
    leagues: Dict[str, list] = {}
    status_groups = {'scheduled': [], 'live': [], 'final': []}
    seen = set()

    for e in sorted(events, key=lambda x: x.timestamp_utc, reverse=True):
        if e.track_id in seen: continue
        seen.add(e.track_id)

        s = e.status.lower()
        if 'final' in s or 'full_time' in s:
            status = 'final'
        elif 'scheduled' in s:
            status = 'scheduled'
        else:
            status = 'live'

        evt = {
            'scheduled': e.scheduled,
            'status': status,
            'home': _parse_team(e.home_uid),
            'away': _parse_team(e.away_uid),
            'home_score': e.home_score,
            'away_score': e.away_score
        }

        if e.league not in leagues: leagues[e.league] = []
        leagues[e.league].append(evt)
        status_groups[status].append(evt)

    output: Dict[str, str] = {}

    # Leagues
    for lid, evts in leagues.items():
        meta = LEAGUE_META.get(lid, LEAGUE_META['default'])
        # No item limit, frontend handles scroll
        cards = ''.join([_render_card(e) for e in evts])
        output[f"league_{lid}"] = _generate_html(f"{meta['name']} FEED", cards, meta)

    # Status Pages
    key_map = {'live': 'in_progress', 'scheduled': 'scheduled', 'final': 'completed'}
    for status, evts in status_groups.items():
        status_key = key_map[status]
        meta = LEAGUE_META.get(status_key, LEAGUE_META['default'])
        cards = ''.join([_render_card(e) for e in evts])
        output[f"{status_key}"] = _generate_html(meta['name'], cards, meta)

    return output


# -------------------------------------------------------------------------
# RUN
# -------------------------------------------------------------------------
if __name__ == "__main__":
    # Test Data
    test_events = [
        SportsEvent(dt.now(timezone.utc), 'basketball', 'nba', '101', 'Celtics at Heat', 'BOS @ MIA',
                    dt.now(timezone.utc), 'STATUS_IN_PROGRESS', '',
                    'heat:mia:miami_heat:98002e:f9a01b', '102',
                    'celtics:bos:boston_celtics:007a33:ba9653', '98', '1'),
        SportsEvent(dt.now(timezone.utc), 'hockey', 'nhl', '105', 'Rangers at Devils', 'NYR @ NJD',
                    dt.now(timezone.utc), 'STATUS_IN_PROGRESS', '',
                    'devils:njd:new_jersey_devils:ce1126:000000', '2',
                    'rangers:nyr:new_york_rangers:0038a8:ce1126', '2', '5'),
        SportsEvent(dt.now(timezone.utc), 'football', 'eng.1', '102', 'Arsenal at Tottenham', 'ARS @ TOT',
                    dt.now(timezone.utc) + timedelta(hours=2), 'STATUS_SCHEDULED', '',
                    'tottenham:tot:tottenham_hotspur:132257:ffffff', '0',
                    'arsenal:ars:arsenal:ef0107:ffffff', '0', '2'),
        SportsEvent(dt.now(timezone.utc), 'hockey', 'nhl', '103', 'Oilers at Flames', 'EDM @ CGY',
                    dt.now(timezone.utc) - timedelta(hours=5), 'STATUS_FINAL', '',
                    'flames:cgy:calgary_flames:c8102e:f1be48', '3',
                    'oilers:edm:edmonton_oilers:041e42:ff4c00', '5', '3'),
    ]

    dashboards = generate_sports_dashboards(test_events)

    for key, html in dashboards.items():
        filename = f"{key}.html"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"✓ GENERATED BROADCAST ASSET: {filename}")