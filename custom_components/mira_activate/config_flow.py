"""Config flow for Mira Activate Shower.

Discovery-only: the integration is offered when HA's bluetooth scanner sees a
device advertising service UUID 267f0001-eb15-43f5-94c3-67d2221188f7. There is
no manual MAC entry path.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS

from .mira_protocol import SERVICE_UUID, device_id_from_name

_LOGGER = logging.getLogger(__name__)

DOMAIN = "mira_activate"


class MiraActivateConfigFlow(ConfigFlow, domain=DOMAIN):
    """Bluetooth-discovery-only config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_devices: dict[str, str] = {}

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Triggered automatically by HA when an Activate is advertising."""
        _LOGGER.debug("BT discovered %s (%s)", discovery_info.address, discovery_info.name)
        # Identity = the stable name-id ('MIRA <hex> ...'), NOT the BLE address.
        # The Activate regenerates its random address (e.g. on power-cycle); keying
        # on the address makes each regeneration look like a brand-new device. If
        # we already know this unit, just refresh its stored address (the entry
        # reloads) instead of offering a duplicate.
        dev_id = device_id_from_name(discovery_info.name)
        await self.async_set_unique_id(dev_id or discovery_info.address)
        self._abort_if_unique_id_configured(
            updates={CONF_ADDRESS: discovery_info.address}
        )
        self._discovery_info = discovery_info
        self.context["title_placeholders"] = {
            "name": discovery_info.name or f"Mira Activate {discovery_info.address[-5:]}"
        }
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm dialog after discovery."""
        assert self._discovery_info is not None
        if user_input is not None:
            return self.async_create_entry(
                title=self._discovery_info.name
                or f"Mira Activate {self._discovery_info.address[-5:]}",
                data={
                    CONF_ADDRESS: self._discovery_info.address,
                    "name": self._discovery_info.name,
                    "device_id": device_id_from_name(self._discovery_info.name),
                },
            )
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={
                "name": self._discovery_info.name or self._discovery_info.address
            },
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manual entry path: pick from any currently-advertising Activate."""
        errors: dict[str, str] = {}
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            name = self._discovered_devices.get(address, address)
            dev_id = device_id_from_name(name)
            await self.async_set_unique_id(dev_id or address)
            self._abort_if_unique_id_configured(updates={CONF_ADDRESS: address})
            return self.async_create_entry(
                title=name,
                data={
                    CONF_ADDRESS: address,
                    "name": name,
                    "device_id": dev_id,
                },
            )

        # Re-scan the HA BT cache. SERVICE_UUID first, guard a None/empty name,
        # and dedupe on the stable name-id (the address may differ from when it
        # was added — the Activate regenerates its random address).
        configured = self._async_current_ids()
        for info in async_discovered_service_info(self.hass):
            if SERVICE_UUID not in info.service_uuids:
                continue
            if not (info.name or "").startswith("Mira"):
                continue
            uid = device_id_from_name(info.name) or info.address
            if uid not in configured:
                self._discovered_devices[info.address] = (
                    info.name or f"Mira Activate {info.address[-5:]}"
                )

        if not self._discovered_devices:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): vol.In(self._discovered_devices),
                }
            ),
            errors=errors,
        )
