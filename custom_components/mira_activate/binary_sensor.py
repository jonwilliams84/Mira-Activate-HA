"""Mira Activate binary sensors — boolean state flags from the 0x2B poll."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import MiraActivateCoordinator

DOMAIN = "mira_activate"


@dataclass(frozen=True, kw_only=True)
class MiraBinaryDesc(BinarySensorEntityDescription):
    value_fn: Callable[[dict], bool | None]


BINARY_SENSORS: tuple[MiraBinaryDesc, ...] = (
    MiraBinaryDesc(
        key="running",
        name="Running",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda d: d.get("running"),
    ),
    MiraBinaryDesc(
        key="paused",
        name="Paused",
        icon="mdi:pause",
        value_fn=lambda d: d.get("paused"),
    ),
    MiraBinaryDesc(
        key="error",
        name="Error",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda d: d.get("error"),
    ),
    MiraBinaryDesc(
        key="session_ready",
        name="Session ready",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:bluetooth-connect",
        value_fn=lambda d: d.get("session_ready"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coord: MiraActivateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(MiraBinarySensor(coord, desc) for desc in BINARY_SENSORS)


class MiraBinarySensor(
    CoordinatorEntity[MiraActivateCoordinator], BinarySensorEntity
):
    entity_description: MiraBinaryDesc
    _attr_has_entity_name = True

    def __init__(self, coord: MiraActivateCoordinator, desc: MiraBinaryDesc) -> None:
        super().__init__(coord)
        self.entity_description = desc
        self._attr_unique_id = f"{coord.unique_base}_{desc.key}"
        self._attr_device_info = coord.device_info

    @property
    def is_on(self) -> bool | None:
        if not self.coordinator.data:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def available(self) -> bool:
        return bool(
            self.coordinator.data and self.coordinator.data.get("available", False)
        )
