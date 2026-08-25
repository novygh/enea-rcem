"""Prosumer deposit ledger for Enea RCEm."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
import logging
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder import statistics as recorder_statistics
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ENERGY_PRICE_NET,
    DEFAULT_ENERGY_PRICE_NET,
    DOMAIN,
)
from .runtime import EneaRcemRuntime
from .tariffs import HISTORICAL_TARIFFS, tariff_for_date

_LOGGER = logging.getLogger(__name__)

REFUND_LIMIT = 0.20
_RECORDER_ENERGY_UNITS = {"energy": UnitOfEnergy.KILO_WATT_HOUR}


@dataclass(slots=True)
class DepositLot:
    """One monthly deposit lot."""

    source_month: str
    assigned_month: str
    expiry_month: str
    original: float
    remaining: float


@dataclass(slots=True)
class DepositSnapshot:
    """Current reconstructed prosumer deposit state."""

    balance: float
    assigned_current_month: float
    used_current_month: float
    active_energy_due_current_month: float
    active_energy_purchase_current_month: float
    total_used: float
    total_refund: float
    total_expired: float
    oldest_source_month: str | None
    oldest_assigned_month: str | None
    oldest_expiry_month: str | None
    oldest_remaining: float | None
    oldest_max_refund: float | None


@dataclass(slots=True)
class SettledMonthSnapshot:
    """Latest closed month for which PSE RCEm and Recorder data are available."""

    month: str
    import_cost: float
    export_compensation: float


def _month_add(month: str, delta: int) -> str:
    year, mon = map(int, month.split("-"))
    absolute = year * 12 + (mon - 1) + delta
    return f"{absolute // 12:04d}-{absolute % 12 + 1:02d}"


def _month_range(start: str, end: str):
    month = start
    while month <= end:
        yield month
        month = _month_add(month, 1)


def _month_date(month: str) -> date:
    year, mon = map(int, month.split("-"))
    return date(year, mon, 1)


def _historical_active_energy_gross(month: str) -> float:
    period = tariff_for_date(_month_date(month))
    if period is None:
        # The contract starts on 2024-06-12, so the first calendar month has
        # no tariff on its first day. Use the tariff that starts inside that
        # month instead of inventing a pre-contract rate.
        period = next(
            (
                candidate
                for candidate in HISTORICAL_TARIFFS
                if f"{candidate.start.year:04d}-{candidate.start.month:02d}" == month
            ),
            None,
        )
    if period is None:
        raise ValueError(f"No historical tariff for {month}")
    return period.energy_price_net * (1.0 + period.vat_rate / 100.0)


def build_deposit_snapshot(
    *,
    imports: dict[str, float],
    compensation: dict[str, float],
    current_month: str,
    current_active_energy_gross: float,
) -> DepositSnapshot:
    """Reconstruct the deposit ledger and return its current state."""
    first = HISTORICAL_TARIFFS[0].start
    start_month = f"{first.year:04d}-{first.month:02d}"

    lots: list[DepositLot] = []
    total_refund = 0.0
    total_expired = 0.0
    total_used = 0.0

    current_assigned = 0.0
    current_used = 0.0
    current_due = 0.0
    current_purchase = 0.0

    for month in _month_range(start_month, current_month):
        refund = 0.0
        expired = 0.0
        survivors: list[DepositLot] = []

        # A lot assigned in month M is usable in M..M+11. In M+12 the
        # remaining amount reaches the statutory refund/expiry stage.
        for lot in lots:
            if lot.expiry_month == month:
                lot_refund = min(lot.remaining, lot.original * REFUND_LIMIT)
                lot_expired = max(lot.remaining - lot_refund, 0.0)
                refund += lot_refund
                expired += lot_expired
            else:
                survivors.append(lot)
        lots = survivors

        source_month = _month_add(month, -1)
        assigned = max(float(compensation.get(source_month, 0.0)), 0.0)
        if assigned > 0:
            lots.append(
                DepositLot(
                    source_month=source_month,
                    assigned_month=month,
                    expiry_month=_month_add(month, 12),
                    original=assigned,
                    remaining=assigned,
                )
            )

        import_kwh = max(float(imports.get(month, 0.0)), 0.0)
        active_rate = (
            current_active_energy_gross
            if month == current_month
            else _historical_active_energy_gross(month)
        )
        energy_purchase = import_kwh * active_rate

        remaining_obligation = energy_purchase
        used = 0.0
        for lot in lots:
            if remaining_obligation <= 1e-12:
                break
            amount = min(lot.remaining, remaining_obligation)
            lot.remaining -= amount
            remaining_obligation -= amount
            used += amount

        lots = [lot for lot in lots if lot.remaining > 1e-9]

        total_refund += refund
        total_expired += expired
        total_used += used

        if month == current_month:
            current_assigned = assigned
            current_used = used
            current_due = max(remaining_obligation, 0.0)
            current_purchase = energy_purchase

    balance = sum(lot.remaining for lot in lots)
    oldest = (
        min(lots, key=lambda lot: (lot.assigned_month, lot.source_month))
        if lots
        else None
    )

    return DepositSnapshot(
        balance=balance,
        assigned_current_month=current_assigned,
        used_current_month=current_used,
        active_energy_due_current_month=current_due,
        active_energy_purchase_current_month=current_purchase,
        total_used=total_used,
        total_refund=total_refund,
        total_expired=total_expired,
        oldest_source_month=oldest.source_month if oldest else None,
        oldest_assigned_month=oldest.assigned_month if oldest else None,
        oldest_expiry_month=oldest.expiry_month if oldest else None,
        oldest_remaining=oldest.remaining if oldest else None,
        oldest_max_refund=(
            min(oldest.remaining, oldest.original * REFUND_LIMIT) if oldest else None
        ),
    )


class DepositCoordinator:
    """Reconstruct the prosumer deposit from Recorder monthly statistics."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        runtime: EneaRcemRuntime,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.runtime = runtime
        self.import_stat_id: str | None = None
        self.import_cost_stat_id: str | None = None
        self.compensation_stat_id: str | None = None
        self._unsubs: list[Callable[[], None]] = []
        self._running = False
        self._rerun_requested = False

    async def async_setup(self) -> None:
        """Resolve statistics, calculate initial ledger and schedule refreshes."""
        registry = er.async_get(self.hass)
        self.import_stat_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{self.entry.entry_id}_balanced_import"
        )
        self.import_cost_stat_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{self.entry.entry_id}_import_cost"
        )
        self.compensation_stat_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{self.entry.entry_id}_export_compensation"
        )

        if (
            self.import_stat_id is None
            or self.import_cost_stat_id is None
            or self.compensation_stat_id is None
        ):
            _LOGGER.error(
                "Cannot start prosumer deposit ledger: statistics entities were not "
                "found (import=%s, import_cost=%s, compensation=%s)",
                self.import_stat_id,
                self.import_cost_stat_id,
                self.compensation_stat_id,
            )
            return

        # Minute 20 is intentionally after the hourly statistics compilation and
        # after the 00:15 daily compensation consistency check.
        self._unsubs.append(
            async_track_time_change(
                self.hass,
                self._handle_hourly_refresh,
                minute=20,
                second=0,
            )
        )
        self.entry.async_create_background_task(
            self.hass,
            self.async_refresh(),
            "Enea RCEm deposit reconstruction",
        )

    async def async_shutdown(self) -> None:
        """Stop scheduled refreshes."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    @callback
    def _handle_hourly_refresh(self, _now: datetime) -> None:
        self.hass.async_create_task(self.async_refresh())

    async def async_refresh(self) -> None:
        """Refresh the reconstructed deposit snapshot."""
        if (
            self.import_stat_id is None
            or self.import_cost_stat_id is None
            or self.compensation_stat_id is None
        ):
            return
        if self._running:
            self._rerun_requested = True
            return

        self._running = True
        try:
            await self._async_refresh_once()
        except Exception:  # noqa: BLE001 - diagnostics must not break integration
            _LOGGER.exception("Prosumer deposit ledger refresh failed")
        finally:
            self._running = False
            if self._rerun_requested:
                self._rerun_requested = False
                self.hass.async_create_task(self.async_refresh())

    async def _async_refresh_once(self) -> None:
        assert self.import_stat_id is not None
        assert self.import_cost_stat_id is not None
        assert self.compensation_stat_id is not None

        now = dt_util.now()
        tz = dt_util.get_time_zone(self.hass.config.time_zone)
        first = HISTORICAL_TARIFFS[0].start
        start_local = datetime(first.year, first.month, 1, tzinfo=tz)
        instance = get_instance(self.hass)

        monthly = await instance.async_add_executor_job(
            recorder_statistics.statistics_during_period,
            self.hass,
            start_local.astimezone(UTC),
            now.astimezone(UTC),
            {
                self.import_stat_id,
                self.import_cost_stat_id,
                self.compensation_stat_id,
            },
            "month",
            _RECORDER_ENERGY_UNITS,
            {"change", "sum"},
        )

        imports = self._changes_by_month(monthly.get(self.import_stat_id, []))
        import_costs = self._changes_by_month(
            monthly.get(self.import_cost_stat_id, [])
        )
        compensation = self._changes_by_month(
            monthly.get(self.compensation_stat_id, [])
        )
        current_month = now.strftime("%Y-%m")
        current_active_energy_gross = (
            self.runtime._rate(CONF_ENERGY_PRICE_NET, DEFAULT_ENERGY_PRICE_NET)
            * self.runtime.vat_multiplier
        )

        snapshot = build_deposit_snapshot(
            imports=imports,
            compensation=compensation,
            current_month=current_month,
            current_active_energy_gross=current_active_energy_gross,
        )
        setattr(self.runtime, "deposit_snapshot", snapshot)

        settled_month = self._latest_settled_month(
            current_month,
            set(import_costs) | set(compensation),
        )
        if settled_month is None:
            setattr(self.runtime, "settled_month_snapshot", None)
        else:
            setattr(
                self.runtime,
                "settled_month_snapshot",
                SettledMonthSnapshot(
                    month=settled_month,
                    import_cost=float(import_costs.get(settled_month, 0.0)),
                    export_compensation=float(
                        compensation.get(settled_month, 0.0)
                    ),
                ),
            )

        self.runtime._notify()

    def _latest_settled_month(
        self,
        current_month: str,
        available_months: set[str],
    ) -> str | None:
        """Return newest closed month with RCEm and Recorder billing data."""
        candidates = [
            month
            for month in available_months
            if month < current_month and month in self.runtime.rcem_prices
        ]
        return max(candidates) if candidates else None

    def _changes_by_month(self, rows: list[dict[str, Any]]) -> dict[str, float]:
        tz = dt_util.get_time_zone(self.hass.config.time_zone)
        result: dict[str, float] = {}
        for row in rows:
            change = row.get("change")
            if change is None:
                continue
            start = datetime.fromtimestamp(self._timestamp(row.get("start")), UTC)
            result[start.astimezone(tz).strftime("%Y-%m")] = float(change)
        return result

    @staticmethod
    def _timestamp(value: Any) -> float:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            return value.timestamp()
        if isinstance(value, (int, float)):
            return float(value)
        raise ValueError(f"Invalid statistics timestamp: {value!r}")
