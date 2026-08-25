"""Enea RCEm integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .compensation import CompensationReconciler
from .const import PLATFORMS
from .deposit import DepositCoordinator
from .runtime import EneaRcemRuntime
from .settled_daily import SettledDailyCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Enea RCEm from a config entry."""
    runtime = EneaRcemRuntime(hass, entry)
    await runtime.async_setup()
    entry.runtime_data = runtime

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    reconciler = CompensationReconciler(hass, entry, runtime)
    deposit = DepositCoordinator(hass, entry, runtime)
    settled_daily = SettledDailyCoordinator(hass, entry, runtime)
    runtime.compensation_reconciler = reconciler
    runtime.deposit_coordinator = deposit
    runtime.settled_daily_coordinator = settled_daily

    await reconciler.async_setup()
    await deposit.async_setup()
    await settled_daily.async_setup()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        runtime: EneaRcemRuntime = entry.runtime_data

        settled_daily: SettledDailyCoordinator | None = getattr(
            runtime, "settled_daily_coordinator", None
        )
        if settled_daily is not None:
            await settled_daily.async_shutdown()

        deposit: DepositCoordinator | None = getattr(
            runtime, "deposit_coordinator", None
        )
        if deposit is not None:
            await deposit.async_shutdown()

        reconciler: CompensationReconciler | None = getattr(
            runtime, "compensation_reconciler", None
        )
        if reconciler is not None:
            await reconciler.async_shutdown()

        await runtime.async_shutdown()
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload after options are changed."""
    await hass.config_entries.async_reload(entry.entry_id)
