"""SMP bond (re)seeding for the Mira Activate, run inside HA's bluetooth stack.

The Activate gates its CCCD/notify behind a persistent LE SMP bond. The bond can
only be established while the unit is in pairing mode at the panel (it rejects an
unsolicited ``createBond`` with ``error 82`` otherwise). The official app seeds
the bond once via ``createBond()`` and then rides it forever.

Doing the pair from a standalone script that talks to the ESPHome proxy directly
is invisible to HA's bluetooth connection manager, so it fights the running
coordinator for the proxy's single connection slot and both time out. Running it
HERE — through ``establish_connection`` like everything else — lets HA's slot
allocator coordinate the one connection, so it actually completes.

Call this from the config flow with the entry UNLOADED (so its coordinator isn't
also holding the slot), while the user has the shower in pairing mode.
"""

from __future__ import annotations

import asyncio
import logging

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components.bluetooth import (
    async_ble_device_from_address,
    async_discovered_service_info,
)
from homeassistant.core import HomeAssistant

from .mira_protocol import (
    FrameAssembler,
    IncompleteError,
    InvalidFrameError,
    NOTIFY_CHAR_UUID,
    OP_UNIT_PRIME,
    SERVICE_UUID,
    WRITE_CHAR_UUID,
    device_id_from_name,
    frame_unit_prime,
)

_LOGGER = logging.getLogger(__name__)

PAIR_TIMEOUT = 35.0  # the app's createBond force-retry window is 35s
VERIFY_TIMEOUT = 12.0


def _resolve_device(
    hass: HomeAssistant, address: str, device_id: str | None
) -> tuple[BLEDevice | None, str]:
    """Find the connectable BLEDevice, following an address rotation by name-id
    (the Activate regenerates its random address on power-cycle)."""
    dev = async_ble_device_from_address(hass, address, connectable=True)
    if dev is not None:
        return dev, address
    if device_id:
        for info in async_discovered_service_info(hass):
            if (
                info.connectable
                and SERVICE_UUID in info.service_uuids
                and device_id_from_name(info.name) == device_id
            ):
                return (
                    async_ble_device_from_address(hass, info.address, connectable=True),
                    info.address,
                )
    return None, address


async def async_seed_bond(
    hass: HomeAssistant, address: str, device_id: str | None = None
) -> tuple[bool, str]:
    """Connect, seed the SMP bond, and prove it works end-to-end.

    Returns ``(ok, detail)``. ``detail`` is a short human string for the flow to
    surface. Never raises — every failure is turned into ``(False, reason)`` so
    the config flow can show it and the bond is never left worse than found.
    """
    device, resolved = _resolve_device(hass, address, device_id)
    if device is None:
        return False, (
            "not_connectable: the shower isn't visible to a connectable proxy. "
            "Is it in range of proxy 3492a8 and actually in pairing mode?"
        )

    try:
        client = await establish_connection(
            BleakClientWithServiceCache, device, resolved, max_attempts=3
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"connect_failed: {type(exc).__name__}: {exc}"

    try:
        # 1. Seed the SMP bond. Over the bleak-esphome backend this maps to
        #    aioesphomeapi.bluetooth_device_pair (ESP-IDF createBond). It only
        #    succeeds while the unit is pairable; otherwise the device rejects it.
        try:
            await asyncio.wait_for(client.pair(), timeout=PAIR_TIMEOUT)
            _LOGGER.info("Activate %s: pair() returned ok", resolved)
        except Exception as exc:  # noqa: BLE001
            # Some stacks bond implicitly on the first auth'd op rather than via
            # an explicit pair — so don't bail yet; let the CCCD subscribe below
            # be the real arbiter of whether a bond now exists.
            _LOGGER.warning(
                "Activate %s: pair() raised (%s) — verifying via CCCD anyway",
                resolved, exc,
            )

        # 2. Prove the bond: CCCD subscribe is the exact op that fails with
        #    Insufficient authentication when the bond is missing.
        assembler = FrameAssembler()
        got = asyncio.Event()
        holder: dict[str, object] = {}

        def _on_notify(_handle: int, data: bytearray) -> None:
            try:
                frame = assembler.feed(bytes(data))
            except IncompleteError:
                return
            except InvalidFrameError as exc:  # noqa: BLE001
                _LOGGER.debug("seed verify: bad frame: %s", exc)
                assembler.reset()
                return
            holder["frame"] = frame
            got.set()

        try:
            await client.start_notify(NOTIFY_CHAR_UUID, _on_notify)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "Insufficient auth" in msg:
                return False, (
                    "still_unauthorised: CCCD subscribe was rejected — the bond "
                    "did not take. Make sure the panel is in pairing mode and try "
                    "again immediately."
                )
            return False, f"notify_failed: {type(exc).__name__}: {exc}"

        # The CCCD subscribe SUCCEEDING is the proof the bond exists — that is the
        # exact encrypted GATT op that returns Insufficient authentication when
        # the bond is missing. From here the bond is good; the 0x2B round-trip is
        # a best-effort extra confirmation, NOT a gate (the device legitimately
        # answers with an unsolicited status frame first, whose opcode isn't
        # 0x2B — failing on that would reject a perfectly good bond).
        try:
            await client.write_gatt_char(
                WRITE_CHAR_UUID, frame_unit_prime(), response=False
            )
            await asyncio.wait_for(got.wait(), timeout=VERIFY_TIMEOUT)
            frame = holder.get("frame")
            op = getattr(frame, "opcode", None)
            extra = (
                f"0x2B echo ok" if op == OP_UNIT_PRIME
                else f"link replied (opcode 0x{op:02X})" if op is not None
                else "no app-layer reply (bond still valid)"
            )
        except asyncio.TimeoutError:
            extra = "no app-layer reply in time (bond still valid)"
        except Exception as exc:  # noqa: BLE001
            extra = f"app-layer probe error {type(exc).__name__} (bond still valid)"

        return True, f"bonded + CCCD subscribed over {resolved} — {extra}"
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass
