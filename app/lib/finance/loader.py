# app/lib/finance/loader.py
"""
Pull global market snapshots from yfinance into the hypergraph.

Readable graph shape:

    finance.observations.<asset-class>.<ticker>.<yyyy-mm-dd>.<observed-ms>
    finance.latest.<asset-class>.<ticker>
    finance.assets.<asset-class>.<ticker>

    finance.fx.USD.<currency>.<yyyy-mm-dd>.<observed-ms>
    finance.latest.fx.USD.<currency>

    finance.continents.<continent>
    finance.continents.<continent>.refs.assets.<asset-class>.<ticker>
    finance.continents.<continent>.refs.rates.USD.<currency>

Most writes use:

    client.put(...)
    client.link(...)

Explicit indexes are optional browsable directories.
Normal search comes from client.put(...).
"""

from __future__ import annotations

import logging
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import yfinance as yf

from HyperCoreSDK.python.client import HyperClient
from HyperCoreSDK.python.helpers.server import create_hyper_server
from HyperCoreSDK.python.helpers.storage import create_default_storage_directory
from HyperCoreSDK.python.helpers.indexes import ScopeSpec, ValueIndexSpec


FINANCE_ROOT = "finance"
BATCH_SLEEP_SECONDS = 2


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    filename="finance_log.txt",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


# ---------------------------------------------------------------------------
# Source configuration
# ---------------------------------------------------------------------------

STOCK_INDICES_BY_CONTINENT: dict[str, dict[str, str]] = {
    "NA": {
        "USA (S&P 500)": "^GSPC",
        "USA (NASDAQ)": "^IXIC",
        "Canada (S&P/TSX)": "^GSPTSE",
        "Mexico (IPC)": "^MXX",
    },
    "EU": {
        "UK (FTSE 100)": "^FTSE",
        "Germany (DAX)": "^GDAXI",
        "France (CAC 40)": "^FCHI",
        "Eurozone (EURO STOXX 50)": "^STOXX50E",
    },
    "AS": {
        "Japan (Nikkei 225)": "^N225",
        "Hong Kong (Hang Seng)": "^HSI",
        "China (Shanghai Composite)": "000001.SS",
        "India (Nifty 50)": "^NSEI",
        "South Korea (KOSPI)": "^KS11",
    },
    "SA": {
        "Brazil (Bovespa)": "^BVSP",
        "Argentina (S&P MERVAL)": "^MERV",
    },
    "OC": {
        "Australia (ASX 200)": "^AXJO",
        "New Zealand (NZX 50)": "^NZ50",
    },
    "AF": {
        "South Africa (iShares MSCI)": "EZA",
    },
}

COMMODITIES: dict[str, str] = {
    "Bitcoin": "BTC-USD",
    "Crude Oil (WTI)": "CL=F",
    "Gold": "GC=F",
    "Silver": "SI=F",
    "Copper": "HG=F",
    "Natural Gas": "NG=F",
    "Corn": "ZC=F",
    "Wheat": "KE=F",
}

CURRENCIES_BY_CONTINENT: dict[str, list[str]] = {
    "NA": ["CAD", "MXN", "USD", "BZD", "HTG", "DOP", "JMD", "TTD", "BSD", "CUP", "NIO", "CRC", "GTQ", "HNL", "PAB"],
    "SA": ["BRL", "ARS", "CLP", "COP", "PEN", "UYU", "PYG", "BOB", "GYD", "SRD"],
    "EU": ["EUR", "GBP", "CHF", "NOK", "SEK", "DKK", "PLN", "CZK", "HUF", "RON", "BGN", "ALL", "MKD", "RSD"],
    "AF": ["ZAR", "NGN", "KES", "EGP", "MAD", "GHS", "TZS", "UGX", "DZD", "NAD", "MWK", "MZN", "SZL", "LYD"],
    "AS": ["JPY", "CNY", "INR", "KRW", "HKD", "SGD", "MYR", "THB", "IDR", "PHP", "PKR", "BDT", "VND", "TWD", "AED", "SAR", "ILS", "TRY"],
    "OC": ["AUD", "NZD", "PGK", "WST", "TOP", "VUV"],
}


# ---------------------------------------------------------------------------
# Explicit browsable indexes
# ---------------------------------------------------------------------------

MARKET_INDEXES: list[ValueIndexSpec] = [
    ValueIndexSpec(
        name="asset_class",
        path="asset_class",
        normalize="slug",
        link_projections={
            "name": "name",
            "ticker": "ticker",
            "last_price": "last_price",
            "observed_at": "observed_at",
        },
    ),
    ValueIndexSpec(
        name="ticker",
        path="ticker",
        normalize="slug",
        scopes=[ScopeSpec(path="asset_class", normalize="slug")],
        link_projections={
            "name": "name",
            "last_price": "last_price",
            "observed_at": "observed_at",
        },
    ),
    ValueIndexSpec(
        name="continent",
        path="continent",
        normalize="upper",
        scopes=[ScopeSpec(path="asset_class", normalize="slug")],
        link_projections={
            "name": "name",
            "ticker": "ticker",
            "last_price": "last_price",
            "observed_at": "observed_at",
        },
    ),
    ValueIndexSpec(
        name="observed_day",
        path="observed_day",
        normalize="slug",
        scopes=[ScopeSpec(path="asset_class", normalize="slug")],
        link_projections={
            "name": "name",
            "ticker": "ticker",
            "last_price": "last_price",
            "observed_at": "observed_at",
        },
    ),
]

FX_INDEXES: list[ValueIndexSpec] = [
    ValueIndexSpec(
        name="base_currency",
        path="base_currency",
        normalize="upper",
        link_projections={
            "quote_currency": "quote_currency",
            "rate": "rate",
            "continent": "continent",
            "observed_at": "observed_at",
        },
    ),
    ValueIndexSpec(
        name="quote_currency",
        path="quote_currency",
        normalize="upper",
        scopes=[ScopeSpec(path="base_currency", normalize="upper")],
        link_projections={
            "rate": "rate",
            "continent": "continent",
            "observed_at": "observed_at",
        },
    ),
    ValueIndexSpec(
        name="continent",
        path="continent",
        normalize="upper",
        scopes=[ScopeSpec(path="base_currency", normalize="upper")],
        link_projections={
            "quote_currency": "quote_currency",
            "rate": "rate",
            "observed_at": "observed_at",
        },
    ),
    ValueIndexSpec(
        name="observed_day",
        path="observed_day",
        normalize="slug",
        scopes=[ScopeSpec(path="base_currency", normalize="upper")],
        link_projections={
            "quote_currency": "quote_currency",
            "rate": "rate",
            "continent": "continent",
            "observed_at": "observed_at",
        },
    ),
]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def slug(value: Any, fallback: str = "unknown") -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", str(value or "").strip().lower())
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text or fallback


def ticker_key(ticker: str) -> str:
    return slug(ticker.replace("^", "idx-").replace("=", "-"), "ticker")


def observed_day(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def stream_link(path: str) -> str:
    return f"{path}?stream=true"


def changes_link(path: str) -> str:
    return f"{path}/api/changes-since"


def safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        n = float(value)
        if pd.isna(n):
            return None
        return n
    except Exception:
        return None


def pct_to_float(value: str) -> float | None:
    text = str(value or "").strip()
    if not text or text == "N/A":
        return None
    try:
        return float(text.replace("%", ""))
    except Exception:
        return None


def fmt_pct(value: float | None) -> str:
    return "N/A" if value is None else f"{value:+.2f}%"


def yf_download(tickers: list[str], **kwargs) -> pd.DataFrame:
    for attempt in range(3):
        try:
            return yf.download(
                tickers,
                progress=False,
                group_by="ticker",
                auto_adjust=False,
                threads=True,
                **kwargs,
            )
        except Exception as exc:
            msg = f"Attempt {attempt + 1}/3 failed for {tickers}: {exc}"
            print(f"⚠️ {msg}")
            logging.warning(msg)
            time.sleep(BATCH_SLEEP_SECONDS)

    msg = f"All retries failed for {tickers}"
    print(f"❌ {msg}")
    logging.error(msg)
    return pd.DataFrame()


def hist_for_ticker(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    try:
        if isinstance(df.columns, pd.MultiIndex):
            if ticker in df.columns.get_level_values(0):
                return df[ticker].dropna(how="all")
            return pd.DataFrame()

        # yfinance returns flat columns for a single ticker.
        if "Close" in df.columns:
            return df.dropna(how="all")

    except Exception:
        return pd.DataFrame()

    return pd.DataFrame()


# ---------------------------------------------------------------------------
# Performance calculations
# ---------------------------------------------------------------------------

def calculate_short_term_performance(hist: pd.DataFrame) -> dict[str, float | None]:
    if hist is None or hist.empty or "Close" not in hist.columns:
        return {
            "last_price": None,
            "hour_pct": None,
            "day_intraday_pct": None,
            "day_vs_previous_close_pct": None,
            "week_pct": None,
        }

    last_price = safe_float(hist["Close"].iloc[-1])

    hour_pct = None
    if len(hist) > 1:
        prior = safe_float(hist["Close"].iloc[-2])
        if last_price is not None and prior:
            hour_pct = ((last_price - prior) / prior) * 100

    day_intraday_pct = None
    try:
        today_hist = hist[hist.index.date == hist.index[-1].date()]
        open_price = safe_float(today_hist["Open"].iloc[0]) if not today_hist.empty else None
        if last_price is not None and open_price:
            day_intraday_pct = ((last_price - open_price) / open_price) * 100
    except Exception:
        pass

    day_vs_previous_close_pct = None
    try:
        prev_days_hist = hist[hist.index.date < hist.index[-1].date()]
        prev_close = safe_float(prev_days_hist["Close"].iloc[-1]) if not prev_days_hist.empty else None
        if last_price is not None and prev_close:
            day_vs_previous_close_pct = ((last_price - prev_close) / prev_close) * 100
    except Exception:
        pass

    week_pct = None
    try:
        week_ago_ts = hist.index[-1] - pd.Timedelta(days=7)
        week_ago_hist = hist[hist.index <= week_ago_ts]
        week_ago_close = safe_float(week_ago_hist["Close"].iloc[-1]) if not week_ago_hist.empty else None
        if last_price is not None and week_ago_close:
            week_pct = ((last_price - week_ago_close) / week_ago_close) * 100
    except Exception:
        pass

    return {
        "last_price": last_price,
        "hour_pct": hour_pct,
        "day_intraday_pct": day_intraday_pct,
        "day_vs_previous_close_pct": day_vs_previous_close_pct,
        "week_pct": week_pct,
    }


def calculate_long_term_performance(hist: pd.DataFrame) -> dict[str, float | None]:
    if hist is None or hist.empty or "Close" not in hist.columns:
        return {
            "one_month_pct": None,
            "three_month_pct": None,
            "six_month_pct": None,
            "one_year_pct": None,
            "five_year_pct": None,
            "ten_year_pct": None,
        }

    last_price = safe_float(hist["Close"].iloc[-1])
    if last_price is None:
        return {
            "one_month_pct": None,
            "three_month_pct": None,
            "six_month_pct": None,
            "one_year_pct": None,
            "five_year_pct": None,
            "ten_year_pct": None,
        }

    periods = {
        "one_month_pct": pd.DateOffset(months=1),
        "three_month_pct": pd.DateOffset(months=3),
        "six_month_pct": pd.DateOffset(months=6),
        "one_year_pct": pd.DateOffset(years=1),
        "five_year_pct": pd.DateOffset(years=5),
        "ten_year_pct": pd.DateOffset(years=10),
    }

    out: dict[str, float | None] = {}

    for key, offset in periods.items():
        try:
            target_date = hist.index[-1] - offset
            prev_idx = hist.index.asof(target_date)

            if pd.isna(prev_idx):
                out[key] = None
                continue

            prev_price = safe_float(hist.loc[prev_idx, "Close"])
            out[key] = ((last_price - prev_price) / prev_price) * 100 if prev_price else None

        except Exception:
            out[key] = None

    return out


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def asset_path(asset_class: str, ticker: str) -> str:
    return f"{FINANCE_ROOT}.assets.{slug(asset_class)}.{ticker_key(ticker)}"


def observation_rel(asset_class: str, ticker: str, dt: datetime) -> str:
    return f"observations/{slug(asset_class)}/{ticker_key(ticker)}/{observed_day(dt)}/{ms(dt)}"


def observation_path(asset_class: str, ticker: str, dt: datetime) -> str:
    return f"{FINANCE_ROOT}.{observation_rel(asset_class, ticker, dt).replace('/', '.')}"


def latest_asset_path(asset_class: str, ticker: str) -> str:
    return f"{FINANCE_ROOT}.latest.{slug(asset_class)}.{ticker_key(ticker)}"


def continent_path(continent: str) -> str:
    return f"{FINANCE_ROOT}.continents.{continent.upper()}"


def fx_rel(base_currency: str, quote_currency: str, dt: datetime) -> str:
    return f"fx/{base_currency.upper()}/{quote_currency.upper()}/{observed_day(dt)}/{ms(dt)}"


def fx_path(base_currency: str, quote_currency: str, dt: datetime) -> str:
    return f"{FINANCE_ROOT}.{fx_rel(base_currency, quote_currency, dt).replace('/', '.')}"


def latest_fx_path(base_currency: str, quote_currency: str) -> str:
    return f"{FINANCE_ROOT}.latest.fx.{base_currency.upper()}.{quote_currency.upper()}"


# ---------------------------------------------------------------------------
# Body builders
# ---------------------------------------------------------------------------

def market_body(
    *,
    name: str,
    ticker: str,
    asset_class: str,
    continent: str | None,
    observed_at: datetime,
    short_perf: dict[str, float | None],
    long_perf: dict[str, float | None],
) -> dict[str, Any]:
    return {
        "model": "market-observation",
        "name": name,
        "ticker": ticker,
        "asset_class": asset_class,
        "continent": continent,
        "observed_at": observed_at.isoformat(),
        "observed_day": observed_day(observed_at),
        "updated_at": ms(observed_at),
        **short_perf,
        **long_perf,
        "hour_pct_display": fmt_pct(short_perf.get("hour_pct")),
        "day_intraday_pct_display": fmt_pct(short_perf.get("day_intraday_pct")),
        "day_vs_previous_close_pct_display": fmt_pct(short_perf.get("day_vs_previous_close_pct")),
        "week_pct_display": fmt_pct(short_perf.get("week_pct")),
        "one_month_pct_display": fmt_pct(long_perf.get("one_month_pct")),
        "three_month_pct_display": fmt_pct(long_perf.get("three_month_pct")),
        "six_month_pct_display": fmt_pct(long_perf.get("six_month_pct")),
        "one_year_pct_display": fmt_pct(long_perf.get("one_year_pct")),
        "five_year_pct_display": fmt_pct(long_perf.get("five_year_pct")),
        "ten_year_pct_display": fmt_pct(long_perf.get("ten_year_pct")),
    }


def latest_market_body(
    *,
    name: str,
    ticker: str,
    asset_class: str,
    continent: str | None,
    target: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    return {
        "model": "market-latest",
        "name": name,
        "ticker": ticker,
        "asset_class": asset_class,
        "continent": continent,
        "target": target,
        "last_price": body.get("last_price"),
        "hour_pct": body.get("hour_pct"),
        "day_intraday_pct": body.get("day_intraday_pct"),
        "day_vs_previous_close_pct": body.get("day_vs_previous_close_pct"),
        "week_pct": body.get("week_pct"),
        "observed_at": body.get("observed_at"),
        "observed_day": body.get("observed_day"),
        "updated_at": body.get("updated_at"),
    }


def fx_body(
    *,
    base_currency: str,
    quote_currency: str,
    continent: str,
    rate: float | None,
    observed_at: datetime,
) -> dict[str, Any]:
    return {
        "model": "fx-rate",
        "base_currency": base_currency.upper(),
        "quote_currency": quote_currency.upper(),
        "pair": f"{base_currency.upper()}/{quote_currency.upper()}",
        "name": f"{base_currency.upper()} to {quote_currency.upper()}",
        "continent": continent.upper(),
        "rate": rate,
        "observed_at": observed_at.isoformat(),
        "observed_day": observed_day(observed_at),
        "updated_at": ms(observed_at),
    }


# ---------------------------------------------------------------------------
# Graph writers
# ---------------------------------------------------------------------------

def ensure_continent(client: HyperClient, continent: str) -> str:
    path = continent_path(continent)

    client.put(
        path=path,
        kind="finance_continent",
        name=continent.upper(),
        content={
            "model": "finance-continent",
            "continent": continent.upper(),
        },
        links={
            "assets": f"{path}.refs.assets",
            "rates": f"{path}.refs.rates",
            "stream": stream_link(path),
            "changes_since": changes_link(path),
        },
    )

    return path


def write_market_observation(
    client: HyperClient,
    *,
    name: str,
    ticker: str,
    asset_class: str,
    continent: str | None,
    observed_at: datetime,
    short_perf: dict[str, float | None],
    long_perf: dict[str, float | None],
) -> str:
    obs_rel = observation_rel(asset_class, ticker, observed_at)
    obs_abs = observation_path(asset_class, ticker, observed_at)
    latest_abs = latest_asset_path(asset_class, ticker)
    asset_abs = asset_path(asset_class, ticker)

    body = market_body(
        name=name,
        ticker=ticker,
        asset_class=asset_class,
        continent=continent,
        observed_at=observed_at,
        short_perf=short_perf,
        long_perf=long_perf,
    )

    links = {
        "asset": asset_abs,
        "latest": latest_abs,
        "stream": stream_link(obs_abs),
        "changes_since": changes_link(obs_abs),
    }

    if continent:
        links["continent"] = continent_path(continent)

    client.write_record_with_indexes(
        root=FINANCE_ROOT,
        record_path=obs_rel,
        record_data=body,
        index_specs=MARKET_INDEXES,
        ref_key=f"{ticker_key(ticker)}-{ms(observed_at)}",
        ref_payload={
            "name": name,
            "ticker": ticker,
            "asset_class": asset_class,
            "continent": continent,
            "last_price": body.get("last_price"),
            "observed_at": body.get("observed_at"),
            "observed_day": body.get("observed_day"),
        },
    )

    client.put(
        path=obs_abs,
        kind="market_observation",
        name=name,
        content=body,
        links=links,
    )

    client.put(
        path=asset_abs,
        kind="market_asset",
        name=name,
        target=latest_abs,
        content={
            "model": "market-asset",
            "name": name,
            "ticker": ticker,
            "asset_class": asset_class,
            "continent": continent,
            "latest": latest_abs,
            "last_observation": obs_abs,
            "updated_at": ms(observed_at),
        },
        links={
            "latest": latest_abs,
            "last_observation": obs_abs,
            "observations": f"{asset_abs}.refs.observations",
            **({"continent": continent_path(continent)} if continent else {}),
            "stream": stream_link(asset_abs),
            "changes_since": changes_link(asset_abs),
        },
    )

    client.put(
        path=latest_abs,
        kind="market_latest",
        name=name,
        target=obs_abs,
        content=latest_market_body(
            name=name,
            ticker=ticker,
            asset_class=asset_class,
            continent=continent,
            target=obs_abs,
            content=body,
        ),
        links={
            "target": obs_abs,
            "asset": asset_abs,
            **({"continent": continent_path(continent)} if continent else {}),
            "stream": stream_link(latest_abs),
            "changes_since": changes_link(latest_abs),
        },
    )

    client.link(
        source=asset_abs,
        rel=f"observations.{observed_day(observed_at)}.{ms(observed_at)}",
        target=obs_abs,
        kind="market_observation",
        name=f"{name} observation {observed_at.isoformat()}",
        content={
            "ticker": ticker,
            "asset_class": asset_class,
            "last_price": body.get("last_price"),
            "observed_at": body.get("observed_at"),
        },
        links={
            "asset": asset_abs,
            "observation": obs_abs,
        },
    )

    if continent:
        ensure_continent(client, continent)

        client.link(
            source=continent_path(continent),
            rel=f"assets.{slug(asset_class)}.{ticker_key(ticker)}",
            target=asset_abs,
            kind="market_asset_ref",
            name=name,
            content={
                "ticker": ticker,
                "asset_class": asset_class,
                "latest": latest_abs,
                "last_price": body.get("last_price"),
            },
            links={
                "asset": asset_abs,
                "latest": latest_abs,
            },
        )

    return obs_abs


def write_fx_rate(
    client: HyperClient,
    *,
    base_currency: str,
    quote_currency: str,
    continent: str,
    rate: float | None,
    observed_at: datetime,
) -> str:
    rel = fx_rel(base_currency, quote_currency, observed_at)
    path = fx_path(base_currency, quote_currency, observed_at)
    latest = latest_fx_path(base_currency, quote_currency)

    body = fx_body(
        base_currency=base_currency,
        quote_currency=quote_currency,
        continent=continent,
        rate=rate,
        observed_at=observed_at,
    )

    links = {
        "latest": latest,
        "continent": continent_path(continent),
        "stream": stream_link(path),
        "changes_since": changes_link(path),
    }

    client.write_record_with_indexes(
        root=FINANCE_ROOT,
        record_path=rel,
        record_data=body,
        index_specs=FX_INDEXES,
        ref_key=f"{base_currency.upper()}-{quote_currency.upper()}-{ms(observed_at)}",
        ref_payload={
            "base_currency": base_currency.upper(),
            "quote_currency": quote_currency.upper(),
            "pair": f"{base_currency.upper()}/{quote_currency.upper()}",
            "continent": continent.upper(),
            "rate": rate,
            "observed_at": body.get("observed_at"),
            "observed_day": body.get("observed_day"),
        },
    )

    client.put(
        path=path,
        kind="fx_rate",
        name=f"{base_currency.upper()} to {quote_currency.upper()}",
        content=body,
        links=links,
    )

    client.put(
        path=latest,
        kind="fx_latest",
        name=f"{base_currency.upper()} to {quote_currency.upper()}",
        target=path,
        content={
            "model": "fx-latest",
            **body,
            "target": path,
        },
        links={
            "target": path,
            "continent": continent_path(continent),
            "stream": stream_link(latest),
            "changes_since": changes_link(latest),
        },
    )

    ensure_continent(client, continent)

    client.link(
        source=continent_path(continent),
        rel=f"rates.{base_currency.upper()}.{quote_currency.upper()}",
        target=latest,
        kind="fx_rate_ref",
        name=f"{base_currency.upper()} to {quote_currency.upper()}",
        content={
            "base_currency": base_currency.upper(),
            "quote_currency": quote_currency.upper(),
            "rate": rate,
            "latest": latest,
        },
        links={
            "latest": latest,
            "rate": path,
        },
    )

    return path


# ---------------------------------------------------------------------------
# Sync functions
# ---------------------------------------------------------------------------

def sync_market_assets(
    client: HyperClient,
    *,
    asset_dict: dict[str, str],
    asset_class: str,
    continent: str | None = None,
    limit: int | None = None,
) -> int:
    selected = list(asset_dict.items())
    if limit is not None:
        selected = selected[: int(limit)]

    tickers = [ticker for _, ticker in selected]
    if not tickers:
        return 0

    observed_at = now_utc()

    short_df = yf_download(tickers, period="8d", interval="1h")
    long_df = yf_download(tickers, period="10y", interval="1d")

    written = 0

    for name, ticker in selected:
        try:
            short_hist = hist_for_ticker(short_df, ticker)
            long_hist = hist_for_ticker(long_df, ticker)

            short_perf = calculate_short_term_performance(short_hist)
            long_perf = calculate_long_term_performance(long_hist)

            path = write_market_observation(
                client,
                name=name,
                ticker=ticker,
                asset_class=asset_class,
                continent=continent,
                observed_at=observed_at,
                short_perf=short_perf,
                long_perf=long_perf,
            )

            written += 1
            price = short_perf.get("last_price")
            price_text = "N/A" if price is None else f"{price:,.2f}"
            print(f"  ok: {name} [{ticker}] price={price_text} -> {path}")

        except Exception as exc:
            msg = f"write failed for {name} [{ticker}]: {type(exc).__name__}: {exc}"
            print(f"  {msg}")
            logging.exception(msg)

    return written


def sync_indices(client: HyperClient, *, limit_per_continent: int | None = None) -> int:
    total = 0

    for continent, assets in STOCK_INDICES_BY_CONTINENT.items():
        print(f"\n--- Stock indices: {continent} ---")
        total += sync_market_assets(
            client,
            asset_dict=assets,
            asset_class="index",
            continent=continent,
            limit=limit_per_continent,
        )

    return total


def sync_commodities(client: HyperClient, *, limit: int | None = None) -> int:
    print("\n--- Commodities ---")
    return sync_market_assets(
        client,
        asset_dict=COMMODITIES,
        asset_class="commodity",
        continent=None,
        limit=limit,
    )


def sync_exchange_rates(
    client: HyperClient,
    *,
    currencies_by_continent: dict[str, list[str]],
    base_currency: str = "USD",
    limit_per_continent: int | None = None,
) -> int:
    total = 0
    observed_at = now_utc()

    for continent, currencies in currencies_by_continent.items():
        selected = list(currencies)
        if limit_per_continent is not None:
            selected = selected[: int(limit_per_continent)]

        # Skip USD->USD because yfinance does not need a pair for identity.
        selected = [c for c in selected if c.upper() != base_currency.upper()]
        if not selected:
            continue

        print(f"\n--- FX rates: {continent} ---")

        tickers = [f"{currency}=X" for currency in selected]
        df = yf_download(tickers, period="5d", interval="1d")

        for currency, ticker in zip(selected, tickers):
            try:
                hist = hist_for_ticker(df, ticker)
                rate = None

                if not hist.empty and "Close" in hist.columns:
                    rate = safe_float(hist["Close"].dropna().iloc[-1])

                path = write_fx_rate(
                    client,
                    base_currency=base_currency,
                    quote_currency=currency,
                    continent=continent,
                    rate=rate,
                    observed_at=observed_at,
                )

                total += 1
                rate_text = "N/A" if rate is None else f"{rate:.4f}"
                print(f"  ok: {base_currency.upper()}->{currency.upper()} rate={rate_text} -> {path}")

            except Exception as exc:
                msg = f"FX write failed for {base_currency}->{currency}: {type(exc).__name__}: {exc}"
                print(f"  {msg}")
                logging.exception(msg)

    return total


def run(
    client: HyperClient,
    *,
    include_indices: bool = True,
    include_commodities: bool = True,
    include_fx: bool = True,
    limit_per_continent: int | None = None,
    commodity_limit: int | None = None,
    close_client: bool = False,
    keep_alive: bool = False,
) -> int:
    total = 0

    try:
        for continent in CURRENCIES_BY_CONTINENT:
            ensure_continent(client, continent)

        if include_commodities:
            total += sync_commodities(client, limit=commodity_limit)

        if include_indices:
            total += sync_indices(client, limit_per_continent=limit_per_continent)

        if include_fx:
            total += sync_exchange_rates(
                client,
                currencies_by_continent=CURRENCIES_BY_CONTINENT,
                base_currency="USD",
                limit_per_continent=limit_per_continent,
            )

        print(f"\ndone — wrote {total} finance observation(s)/rate(s)")

        if keep_alive:
            print(f"relay still running at {client.url} (Ctrl-C to stop)")
            while True:
                time.sleep(3600)

        return total

    except KeyboardInterrupt:
        return total

    finally:
        if close_client:
            client.close()


def main() -> int:
    client = create_hyper_server(
        root=FINANCE_ROOT,
        data_path=create_default_storage_directory(),
    )

    total = run(
        client,
        include_indices=True,
        include_commodities=True,
        include_fx=True,
        limit_per_continent=None,
        commodity_limit=None,
        close_client=True,
        keep_alive=False,
    )

    return 0 if total >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())