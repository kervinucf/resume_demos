"""
Build the finance database from a yfinance provider (root="finance").

Market observations land at finance.observations.<asset_class>-<ticker>-<observed_ms>;
FX rates at finance.fx.<base>-<quote>-<observed_ms>. Each has a flat asset/latest
node and a flat continent ref. Continents are seeded once up front.

Orchestration only. The provider is read inside the create_*/list_* verbs and named
in exactly one place (app/lib/helpers/finance/__init__.py); this file never touches
yfinance, ticker tables, or the source module — switching providers doesn't touch it.
"""
from __future__ import annotations

import sys

from app.lib.helpers.finance import (
    HyperClient,
    seed_continents,
    list_market_candidates, create_market_object, apply_market_operations,
    list_fx_candidates, create_fx_object, apply_fx_operations,
    MarketObservationObject, FxRateObject,
)

__all__ = ["load_finance", "MarketObservationObject", "FxRateObject"]

FINANCE_ROOT = "finance"


def load_finance(
        ROOT: str = FINANCE_ROOT,
        DATA_DIR: str = None,
        limit_per_continent: int | None = None,
        commodity_limit: int | None = None,
        include_market: bool = True,
        include_fx: bool = True,
) -> int:
    print(f"data dir: {DATA_DIR}", flush=True)

    try:
        with HyperClient.open_sqlite_file(root_key=ROOT, reset=True, path=DATA_DIR) as data_store:
            seed_continents(data_store, FINANCE_ROOT)

            written = 0
            if include_market:
                latest_done: set[str] = set()
                for record, candidate_proof in list_market_candidates(
                        limit_per_continent=limit_per_continent, commodity_limit=commodity_limit):
                    if not candidate_proof:
                        continue
                    obj, object_proof = create_market_object(record)
                    if not obj:
                        print("skipping", record.get("ticker"))
                        continue
                    key = obj.latest_key()
                    write_latest = key not in latest_done
                    latest_done.add(key)
                    apply_market_operations(obj=obj, client_instance=data_store,
                                            namespace=FINANCE_ROOT, write_latest=write_latest)
                    written += 1
                    price = obj.last_price
                    print(f"  ok: {obj.name} [{obj.ticker}] "
                          f"price={'N/A' if price is None else f'{price:,.2f}'}", flush=True)

            if include_fx:
                latest_done = set()
                for record, candidate_proof in list_fx_candidates(
                        base_currency="USD", limit_per_continent=limit_per_continent):
                    if not candidate_proof:
                        continue
                    obj, object_proof = create_fx_object(record)
                    if not obj:
                        print("skipping", record.get("quote_currency"))
                        continue
                    key = obj.latest_key()
                    write_latest = key not in latest_done
                    latest_done.add(key)
                    apply_fx_operations(obj=obj, client_instance=data_store,
                                        namespace=FINANCE_ROOT, write_latest=write_latest)
                    written += 1
                    rate = obj.rate
                    print(f"  ok: USD->{obj.quote_currency} "
                          f"rate={'N/A' if rate is None else f'{rate:.4f}'}", flush=True)

            print(f"done: built {data_store.count:,} records ({data_store.written:,} writes)", flush=True)

    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(load_finance())
