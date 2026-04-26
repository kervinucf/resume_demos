from typing import Callable, Any, Dict
from machine.m2.pds.lib.helpers.fe.controller import FrontendController
from machine.m2.server.src._services.content_manager.segments._.weather_routine import get_weather_graphics
from machine.m2.server.src._services.content_manager.segments._.air_traffic_routine import get_flight_graphics
from machine.m2.server.src._services.content_manager.segments._.sun_routine import get_sun_graphics
#
from machine.m2.server.src._services.content_manager.segments._.earthquake.segment import get_earthquake_graphics
from machine.m2.server.src._services.content_manager.segments._.finance.segment import get_finance_graphics
from machine.m2.server.src._services.content_manager.segments._.news.segment import get_news_graphics
from machine.m2.server.src._services.content_manager.segments._.sports.segment import get_sports_graphics

GRAPHICS_HANDLERS: Dict[str, Callable[..., str]] = {
    "global_headlines": get_news_graphics,
    "regional_headlines": get_news_graphics,
    "global_weather": get_weather_graphics,
    "regional_weather": get_weather_graphics,
    "global_finances": get_finance_graphics,
    "regional_finances": get_finance_graphics,
    "regional_flights": get_flight_graphics,
    "sports_events": get_sports_graphics,
    "earthquakes": get_earthquake_graphics,
    "sun_rises": get_sun_graphics,
    "sun_sets": get_sun_graphics,
}


def construct_content_script(
        current_segment: str,
        controller: FrontendController,
        broadcast: Any = None,
        database_adapter: Callable = None
):
    print(f"Constructing graphics for segment: {current_segment}")

    if not broadcast or not broadcast.showing:
        print(f"No broadcast data found for segment: {current_segment}")
        return

    handler = GRAPHICS_HANDLERS.get(current_segment)

    if not handler:
        print(f"No graphics handler found for segment: {current_segment}")
        return

    return handler(
        controller=controller,
        event_showing=broadcast.showing,
        ds=database_adapter
    ) or ""
