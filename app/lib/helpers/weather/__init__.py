from __future__ import annotations

from HyperCoreSDK.python.client import HyperClient, projection, ScopeSpec, ValueIndexSpec
from app.lib.sources.open_metro import send_open_meteo_request
from ..geo import LocationObject
from .factory import WeatherFactory, WeatherEventObject

weather_factory = WeatherFactory()

__all__ = [
    "HyperClient",
    "create_weather_candidate",
    "create_weather_event_object",
    "apply_graph_operations",
    "WeatherEventObject",
    "WEATHER_INDEXES",
    "weather_factory",
]
# Fields each index entry carries forward for cheap rendering (information density).
PROJECT = projection(
    "name", "country_code", "country_flag_emoji",
    "temperature", "condition", "observed_at", "lat", "lon",
)

WEATHER_INDEXES = [
            ValueIndexSpec("country_code", "country_code", normalize="upper", link_projections=PROJECT),
            ValueIndexSpec("condition", "condition", normalize="slug", link_projections=PROJECT),
            ValueIndexSpec(
                "condition", "condition", normalize="slug",
                scopes=[ScopeSpec("country_code", normalize="upper")],
                link_projections=PROJECT,
            ),
        ]

def create_weather_candidate(latitude: float, longitude: float):
    observation = send_open_meteo_request(
        latitude=latitude,
        longitude=longitude,
    )

    if observation is None:
        return None, False

    try:
        # send_open_meteo_request returns: time, temp, wind, precip, code_int, condition
        local_time, temp, wind, precip, code_int, condition = observation
    except (TypeError, ValueError) as exc:
        print(f"  skip: invalid response ({exc})", flush=True)
        return observation, False

    proof = temp is not None
    return observation, proof


def create_weather_event_object(
        location_object: LocationObject,
        observation,
):
    try:
        local_time, temp, wind, precip, code_int, condition = observation
    except (TypeError, ValueError) as exc:
        print(f"  skip: invalid response ({exc})", flush=True)
        return None, False

    weather_event_object = weather_factory.create_weather_object(
        location_object=location_object,
        temp=temp,
        wind=wind,
        precip=precip,
        condition=condition,
        code_int=code_int,
        local_time=local_time,
    )

    # EV: proof must reflect a real object, not a hardcoded False
    proof = weather_event_object is not None and temp is not None
    print(f"  event: {location_object.name} temp={temp} proof={proof}", flush=True)
    return weather_event_object, proof


def apply_graph_operations(
        weather_event_object: WeatherEventObject,
        client_instance: HyperClient,
        namespace,
):
    loc = weather_event_object.location                    # "1a-12748861"
    compact = weather_event_object.compact_ts()          # "20260602T203325"

    event_path = f"events/{loc}/{compact}"
    latest_path = f"latest/{loc}"
    sidecar_path = f"locations/{loc}/refs/weather"

    event_dot = f"{namespace}.{event_path.replace('/', '.')}"
    latest_dot = f"{namespace}.{latest_path.replace('/', '.')}"

    # 1) RECORD — indexed, dense
    # weather (the event record; latest + sidecar stay as write_ops — they're pointers, not records)
    n1 = client_instance.save_record(
        path=f"events/{loc}/{compact}",
        data=weather_event_object.__dict__,
        indexes=WEATHER_INDEXES,
        root=namespace,
    )

    # 2) POINTER weather.latest.<loc> -> event
    n2 = client_instance.write_ops([{
        "path": latest_dot,
        "data": {"data": {"tag": "weather_latest", "origin": loc},
                 "links": {"event": event_dot}},
    }])

    # 3) SIDECAR geo.locations.<loc>.refs.weather -> latest
    n3 = client_instance.write_ops(
        [{
            "path": f"geo.{sidecar_path.replace('/', '.')}",
            "data": {"data": {"tag": "ref", "rel": "weather"},
                     "links": {"source": latest_dot}},
        }],
        root="geo",  # <-- send to /geo/api/batch, not /weather/api/batch
    )

    # EV: three writes, one line — any zero count flags the broken kind
    print(f"[weather] {event_dot} record={n1} latest={n2} sidecar={n3}", flush=True)
    return {"event": event_dot, "latest": latest_dot}



