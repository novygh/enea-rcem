"""Guarded repair helpers for the 2026-08-25 live-transition incident."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import logging
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder import statistics as recorder_statistics
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .runtime import EneaRcemRuntime
from .statistics_alignment import StatisticsAligner

_LOGGER = logging.getLogger(__name__)

SERVICE_REPAIR_20260825_V2 = "repair_20260825_transition_v2"
SERVICE_CLEANUP_RAW_HISTORY = "cleanup_20260825_raw_history"
_REPAIR_ID = "2026-08-25-transition-v2"
_REPAIR_MONTH = "2026-08"
_BROKEN_START_UTC = datetime(2026, 8, 25, 0, 0, tzinfo=UTC)
_RECORDER_ENERGY_UNITS = {"energy": UnitOfEnergy.KILO_WATT_HOUR}
_MAX_TARGET_HOURS = 168


def register_repair_service(hass: HomeAssistant) -> None:
    """Register guarded repair and raw-history cleanup services."""
    if not hass.services.has_service(DOMAIN, SERVICE_REPAIR_20260825_V2):

        async def _repair(call: ServiceCall) -> None:
            await _async_handle_repair_20260825_v2(hass, call)

        hass.services.async_register(DOMAIN, SERVICE_REPAIR_20260825_V2, _repair)

    if not hass.services.has_service(DOMAIN, SERVICE_CLEANUP_RAW_HISTORY):

        async def _cleanup(call: ServiceCall) -> None:
            await _async_cleanup_raw_history(hass, call)

        hass.services.async_register(DOMAIN, SERVICE_CLEANUP_RAW_HISTORY, _cleanup)


async def _async_handle_repair_20260825_v2(
    hass: HomeAssistant, _call: ServiceCall
) -> None:
    """Rebuild only the affected transition interval from physical source LTS."""
    runtime = _single_runtime(hass)
    _validate_expected_runtime(runtime)

    aligner = getattr(runtime, "statistics_aligner", None)
    if not isinstance(aligner, StatisticsAligner):
        raise HomeAssistantError("Hourly statistics aligner is not loaded; repair refused")
    if not all((aligner.import_stat_id, aligner.export_stat_id, aligner.cost_stat_id)):
        raise HomeAssistantError("Hourly statistics IDs are unresolved; repair refused")

    tz = dt_util.get_time_zone(hass.config.time_zone)
    month_start_local = datetime(2026, 8, 1, tzinfo=tz)
    month_start_utc = month_start_local.astimezone(UTC)
    end_utc = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    if end_utc <= _BROKEN_START_UTC:
        raise HomeAssistantError("No completed transition hours are available to repair")

    instance = get_instance(hass)

    # Everything before the live-transition boundary already belongs to the
    # validated/backfilled Enea statistics. Do not require physical LTS to be
    # complete there: brief source-LTS holes before 25 August are unrelated to
    # this incident and must not block the repair.
    pre_rows = await instance.async_add_executor_job(
        recorder_statistics.statistics_during_period,
        hass,
        month_start_utc,
        _BROKEN_START_UTC,
        {aligner.import_stat_id, aligner.export_stat_id, aligner.cost_stat_id},
        "hour",
        _RECORDER_ENERGY_UNITS,
        {"change", "sum"},
    )
    pre_import = _sum_changes(pre_rows.get(aligner.import_stat_id, []))
    pre_export = _sum_changes(pre_rows.get(aligner.export_stat_id, []))
    pre_cost = _sum_changes(pre_rows.get(aligner.cost_stat_id, []))

    # Reconstruct only the broken/live interval from the physical cumulative
    # source statistics. Here completeness is mandatory: no guessing.
    source_rows = await instance.async_add_executor_job(
        recorder_statistics.statistics_during_period,
        hass,
        _BROKEN_START_UTC,
        end_utc,
        {runtime.import_entity, runtime.export_entity},
        "hour",
        _RECORDER_ENERGY_UNITS,
        {"change", "sum"},
    )

    import_changes = _changes_by_start(source_rows.get(runtime.import_entity, []))
    export_changes = _changes_by_start(source_rows.get(runtime.export_entity, []))
    expected_hours = _hour_starts(_BROKEN_START_UTC, end_utc)
    missing = [
        start.isoformat()
        for start in expected_hours
        if int(start.timestamp()) not in import_changes
        or int(start.timestamp()) not in export_changes
    ]
    if missing:
        preview = ", ".join(missing[:6])
        raise HomeAssistantError(
            f"Physical source LTS is incomplete inside the affected interval for "
            f"{len(missing)} hours ({preview}); repair refused without guessing"
        )

    repair_targets: dict[str, dict[str, float]] = {}
    repaired_import = 0.0
    repaired_export = 0.0
    repaired_cost = 0.0

    for start in expected_hours:
        stamp = int(start.timestamp())
        imp = max(import_changes[stamp], 0.0)
        exp = max(export_changes[stamp], 0.0)
        raw_balanced_import = max(imp - exp, 0.0)
        raw_balanced_export = max(exp - imp, 0.0)
        balanced_import = raw_balanced_import * runtime.import_correction_multiplier
        balanced_export = raw_balanced_export * runtime.export_correction_multiplier

        local_start = start.astimezone(tz)
        hour_key = runtime._hour_key(local_start)
        fixed_hour = runtime.fixed_monthly_gross / runtime._hours_in_month(hour_key)
        import_cost = fixed_hour + balanced_import * runtime.variable_rate_gross

        repair_targets[hour_key] = {
            "import_kwh": balanced_import,
            "export_kwh": balanced_export,
            "import_cost_pln": import_cost,
        }
        repaired_import += balanced_import
        repaired_export += balanced_export
        repaired_cost += import_cost

    if not repair_targets:
        raise HomeAssistantError("No reconstructed transition targets; repair refused")

    month_import = pre_import + repaired_import
    month_export = pre_export + repaired_export
    month_cost = pre_cost + repaired_cost

    monthly_import = dict(runtime._data.get("monthly_import", {}))
    monthly_export = dict(runtime._data.get("monthly_export", {}))
    old_month_import = float(monthly_import.get(_REPAIR_MONTH, 0.0))
    old_month_export = float(monthly_export.get(_REPAIR_MONTH, 0.0))
    monthly_import[_REPAIR_MONTH] = month_import
    monthly_export[_REPAIR_MONTH] = month_export
    runtime._data["monthly_import"] = monthly_import
    runtime._data["monthly_export"] = monthly_export

    stored_targets = dict(runtime._data.get("statistics_targets", {}))
    stored_targets.update(repair_targets)
    if len(stored_targets) > _MAX_TARGET_HOURS:
        for old_key in sorted(stored_targets)[: len(stored_targets) - _MAX_TARGET_HOURS]:
            stored_targets.pop(old_key, None)
    runtime._data["statistics_targets"] = stored_targets

    repair_markers = dict(runtime._data.get("repair_markers", {}))
    marker = {
        "reconstructed_at": datetime.now(UTC).isoformat(),
        "month": _REPAIR_MONTH,
        "month_start_utc": month_start_utc.isoformat(),
        "repair_start_utc": _BROKEN_START_UTC.isoformat(),
        "repair_end_utc_exclusive": end_utc.isoformat(),
        "pre_transition_import_kwh": pre_import,
        "pre_transition_export_kwh": pre_export,
        "pre_transition_import_cost_pln": pre_cost,
        "repair_hours": len(repair_targets),
        "repaired_interval_import_kwh": repaired_import,
        "repaired_interval_export_kwh": repaired_export,
        "repaired_interval_import_cost_pln": repaired_cost,
        "monthly_import_before_kwh": old_month_import,
        "monthly_import_after_kwh": month_import,
        "monthly_export_before_kwh": old_month_export,
        "monthly_export_after_kwh": month_export,
        "reconstructed_import_cost_pln": month_cost,
        "cumulative_sensor_baselines_changed": False,
        "raw_history_cleanup_queued": False,
    }
    repair_markers[_REPAIR_ID] = marker
    runtime._data["repair_markers"] = repair_markers
    runtime._recalculate_compensation()
    await runtime._store.async_save(runtime._serialize())
    runtime._notify()

    adjustments = await aligner.async_reconcile(repair_targets)
    marker = dict(runtime._data.get("repair_markers", {}).get(_REPAIR_ID, {}))
    marker["statistics_repair_queued_at"] = datetime.now(UTC).isoformat()
    marker["statistics_adjustments_queued"] = len(adjustments)
    repair_markers = dict(runtime._data.get("repair_markers", {}))
    repair_markers[_REPAIR_ID] = marker
    runtime._data["repair_markers"] = repair_markers
    await runtime._store.async_save(runtime._serialize())

    _LOGGER.warning(
        "Enea RCEm transition v2 preserved pre-transition August statistics, "
        "reconstructed %d affected hours, rebuilt August to %.6f kWh import / "
        "%.6f kWh export and queued %d statistics adjustments",
        len(expected_hours),
        month_import,
        month_export,
        len(adjustments),
    )


async def _async_cleanup_raw_history(
    hass: HomeAssistant, _call: ServiceCall
) -> None:
    """Purge only raw stitched entity history after the v2 LTS repair is verified."""
    runtime = _single_runtime(hass)
    marker = runtime._data.get("repair_markers", {}).get(_REPAIR_ID, {})
    if not marker.get("statistics_repair_queued_at"):
        raise HomeAssistantError("Transition v2 statistics repair has not run; cleanup refused")

    registry = er.async_get(hass)
    entity_ids = [
        registry.async_get_entity_id(
            "sensor", DOMAIN, f"{runtime.entry.entry_id}_{key}"
        )
        for key in (
            "balanced_import",
            "balanced_export",
            "import_cost",
            "export_compensation",
        )
    ]
    resolved = [entity_id for entity_id in entity_ids if entity_id is not None]
    if len(resolved) != 4:
        raise HomeAssistantError(
            f"Expected four stitched Enea entities for raw-history cleanup, got {resolved}"
        )

    await hass.services.async_call(
        "recorder",
        "purge_entities",
        {"entity_id": resolved, "keep_days": 0},
        blocking=True,
    )

    marker = dict(runtime._data.get("repair_markers", {}).get(_REPAIR_ID, {}))
    marker["raw_history_cleanup_queued"] = True
    marker["raw_history_cleanup_queued_at"] = datetime.now(UTC).isoformat()
    marker["raw_history_entities"] = resolved
    repair_markers = dict(runtime._data.get("repair_markers", {}))
    repair_markers[_REPAIR_ID] = marker
    runtime._data["repair_markers"] = repair_markers
    await runtime._store.async_save(runtime._serialize())


def _single_runtime(hass: HomeAssistant) -> EneaRcemRuntime:
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
    return runtimes[0]


def _validate_expected_runtime(runtime: EneaRcemRuntime) -> None:
    """Refuse repair if this is not the diagnosed installation."""
    if runtime.import_entity != "sensor.miernik_energii_elektrycznej_energy":
        raise HomeAssistantError("Unexpected physical import source; repair refused")
    if runtime.export_entity != "sensor.miernik_energii_elektrycznej_produced_energy":
        raise HomeAssistantError("Unexpected physical export source; repair refused")
    if abs(runtime.import_correction_percent - 2.8811) > 1e-6:
        raise HomeAssistantError("Unexpected import correction; repair refused")
    if abs(runtime.export_correction_percent - (-0.2019)) > 1e-6:
        raise HomeAssistantError("Unexpected export correction; repair refused")
    if abs(runtime.variable_rate_gross - 0.955710) > 1e-6:
        raise HomeAssistantError("Unexpected current gross variable rate; repair refused")
    if abs(runtime.fixed_monthly_gross - 55.3746) > 1e-4:
        raise HomeAssistantError("Unexpected current gross fixed monthly rate; repair refused")


def _hour_starts(start: datetime, end: datetime) -> list[datetime]:
    result: list[datetime] = []
    current = start
    while current < end:
        result.append(current)
        current += timedelta(hours=1)
    return result


def _changes_by_start(rows: list[dict[str, Any]]) -> dict[int, float]:
    result: dict[int, float] = {}
    for row in rows:
        result[int(round(_timestamp(row["start"])))] = float(
            row.get("change", 0.0) or 0.0
        )
    return result


def _sum_changes(rows: list[dict[str, Any]]) -> float:
    return sum(float(row.get("change", 0.0) or 0.0) for row in rows)


def _timestamp(value: Any) -> float:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    raise ValueError(f"Invalid statistics timestamp: {value!r}")
