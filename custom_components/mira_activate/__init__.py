"""Mira Activate Shower — BLE integration."""

from __future__ import annotations

import logging
import re

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .coordinator import MiraActivateCoordinator

_LOGGER = logging.getLogger(__name__)

DOMAIN = "mira_activate"
PLATFORMS: list[Platform] = [
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
]

# Entity unique_ids used to be prefixed with the BLE address, e.g.
# "E5:B6:22:B1:DF:6D_outlet1_on". The Activate regenerates that address on
# power-cycle, so the prefix is unstable. This matches "<MAC>_<suffix>".
_ADDR_UID_RE = re.compile(r"^(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}_(?P<suffix>.+)$")


def _migrate_identity(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Heal entries created before identity was keyed on the stable name-id.

    Two legacy problems, both rooted in the BLE address being used as identity:

    1. The config entry has ``unique_id is None``, so the config-flow dedup
       (``_abort_if_unique_id_configured``) can't match this unit — every
       address rotation re-offers the shower as a brand-new device.
    2. Entity unique_ids embed the (rotating) BLE address, so a rotation can
       spawn duplicate entities.

    Both are fixed by re-keying on the name-id stored in ``entry.data``.
    """
    device_id = entry.data.get("device_id")
    if not device_id:
        # No name-id was ever parsed for this unit — nothing stable to key on.
        return

    if entry.unique_id is None:
        hass.config_entries.async_update_entry(entry, unique_id=device_id)
        _LOGGER.info(
            "Migrated config entry %s to stable unique_id %s", entry.title, device_id
        )

    ent_reg = er.async_get(hass)
    for reg_entry in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        match = _ADDR_UID_RE.match(reg_entry.unique_id)
        if not match:
            continue
        new_uid = f"{device_id}_{match.group('suffix')}"
        if new_uid == reg_entry.unique_id:
            continue
        if ent_reg.async_get_entity_id(reg_entry.domain, DOMAIN, new_uid):
            _LOGGER.warning(
                "Cannot migrate %s -> %s: target unique_id already exists",
                reg_entry.unique_id,
                new_uid,
            )
            continue
        ent_reg.async_update_entity(reg_entry.entity_id, new_unique_id=new_uid)
        _LOGGER.info(
            "Migrated entity %s unique_id %s -> %s",
            reg_entry.entity_id,
            reg_entry.unique_id,
            new_uid,
        )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Mira Activate from a config entry."""
    address: str = entry.data["address"]
    _LOGGER.debug("Setting up Mira Activate at %s", address)

    _migrate_identity(hass, entry)

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
