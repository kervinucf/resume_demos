from __future__ import annotations

import json
import ssl
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import certifi


class OpenMeteoApiError(RuntimeError):
    pass


class OpenMeteoApiClient:
    """
    Transport-only Open-Meteo client.

    Responsibilities:
    - fetch raw geocoding JSON
    - fetch raw forecast/current weather JSON

    Non-responsibilities:
    - map weather codes to descriptions
    - build DTOs
    - publish into HyperCore
    """

    GEOCODING_API_BASE = "https://geocoding-api.open-meteo.com/v1/search"
    FORECAST_API_BASE = "https://api.open-meteo.com/v1/forecast"
    USER_AGENT = "hypercore-open-meteo-client/1.0"

    def __init__(
        self,
        *,
        timeout: int = 20,
        user_agent: str | None = None,
    ) -> None:
        self.timeout = int(timeout)
        self.user_agent = user_agent or self.USER_AGENT
        self.ssl_ctx = ssl.create_default_context(cafile=certifi.where())

    def _request(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        full_url = url
        if params:
            full_url = f"{url}?{urlencode(params, doseq=True)}"

        req = Request(
            full_url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json, */*",
            },
        )

        try:
            with urlopen(req, timeout=timeout or self.timeout, context=self.ssl_ctx) as resp:
                body = resp.read().decode("utf-8")
                data = json.loads(body)
        except Exception as exc:
            raise OpenMeteoApiError(
                f"GET {full_url} failed: {type(exc).__name__}: {exc}"
            ) from exc

        if not isinstance(data, dict):
            raise OpenMeteoApiError(f"GET {full_url} returned non-object JSON")

        return data

    def geocode_location(
        self,
        *,
        name: str,
        count: int = 1,
        language: str | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "name": name,
            "count": count,
        }
        if language:
            params["language"] = language

        return self._request(
            self.GEOCODING_API_BASE,
            params=params,
            timeout=timeout,
        )

    def get_current_weather(
        self,
        *,
        latitude: float,
        longitude: float,
        timezone: str = "auto",
        current: list[str] | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        current_fields = current or [
            "temperature_2m",
            "wind_speed_10m",
            "weather_code",
            "precipitation",
            "rain",
            "showers",
            "snowfall",
        ]

        params: dict[str, Any] = {
            "latitude": latitude,
            "longitude": longitude,
            "current": ",".join(current_fields),
            "timezone": timezone,
        }

        return self._request(
            self.FORECAST_API_BASE,
            params=params,
            timeout=timeout,
        )

    def geocode_first_result(
        self,
        *,
        name: str,
        language: str | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        data = self.geocode_location(
            name=name,
            count=1,
            language=language,
            timeout=timeout,
        )
        results = data.get("results")

        if not isinstance(results, list) or not results:
            raise OpenMeteoApiError(f"Location not found: {name}")

        first = results[0]
        if not isinstance(first, dict):
            raise OpenMeteoApiError(f"Invalid geocoding result for location: {name}")

        return first

    def get_current_weather_for_location(
        self,
        *,
        name: str,
        language: str | None = None,
        timezone: str = "auto",
        current: list[str] | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        place = self.geocode_first_result(
            name=name,
            language=language,
            timeout=timeout,
        )

        latitude = place.get("latitude")
        longitude = place.get("longitude")

        if latitude is None or longitude is None:
            raise OpenMeteoApiError(f"Missing coordinates for location: {name}")

        weather = self.get_current_weather(
            latitude=float(latitude),
            longitude=float(longitude),
            timezone=timezone,
            current=current,
            timeout=timeout,
        )

        return {
            "place": place,
            "weather": weather,
        }