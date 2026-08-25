"""Reconcile RCEm export compensation into the month it belongs to."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
import logging
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder import statistics as recorder_statistics
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later, async_track_time_change
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .runtime import EneaRcemRuntime
from .tariffs import HISTORICAL_TARIFFS, prosumer_factor_for_month

_LOGGER = logging.getLogger(__name__)

_RECONCILE_TOLERANCE_PLN = 0.005
_PRICE_CHANGE_DELAY = timedelta(minutes=7)
_RETRY_DELAY = timedelta(minutes=10)
_RECORDER_ENERGY_UNITS = {"energy": UnitOfEnergy.KILO_WATT_HOUR}


class CompensationReconciler:
    """Keep monthly compensation statistics aligned with RCEm settlement months.

    The runtime sensor is intentionally allowed to change when PSE publishes or
    corrects RCEm. Recorder would normally attribute that state delta to the
    publication month. This reconciler compares monthly long-term statistics
    with the amount that belongs to each export month and moves any difference
    to the correct month by adjusting the cumulative statistic sum.

    Reconciliation is idempotent: every run compares desired monthly changes
    with the values already stored by Recorder and applies only the difference.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        runtime: EneaRcemRuntime,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.runtime = runtime
        self.export_stat_id: str | None = None
        self.compensation_stat_id: str | None = None
        self._unsubs: list[Callable[[], None]] = []
        self._scheduled_unsub: Callable[[], None] | None = None
        self._running = False
        self._rerun_requested = False
        self._price_fingerprint: tuple[tuple[str, float, str], ...] = ()

    async def async_setup(self) -> None:
        """Resolve statistics, run an initial check and subscribe to changes."""
        registry = er.async_get(self.hass)
        self.export_stat_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{self.entry.entry_id}_balanced_export"
        )
        self.compensation_stat_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{self.entry.entry_id}_export_compensation"
        )

        if self.export_stat_id is None or self.compensation_stat_id is None:
            _LOGGER.error(
                "Cannot start compensation reconciliation: statistics entities "
                "were not found (export=%s, compensation=%s)",
                self.export_stat_id,
                self.compensation_stat_id,
            )
            return

        self._price_fingerprint = self._current_price_fingerprint()
        self._unsubs.append(self.runtime.add_listener(self._handle_runtime_update))
        self._unsubs.append(
            async_track_time_change(
                self.hass,
                self._handle_daily_reconcile,
                hour=0,
                minute=15,
                second=0,
            )
        )

        self.entry.async_create_background_task(
            self.hass,
            self.async_reconcile(),
            "Enea RCEm compensation reconciliation",
        )

    async def async_shutdown(self) -> None:
        """Stop listeners and pending reconciliation."""
        if self._scheduled_unsub is not None:
            self._scheduled_unsub()
            self._scheduled_unsub = None
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    def _current_price_fingerprint(self) -> tuple[tuple[str, float, str], ...]:
        return tuple(
            (month, float(item.price_pln_mwh), str(item.published))
            for month, item in sorted(self.runtime.rcem_prices.items())
        )

    @callback
    def _handle_runtime_update(self) -> None:
        """Schedule reconciliation only when the PSE price table changed."""
        fingerprint = self._current_price_fingerprint()
        if fingerprint == self._price_fingerprint:
            return
        self._price_fingerprint = fingerprint
        self._schedule_reconcile(_PRICE_CHANGE_DELAY)

    @callback
    def _handle_daily_reconcile(self, _now: datetime) -> None:
        """Run a low-cost daily consistency check."""
        self.hass.async_create_task(self.async_reconcile())

    @callback
    def _schedule_reconcile(self, delay: timedelta) -> None:
        if self._scheduled_unsub is not None:
            self._scheduled_unsub()
            self._scheduled_unsub = None

        @callback
        def _run(_now: datetime) -> None:
            self._scheduled_unsub = None
            self.hass.async_create_task(self.async_reconcile())

        self._scheduled_unsub = async_call_later(self.hass, delay, _run)

    async def async_reconcile(self) -> None:
        """Reconcile every RCEm month against Recorder long-term statistics."""
        if self.export_stat_id is None or self.compensation_stat_id is None:
            return
        if self._running:
            self._rerun_requested = True
            return

        self._running = True
        try:
            await self._async_reconcile_once()
            deposit = getattr(self.runtime, "deposit_coordinator", None)
            if deposit is not None:
                await deposit.async_refresh()
        except Exception:  # noqa: BLE001 - reconciliation must never break integration
            _LOGGER.exception("RCEm compensation reconciliation failed")
            self._schedule_reconcile(_RETRY_DELAY)
        finally:
            self._running = False
            if self._rerun_requested:
                self._rerun_requested = False
                self._schedule_reconcile(timedelta(seconds=5))

    async def _async_reconcile_once(self) -> None:
        assert self.export_stat_id is not None
        assert self.compensation_stat_id is not None

        now = dt_util.now()
        tz = dt_util.get_time_zone(self.hass.config.time_zone)
        history_start = HISTORICAL_TARIFFS[0].start
        start_local = datetime(
            history_start.year,
            history_start.month,
            1,
            tzinfo=tz,
        )
        end_utc = now.astimezone(UTC)
        instance = get_instance(self.hass)

        monthly = await instance.async_add_executor_job(
            recorder_statistics.statistics_during_period,
            self.hass,
            start_local.astimezone(UTC),
            end_utc,
            {self.export_stat_id, self.compensation_stat_id},
            "month",
            _RECORDER_ENERGY_UNITS,
            {"change", "sum"},
        )

        export_changes = self._changes_by_month(monthly.get(self.export_stat_id, []))
        compensation_changes = self._changes_by_month(
            monthly.get(self.compensation_stat_id, [])
        )
        current_month = now.strftime("%Y-%m")

        months = sorted(set(export_changes) | set(compensation_changes))
        adjustments: list[tuple[str, float, datetime]] = []

        current_adjustment_start: datetime | None = None

        for month in months:
            export_kwh = float(export_changes.get(month, 0.0))
            actual = float(compensation_changes.get(month, 0.0))
            desired = 0.0

            # Only a closed month can be settled. A missing PSE RCEm means the
            # correct compensation for that month is still zero.
            if month < current_month and (price := self.runtime.rcem_prices.get(month)):
                desired = (
                    export_kwh
                    * float(price.price_pln_mwh)
                    / 1000.0
                    * prosumer_factor_for_month(month)
                )

            delta = desired - actual
            if abs(delta) < _RECONCILE_TOLERANCE_PLN:
                continue

            if month == current_month:
                if current_adjustment_start is None:
                    current_adjustment_start = await self._latest_hour_start(
                        start_local=datetime(
                            now.year,
                            now.month,
                            1,
                            tzinfo=tz,
                        ),
                        end_utc=end_utc,
                    )
                if current_adjustment_start is None:
                    _LOGGER.warning(
                        "Cannot reconcile current-month RCEm compensation %.6f PLN: "
                        "no hourly compensation statistics are available yet",
                        delta,
                    )
                    self._schedule_reconcile(_RETRY_DELAY)
                    continue
                adjustment_start = current_adjustment_start
            else:
                adjustment_start = self._last_hour_start(month, tz)

            adjustments.append((month, delta, adjustment_start))

        if not adjustments:
            _LOGGER.debug("RCEm compensation statistics are already reconciled")
            return

        for month, delta, adjustment_start in adjustments:
            _LOGGER.info(
                "Reconciling RCEm compensation for %s by %+.6f PLN from %s",
                month,
                delta,
                adjustment_start.isoformat(),
            )
            instance.async_adjust_statistics(
                self.compensation_stat_id,
                adjustment_start,
                delta,
                "PLN",
            )

        await instance.async_block_till_done()

        # A follow-up pass is deliberate. Recorder state changes caused by an
        # RCEm publication can reach statistics shortly after the price refresh;
        # the next pass removes that publication-month posting if necessary.
        self._schedule_reconcile(_RETRY_DELAY)

    async def _latest_hour_start(
        self,
        *,
        start_local: datetime,
        end_utc: datetime,
    ) -> datetime | None:
        """Return the newest stored hourly compensation statistic timestamp."""
        assert self.compensation_stat_id is not None
        instance = get_instance(self.hass)
        hourly = await instance.async_add_executor_job(
            recorder_statistics.statistics_during_period,
            self.hass,
            start_local.astimezone(UTC),
            end_utc,
            {self.compensation_stat_id},
            "hour",
            None,
            {"sum"},
        )
        rows = hourly.get(self.compensation_stat_id, [])
        if not rows:
            return None
        latest = max(rows, key=lambda row: self._timestamp(row.get("start")))
        return datetime.fromtimestamp(self._timestamp(latest.get("start")), UTC)

    def _changes_by_month(self, rows: list[dict[str, Any]]) -> dict[str, float]:
        tz = dt_util.get_time_zone(self.hass.config.time_zone)
        result: dict[str, float] = {}
        for row in rows:
            change = row.get("change")
            if change is None:
                continue
            start = datetime.fromtimestamp(self._timestamp(row.get("start")), UTC)
            month = start.astimezone(tz).strftime("%Y-%m")
            result[month] = float(change)
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

    @staticmethod
    def _last_hour_start(month: str, tz: Any) -> datetime:
        year_text, month_text = month.split("-", 1)
        year = int(year_text)
        month_number = int(month_text)
        if month_number == 12:
            next_month = datetime(year + 1, 1, 1, tzinfo=tz)
        else:
            next_month = datetime(year, month_number + 1, 1, tzinfo=tz)
        return next_month.astimezone(UTC) - timedelta(hours=1)
