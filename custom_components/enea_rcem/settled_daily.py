"""Daily detail for the latest settled RCEm month."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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

from .const import DOMAIN
from .runtime import EneaRcemRuntime
from .tariffs import HISTORICAL_TARIFFS, prosumer_factor_for_month

_LOGGER = logging.getLogger(__name__)

_RECORDER_ENERGY_UNITS = {"energy": UnitOfEnergy.KILO_WATT_HOUR}


@dataclass(slots=True)
class SettledDailyPoint:
    """One calendar day of the latest settled month."""

    date: str
    import_cost: float
    export_compensation: float
    export_kwh: float


@dataclass(slots=True)
class SettledDailySnapshot:
    """Daily series for the latest closed month with official RCEm."""

    month: str
    points: list[SettledDailyPoint]


def _month_add(month: str, delta: int) -> str:
    year, mon = map(int, month.split("-"))
    absolute = year * 12 + (mon - 1) + delta
    return f"{absolute // 12:04d}-{absolute % 12 + 1:02d}"


class SettledDailyCoordinator:
    """Build daily billing series for the latest settled month."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        runtime: EneaRcemRuntime,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.runtime = runtime
        self.import_cost_stat_id: str | None = None
        self.export_stat_id: str | None = None
        self._unsubs: list[Callable[[], None]] = []
        self._running = False
        self._rerun_requested = False

    async def async_setup(self) -> None:
        """Resolve Recorder statistics and schedule refreshes."""
        registry = er.async_get(self.hass)
        self.import_cost_stat_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{self.entry.entry_id}_import_cost"
        )
        self.export_stat_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{self.entry.entry_id}_balanced_export"
        )

        if self.import_cost_stat_id is None or self.export_stat_id is None:
            _LOGGER.error(
                "Cannot build settled daily series: statistics entities were not "
                "found (import_cost=%s, export=%s)",
                self.import_cost_stat_id,
                self.export_stat_id,
            )
            return

        self._unsubs.append(
            async_track_time_change(
                self.hass,
                self._handle_hourly_refresh,
                minute=25,
                second=0,
            )
        )
        self.entry.async_create_background_task(
            self.hass,
            self.async_refresh(),
            "Enea RCEm settled daily series",
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
        """Refresh the daily settled-month snapshot."""
        if self.import_cost_stat_id is None or self.export_stat_id is None:
            return
        if self._running:
            self._rerun_requested = True
            return

        self._running = True
        try:
            await self._async_refresh_once()
        except Exception:  # noqa: BLE001 - diagnostics must not break integration
            _LOGGER.exception("Settled daily series refresh failed")
        finally:
            self._running = False
            if self._rerun_requested:
                self._rerun_requested = False
                self.hass.async_create_task(self.async_refresh())

    async def _async_refresh_once(self) -> None:
        assert self.import_cost_stat_id is not None
        assert self.export_stat_id is not None

        now = dt_util.now()
        tz = dt_util.get_time_zone(self.hass.config.time_zone)
        first = HISTORICAL_TARIFFS[0].start
        history_start_local = datetime(first.year, first.month, 1, tzinfo=tz)
        instance = get_instance(self.hass)

        monthly = await instance.async_add_executor_job(
            recorder_statistics.statistics_during_period,
            self.hass,
            history_start_local.astimezone(UTC),
            now.astimezone(UTC),
            {self.import_cost_stat_id, self.export_stat_id},
            "month",
            _RECORDER_ENERGY_UNITS,
            {"change", "sum"},
        )

        available_months = self._months_with_changes(
            monthly.get(self.import_cost_stat_id, [])
        ) | self._months_with_changes(monthly.get(self.export_stat_id, []))
        current_month = now.strftime("%Y-%m")
        candidates = [
            month
            for month in available_months
            if month < current_month and month in self.runtime.rcem_prices
        ]

        if not candidates:
            setattr(self.runtime, "settled_daily_snapshot", None)
            self.runtime._notify()
            return

        month = max(candidates)
        year, mon = map(int, month.split("-"))
        next_month = _month_add(month, 1)
        next_year, next_mon = map(int, next_month.split("-"))
        start_local = datetime(year, mon, 1, tzinfo=tz)
        end_local = datetime(next_year, next_mon, 1, tzinfo=tz)

        daily = await instance.async_add_executor_job(
            recorder_statistics.statistics_during_period,
            self.hass,
            start_local.astimezone(UTC),
            end_local.astimezone(UTC),
            {self.import_cost_stat_id, self.export_stat_id},
            "day",
            _RECORDER_ENERGY_UNITS,
            {"change", "sum"},
        )

        import_costs = self._changes_by_day(
            daily.get(self.import_cost_stat_id, []), tz
        )
        exports = self._changes_by_day(daily.get(self.export_stat_id, []), tz)
        price = self.runtime.rcem_prices[month]
        factor = prosumer_factor_for_month(month)

        points: list[SettledDailyPoint] = []
        day = start_local
        while day < end_local:
            day_key = day.strftime("%Y-%m-%d")
            export_kwh = max(float(exports.get(day_key, 0.0)), 0.0)
            export_compensation = (
                export_kwh * float(price.price_pln_mwh) / 1000.0 * factor
            )
            points.append(
                SettledDailyPoint(
                    date=day_key,
                    import_cost=max(float(import_costs.get(day_key, 0.0)), 0.0),
                    export_compensation=export_compensation,
                    export_kwh=export_kwh,
                )
            )
            day += timedelta(days=1)

        setattr(
            self.runtime,
            "settled_daily_snapshot",
            SettledDailySnapshot(month=month, points=points),
        )
        self.runtime._notify()

    def _months_with_changes(self, rows: list[dict[str, Any]]) -> set[str]:
        tz = dt_util.get_time_zone(self.hass.config.time_zone)
        result: set[str] = set()
        for row in rows:
            if row.get("change") is None:
                continue
            start = datetime.fromtimestamp(self._timestamp(row.get("start")), UTC)
            result.add(start.astimezone(tz).strftime("%Y-%m"))
        return result

    def _changes_by_day(
        self,
        rows: list[dict[str, Any]],
        tz,
    ) -> dict[str, float]:
        result: dict[str, float] = {}
        for row in rows:
            change = row.get("change")
            if change is None:
                continue
            start = datetime.fromtimestamp(self._timestamp(row.get("start")), UTC)
            result[start.astimezone(tz).strftime("%Y-%m-%d")] = float(change)
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
