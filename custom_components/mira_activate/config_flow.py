"""Config flow for Mira Activate Shower.

Discovery-driven: HA offers the integration when its bluetooth scanner sees a
device advertising service UUID 267f0001-eb15-43f5-94c3-67d2221188f7.

Every route to a working setup — bluetooth discovery, manual pick, and re-auth —
funnels through ONE guided pairing step (``pair_confirm``) that tells the user to
put the shower into pairing mode *before* they submit, and only completes once an
SMP bond has been seeded and verified. The Activate gates all of its GATT
behind that bond, so a config flow that finishes without it just produces a dead
entry (the bug this flow exists to avoid).
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS

from .bonding import async_seed_bond
from .mira_protocol import SERVICE_UUID, device_id_from_name

_LOGGER = logging.getLogger(__name__)

DOMAIN = "mira_activate"


class MiraActivateConfigFlow(ConfigFlow, domain=DOMAIN):
    """Bluetooth-discovery-driven config flow with a guided pairing step."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_devices: dict[str, str] = {}
        # Target for the guided pair step on the *initial-add* path (no entry
        # exists yet): (address, name, device_id).
        self._pending: tuple[str, str | None, str | None] | None = None
        self._pair_detail: str = ""

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Triggered automatically by HA when an Activate is advertising."""
        _LOGGER.debug("BT discovered %s (%s)", discovery_info.address, discovery_info.name)
        # Identity = the stable name-id ('MIRA <hex>'), NOT the BLE address. The
        # Activate regenerates its random address (e.g. on power-cycle); keying on
        # the address makes each regeneration look like a brand-new device. If we
        # already know this unit, just refresh its stored address (entry reloads).
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
        """Confirm dialog after discovery, then hand off to the guided pair."""
        assert self._discovery_info is not None
        name = self._discovery_info.name or f"Mira Activate {self._discovery_info.address[-5:]}"
        if user_input is not None:
            self._pending = (
                self._discovery_info.address,
                self._discovery_info.name,
                device_id_from_name(self._discovery_info.name),
            )
            return await self.async_step_pair_confirm()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={"name": name},
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manual entry path: pick from any currently-advertising Activate."""
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            name = self._discovered_devices.get(address, address)
            dev_id = device_id_from_name(name)
            await self.async_set_unique_id(dev_id or address)
            self._abort_if_unique_id_configured(updates={CONF_ADDRESS: address})
            self._pending = (address, name, dev_id)
            return await self.async_step_pair_confirm()

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
                {vol.Required(CONF_ADDRESS): vol.In(self._discovered_devices)}
            ),
        )

    # ---- (Re)pairing — seed the SMP bond from inside HA's BT stack ----------
    #
    # The Activate gates its CCCD behind a persistent LE bond that can only be
    # (re)seeded while the unit is in pairing mode at the panel. Doing it here —
    # rather than from a standalone proxy script — lets HA's connection manager
    # coordinate the proxy's single slot, so it doesn't fight the coordinator.
    #
    # Entry points, all sharing async_step_pair_confirm:
    #   • bluetooth_confirm / user — initial add (no entry yet → create on bond).
    #   • reauth                   — auto-surfaced when the bond goes missing.
    #   • reconfigure              — user-initiated from the device's ⋮ menu.

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Auto-triggered when the coordinator reports the bond is lost."""
        return await self.async_step_pair_confirm()

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """User-initiated re-pair from the device menu."""
        return await self.async_step_pair_confirm(user_input)

    def _target_entry(self) -> ConfigEntry | None:
        """The existing entry this flow acts on (reauth/reconfigure), else None
        for the initial-add path."""
        entry_id = self.context.get("entry_id")
        if entry_id:
            return self.hass.config_entries.async_get_entry(entry_id)
        return None

    async def async_step_pair_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Guide the user to put the shower in pairing mode, then bond + verify.

        For an existing entry (reauth/reconfigure) the entry is unloaded first so
        its coordinator releases the proxy slot, bonded, then reloaded — the bond
        is never left worse than found. For an initial add the entry is created
        only after the bond verifies.
        """
        entry = self._target_entry()
        if entry is not None:
            name = entry.title or entry.data.get(CONF_ADDRESS, "the shower")
            address = entry.data[CONF_ADDRESS]
            device_id = entry.data.get("device_id")
        elif self._pending is not None:
            address, raw_name, device_id = self._pending
            name = raw_name or f"Mira Activate {address[-5:]}"
        else:
            return self.async_abort(reason="entry_not_found")

        errors: dict[str, str] = {}

        if user_input is not None:
            ok, detail = await self._seed_for(entry, address, device_id)
            self._pair_detail = detail
            _LOGGER.info("pair attempt for %s: ok=%s (%s)", address, ok, detail)
            if ok:
                if entry is not None:
                    return self.async_abort(
                        reason="pair_successful",
                        description_placeholders={"detail": detail},
                    )
                return self.async_create_entry(
                    title=name,
                    data={
                        CONF_ADDRESS: address,
                        "name": self._pending[1] if self._pending else name,
                        "device_id": device_id,
                    },
                )
            errors["base"] = "pair_failed"

        return self.async_show_form(
            step_id="pair_confirm",
            data_schema=vol.Schema({}),  # confirm-only: a reliable Submit button
            errors=errors,
            description_placeholders={
                "name": name,
                "detail": self._pair_detail or "(none yet)",
            },
        )

    async def _seed_for(
        self, entry: ConfigEntry | None, address: str, device_id: str | None
    ) -> tuple[bool, str]:
        """Run the bond seed, freeing the coordinator's slot first if the entry
        already exists. Always brings the entry back up afterwards."""
        if entry is None:
            return await async_seed_bond(self.hass, address, device_id)
        await self.hass.config_entries.async_unload(entry.entry_id)
        try:
            return await async_seed_bond(self.hass, address, device_id)
        finally:
            await self.hass.config_entries.async_setup(entry.entry_id)
