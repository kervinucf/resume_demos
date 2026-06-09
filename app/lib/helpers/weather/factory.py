from dataclasses import dataclass
from datetime import datetime, timezone
from HyperCoreSDK.python.dtos.object import HyperObject

from app.lib.helpers.geo import LocationObject


# ---------------------------------------------------------------------------
# Weather event
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class WeatherEventObject:
    location: str  # "1a-12748861"
    #
    name: str
    lat: float
    lon: float
    country_code: str
    country_flag_emoji: str
    temperature: float
    condition: str | None
    weather_code: int | None
    wind_speed: float | None
    precipitation: float | None
    local_time: str
    observed_at: str

    def compact_ts(self) -> str:
        """Sortable, dot/slash-safe event id: 20260602T203325 (lexical == chronological)."""
        return datetime.fromisoformat(self.observed_at).strftime("%Y%m%dT%H%M%S")


class WeatherFactory(HyperObject):

    @staticmethod
    def _from_record(record_dict: dict) -> WeatherEventObject:
        # The reader hands us a clean record body; build straight from it.
        return WeatherEventObject(**record_dict)

    @classmethod
    def create_weather_object(
            cls,
            location_object: LocationObject = None,
            temp: float = None,
            condition: str = None,
            code_int: int = None,
            wind: float = None,
            precip: float = None,
            local_time: str = None,
            from_record: dict[str, "WeatherEventObject"] = None,
    ) -> WeatherEventObject:
        if from_record:
            return cls._from_record(record_dict=from_record)

        return WeatherEventObject(
            location=f"{location_object.name}-{location_object.geoname_id}",  #
            name=location_object.name,
            lat=location_object.lat,
            lon=location_object.lon,
            country_code=location_object.country_code,
            country_flag_emoji=location_object.country_flag_emoji,
            #
            temperature=float(temp),
            condition=condition,
            weather_code=code_int,
            wind_speed=float(wind) if wind is not None else None,
            precipitation=float(precip) if precip is not None else None,
            local_time=str(local_time or ""),
            observed_at=datetime.now(timezone.utc).isoformat(),
        )
