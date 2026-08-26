"""One-shot repair of malformed 5-minute Recorder states from the 2026-08-25 stitch."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import logging
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder import statistics as recorder_statistics
from homeassistant.components.recorder.db_schema import StatisticsShortTerm
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .runtime import EneaRcemRuntime
from .statistics_alignment import StatisticsAligner

_LOGGER = logging.getLogger(__name__)

SERVICE_REPAIR_20260825_SHORTTERM_STATE = "repair_20260825_shortterm_state"
_REPAIR_ID = "2026-08-25-shortterm-state-v1"
_START_UTC = datetime(2026, 8, 25, 0, 0, tzinfo=UTC)
_END_UTC = datetime(2026, 8, 25, 20, 0, tzinfo=UTC)
_LOW_STATE_LIMIT = 100.0
_RECORDER_ENERGY_UNITS = {"energy": UnitOfEnergy.KILO_WATT_HOUR}

_IMPORT_BASELINE_KWH = 7092.736203209972
_EXPORT_BASELINE_KWH = 11174.892247500058
_COST_BASELINE_PLN = 7930.756001666447


def register_shortterm_repair_service(hass: HomeAssistant) -> None:
    """Register a uniquely named guarded 5-minute state repair service."""
    if hass.services.has_service(DOMAIN, SERVICE_REPAIR_20260825_SHORTTERM_STATE):
        return

    async def _repair(call: ServiceCall) -> None:
        await _async_repair_shortterm_state(hass, call)

    hass.services.async_register(
        DOMAIN,
        SERVICE_REPAIR_20260825_SHORTTERM_STATE,
        _repair,
    )


async def _async_repair_shortterm_state(
    hass: HomeAssistant, _call: ServiceCall
) -> None:
    """Offset only malformed 5-minute state fields and preserve sums exactly."""
    runtime = _single_runtime(hass)
    _validate_expected_runtime(runtime)

    aligner = getattr(runtime, "statistics_aligner", None)
    if not isinstance(aligner, StatisticsAligner):
        raise HomeAssistantError("Hourly statistics aligner is not loaded; repair refused")
    if not all((aligner.import_stat_id, aligner.export_stat_id, aligner.cost_stat_id)):
        raise HomeAssistantError("Statistics IDs are unresolved; repair refused")

    baselines = {
        aligner.import_stat_id: (_IMPORT_BASELINE_KWH, "kWh"),
        aligner.export_stat_id: (_EXPORT_BASELINE_KWH, "kWh"),
        aligner.cost_stat_id: (_COST_BASELINE_PLN, "PLN"),
    }
    stat_ids = set(baselines)

    metadata_rows = await recorder_statistics.async_list_statistic_ids(hass, stat_ids)
    metadata_by_id = {row["statistic_id"]: row for row in metadata_rows}
    if set(metadata_by_id) != stat_ids:
        raise HomeAssistantError("Statistics metadata incomplete; repair refused")

    import_metadata: dict[str, dict[str, Any]] = {}
    for statistic_id, (_baseline, expected_unit) in baselines.items():
        info = metadata_by_id[statistic_id]
        actual_unit = info.get("statistics_unit_of_measurement")
        if actual_unit != expected_unit:
            raise HomeAssistantError(
                f"Unexpected statistics unit for {statistic_id}: {actual_unit!r}; repair refused"
            )
        if not info.get("has_sum"):
            raise HomeAssistantError(f"{statistic_id} is not a sum statistic; repair refused")
        import_metadata[statistic_id] = {
            "statistic_id": statistic_id,
            "source": info["source"],
            "name": info.get("name"),
            "unit_of_measurement": actual_unit,
            "unit_class": info.get("unit_class"),
            "has_mean": info.get("has_mean", False),
            "mean_type": info["mean_type"],
            "has_sum": True,
        }

    rows = await _read_short_rows(hass, stat_ids)
    plans: dict[str, list[dict[str, Any]]] = {}
    backups: list[dict[str, Any]] = []

    for statistic_id, (baseline, _unit) in baselines.items():
        stat_rows = rows.get(statistic_id, [])
        if not stat_rows:
            raise HomeAssistantError(
                f"No 5-minute statistics found for {statistic_id}; repair refused"
            )

        repaired: list[dict[str, Any]] = []
        for row in stat_rows:
            state = row.get("state")
            if state is None:
                raise HomeAssistantError(
                    f"5-minute state is NULL for {statistic_id} at {row.get('start')}; repair refused"
                )
            old_state = float(state)

            # Already cumulative: leave it untouched.
            if old_state >= baseline - _LOW_STATE_LIMIT:
                continue

            # In this incident window the broken relative state is always below 100.
            if abs(old_state) >= _LOW_STATE_LIMIT:
                raise HomeAssistantError(
                    f"Unexpected intermediate state for {statistic_id} at {row.get('start')}: "
                    f"{old_state}; repair refused"
                )

            new_state = old_state + baseline
            stat = _copy_stat_with_state(row, new_state)
            repaired.append(stat)
            backups.append(
                {
                    "statistic_id": statistic_id,
                    "start": stat["start"].isoformat(),
                    "old_state": old_state,
                    "new_state": new_state,
                    "sum": row.get("sum"),
                }
            )

        plans[statistic_id] = repaired

    if not any(plans.values()):
        _LOGGER.warning("Enea RCEm 5-minute state repair already applied; nothing to do")
        return

    repair_markers = dict(runtime._data.get("repair_markers", {}))
    marker = {
        "planned_at": datetime.now(UTC).isoformat(),
        "start_utc": _START_UTC.isoformat(),
        "end_utc_exclusive": _END_UTC.isoformat(),
        "rows_planned": sum(len(v) for v in plans.values()),
        "rows": backups,
        "sum_change_fields_modified": False,
    }
    repair_markers[_REPAIR_ID] = marker
    runtime._data["repair_markers"] = repair_markers
    await runtime._store.async_save(runtime._serialize())

    instance = get_instance(hass)
    for statistic_id, stats in plans.items():
        if stats:
            instance.async_import_statistics(
                import_metadata[statistic_id],
                stats,
                StatisticsShortTerm,
            )

    verified = False
    for _ in range(80):
        await asyncio.sleep(0.25)
        check = await _read_short_rows(hass, stat_ids)
        if _verify_short(check, baselines):
            verified = True
            break

    marker = dict(runtime._data.get("repair_markers", {}).get(_REPAIR_ID, {}))
    marker["queued_at"] = datetime.now(UTC).isoformat()
    marker["verified"] = verified
    marker["verified_at"] = datetime.now(UTC).isoformat() if verified else None
    repair_markers = dict(runtime._data.get("repair_markers", {}))
    repair_markers[_REPAIR_ID] = marker
    runtime._data["repair_markers"] = repair_markers
    await runtime._store.async_save(runtime._serialize())

    if not verified:
        raise HomeAssistantError(
            "5-minute state repair was queued but did not verify within 20 seconds; "
            "do not rerun blindly"
        )

    _LOGGER.warning(
        "Enea RCEm 5-minute state repair verified: corrected %d state rows; sums preserved",
        sum(len(v) for v in plans.values()),
    )


async def _read_short_rows(
    hass: HomeAssistant, stat_ids: set[str]
) -> dict[str, list[dict[str, Any]]]:
    instance = get_instance(hass)
    return await instance.async_add_executor_job(
        recorder_statistics.statistics_during_period,
        hass,
        _START_UTC,
        _END_UTC,
        stat_ids,
        "5minute",
        _RECORDER_ENERGY_UNITS,
        {"change", "last_reset", "max", "mean", "min", "state", "sum"},
    )


def _verify_short(
    rows: dict[str, list[dict[str, Any]]],
    baselines: dict[str, tuple[float, str]],
) -> bool:
    for statistic_id, (baseline, _unit) in baselines.items():
        stat_rows = rows.get(statistic_id, [])
        if not stat_rows:
            return False
        for row in stat_rows:
            state = row.get("state")
            if state is None or float(state) < baseline - _LOW_STATE_LIMIT:
                return False
    return True


def _copy_stat_with_state(row: dict[str, Any], state: float) -> dict[str, Any]:
    stat: dict[str, Any] = {
        "start": _as_datetime(row["start"]),
        "state": state,
        "sum": row.get("sum"),
    }
    if "last_reset" in row:
        stat["last_reset"] = _as_datetime_or_none(row.get("last_reset"))
    for key in ("mean", "min", "max"):
        if key in row:
            stat[key] = row.get(key)
    return stat


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
    if runtime.import_entity != "sensor.miernik_energii_elektrycznej_energy":
        raise HomeAssistantError("Unexpected physical import source; repair refused")
    if runtime.export_entity != "sensor.miernik_energii_elektrycznej_produced_energy":
        raise HomeAssistantError("Unexpected physical export source; repair refused")
    if abs(runtime.import_correction_percent - 2.8811) > 1e-6:
        raise HomeAssistantError("Unexpected import correction; repair refused")
    if abs(runtime.export_correction_percent - (-0.2019)) > 1e-6:
        raise HomeAssistantError("Unexpected export correction; repair refused")


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return datetime.fromtimestamp(float(value), UTC)


def _as_datetime_or_none(value: Any) -> datetime | None:
    if value is None:
        return None
    return _as_datetime(value)
