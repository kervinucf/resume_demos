import random
import datetime
from typing import List, Dict
from collections import defaultdict
from datetime import datetime as dt, timezone, timedelta


# --- [I] DTO (Data Transfer Object) for Sports Events ---
class SportsEvent:
    """A simple class to hold sports event data."""

    def __init__(self, sport, league, name, short_name, scheduled, status, home_uid, home_score, away_uid, away_score,
                 uid):
        self.sport = sport
        self.league = league
        self.name = name
        self.short_name = short_name
        self.scheduled = scheduled
        self.status = status
        self.home_uid = home_uid
        self.home_score = home_score
        self.away_uid = away_uid
        self.away_score = away_score
        self.uid = uid


# --- [II] METADATA & HELPER FUNCTIONS ---

LEAGUE_METADATA = {
        'in_progress': {'name': 'In-Progress Games', 'logo': '🏅'},
        'completed': {'name': 'Completed Games', 'logo': '🏅'},
        'scheduled': {'name': 'Scheduled Games', 'logo': '🏅'},
        'nhl': {'name': 'National Hockey League', 'logo': '🏒'},
        'nba': {'name': 'National Basketball Association', 'logo': '🏀'},
        'mlb': {'name': 'Major League Baseball', 'logo': '⚾'},
    'nfl': {'name': 'National Football League', 'logo': '🏈'},
        'mens-college-basketball': {'name': 'NCAAM Basketball', 'logo': '🏀'},
        'usa.1': {'name': 'Major League Soccer', 'logo': '⚽'},
        'mex.1': {'name': 'Liga MX', 'logo': '⚽'},
        'eng.1': {'name': 'Premier League', 'logo': '⚽'},
        'esp.1': {'name': 'La Liga', 'logo': '⚽'},
        'ita.1': {'name': 'Serie A', 'logo': '⚽'},
        'ger.1': {'name': 'Bundesliga', 'logo': '⚽'},
        'fra.1': {'name': 'Ligue 1', 'logo': '⚽'},
        'uefa.champions': {'name': 'UEFA Champions League', 'logo': '⚽'},
        'uefa.europa': {'name': 'UEFA Europa League', 'logo': '⚽'},
        'ksa.1': {'name': 'Saudi Pro League', 'logo': '⚽'},
        'jpn.1': {'name': 'J1 League', 'logo': '⚽'},
        'aus.1': {'name': 'A-League', 'logo': '⚽'},
        'usa.nwsl': {'name': 'NWSL', 'logo': '⚽'},
        'por.1': {'name': 'Primeira Liga', 'logo': '⚽'},
        'ned.1': {'name': 'Eredivisie', 'logo': '⚽'},
        'default': {'name': 'Sports Center', 'logo': '🏅'}
    }


def _parse_team_uid(uid_str: str) -> Dict:
    """Parses a team's UID string into a structured dictionary."""
    parts = uid_str.split(':')
    try:
        return {'abbr': parts[1].upper(), 'name': parts[2].replace('_', ' ').title()}
    except IndexError:
        return {'abbr': 'N/A', 'name': 'Unknown Team'}


def _format_game_time(scheduled_dt: dt) -> str:
    """Formats a datetime object into a clean time string like '7:00 PM CT'."""
    if not isinstance(scheduled_dt, dt):
        return "TBD"
    # Convert from UTC to Central Time (CDT is UTC-5)
    local_time = scheduled_dt.astimezone(timezone(timedelta(hours=-5)))
    return local_time.strftime('%-I:%M %p CT')


# --- [III] SPORTS TICKER AND CHYRON GENERATORS ---

def create_sports_chyron(events: List[SportsEvent], count: int = 4) -> Dict[str, str]:
    """
    Generates a dictionary of distinct, formatted game updates for a sports broadcast chyron.
    It prioritizes live, then recently completed, then upcoming games.
    """
    chyrons = {}

    # Define a sort order: live (1) > completed (2) > scheduled (3)
    status_order = {'in_progress': 1, 'completed': 2, 'scheduled': 3}

    # Sort events by priority
    sorted_events = sorted(events, key=lambda e: (
        status_order.get(e.status.lower().replace('status_', ''), 99),
        -e.scheduled.timestamp() if 'final' in e.status.lower() else e.scheduled.timestamp()
    ))

    # Define templates for different game statuses
    templates = {
        "completed": [
            "FINAL SCORE: {winning_team_name} tops {losing_team_name} {winning_score}-{losing_score}",
            "{league_logo} {winning_team_abbr} DEFEATS {losing_team_abbr} | FINAL: {winning_score}-{losing_score}",
        ],
        "scheduled": [
            "UP NEXT ON {league_name}: {away_team_name} at {home_team_name} - {time}",
            "MATCHUP ALERT: {away_team_abbr} vs {home_team_abbr} at {time}",
        ],
        "in_progress": [
            "LIVE SCORE: {away_team_name} {away_score} - {home_team_name} {home_score}",
            "{league_logo} NOW: {away_team_abbr} {away_score} | {home_team_abbr} {home_score}",
        ]
    }

    for i, event in enumerate(sorted_events[:count]):
        home_team = _parse_team_uid(event.home_uid)
        away_team = _parse_team_uid(event.away_uid)
        league_info = LEAGUE_METADATA.get(event.league, LEAGUE_METADATA['default'])

        status_key = 'completed' if 'final' in event.status.lower() else \
            'scheduled' if 'scheduled' in event.status.lower() else 'in_progress'

        template = random.choice(templates[status_key])

        # Prepare data for formatting
        format_data = {
            "home_team_name": home_team['name'], "away_team_name": away_team['name'],
            "home_team_abbr": home_team['abbr'], "away_team_abbr": away_team['abbr'],
            "home_score": event.home_score, "away_score": event.away_score,
            "league_name": league_info['name'], "league_logo": league_info['logo'],
            "time": _format_game_time(event.scheduled)
        }

        # Add winner/loser info for completed games
        if status_key == 'completed':
            try:
                if int(event.home_score) > int(event.away_score):
                    format_data.update({"winning_team_name": home_team['name'], "losing_team_name": away_team['name'],
                                        "winning_team_abbr": home_team['abbr'], "losing_team_abbr": away_team['abbr'],
                                        "winning_score": event.home_score, "losing_score": event.away_score})
                else:
                    format_data.update({"winning_team_name": away_team['name'], "losing_team_name": home_team['name'],
                                        "winning_team_abbr": away_team['abbr'], "losing_team_abbr": home_team['abbr'],
                                        "winning_score": event.away_score, "losing_score": event.home_score})
            except (ValueError, TypeError):
                continue  # Skip if scores aren't comparable integers

        chyrons[f"update_{i + 1}"] = template.format(**format_data).upper()

    return chyrons


def create_sports_ticker(events: List[SportsEvent]) -> str:
    """
    Generates a continuous, scrolling-style sports ticker string.
    """
    ticker_segments = []

    # Use the same priority sorting as the chyron
    status_order = {'in_progress': 1, 'completed': 2, 'scheduled': 3}
    sorted_events = sorted(events, key=lambda e: (
        status_order.get(e.status.lower().replace('status_', ''), 99),
        -e.scheduled.timestamp() if 'final' in e.status.lower() else e.scheduled.timestamp()
    ))

    for event in sorted_events:
        home_team = _parse_team_uid(event.home_uid)
        away_team = _parse_team_uid(event.away_uid)
        league_name = LEAGUE_METADATA.get(event.league, LEAGUE_METADATA['default'])['name']

        segment = ""
        if 'final' in event.status.lower():
            segment = f"{league_name} FINAL: {away_team['abbr']} {event.away_score} - {home_team['abbr']} {event.home_score}"
        elif 'scheduled' in event.status.lower():
            segment = f"{league_name}: {away_team['abbr']} at {home_team['abbr']} ({_format_game_time(event.scheduled)})"
        else:  # In-Progress
            segment = f"{league_name} LIVE 🔴 : {away_team['abbr']} {event.away_score} - {home_team['abbr']} {event.home_score}"

        ticker_segments.append(segment)

    if not ticker_segments:
        return "No game updates available."
    return " ••• ".join(ticker_segments)


# --- [IV] DEMONSTRATION ---
if __name__ == "__main__":
    your_sports_events_list = [
        SportsEvent(sport='basketball', league='nba', name='Warriors at Lakers', short_name='GS @ LAL',
                    scheduled=dt(2025, 10, 22, 2, 0, tzinfo=timezone.utc), status='STATUS_FINAL',
                    home_uid='lakers:lal:los_angeles_lakers', home_score='109',
                    away_uid='warriors:gs:golden_state_warriors', away_score='119', uid='1'),
        SportsEvent(sport='hockey', league='nhl', name='Blue Jackets at Stars', short_name='CBJ @ DAL',
                    scheduled=dt(2025, 10, 22, 0, 0, tzinfo=timezone.utc), status='STATUS_FINAL',
                    home_uid='stars:dal:dallas_stars', home_score='1',
                    away_uid='blue_jackets:cbj:columbus_blue_jackets', away_score='5', uid='2'),
        SportsEvent(sport='soccer', league='eng.1', name='West Ham at Leeds', short_name='WHU @ LEE',
                    scheduled=dt(2025, 10, 22, 10, 0, tzinfo=timezone.utc), status='STATUS_IN_PROGRESS',
                    home_uid='leeds_united:lee:leeds_united', home_score='1',
                    away_uid='west_ham_united:whu:west_ham_united', away_score='1', uid='3'),
        SportsEvent(sport='hockey', league='nhl', name='Bruins at Maple Leafs', short_name='BOS @ TOR',
                    scheduled=dt(2025, 10, 22, 23, 0, tzinfo=timezone.utc), status='STATUS_SCHEDULED',
                    home_uid='maple_leafs:tor:toronto_maple_leafs', home_score='0', away_uid='bruins:bos:boston_bruins',
                    away_score='0', uid='4'),
        SportsEvent(sport='baseball', league='mlb', name='Dodgers at Blue Jays', short_name='LAD @ TOR',
                    scheduled=dt(2025, 10, 25, 0, 0, tzinfo=timezone.utc), status='STATUS_SCHEDULED',
                    home_uid='blue_jays:tor:toronto_blue_jays', home_score='0',
                    away_uid='dodgers:lad:los_angeles_dodgers', away_score='0', uid='5')
    ]

    print("--- DYNAMIC SPORTS CHYRON GENERATION (showing 3 runs for variety) ---")
    for i in range(3):
        print(f"\n--- RUN #{i + 1} ---")
        chyron_outputs = create_sports_chyron(your_sports_events_list)
        for key, value in chyron_outputs.items():
            print(f"[{key.upper()}]: {value}")

    print("\n" + "=" * 50 + "\n")

    print("--- COMPREHENSIVE SPORTS TICKER ---")
    sequential_ticker = create_sports_ticker(your_sports_events_list)
    print(f"TICKER: {sequential_ticker}")