"""One-shot repair of malformed hourly LTS states from the 2026-08-25 stitch."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import logging
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder import statistics as recorder_statistics
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .runtime import EneaRcemRuntime
from .statistics_alignment import StatisticsAligner

_LOGGER = logging.getLogger(__name__)

SERVICE_REPAIR_20260825_LTS_STATE = "repair_20260825_lts_state"
_REPAIR_ID = "2026-08-25-lts-state-v1"
_START_UTC = datetime(2026, 8, 25, 0, 0, tzinfo=UTC)
_POST_STITCH_HOUR_UTC = datetime(2026, 8, 25, 19, 0, tzinfo=UTC)
_END_UTC = datetime(2026, 8, 25, 20, 0, tzinfo=UTC)
_LOW_STATE_LIMIT = 100.0
_CONTINUITY_TOLERANCE = 0.01
_RECORDER_ENERGY_UNITS = {"energy": UnitOfEnergy.KILO_WATT_HOUR}

# Exact cumulative baselines immediately before the broken live-relative states.
# These were already validated against the historical backfill before the stitch.
_IMPORT_BASELINE_KWH = 7092.736203209972
_EXPORT_BASELINE_KWH = 11174.892247500058
_COST_BASELINE_PLN = 7930.756001666447


def register_state_repair_service(hass: HomeAssistant) -> None:
    """Register the guarded one-shot LTS state repair service."""
    if hass.services.has_service(DOMAIN, SERVICE_REPAIR_20260825_LTS_STATE):
        return

    async def _repair(call: ServiceCall) -> None:
        await _async_repair_lts_state(hass, call)

    hass.services.async_register(DOMAIN, SERVICE_REPAIR_20260825_LTS_STATE, _repair)


async def _async_repair_lts_state(
    hass: HomeAssistant, _call: ServiceCall
) -> None:
    """Offset only malformed hourly state fields; preserve sum/change exactly."""
    runtime = _single_runtime(hass)
    _validate_expected_runtime(runtime)

    aligner = getattr(runtime, "statistics_aligner", None)
    if not isinstance(aligner, StatisticsAligner):
        raise HomeAssistantError("Hourly statistics aligner is not loaded; state repair refused")
    if not all((aligner.import_stat_id, aligner.export_stat_id, aligner.cost_stat_id)):
        raise HomeAssistantError("Hourly statistics IDs are unresolved; state repair refused")

    baselines = {
        aligner.import_stat_id: (_IMPORT_BASELINE_KWH, "kWh"),
        aligner.export_stat_id: (_EXPORT_BASELINE_KWH, "kWh"),
        aligner.cost_stat_id: (_COST_BASELINE_PLN, "PLN"),
    }
    stat_ids = set(baselines)

    metadata_rows = await recorder_statistics.async_list_statistic_ids(hass, stat_ids)
    metadata_by_id = {row["statistic_id"]: row for row in metadata_rows}
    if set(metadata_by_id) != stat_ids:
        raise HomeAssistantError(
            f"Statistics metadata incomplete; expected {sorted(stat_ids)}, got {sorted(metadata_by_id)}"
        )

    import_metadata: dict[str, dict[str, Any]] = {}
    for statistic_id, (_baseline, expected_unit) in baselines.items():
        info = metadata_by_id[statistic_id]
        actual_unit = info.get("statistics_unit_of_measurement")
        if actual_unit != expected_unit:
            raise HomeAssistantError(
                f"Unexpected statistics unit for {statistic_id}: {actual_unit!r}, "
                f"expected {expected_unit!r}; state repair refused"
            )
        if not info.get("has_sum"):
            raise HomeAssistantError(
                f"{statistic_id} is not a sum statistic; state repair refused"
            )
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

    rows = await _read_rows(hass, stat_ids)
    plans: dict[str, list[dict[str, Any]]] = {}
    backup_rows: list[dict[str, Any]] = []

    expected_pre_stamps = [
        int(_START_UTC.timestamp()) + hour * 3600 for hour in range(19)
    ]
    post_stamp = int(_POST_STITCH_HOUR_UTC.timestamp())

    for statistic_id, (baseline, _unit) in baselines.items():
        by_start = {
            int(round(_timestamp(row["start"]))): row
            for row in rows.get(statistic_id, [])
        }
        missing = [stamp for stamp in expected_pre_stamps + [post_stamp] if stamp not in by_start]
        if missing:
            missing_iso = [datetime.fromtimestamp(stamp, UTC).isoformat() for stamp in missing]
            raise HomeAssistantError(
                f"Hourly LTS rows missing for {statistic_id}: {missing_iso}; state repair refused"
            )

        pre_rows = [by_start[stamp] for stamp in expected_pre_stamps]
        post_row = by_start[post_stamp]
        pre_states = [row.get("state") for row in pre_rows]
        post_state = post_row.get("state")

        # Idempotent success: every affected state is already cumulative.
        if all(state is not None and float(state) >= _LOW_STATE_LIMIT for state in pre_states):
            plans[statistic_id] = []
            continue

        if any(state is None or abs(float(state)) >= _LOW_STATE_LIMIT for state in pre_states):
            raise HomeAssistantError(
                f"Unexpected mixed pre-stitch states for {statistic_id}; state repair refused"
            )
        if post_state is None or float(post_state) < _LOW_STATE_LIMIT:
            raise HomeAssistantError(
                f"Post-stitch state is not cumulative for {statistic_id}; state repair refused"
            )

        corrected_last_pre = float(pre_states[-1]) + baseline
        post_change = float(post_row.get("change", 0.0) or 0.0)
        expected_post = corrected_last_pre + post_change
        if abs(float(post_state) - expected_post) > _CONTINUITY_TOLERANCE:
            raise HomeAssistantError(
                f"Continuity proof failed for {statistic_id}: corrected 18:00="
                f"{corrected_last_pre:.9f}, 19:00 change={post_change:.9f}, "
                f"19:00 state={float(post_state):.9f}; state repair refused"
            )

        repaired: list[dict[str, Any]] = []
        for row in pre_rows:
            old_state = float(row["state"])
            new_state = old_state + baseline
            stat = {
                "start": _as_datetime(row["start"]),
                "state": new_state,
                "sum": row.get("sum"),
            }
            if "last_reset" in row:
                stat["last_reset"] = _as_datetime_or_none(row.get("last_reset"))
            for key in ("mean", "min", "max"):
                if key in row:
                    stat[key] = row.get(key)
            repaired.append(stat)
            backup_rows.append(
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
        _LOGGER.warning("Enea RCEm LTS state repair already applied; nothing to do")
        return

    repair_markers = dict(runtime._data.get("repair_markers", {}))
    marker = {
        "planned_at": datetime.now(UTC).isoformat(),
        "start_utc": _START_UTC.isoformat(),
        "end_utc_exclusive": _POST_STITCH_HOUR_UTC.isoformat(),
        "import_baseline_kwh": _IMPORT_BASELINE_KWH,
        "export_baseline_kwh": _EXPORT_BASELINE_KWH,
        "cost_baseline_pln": _COST_BASELINE_PLN,
        "rows": backup_rows,
        "sum_change_fields_modified": False,
    }
    repair_markers[_REPAIR_ID] = marker
    runtime._data["repair_markers"] = repair_markers
    await runtime._store.async_save(runtime._serialize())

    for statistic_id, stats in plans.items():
        if stats:
            recorder_statistics.async_import_statistics(
                hass,
                import_metadata[statistic_id],
                stats,
            )

    # Recorder imports are queued. Poll the public statistics read path and only
    # report success once the state fields are actually visible as cumulative.
    verified = False
    for _ in range(40):
        await asyncio.sleep(0.25)
        check = await _read_rows(hass, stat_ids)
        if _verify_applied(check, baselines, expected_pre_stamps):
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
            "LTS state repair was queued but did not verify within 10 seconds; "
            "do not rerun blindly"
        )

    _LOGGER.warning(
        "Enea RCEm LTS state repair verified: corrected %d hourly state rows; "
        "sum/change were preserved",
        len(backup_rows),
    )


async def _read_rows(
    hass: HomeAssistant, stat_ids: set[str]
) -> dict[str, list[dict[str, Any]]]:
    instance = get_instance(hass)
    return await instance.async_add_executor_job(
        recorder_statistics.statistics_during_period,
        hass,
        _START_UTC,
        _END_UTC,
        stat_ids,
        "hour",
        _RECORDER_ENERGY_UNITS,
        {"change", "last_reset", "max", "mean", "min", "state", "sum"},
    )


def _verify_applied(
    rows: dict[str, list[dict[str, Any]]],
    baselines: dict[str, tuple[float, str]],
    expected_pre_stamps: list[int],
) -> bool:
    for statistic_id, (baseline, _unit) in baselines.items():
        by_start = {
            int(round(_timestamp(row["start"]))): row
            for row in rows.get(statistic_id, [])
        }
        for stamp in expected_pre_stamps:
            row = by_start.get(stamp)
            if row is None or row.get("state") is None:
                return False
            if float(row["state"]) < baseline - _LOW_STATE_LIMIT:
                return False
    return True


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
        raise HomeAssistantError("Unexpected physical import source; state repair refused")
    if runtime.export_entity != "sensor.miernik_energii_elektrycznej_produced_energy":
        raise HomeAssistantError("Unexpected physical export source; state repair refused")
    if abs(runtime.import_correction_percent - 2.8811) > 1e-6:
        raise HomeAssistantError("Unexpected import correction; state repair refused")
    if abs(runtime.export_correction_percent - (-0.2019)) > 1e-6:
        raise HomeAssistantError("Unexpected export correction; state repair refused")


def _timestamp(value: Any) -> float:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    raise ValueError(f"Invalid statistics timestamp: {value!r}")


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return datetime.fromtimestamp(float(value), UTC)


def _as_datetime_or_none(value: Any) -> datetime | None:
    if value is None:
        return None
    return _as_datetime(value)
