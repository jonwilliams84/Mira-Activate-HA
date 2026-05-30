"""Mira Activate sensor entities — read-only state from the 0x2B poll."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import MiraActivateCoordinator

DOMAIN = "mira_activate"


@dataclass(frozen=True, kw_only=True)
class MiraSensorDesc(SensorEntityDescription):
    """Sensor entity description carrying the state-dict extractor."""

    value_fn: Callable[[dict], object]


SENSORS: tuple[MiraSensorDesc, ...] = (
    MiraSensorDesc(
        key="measured_temp",
        translation_key="measured_temp",
        name="Water temperature",
        icon="mdi:thermometer-water",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.get("measured_temp"),
    ),
    MiraSensorDesc(
        key="target_temp",
        translation_key="target_temp",
        name="Target temperature (reported)",
        icon="mdi:thermometer-check",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.get("target_temp"),
    ),
    MiraSensorDesc(
        key="flow_lpm",
        translation_key="flow_lpm",
        name="Flow",
        icon="mdi:water",
        native_unit_of_measurement="L/min",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.get("flow_lpm"),
    ),
    MiraSensorDesc(
        key="flow_raw",
        translation_key="flow_raw",
        name="Flow raw byte",
        icon="mdi:numeric",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.get("flow_raw"),
    ),
    MiraSensorDesc(
        key="outlet_state",
        translation_key="outlet_state",
        name="Outlet state byte",
        icon="mdi:shower-head",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: (
            None if d.get("outlet_state") is None
            else f"0x{int(d['outlet_state']):02x}"
        ),
    ),
    MiraSensorDesc(
        key="iot_status",
        translation_key="iot_status",
        name="IOT module status raw",
        icon="mdi:wifi",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: (
            None if d.get("iot_status") is None
            else f"0x{int(d['iot_status']):02x}"
        ),
    ),
    MiraSensorDesc(
        key="raw_status0",
        translation_key="raw_status0",
        name="System status byte",
        icon="mdi:information-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: (
            None if d.get("raw_status0") is None
            else f"0x{int(d['raw_status0']):02x}"
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coord: MiraActivateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(MiraSensor(coord, desc) for desc in SENSORS)


class MiraSensor(CoordinatorEntity[MiraActivateCoordinator], SensorEntity):
    entity_description: MiraSensorDesc
    _attr_has_entity_name = True

    def __init__(self, coord: MiraActivateCoordinator, desc: MiraSensorDesc) -> None:
        super().__init__(coord)
        self.entity_description = desc
        self._attr_unique_id = f"{coord.address}_{desc.key}"
        self._attr_device_info = coord.device_info

    @property
    def native_value(self):
        if not self.coordinator.data:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def available(self) -> bool:
        return bool(
            self.coordinator.data and self.coordinator.data.get("available", False)
        )
