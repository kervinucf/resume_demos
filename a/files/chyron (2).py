import numpy as np
from collections import Counter
from datetime import datetime

def extract_location_focus(locations: list[str]) -> str:
    """Chooses the most representative full location string from a group."""
    if not locations:
        return "Unknown Location"
    # Normalize and count frequency of entire location lines
    normalized = [loc.strip() for loc in locations if loc.strip()]
    most_common = Counter(normalized).most_common(1)
    if most_common:
        return most_common[0][0]
    return normalized[0]

def summarize_cluster_region(events):
    """Computes centroid and determines if cluster is compact or widespread."""
    lats = np.array([float(ev.latitude) for ev in events])
    lngs = np.array([float(ev.longitude) for ev in events])
    centroid = (np.mean(lats), np.mean(lngs))
    lat_spread = np.ptp(lats)
    lon_spread = np.ptp(lngs)

    if lat_spread > 20 or lon_spread > 20:
        return "Widespread Activity Zone", centroid
    return extract_location_focus([ev.location for ev in events]), centroid

def create_chyron_text(cluster_events: list, access_clock) -> str:
    """Builds chyron text for a group of earthquakes."""
    if not cluster_events:
        return "No recent activity"

    region_label, _ = summarize_cluster_region(cluster_events)

    timestamps = sorted(
        [ev.timestamp_utc for ev in cluster_events],
        key=lambda t: t if isinstance(t, datetime) else datetime.fromisoformat(str(t))
    )
    earliest, latest = timestamps[0], timestamps[-1]
    hours_span = access_clock(time_since=earliest) or 0.1
    most_recent = cluster_events[-1]
    time_since = access_clock(time_since=most_recent.timestamp_utc) or 0
    recent_text = (
        f"{time_since * 60:.0f} min ago" if time_since < 1 else f"{int(time_since)} hr ago"
    )
    total_events = len(cluster_events)
    mags = [float(ev.magnitude) for ev in cluster_events]
    max_mag = max(mags)

    if total_events == 1:
        return f"{region_label} — {recent_text}"
    elif hours_span < 1:
        return f"{region_label} — {total_events} tremors within the past hour (max M{max_mag:.1f})"
    elif hours_span < 3:
        return f"{region_label} — {total_events} events in the last {int(hours_span)} hrs (max M{max_mag:.1f})"
    else:
        return f"{region_label} — {total_events} quakes over {int(hours_span)} hrs (max M{max_mag:.1f})"
