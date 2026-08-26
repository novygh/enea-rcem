"""Keep Enea RCEm hourly Recorder statistics aligned with billing buckets."""

from __future__ import annotations

from collections.abc import Callable, Mapping
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

from .const import DOMAIN
from .runtime import EneaRcemRuntime

_LOGGER = logging.getLogger(__name__)

_TOLERANCE = 1e-9
_MAX_TARGET_HOURS = 168
_RECORDER_ENERGY_UNITS = {"energy": UnitOfEnergy.KILO_WATT_HOUR}


class StatisticsAligner:
    """Align Recorder changes with the runtime's finalized local-hour buckets."""

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
        self.export_stat_id: str | None = None
        self.cost_stat_id: str | None = None
        self._unsubs: list[Callable[[], None]] = []
        self._running = False
        self._rerun_requested = False
        self._capture_installed = False
        self._original_finalize_values = runtime._finalize_values

    def install_capture(self) -> None:
        """Capture finalized buckets before runtime startup recovery can consume them."""
        if self._capture_installed:
            return
        self.runtime._finalize_values = self._capture_finalize_values
        self._capture_installed = True

    def _capture_finalize_values(self, hour_key: str, imp: float, exp: float) -> None:
        """Run the normal finalizer and remember its exact per-hour Recorder target."""
        raw_balanced_import = max(imp - exp, 0.0)
        raw_balanced_export = max(exp - imp, 0.0)
        balanced_import = raw_balanced_import * self.runtime.import_correction_multiplier
        balanced_export = raw_balanced_export * self.runtime.export_correction_multiplier
        fixed_hour = self.runtime.fixed_monthly_gross / self.runtime._hours_in_month(
            hour_key
        )
        import_cost = fixed_hour + balanced_import * self.runtime.variable_rate_gross

        self._original_finalize_values(hour_key, imp, exp)

        targets = dict(self.runtime._data.get("statistics_targets", {}))
        targets[hour_key] = {
            "import_kwh": balanced_import,
            "export_kwh": balanced_export,
            "import_cost_pln": import_cost,
        }
        if len(targets) > _MAX_TARGET_HOURS:
            for old_key in sorted(targets)[: len(targets) - _MAX_TARGET_HOURS]:
                targets.pop(old_key, None)
        self.runtime._data["statistics_targets"] = targets

    async def async_setup(self) -> None:
        """Resolve entity statistics, schedule and run an initial reconciliation."""
        registry = er.async_get(self.hass)
        self.import_stat_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{self.entry.entry_id}_balanced_import"
        )
        self.export_stat_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{self.entry.entry_id}_balanced_export"
        )
        self.cost_stat_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{self.entry.entry_id}_import_cost"
        )
        if not all((self.import_stat_id, self.export_stat_id, self.cost_stat_id)):
            _LOGGER.error(
                "Cannot start hourly statistics alignment: import=%s export=%s cost=%s",
                self.import_stat_id,
                self.export_stat_id,
                self.cost_stat_id,
            )
            return

        self._unsubs.append(
            async_track_time_change(
                self.hass,
                self._handle_hourly_alignment,
                minute=10,
                second=0,
            )
        )
        self.entry.async_create_background_task(
            self.hass,
            self.async_reconcile(),
            "Enea RCEm hourly statistics alignment",
        )

    async def async_shutdown(self) -> None:
        """Stop alignment and restore the runtime finalizer."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._capture_installed:
            self.runtime._finalize_values = self._original_finalize_values
            self._capture_installed = False

    @callback
    def _handle_hourly_alignment(self, _now: datetime) -> None:
        self.hass.async_create_task(self.async_reconcile())

    async def async_reconcile(
        self,
        targets: Mapping[str, Mapping[str, float]] | None = None,
    ) -> list[dict[str, Any]]:
        """Adjust only the differences still required for finalized hours."""
        if self._running:
            self._rerun_requested = True
            return []

        self._running = True
        try:
            return await self._async_reconcile_once(targets)
        finally:
            self._running = False
            if self._rerun_requested:
                self._rerun_requested = False
                self.hass.async_create_task(self.async_reconcile())

    async def _async_reconcile_once(
        self,
        targets: Mapping[str, Mapping[str, float]] | None,
    ) -> list[dict[str, Any]]:
        if not all((self.import_stat_id, self.export_stat_id, self.cost_stat_id)):
            return []

        target_map = dict(
            targets
            if targets is not None
            else self.runtime._data.get("statistics_targets", {})
        )
        parsed_targets: list[tuple[datetime, Mapping[str, float]]] = []
        for hour_key, values in target_map.items():
            parsed = datetime.fromisoformat(hour_key)
            if parsed.tzinfo is None:
                continue
            parsed_targets.append((parsed.astimezone(UTC), values))
        if not parsed_targets:
            return []

        parsed_targets.sort(key=lambda item: item[0])
        start = parsed_targets[0][0]
        end = parsed_targets[-1][0].replace(minute=0, second=0, microsecond=0)
        end += timedelta(hours=1)

        instance = get_instance(self.hass)
        stat_ids = {self.import_stat_id, self.export_stat_id, self.cost_stat_id}
        rows = await instance.async_add_executor_job(
            recorder_statistics.statistics_during_period,
            self.hass,
            start,
            end,
            stat_ids,
            "hour",
            _RECORDER_ENERGY_UNITS,
            {"change", "sum"},
        )

        current: dict[str, dict[int, float]] = {}
        for statistic_id, stat_rows in rows.items():
            current[statistic_id] = {
                int(round(_timestamp(row["start"]))): float(
                    row.get("change", 0.0) or 0.0
                )
                for row in stat_rows
            }

        adjustments: list[dict[str, Any]] = []
        for hour_start, values in parsed_targets:
            stamp = int(round(hour_start.timestamp()))
            self._queue_difference(
                instance,
                self.import_stat_id,
                hour_start,
                current.get(self.import_stat_id, {}).get(stamp, 0.0),
                float(values.get("import_kwh", 0.0)),
                UnitOfEnergy.KILO_WATT_HOUR,
                adjustments,
            )
            self._queue_difference(
                instance,
                self.export_stat_id,
                hour_start,
                current.get(self.export_stat_id, {}).get(stamp, 0.0),
                float(values.get("export_kwh", 0.0)),
                UnitOfEnergy.KILO_WATT_HOUR,
                adjustments,
            )
            self._queue_difference(
                instance,
                self.cost_stat_id,
                hour_start,
                current.get(self.cost_stat_id, {}).get(stamp, 0.0),
                float(values.get("import_cost_pln", 0.0)),
                "PLN",
                adjustments,
            )

        self.runtime._data["statistics_alignment_last"] = {
            "at": datetime.now(UTC).isoformat(),
            "target_hours": len(parsed_targets),
            "adjustments_queued": len(adjustments),
        }
        self.runtime._schedule_save()
        if adjustments:
            _LOGGER.info(
                "Queued %d Enea RCEm Recorder alignment adjustments for %d finalized hours",
                len(adjustments),
                len(parsed_targets),
            )
        return adjustments

    @staticmethod
    def _queue_difference(
        instance,
        statistic_id: str,
        start: datetime,
        current: float,
        target: float,
        unit: str,
        queued: list[dict[str, Any]],
    ) -> None:
        adjustment = target - current
        if abs(adjustment) <= _TOLERANCE:
            return
        instance.async_adjust_statistics(statistic_id, start, adjustment, unit)
        queued.append(
            {
                "statistic_id": statistic_id,
                "start": start.isoformat(),
                "current_change": current,
                "target_change": target,
                "adjustment": adjustment,
                "unit": unit,
            }
        )


def _timestamp(value: Any) -> float:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    raise ValueError(f"Invalid statistics timestamp: {value!r}")
