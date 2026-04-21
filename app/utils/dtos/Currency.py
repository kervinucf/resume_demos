from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, timedelta, timezone

from ___.HyperCoreSDK import HyperClient, HyperCoreNode


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


@dataclass(frozen=True)
class CurrencyRate(HyperCoreNode):
    currency_code: str
    currency_name: str
    fx_date: str
    usd_per_local: float
    local_per_usd: float
    fetched_at: str | None = None

    def to_kv(self) -> dict:
        return asdict(self)

    @classmethod
    def properties(cls) -> list[str]:
        return [field.name for field in fields(cls)]

    def save_to(self, hyper_client: HyperClient) -> None:
        old_root = hyper_client.root
        try:
            hyper_client.root = "finance"
            self.commit(
                hyper_client,
                sub_paths=["rates", self.currency_code.upper()],
            )
        finally:
            hyper_client.root = old_root

    @classmethod
    def load_from(
        cls,
        hyper_client: HyperClient,
        *,
        currency_code: str,
        max_age_days: int = 4,
    ) -> tuple["CurrencyRate | None", bool]:
        old_root = hyper_client.root
        try:
            hyper_client.root = "finance"
            clean_data, metadata = cls.read_out(
                hyper_client,
                sub_paths=["rates", currency_code.upper()],
            )
        finally:
            hyper_client.root = old_root

        if not clean_data:
            return None, True

        try:
            instance = cls(**clean_data)
            object.__setattr__(instance, "_metadata", metadata)

            fx_day = datetime.strptime(instance.fx_date, "%Y-%m-%d").date()
            update_required = (date.today() - fx_day).days >= max_age_days

            return instance, update_required
        except (TypeError, ValueError, KeyError):
            return None, True


@dataclass(frozen=True)
class CountryCurrency(HyperCoreNode):
    country_code: str
    currency_code: str
    currency_name: str
    fx_supported: bool

    def to_kv(self) -> dict:
        return asdict(self)

    @classmethod
    def properties(cls) -> list[str]:
        return [field.name for field in fields(cls)]

    def save_to(self, hyper_client: HyperClient) -> None:
        old_root = hyper_client.root
        try:
            hyper_client.root = "finance"
            self.commit(
                hyper_client,
                sub_paths=["country", self.country_code.upper()],
            )
        finally:
            hyper_client.root = old_root

    @classmethod
    def load_from(
        cls,
        hyper_client: HyperClient,
        *,
        country_code: str,
    ) -> "CountryCurrency | None":
        old_root = hyper_client.root
        try:
            hyper_client.root = "finance"
            clean_data, metadata = cls.read_out(
                hyper_client,
                sub_paths=["country", country_code.upper()],
            )
        finally:
            hyper_client.root = old_root

        if not clean_data:
            return None

        try:
            instance = cls(**clean_data)
            object.__setattr__(instance, "_metadata", metadata)
            return instance
        except (TypeError, ValueError, KeyError):
            return None


@dataclass(frozen=True)
class CurrencyRateState(HyperCoreNode):
    currency_code: str
    fx_date: str | None
    last_checked_at: str
    refreshed_at: str | None
    ok: bool
    error: str | None = None

    def to_kv(self) -> dict:
        return asdict(self)

    @classmethod
    def properties(cls) -> list[str]:
        return [field.name for field in fields(cls)]

    def save_to(self, hyper_client: HyperClient) -> None:
        old_root = hyper_client.root
        try:
            hyper_client.root = "finance"
            self.commit(
                hyper_client,
                sub_paths=["state", "rates", self.currency_code.upper()],
            )
        finally:
            hyper_client.root = old_root

    @classmethod
    def load_from(
        cls,
        hyper_client: HyperClient,
        *,
        currency_code: str,
        max_age_hours: int = 24,
    ) -> tuple["CurrencyRateState | None", bool]:
        old_root = hyper_client.root
        try:
            hyper_client.root = "finance"
            clean_data, metadata = cls.read_out(
                hyper_client,
                sub_paths=["state", "rates", currency_code.upper()],
            )
        finally:
            hyper_client.root = old_root

        if not clean_data:
            return None, True

        try:
            instance = cls(**clean_data)
            object.__setattr__(instance, "_metadata", metadata)

            basis = _parse_iso_datetime(instance.refreshed_at) or _parse_iso_datetime(
                instance.last_checked_at
            )
            if basis is None:
                return instance, True

            update_required = (
                datetime.now(timezone.utc) - basis
            ) >= timedelta(hours=max_age_hours)

            return instance, update_required
        except (TypeError, ValueError, KeyError):
            return None, True