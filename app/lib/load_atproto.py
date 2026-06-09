from __future__ import annotations

from typing import Any

from app.lib.link_social import link_social_to_weather


def load_social_for_weather_events(
    *,
    DATA_DIR: str | None,
    weather_events: list[dict[str, Any]],
    force: bool = False,
    max_events_per_weather: int = 25,
    max_seconds_per_weather: int = 8,
    wait_seconds: int = 30 * 60,
) -> int:
    # Kept so old loop imports do not break.
    # The social stream is persistent now.
    # This function only links existing stored social records to weather events.
    return link_social_to_weather(
        DATA_DIR=DATA_DIR,
        events=weather_events,
        force=force,
    )