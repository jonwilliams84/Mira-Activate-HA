"""Mira Activate Shower — BLE integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import MiraActivateCoordinator

_LOGGER = logging.getLogger(__name__)

DOMAIN = "mira_activate"
PLATFORMS: list[Platform] = [
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Mira Activate from a config entry."""
    address: str = entry.data["address"]
    _LOGGER.debug("Setting up Mira Activate at %s", address)

    coordinator = MiraActivateCoordinator(hass, entry)
    await coordinator.async_init()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coord: MiraActivateCoordinator | None = hass.data[DOMAIN].pop(
            entry.entry_id, None
        )
        if coord is not None:
            await coord.async_close()
    return unload_ok
