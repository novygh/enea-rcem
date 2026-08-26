"""One-shot repair helpers for a known 2026-08-25 transition issue."""

from __future__ import annotations

from datetime import UTC, datetime
import logging
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder import statistics as recorder_statistics
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .runtime import EneaRcemRuntime

_LOGGER = logging.getLogger(__name__)

SERVICE_REPAIR_20260825 = "repair_20260825_transition"
_REPAIR_ID = "2026-08-25-transition-v1"
_REPAIR_MONTH = "2026-08"
_TOLERANCE = 1e-9

# The cumulative sensor states are deliberately NOT changed here. They are
# monotonic baselines and changing them live would create an artificial Recorder
# jump (or, for export, could be interpreted as a total_increasing reset).
# Recorder sums and the current-month runtime buckets are the authoritative
# billing data that need correction.
_MONTHLY_IMPORT_DELTA = 0.60280798
_MONTHLY_EXPORT_DELTA = -0.04492275

# Target hourly Recorder changes after reconstructing the known transition from
# the physical cumulative meters and applying the configured post-balance
# corrections. Timestamps are UTC and follow the existing live sensor timing.
_IMPORT_TARGETS: tuple[tuple[str, float], ...] = (
    ("2026-08-25T01:00:00+00:00", 0.55555794),
    ("2026-08-25T02:00:00+00:00", 0.55555794),
    ("2026-08-25T03:00:00+00:00", 0.47325306),
    ("2026-08-25T04:00:00+00:00", 0.40123629),
    ("2026-08-25T05:00:00+00:00", 0.25720275),
)

_EXPORT_TARGETS: tuple[tuple[str, float], ...] = (
    ("2026-08-25T06:00:00+00:00", 0.68860689),
    ("2026-08-25T07:00:00+00:00", 2.09576010),
    ("2026-08-25T08:00:00+00:00", 3.11370072),
    ("2026-08-25T09:00:00+00:00", 3.51289312),
    ("2026-08-25T10:00:00+00:00", 3.93204514),
    ("2026-08-25T11:00:00+00:00", 1.90614371),
    ("2026-08-25T12:00:00+00:00", 0.38921259),
    ("2026-08-25T13:00:00+00:00", 1.87620428),
    ("2026-08-25T14:00:00+00:00", 2.79434680),
    ("2026-08-25T15:00:00+00:00", 1.89616390),
)

_COST_TARGETS: tuple[tuple[str, float], ...] = (
    ("2026-08-25T01:00:00+00:00", 0.6053805046438516),
    ("2026-08-25T02:00:00+00:00", 0.6053805046438516),
    ("2026-08-25T03:00:00+00:00", 0.5267209077790516),
    ("2026-08-25T04:00:00+00:00", 0.4578937605223515),
    ("2026-08-25T05:00:00+00:00", 0.3202394660089516),
)


def register_repair_service(hass: HomeAssistant) -> None:
    """Register the guarded one-shot repair service once."""
    if hass.services.has_service(DOMAIN, SERVICE_REPAIR_20260825):
        return
    hass.services.async_register(
        DOMAIN,
        SERVICE_REPAIR_20260825,
        _async_handle_repair_20260825,
    )


async def _async_handle_repair_20260825(call: ServiceCall) -> None:
    """Apply or resume the guarded transition repair."""
    hass = call.hass
    entries = hass.config_entries.async_entries(DOMAIN)
    runtimes = [
        entry.runtime_data
        for entry in entries
        if isinstance(getattr(entry, "runtime_data", None), EneaRcemRuntime)
    ]
    if len(runtimes) != 1:
        raise HomeAssistantError(
            f"Expected exactly one loaded {DOMAIN} runtime, found {len(runtimes)}"
        )

    runtime = runtimes[0]
    _validate_expected_runtime(runtime)

    registry = er.async_get(hass)
    import_stat_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{runtime.entry.entry_id}_balanced_import"
    )
    export_stat_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{runtime.entry.entry_id}_balanced_export"
    )
    cost_stat_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{runtime.entry.entry_id}_import_cost"
    )
    if not import_stat_id or not export_stat_id or not cost_stat_id:
        raise HomeAssistantError(
            "Cannot resolve Enea RCEm Recorder statistic IDs for the repair"
        )

    repair_markers = dict(runtime._data.get("repair_markers", {}))
    marker = dict(repair_markers.get(_REPAIR_ID, {}))

    # Apply the current-month runtime correction atomically with its marker.
    # Re-running the service after a partial Recorder repair will not apply these
    # monthly deltas twice.
    if not marker.get("monthly_runtime_applied"):
        monthly_import = dict(runtime._data.get("monthly_import", {}))
        monthly_export = dict(runtime._data.get("monthly_export", {}))
        monthly_import[_REPAIR_MONTH] = (
            float(monthly_import.get(_REPAIR_MONTH, 0.0)) + _MONTHLY_IMPORT_DELTA
        )
        monthly_export[_REPAIR_MONTH] = (
            float(monthly_export.get(_REPAIR_MONTH, 0.0)) + _MONTHLY_EXPORT_DELTA
        )
        runtime._data["monthly_import"] = monthly_import
        runtime._data["monthly_export"] = monthly_export

        marker["monthly_runtime_applied"] = True
        marker["monthly_import_delta_kwh"] = _MONTHLY_IMPORT_DELTA
        marker["monthly_export_delta_kwh"] = _MONTHLY_EXPORT_DELTA
        marker["applied_at"] = datetime.now(UTC).isoformat()
        repair_markers[_REPAIR_ID] = marker
        runtime._data["repair_markers"] = repair_markers
        runtime._recalculate_compensation()
        await runtime._store.async_save(runtime._serialize())
        runtime._notify()

    instance = get_instance(hass)
    stat_ids = {import_stat_id, export_stat_id, cost_stat_id}
    rows = await instance.async_add_executor_job(
        recorder_statistics.statistics_during_period,
        hass,
        datetime(2026, 8, 25, 1, 0, tzinfo=UTC),
        datetime(2026, 8, 25, 16, 0, tzinfo=UTC),
        stat_ids,
        "hour",
        {"energy": UnitOfEnergy.KILO_WATT_HOUR},
        {"change", "sum"},
    )

    current = {
        stat_id: {
            datetime.fromtimestamp(float(row["start"]), UTC).isoformat(): float(
                row.get("change", 0.0) or 0.0
            )
            for row in stat_rows
        }
        for stat_id, stat_rows in rows.items()
    }

    adjustments: list[dict[str, Any]] = []
    _queue_remaining_adjustments(
        instance,
        import_stat_id,
        current.get(import_stat_id, {}),
        _IMPORT_TARGETS,
        UnitOfEnergy.KILO_WATT_HOUR,
        adjustments,
    )
    _queue_remaining_adjustments(
        instance,
        export_stat_id,
        current.get(export_stat_id, {}),
        _EXPORT_TARGETS,
        UnitOfEnergy.KILO_WATT_HOUR,
        adjustments,
    )
    _queue_remaining_adjustments(
        instance,
        cost_stat_id,
        current.get(cost_stat_id, {}),
        _COST_TARGETS,
        "PLN",
        adjustments,
    )

    marker = dict(runtime._data.get("repair_markers", {}).get(_REPAIR_ID, {}))
    marker["statistics_last_queued_at"] = datetime.now(UTC).isoformat()
    marker["statistics_adjustments_queued"] = adjustments
    repair_markers = dict(runtime._data.get("repair_markers", {}))
    repair_markers[_REPAIR_ID] = marker
    runtime._data["repair_markers"] = repair_markers
    await runtime._store.async_save(runtime._serialize())

    _LOGGER.warning(
        "Enea RCEm %s repair queued %d Recorder adjustments; cumulative sensor "
        "baselines were intentionally left unchanged",
        _REPAIR_ID,
        len(adjustments),
    )


def _validate_expected_runtime(runtime: EneaRcemRuntime) -> None:
    """Refuse to run if this is not the diagnosed configuration."""
    if runtime.import_entity != "sensor.miernik_energii_elektrycznej_energy":
        raise HomeAssistantError("Unexpected physical import source; repair refused")
    if runtime.export_entity != "sensor.miernik_energii_elektrycznej_produced_energy":
        raise HomeAssistantError("Unexpected physical export source; repair refused")
    if abs(runtime.import_correction_percent - 2.8811) > 1e-6:
        raise HomeAssistantError("Unexpected import correction; repair refused")
    if abs(runtime.export_correction_percent - (-0.2019)) > 1e-6:
        raise HomeAssistantError("Unexpected export correction; repair refused")


def _queue_remaining_adjustments(
    instance,
    statistic_id: str,
    current_changes: dict[str, float],
    targets: tuple[tuple[str, float], ...],
    unit: str,
    queued: list[dict[str, Any]],
) -> None:
    """Queue only the difference still required for each target hour."""
    for start_iso, target in targets:
        start = datetime.fromisoformat(start_iso)
        current = float(current_changes.get(start.isoformat(), 0.0))
        adjustment = target - current
        if abs(adjustment) <= _TOLERANCE:
            continue
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
