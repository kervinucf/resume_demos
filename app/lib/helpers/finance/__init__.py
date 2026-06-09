"""
Finance operations — the verbs the main loader orchestrates.

Mirrors app/lib/helpers/geo/__init__.py: the provider is read inside the create_*/
list_* functions, so the loader only ever imports these verbs. This module is the
ONE place that names the concrete provider — swap the app.lib.sources.yfinance_src
import to change source.
"""
from __future__ import annotations

from typing import Any, Iterator

from HyperCoreSDK.python.client import HyperClient, projection, ScopeSpec, ValueIndexSpec

# --- provider boundary: swap this single import to change source ------------
from app.lib.sources.yfinance import (
    iter_market_candidates, iter_fx_candidates, all_continents, source_available,
)
# ---------------------------------------------------------------------------
from app.lib.helpers.finance.factory import (
    FinanceFactory, MarketObservationObject, FxRateObject,
)

finance_factory = FinanceFactory()

__all__ = [
    "HyperClient",
    "seed_continents",
    "list_market_candidates", "create_market_object", "apply_market_operations",
    "list_fx_candidates", "create_fx_object", "apply_fx_operations",
    "all_continents", "source_available",
    "MarketObservationObject", "FxRateObject",
]

FINANCE_ROOT = "finance"

MARKET_PROJECT = projection("name", "ticker", "last_price", "observed_at", "observed_day")
MARKET_INDEXES: list[ValueIndexSpec] = [
    ValueIndexSpec("asset_class", "asset_class", normalize="slug", link_projections=MARKET_PROJECT),
    ValueIndexSpec("ticker", "ticker", normalize="slug",
                   scopes=[ScopeSpec("asset_class", normalize="slug")], link_projections=MARKET_PROJECT),
    ValueIndexSpec("continent", "continent", normalize="upper",
                   scopes=[ScopeSpec("asset_class", normalize="slug")], link_projections=MARKET_PROJECT),
    ValueIndexSpec("observed_day", "observed_day", normalize="slug",
                   scopes=[ScopeSpec("asset_class", normalize="slug")], link_projections=MARKET_PROJECT),
]

FX_PROJECT = projection("quote_currency", "rate", "continent", "observed_at", "observed_day")
FX_INDEXES: list[ValueIndexSpec] = [
    ValueIndexSpec("base_currency", "base_currency", normalize="upper", link_projections=FX_PROJECT),
    ValueIndexSpec("quote_currency", "quote_currency", normalize="upper",
                   scopes=[ScopeSpec("base_currency", normalize="upper")], link_projections=FX_PROJECT),
    ValueIndexSpec("continent", "continent", normalize="upper",
                   scopes=[ScopeSpec("base_currency", normalize="upper")], link_projections=FX_PROJECT),
    ValueIndexSpec("observed_day", "observed_day", normalize="slug",
                   scopes=[ScopeSpec("base_currency", normalize="upper")], link_projections=FX_PROJECT),
]


def seed_continents(client_instance: HyperClient, namespace) -> int:
    """Write each continent node ONCE up front (avoids re-inserting the shared
    continents.<CC> node per observation -> UNIQUE collision at bulk flush)."""
    ops = [{
        "path": f"{namespace}.continents.{cc.upper()}",
        "data": {"data": {"tag": "finance_continent", "continent": cc.upper()}},
    } for cc in all_continents()]
    n = client_instance.write_ops(ops, root=namespace)
    print(f"[finance] seeded continents={n}", flush=True)
    return n


# ---------------------------------------------------------------------------
# Market verbs
# ---------------------------------------------------------------------------
def list_market_candidates(
        *, limit_per_continent: int | None = None, commodity_limit: int | None = None,
) -> Iterator[tuple[dict[str, Any], bool]]:
    for record in iter_market_candidates(
            limit_per_continent=limit_per_continent, commodity_limit=commodity_limit):
        proof = bool(record.get("ticker")) and bool(record.get("observed_ms"))
        yield record, proof


def create_market_object(record: dict[str, Any]) -> tuple[MarketObservationObject | None, bool]:
    try:
        obj = finance_factory.create_market_object(record=record)
    except (TypeError, ValueError) as exc:
        print(f"  skip: bad market record ({exc})", flush=True)
        return None, False
    proof = bool(obj.ticker) and obj.observed_ms > 0
    return obj, proof


def apply_market_operations(
        obj: MarketObservationObject, client_instance: HyperClient, namespace,
        write_latest: bool = True,
) -> dict[str, str]:
    record_key = obj.record_key()
    obs_path = f"observations/{record_key}"
    obs_dot = f"{namespace}.{obs_path.replace('/', '.')}"
    asset_dot = f"{namespace}.assets.{obj.latest_key()}"
    latest_dot = f"{namespace}.latest.{obj.latest_key()}"

    links = {"asset": asset_dot, "latest": latest_dot}
    if obj.continent:
        links["continent"] = f"{namespace}.continents.{obj.continent.upper()}"

    n1 = client_instance.save_record(
        record_path=obs_path,
        record_data={"tag": "market_observation", **obj.stored()},
        index_specs=MARKET_INDEXES,
        ref_key=record_key,
        ref_payload=obj.ref_payload(),
        links=links,
        root=namespace,
    )

    ops = [
        {"path": asset_dot,
         "data": {"data": {"tag": "market_asset", "name": obj.name, "ticker": obj.ticker,
                           "asset_class": obj.asset_class, "continent": obj.continent,
                           "last_observation": obs_dot, "updated_at": obj.updated_at},
                  "links": {"latest": latest_dot, "last_observation": obs_dot}}},
    ]
    n2 = 0
    if write_latest:
        ops.append({"path": latest_dot,
                    "data": {"data": {"tag": "market_latest", "model": "market-latest",
                                      "name": obj.name, "ticker": obj.ticker,
                                      "asset_class": obj.asset_class, "continent": obj.continent,
                                      "last_price": obj.last_price, "observed_at": obj.observed_at},
                             "links": {"source": obs_dot, "asset": asset_dot}}})
        n2 = 1
    # continent asset ref as a flat leaf (no deep continents.<CC>.refs nesting)
    n3 = 0
    if obj.continent:
        cc = obj.continent.upper()
        ops.append({"path": f"{namespace}.continent_refs.{cc}-{obj.latest_key()}",
                    "data": {"data": {"tag": "market_asset_ref", "continent": cc,
                                      "ticker": obj.ticker, "last_price": obj.last_price},
                             "links": {"asset": asset_dot, "latest": latest_dot,
                                       "continent": f"{namespace}.continents.{cc}"}}})
        n3 = 1
    n = client_instance.write_ops(ops, root=namespace)

    print(f"[finance] {obs_dot} record={n1} aux={n} latest={n2} cont_ref={n3}", flush=True)
    return {"observation": obs_dot, "asset": asset_dot, "latest": latest_dot}


# ---------------------------------------------------------------------------
# FX verbs
# ---------------------------------------------------------------------------
def list_fx_candidates(
        *, base_currency: str = "USD", limit_per_continent: int | None = None,
) -> Iterator[tuple[dict[str, Any], bool]]:
    for record in iter_fx_candidates(base_currency=base_currency, limit_per_continent=limit_per_continent):
        proof = bool(record.get("quote_currency")) and bool(record.get("observed_ms"))
        yield record, proof


def create_fx_object(record: dict[str, Any]) -> tuple[FxRateObject | None, bool]:
    try:
        obj = finance_factory.create_fx_object(record=record)
    except (TypeError, ValueError) as exc:
        print(f"  skip: bad fx record ({exc})", flush=True)
        return None, False
    proof = bool(obj.quote_currency) and obj.observed_ms > 0
    return obj, proof


def apply_fx_operations(
        obj: FxRateObject, client_instance: HyperClient, namespace,
        write_latest: bool = True,
) -> dict[str, str]:
    record_key = obj.record_key()
    fx_path = f"fx/{record_key}"
    fx_dot = f"{namespace}.{fx_path.replace('/', '.')}"
    latest_dot = f"{namespace}.latest_fx.{obj.latest_key()}"
    cc = obj.continent.upper()
    cont_dot = f"{namespace}.continents.{cc}"

    n1 = client_instance.save_record(
        path=fx_path,
        data={"tag": "fx_rate", **obj.stored()},
        indexes=FX_INDEXES,
        links={"latest": latest_dot, "continent": cont_dot},
        root=namespace,
    )

    ops = []
    n2 = 0
    if write_latest:
        ops.append({"path": latest_dot,
                    "data": {"data": {"tag": "fx_latest", "model": "fx-latest", **obj.stored()},
                             "links": {"source": fx_dot, "continent": cont_dot}}})
        n2 = 1
    # flat continent rate ref
    ops.append({"path": f"{namespace}.rate_refs.{cc}-{obj.latest_key()}",
                "data": {"data": {"tag": "fx_rate_ref", "continent": cc, "pair": obj.pair,
                                  "rate": obj.rate},
                         "links": {"latest": latest_dot, "rate": fx_dot, "continent": cont_dot}}})
    n = client_instance.write_ops(ops, root=namespace)

    print(f"[finance.fx] {fx_dot} record={n1} aux={n} latest={n2}", flush=True)
    return {"rate": fx_dot, "latest": latest_dot}