"""Mira Activate outlet switches.

Two outlets per the live HCI capture 2026-05-30:
  bit 0 (0x01) — outlet A (handheld on Jon's ensuite)
  bit 1 (0x02) — outlet B (rain head — verified)

Per-entity optimistic state (matches the mira_mode integration pattern):
the entity flips its own cached state immediately, calls
async_write_ha_state(), THEN awaits the BLE write. So the UI never blocks.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import MiraActivateCoordinator

_LOGGER = logging.getLogger(__name__)
DOMAIN = "mira_activate"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coord: MiraActivateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            RainHeadSwitch(coord),
            HandheldSwitch(coord),
        ]
    )


class _OutletBase(CoordinatorEntity[MiraActivateCoordinator], SwitchEntity):
    _attr_has_entity_name = True
    _attr_assumed_state = True
    _state_key: str = ""        # set on subclass
    _coord_setter: str = ""     # name of coordinator method to call

    def __init__(self, coord: MiraActivateCoordinator) -> None:
        super().__init__(coord)
        self._attr_unique_id = f"{coord.address}_{self._state_key}"
        self._attr_device_info = coord.device_info
        self._is_on: bool | None = self._read_state()

    def _read_state(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return bool(self.coordinator.data.get(self._state_key))

    @callback
    def _handle_coordinator_update(self) -> None:
        self._is_on = self._read_state()
        super()._handle_coordinator_update()

    @property
    def is_on(self) -> bool | None:
        return self._is_on

    @property
    def available(self) -> bool:
        return bool(
            self.coordinator.data and self.coordinator.data.get("available", False)
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._is_on = True
        self.async_write_ha_state()
        try:
            await getattr(self.coordinator, self._coord_setter)(True)
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Failed to turn on %s — will correct on next poll", self._state_key)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._is_on = False
        self.async_write_ha_state()
        try:
            await getattr(self.coordinator, self._coord_setter)(False)
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Failed to turn off %s — will correct on next poll", self._state_key)


class RainHeadSwitch(_OutletBase):
    _attr_name = "Rain Head"
    _attr_icon = "mdi:shower"
    _state_key = "outlet1_on"
    _coord_setter = "async_set_outlet1"


class HandheldSwitch(_OutletBase):
    _attr_name = "Handheld"
    _attr_icon = "mdi:shower-head"
    _state_key = "outlet0_on"
    _coord_setter = "async_set_outlet0"
