"""Mira Activate number entities: target temperature and flow rate.

Per-entity optimistic state — matches mira_mode pattern. The entity sets
its local value + writes HA state immediately, THEN awaits the BLE write.
"""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
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
            TargetTempNumber(coord),
            FlowRateNumber(coord),
        ]
    )


class _CoordNumberBase(CoordinatorEntity[MiraActivateCoordinator], NumberEntity):
    _attr_has_entity_name = True
    _attr_assumed_state = True
    _state_key: str = ""
    _coord_setter: str = ""

    def __init__(self, coord: MiraActivateCoordinator, uid_suffix: str) -> None:
        super().__init__(coord)
        self._attr_unique_id = f"{coord.address}_{uid_suffix}"
        self._cached: float | None = self._read_from_coord()

    def _read_from_coord(self) -> float | None:
        if self.coordinator.data is None:
            return None
        v = self.coordinator.data.get(self._state_key)
        return None if v is None else float(v)

    @callback
    def _handle_coordinator_update(self) -> None:
        self._cached = self._read_from_coord()
        super()._handle_coordinator_update()

    @property
    def native_value(self) -> float | None:
        return self._cached

    @property
    def available(self) -> bool:
        return bool(
            self.coordinator.data and self.coordinator.data.get("available", False)
        )

    async def async_set_native_value(self, value: float) -> None:
        self._cached = value
        self.async_write_ha_state()
        try:
            await self._invoke_setter(value)
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Failed to set %s — will correct on next poll", self._state_key)

    async def _invoke_setter(self, value: float) -> None:
        await getattr(self.coordinator, self._coord_setter)(value)


class TargetTempNumber(_CoordNumberBase):
    _attr_name = "Target temperature"
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_min_value = 20.0
    _attr_native_max_value = 48.0
    _attr_native_step = 0.5
    _attr_mode = NumberMode.SLIDER
    _attr_icon = "mdi:thermometer"
    _state_key = "target_temp"
    _coord_setter = "async_set_temperature"

    def __init__(self, coord: MiraActivateCoordinator) -> None:
        super().__init__(coord, "target_temp")


class FlowRateNumber(_CoordNumberBase):
    """Flow rate target in L/min. Max 16 L/min (= 64 raw). Wire encoding: byte 7 = LPM × 4."""

    _attr_name = "Flow rate"
    _attr_native_unit_of_measurement = "L/min"
    _attr_native_min_value = 0.0
    _attr_native_max_value = 16.0
    _attr_native_step = 0.25
    _attr_mode = NumberMode.SLIDER
    _attr_icon = "mdi:water-pump"
    _state_key = "flow_lpm"
    _coord_setter = "async_set_flow_rate"  # raw bytes, not LPM

    def __init__(self, coord: MiraActivateCoordinator) -> None:
        super().__init__(coord, "flow_rate")

    async def _invoke_setter(self, value: float) -> None:
        raw = max(0, min(int(round(value * 4)), 64))
        await self.coordinator.async_set_flow_rate(raw)
