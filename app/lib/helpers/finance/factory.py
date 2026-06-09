"""
Finance factory — the typed objects + builders for the finance database.

Mirrors geo/factory.py (which carries two shapes, LocationObject + CountryCurrency):
    MarketObservationObject   — one market snapshot (stored body)
    FxRateObject              — one USD->X rate (stored body)
    FinanceFactory            — builds both from NORMALIZED provider fields

Nothing here knows yfinance — builders take plain named fields. Provider decoding
(downloads, performance math) lives in app/lib/sources/yfinance_src.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_SLUG_RE = re.compile(r"[^a-zA-Z0-9]+")
_DASHES_RE = re.compile(r"-{2,}")


def slugify(value: Any, fallback: str = "unknown") -> str:
    text = _DASHES_RE.sub("-", _SLUG_RE.sub("-", str(value or "").strip().lower())).strip("-")
    return text or fallback


def ticker_key(ticker: str) -> str:
    return slugify(str(ticker).replace("^", "idx-").replace("=", "-"), "ticker")


def _fmt_pct(value: float | None) -> str:
    return "N/A" if value is None else f"{value:+.2f}%"


@dataclass(frozen=True)
class MarketObservationObject:
    name: str
    ticker: str
    asset_class: str
    continent: str | None
    observed_at: str
    observed_day: str
    observed_ms: int
    updated_at: int

    last_price: float | None = None
    hour_pct: float | None = None
    day_intraday_pct: float | None = None
    day_vs_previous_close_pct: float | None = None
    week_pct: float | None = None
    one_month_pct: float | None = None
    three_month_pct: float | None = None
    six_month_pct: float | None = None
    one_year_pct: float | None = None
    five_year_pct: float | None = None
    ten_year_pct: float | None = None

    _finance_root: str = "finance"

    @property
    def asset_key(self) -> str:
        return slugify(self.asset_class)

    @property
    def tkey(self) -> str:
        return ticker_key(self.ticker)

    def record_key(self) -> str:
        return f"{self.asset_key}-{self.tkey}-{self.observed_ms}"

    def latest_key(self) -> str:
        return f"{self.asset_key}-{self.tkey}"

    def stored(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
        d["hour_pct_display"] = _fmt_pct(self.hour_pct)
        d["day_intraday_pct_display"] = _fmt_pct(self.day_intraday_pct)
        d["day_vs_previous_close_pct_display"] = _fmt_pct(self.day_vs_previous_close_pct)
        d["week_pct_display"] = _fmt_pct(self.week_pct)
        return d

    def ref_payload(self) -> dict[str, Any]:
        return {
            "name": self.name, "ticker": self.ticker, "asset_class": self.asset_class,
            "continent": self.continent, "last_price": self.last_price,
            "observed_at": self.observed_at, "observed_day": self.observed_day,
        }


@dataclass(frozen=True)
class FxRateObject:
    base_currency: str
    quote_currency: str
    pair: str
    continent: str
    rate: float | None
    observed_at: str
    observed_day: str
    observed_ms: int
    updated_at: int

    _finance_root: str = "finance"

    @property
    def name(self) -> str:
        return f"{self.base_currency} to {self.quote_currency}"

    def record_key(self) -> str:
        return f"{self.base_currency}-{self.quote_currency}-{self.observed_ms}"

    def latest_key(self) -> str:
        return f"{self.base_currency}-{self.quote_currency}"

    def stored(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
        d["name"] = self.name
        return d

    def ref_payload(self) -> dict[str, Any]:
        return {
            "base_currency": self.base_currency, "quote_currency": self.quote_currency,
            "pair": self.pair, "continent": self.continent, "rate": self.rate,
            "observed_at": self.observed_at, "observed_day": self.observed_day,
        }


class FinanceFactory:
    """Builds typed finance objects from already-normalized provider fields."""

    @classmethod
    def create_market_object(cls, *, record: dict[str, Any]) -> MarketObservationObject:
        keys = MarketObservationObject.__dataclass_fields__
        return MarketObservationObject(**{k: record.get(k) for k in keys if not k.startswith("_") and k in record})

    @classmethod
    def create_fx_object(cls, *, record: dict[str, Any]) -> FxRateObject:
        keys = FxRateObject.__dataclass_fields__
        return FxRateObject(**{k: record.get(k) for k in keys if not k.startswith("_") and k in record})