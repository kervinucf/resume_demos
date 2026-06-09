"""
yfinance source — the provider-specific read layer for the finance loader.

Analog of app/lib/sources/open_metro.py: everything that knows *how yfinance encodes
data* (download shape, multi-index columns, performance math, which tickers exist)
lives here and nowhere else. To swap providers, write another module exposing the
same callables and re-point the import in app/lib/helpers/finance/__init__.py:

    source_available()              -> (bool, str)
    all_continents()                -> list[str]
    iter_market_candidates(...)     -> Iterator[dict]   # one normalized observation
    iter_fx_candidates(...)         -> Iterator[dict]   # one normalized fx rate

Records are plain dicts with neutral keys — no app typed imports here.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Iterator

import pandas as pd
import yfinance as yf

BATCH_SLEEP_SECONDS = 2

logging.basicConfig(
    filename="finance_log.txt", level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
)

STOCK_INDICES_BY_CONTINENT: dict[str, dict[str, str]] = {
    "NA": {"USA (S&P 500)": "^GSPC", "USA (NASDAQ)": "^IXIC",
           "Canada (S&P/TSX)": "^GSPTSE", "Mexico (IPC)": "^MXX"},
    "EU": {"UK (FTSE 100)": "^FTSE", "Germany (DAX)": "^GDAXI",
           "France (CAC 40)": "^FCHI", "Eurozone (EURO STOXX 50)": "^STOXX50E"},
    "AS": {"Japan (Nikkei 225)": "^N225", "Hong Kong (Hang Seng)": "^HSI",
           "China (Shanghai Composite)": "000001.SS", "India (Nifty 50)": "^NSEI",
           "South Korea (KOSPI)": "^KS11"},
    "SA": {"Brazil (Bovespa)": "^BVSP", "Argentina (S&P MERVAL)": "^MERV"},
    "OC": {"Australia (ASX 200)": "^AXJO", "New Zealand (NZX 50)": "^NZ50"},
    "AF": {"South Africa (iShares MSCI)": "EZA"},
}

COMMODITIES: dict[str, str] = {
    "Bitcoin": "BTC-USD", "Crude Oil (WTI)": "CL=F", "Gold": "GC=F", "Silver": "SI=F",
    "Copper": "HG=F", "Natural Gas": "NG=F", "Corn": "ZC=F", "Wheat": "KE=F",
}

CURRENCIES_BY_CONTINENT: dict[str, list[str]] = {
    "NA": ["CAD", "MXN", "USD", "BZD", "HTG", "DOP", "JMD", "TTD", "BSD", "CUP", "NIO", "CRC", "GTQ", "HNL", "PAB"],
    "SA": ["BRL", "ARS", "CLP", "COP", "PEN", "UYU", "PYG", "BOB", "GYD", "SRD"],
    "EU": ["EUR", "GBP", "CHF", "NOK", "SEK", "DKK", "PLN", "CZK", "HUF", "RON", "BGN", "ALL", "MKD", "RSD"],
    "AF": ["ZAR", "NGN", "KES", "EGP", "MAD", "GHS", "TZS", "UGX", "DZD", "NAD", "MWK", "MZN", "SZL", "LYD"],
    "AS": ["JPY", "CNY", "INR", "KRW", "HKD", "SGD", "MYR", "THB", "IDR", "PHP", "PKR", "BDT", "VND", "TWD", "AED", "SAR", "ILS", "TRY"],
    "OC": ["AUD", "NZD", "PGK", "WST", "TOP", "VUV"],
}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _observed_day(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        n = float(value)
        return None if pd.isna(n) else n
    except Exception:
        return None


def source_available() -> tuple[bool, str]:
    return True, "yfinance"


def all_continents() -> list[str]:
    return sorted({*STOCK_INDICES_BY_CONTINENT, *CURRENCIES_BY_CONTINENT})


def _yf_download(tickers: list[str], **kwargs) -> pd.DataFrame:
    for attempt in range(3):
        try:
            return yf.download(tickers, progress=False, group_by="ticker",
                               auto_adjust=False, threads=True, **kwargs)
        except Exception as exc:
            logging.warning(f"Attempt {attempt + 1}/3 failed for {tickers}: {exc}")
            time.sleep(BATCH_SLEEP_SECONDS)
    logging.error(f"All retries failed for {tickers}")
    return pd.DataFrame()


def _hist_for_ticker(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    try:
        if isinstance(df.columns, pd.MultiIndex):
            if ticker in df.columns.get_level_values(0):
                return df[ticker].dropna(how="all")
            return pd.DataFrame()
        if "Close" in df.columns:
            return df.dropna(how="all")
    except Exception:
        return pd.DataFrame()
    return pd.DataFrame()


def _short_perf(hist: pd.DataFrame) -> dict[str, float | None]:
    out = {"last_price": None, "hour_pct": None, "day_intraday_pct": None,
           "day_vs_previous_close_pct": None, "week_pct": None}
    if hist is None or hist.empty or "Close" not in hist.columns:
        return out
    last = _safe_float(hist["Close"].iloc[-1])
    out["last_price"] = last
    if len(hist) > 1:
        prior = _safe_float(hist["Close"].iloc[-2])
        if last is not None and prior:
            out["hour_pct"] = ((last - prior) / prior) * 100
    try:
        today = hist[hist.index.date == hist.index[-1].date()]
        op = _safe_float(today["Open"].iloc[0]) if not today.empty else None
        if last is not None and op:
            out["day_intraday_pct"] = ((last - op) / op) * 100
    except Exception:
        pass
    try:
        prev = hist[hist.index.date < hist.index[-1].date()]
        pc = _safe_float(prev["Close"].iloc[-1]) if not prev.empty else None
        if last is not None and pc:
            out["day_vs_previous_close_pct"] = ((last - pc) / pc) * 100
    except Exception:
        pass
    try:
        wk = hist[hist.index <= hist.index[-1] - pd.Timedelta(days=7)]
        wc = _safe_float(wk["Close"].iloc[-1]) if not wk.empty else None
        if last is not None and wc:
            out["week_pct"] = ((last - wc) / wc) * 100
    except Exception:
        pass
    return out


def _long_perf(hist: pd.DataFrame) -> dict[str, float | None]:
    keys = ["one_month_pct", "three_month_pct", "six_month_pct",
            "one_year_pct", "five_year_pct", "ten_year_pct"]
    out: dict[str, float | None] = {k: None for k in keys}
    if hist is None or hist.empty or "Close" not in hist.columns:
        return out
    last = _safe_float(hist["Close"].iloc[-1])
    if last is None:
        return out
    offsets = {
        "one_month_pct": pd.DateOffset(months=1), "three_month_pct": pd.DateOffset(months=3),
        "six_month_pct": pd.DateOffset(months=6), "one_year_pct": pd.DateOffset(years=1),
        "five_year_pct": pd.DateOffset(years=5), "ten_year_pct": pd.DateOffset(years=10),
    }
    for key, off in offsets.items():
        try:
            idx = hist.index.asof(hist.index[-1] - off)
            if pd.isna(idx):
                continue
            prev = _safe_float(hist.loc[idx, "Close"])
            out[key] = ((last - prev) / prev) * 100 if prev else None
        except Exception:
            out[key] = None
    return out


def _market_record(*, name, ticker, asset_class, continent, observed_at, short, long_) -> dict[str, Any]:
    return {
        "name": name, "ticker": ticker, "asset_class": asset_class, "continent": continent,
        "observed_at": observed_at.isoformat(), "observed_day": _observed_day(observed_at),
        "observed_ms": _ms(observed_at), "updated_at": _ms(observed_at),
        **short, **long_,
    }


def _sync_group(asset_dict, asset_class, continent, limit) -> Iterator[dict[str, Any]]:
    selected = list(asset_dict.items())
    if limit is not None:
        selected = selected[: int(limit)]
    tickers = [t for _, t in selected]
    if not tickers:
        return
    observed_at = _now_utc()
    short_df = _yf_download(tickers, period="8d", interval="1h")
    long_df = _yf_download(tickers, period="10y", interval="1d")
    for name, ticker in selected:
        short = _short_perf(_hist_for_ticker(short_df, ticker))
        long_ = _long_perf(_hist_for_ticker(long_df, ticker))
        yield _market_record(name=name, ticker=ticker, asset_class=asset_class,
                             continent=continent, observed_at=observed_at, short=short, long_=long_)


def iter_market_candidates(
        *, limit_per_continent: int | None = None, commodity_limit: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Stream normalized market observations: commodities, then indices by continent."""
    print("  group: commodities", flush=True)
    yield from _sync_group(COMMODITIES, "commodity", None, commodity_limit)
    for continent, assets in STOCK_INDICES_BY_CONTINENT.items():
        print(f"  group: indices {continent}", flush=True)
        yield from _sync_group(assets, "index", continent, limit_per_continent)


def iter_fx_candidates(
        *, base_currency: str = "USD", limit_per_continent: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Stream normalized FX rates (base -> each currency) per continent."""
    base = base_currency.upper()
    for continent, currencies in CURRENCIES_BY_CONTINENT.items():
        selected = [c for c in currencies if c.upper() != base]
        if limit_per_continent is not None:
            selected = selected[: int(limit_per_continent)]
        if not selected:
            continue
        print(f"  group: fx {continent}", flush=True)
        observed_at = _now_utc()
        tickers = [f"{c}=X" for c in selected]
        df = _yf_download(tickers, period="5d", interval="1d")
        for currency, ticker in zip(selected, tickers):
            hist = _hist_for_ticker(df, ticker)
            rate = None
            if not hist.empty and "Close" in hist.columns:
                rate = _safe_float(hist["Close"].dropna().iloc[-1])
            yield {
                "base_currency": base, "quote_currency": currency.upper(),
                "pair": f"{base}/{currency.upper()}", "continent": continent.upper(),
                "rate": rate, "observed_at": observed_at.isoformat(),
                "observed_day": _observed_day(observed_at), "observed_ms": _ms(observed_at),
                "updated_at": _ms(observed_at),
            }