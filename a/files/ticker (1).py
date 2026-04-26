import random
import datetime
from typing import List, Dict

# --- [I] DTO (Data Transfer Object) for News Events ---
class NewsEvent:
    """A simple class to hold news event data."""
    def __init__(self, published_utc, date_published, region, title, source, link, summary, author, credit, image, video, latitude, longitude, uid):
        self.published_utc = published_utc
        self.date_published = date_published
        self.region = region
        self.title = title
        self.source = source
        self.link = link
        self.summary = summary
        self.author = author
        self.credit = credit
        self.image = image
        self.video = video
        self.latitude = latitude
        self.longitude = longitude
        self.uid = uid

# --- [II] HELPER FUNCTIONS ---
def _get_region_flag(text_to_search: str) -> str:
    """
    Finds a relevant country or regional flag emoji based on keywords in the text.
    Searches for specific countries first, then falls back to broader regions.
    """
    flags = {
        'North Korea': '🇰🇵', 'South Korea': '🇰🇷', 'Nigeria': '🇳🇬', 'Australia': '🇦🇺',
        'New Zealand': '🇳🇿', 'Japan': '🇯🇵', 'Dominican Republic': '🇩🇴',
        'Russia': '🇷🇺', 'Ukraine': '🇺🇦', 'USA': '🇺🇸', 'Hungary': '🇭🇺',
        'Asia': '🌏', 'Europe': '🇪🇺', 'Africa': '🌍', 'Americas': '🌎',
        'Middle East': '🌍'
    }
    for name, flag in flags.items():
        if name in text_to_search:
            return flag
    if text_to_search in flags:
        return flags[text_to_search]
    return '🌐'

def _get_attribution(event: NewsEvent) -> str:
    """
    Determines the best attribution string, prioritizing a known author,
    then a credit (if different from the source), and finally 'Unknown'.
    """
    # 1. Prioritize a known author
    author = event.author.strip() if event.author else ''
    if author and author.lower() != 'unknown':
        return author

    # 2. Fallback to credit, but only if it's useful and not redundant
    credit = event.credit.strip() if event.credit else ''
    source = event.source.strip() if event.source else ''
    if credit and credit.lower() != 'unknown' and credit.lower() != source.lower():
        return credit

    # 3. Default if no valid author or credit is found
    return 'Unknown'


# --- [III] NEWS TICKER AND CHYRON GENERATORS ---

def create_news_chyron(events: List[NewsEvent], count: int = 4) -> Dict[str, str]:
    """
    Generates a dictionary of distinct, formatted headlines suitable for a news broadcast chyron.
    It prioritizes the most recent unique headlines.
    """
    chyrons = {}
    seen_titles = set()
    sorted_events = sorted(events, key=lambda x: x.published_utc, reverse=True)

    templates = [
        "FROM {source}: {title} (VIA {attribution})",
        "{source}: {title} (VIA {attribution})"

    ]

    events_to_display = []
    for event in sorted_events:
        if event.title not in seen_titles:
            events_to_display.append(event)
            seen_titles.add(event.title)
        if len(events_to_display) >= count:
            break

    for i, event in enumerate(events_to_display):
        template = random.choice(templates)
        flag = _get_region_flag(event.title) or _get_region_flag(event.region)
        short_source = event.source.split('|')[0].replace('World News Today: International News Headlines - ', '').strip()
        attribution = _get_attribution(event)

        # Smartly adjust template if attribution is 'Unknown' to avoid "VIA Unknown"
        final_template = template
        if "VIA {attribution}" in template and attribution == 'Unknown':
            final_template = "FROM {source}: {title}"

        formatted_chyron = final_template.format(
            title=event.title.strip(),
            region=event.region.upper(),
            source=short_source,
            region_flag=flag,
            attribution=attribution
        ).upper()
        chyrons[f"headline_{i+1}"] = formatted_chyron

    return chyrons


def create_news_ticker(events: List[NewsEvent]) -> str:
    """
    Generates a continuous, scrolling-style news ticker string.
    This includes all unique headlines, formatted with source and attribution (if known).
    """
    ticker_segments = []
    seen_titles = set()
    sorted_events = sorted(events, key=lambda x: x.published_utc, reverse=True)

    for event in sorted_events:
        if event.title not in seen_titles:
            flag = _get_region_flag(event.title) or _get_region_flag(event.region)
            short_source = event.source.split('|')[0].strip()
            attribution = _get_attribution(event)

            # **MODIFIED LOGIC**: Conditionally format the segment to hide 'Unknown' attribution
            if attribution != 'Unknown':
                # Format with attribution
                segment = f"{flag} {event.title.strip()} ({short_source} / {attribution})"
            else:
                # Format without attribution
                segment = f"{flag} {event.title.strip()} ({short_source})"

            ticker_segments.append(segment)
            seen_titles.add(event.title)

    if not ticker_segments:
        return "No news available to generate ticker."
    return " ••• ".join(ticker_segments)


# --- [IV] DEMONSTRATION ---
if __name__ == "__main__":
    your_news_events_list = [NewsEvent(published_utc=datetime.datetime(2025, 10, 22, 5, 35), date_published='2025-10-22', region='Asia', title='North Korea fires ballistic missile towards east, South Korea says', source='World News Today: International News Headlines - The Hindu | The Hindu', link='...', summary='...', author='Unknown', credit='', image='...', video=None, latitude='...', longitude='...', uid='...'), NewsEvent(published_utc=datetime.datetime(2025, 10, 22, 5, 40), date_published='2025-10-22', region='Asia', title='Truck laden with gasoline explodes in Nigeria and kills at least 31 people, police say', source='World News Today: International News Headlines - The Hindu | The Hindu', link='...', summary='...', author='Chinedu Asadu', credit='AP', image='...', video=None, latitude='...', longitude='...', uid='...'), NewsEvent(published_utc=datetime.datetime(2025, 10, 22, 5, 41), date_published='2025-10-22', region='Asia', title='Australia and New Zealand set for fierce winds and heat', source='The Straits Times World News', link='...', summary='...', author='Unknown', credit='The Straits Times World News', image=None, video=None, latitude='...', longitude='...', uid='...'), NewsEvent(published_utc=datetime.datetime(2025, 10, 22, 5, 51), date_published='2025-10-22', region='Asia', title='North Korea fires ballistic missile one day after Takaichi named PM', source='Latest articles - The Japan Times', link='...', summary='...', author='Unknown', credit='REUTERS', image='...', video=None, latitude='...', longitude='...', uid='...'), NewsEvent(published_utc=datetime.datetime(2025, 10, 22, 5, 52), date_published='2025-10-22', region='Asia', title='North Korea fires ballistic missile towards east, South Korea says', source='The Straits Times World News', link='...', summary='...', author='Unknown', credit='', image=None, video=None, latitude='...', longitude='...', uid='...'), NewsEvent(published_utc=datetime.datetime(2025, 10, 22, 5, 59), date_published='2025-10-22', region='Asia', title="Trump doesn't want 'wasted meeting' with Putin, confirms talks on Ukraine war are off for now", source='World News Today: International News Headlines - The Hindu | The Hindu', link='...', summary='...', author='Unknown', credit='', image='...', video=None, latitude='...', longitude='...', uid='...'), NewsEvent(published_utc=datetime.datetime(2025, 10, 22, 6, 0), date_published='2025-10-22', region='Asia', title='80 rescued after mine collapses in Dominican Republic, no injury or death reported', source='World News Today: International News Headlines - The Hindu | The Hindu', link='...', summary='...', author='Unknown', credit='Agency', image='...', video=None, latitude='...', longitude='...', uid='...')]

    print("--- DYNAMIC NEWS CHYRON GENERATION (showing 3 runs for variety) ---")
    for i in range(3):
        print(f"\n--- RUN #{i + 1} ---")
        chyron_outputs = create_news_chyron(your_news_events_list)
        for key, value in chyron_outputs.items():
            print(f"[{key.upper()}]: {value}")

    print("\n" + "="*50 + "\n")

    print("--- COMPREHENSIVE NEWS TICKER (WITH CLEANER ATTRIBUTION) ---")
    sequential_ticker = create_news_ticker(your_news_events_list)
    print(f"TICKER: {sequential_ticker}")