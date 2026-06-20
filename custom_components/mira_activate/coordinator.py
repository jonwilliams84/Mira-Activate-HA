"""Mira Activate BLE coordinator.

A single persistent BLE connection per config entry. Serializes all app-layer
ops over the queue (spec §3.3 — "no per-frame sequence id, queue is strictly
serialized"). 10s op timeout per Lf5/c;->i.

Routes through HA's bluetooth integration (bleak-esphome backend), so any
configured BT proxy is used transparently.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from time import monotonic
from typing import Any

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    establish_connection,
)
from homeassistant.components.bluetooth import (
    async_address_present,
    async_ble_device_from_address,
    async_discovered_service_info,
    async_register_callback,
    BluetoothCallbackMatcher,
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .mira_protocol import (
    FrameAssembler,
    IncompleteError,
    InvalidFrameError,
    NOTIFY_CHAR_UUID,
    device_id_from_name,
    OP_SET_TEMPERATURE,
    OP_UNIT_PRIME,
    OUTLET_BIT_0,
    OUTLET_BIT_1,
    OUTLET_BIT_2,
    OUTLET_PAUSE,
    SERVICE_UUID,
    WRITE_CHAR_UUID,
    frame_set_temperature,
    frame_unit_prime,
    parse_unit_prime,
)

_LOGGER = logging.getLogger(__name__)

DOMAIN = "mira_activate"
# 30s — Mode integration's setting. The Activate drops the BLE link after
# ~36s idle, so each poll usually triggers a reconnect. The actual
# responsiveness floor is dominated by reconnect time, not poll cadence.
POLL_INTERVAL = timedelta(seconds=25)  # the 0x2B response takes ~6s, so a 10s
                                       # interval made polls pile up (the next
                                       # one cancelling the in-flight one →
                                       # CancelledError drops). 25s > one poll's
                                       # round-trip, so polls never overlap and
                                       # the lock is free for user commands far
                                       # more of the time.
OP_TIMEOUT = 10.0  # Activate's notifications sometimes arrive 4-8s after the
                   # write; shorter timeouts just give up before the reply.

# Hold the link without hammering the proxy. The Activate firmware idle-drops
# the BLE link after ~36s of no GATT activity; a lightweight keepalive just
# inside that window keeps the connection up so we avoid the connect/op/drop
# churn that saturates a shared BT proxy. Fires only when no real op has run
# recently, so it adds zero load while the coordinator is actively polling.
KEEPALIVE_INTERVAL = 20.0  # ping once the link is idle this long (< ~36s drop)
HEARTBEAT_CHECK = 5.0      # how often the heartbeat re-checks idle time; small
                           # so the ping reliably lands inside the drop window

# Connection-interval control (proxy-side, via ESPHome's
# bluetooth_device_set_connection_params — needs proxy firmware ≥ 2025.x with
# the CONNECTION_PARAMS_SETTING feature flag; older firmware no-ops with a
# warning). Units are 1.25ms. The official app's HCI capture shows it bumps the
# link to a 15ms interval whenever it needs to talk and lets it relax to ~4s
# idle otherwise; round-trips at 15ms are 24-65ms vs multi-second when the poll
# lands during the idle window. We hold a single persistent connection (like the
# app) and pin it to a tight interval so every poll AND every user command lands
# fast, instead of paying a reconnect + idle-interval penalty per poll.
# NB: a tight 15ms interval looked good in the app's HCI capture, but the app
# owns the phone's radio. Pinning 15ms on a proxy that ALSO serves the Mira Mode
# unit + presence scanning overloads the shared radio and trips the (short)
# supervision timeout → periodic link drops → slow reconnects → the minute-long
# command latency. For a fire-and-return command the write goes out within ONE
# connection interval anyway, so 30-75ms is imperceptible while being far gentler
# on the shared proxy. Stability (link stays UP) beats raw interval for perceived
# responsiveness — a held link makes every command instant.
FAST_MIN_INTERVAL = 24   # 30.0 ms
FAST_MAX_INTERVAL = 60   # 75.0 ms  (gives the shared proxy real slack)
CONN_LATENCY = 0
CONN_TIMEOUT = 2000      # 20.0 s supervision. The Activate stalls (no 0x2B reply
                         # for >10s) when idle; a short supervision timeout then
                         # tears the link down mid-poll. A long one lets the link
                         # ride out the stall instead of dropping + reconnecting.

# When polls keep failing (marginal RF / broken bond), backing off stops the
# 10s reconnect churn from hammering a struggling link and flooding the log.
# Interval doubles per consecutive failure up to this cap, then resets on the
# first good poll.
BACKOFF_MAX = timedelta(seconds=30)  # a shower should recover fast; eager
                                     # reconnect-on-drop is the primary path back


class MiraActivateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """One-connection-per-device coordinator."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"mira_activate[{entry.data['address']}]",
            update_interval=POLL_INTERVAL,
        )
        self.entry = entry
        self.address: str = entry.data["address"]
        # Stable per-unit id (from the advertised name). The BLE address can be
        # regenerated by the shower, so we use this to re-find it after a rotation
        # and to key the device-registry entry so entities survive the change.
        self._device_id: str | None = entry.data.get("device_id")
        # Stable unique-id base for entities and the device-registry entry.
        # Prefer the name-id so identifiers survive a BLE-address rotation; fall
        # back to the address only for legacy entries that never parsed a name-id.
        self.unique_base: str = self._device_id or self.address
        self.device_info = DeviceInfo(
            identifiers={(DOMAIN, self.unique_base)},
            name=entry.title or f"Mira Activate {self.address}",
            manufacturer="Mira (Kohler)",
            model="Mira Activate",
            connections={("bluetooth", self.address)},
        )
        self._client: BleakClient | None = None
        self._lock = asyncio.Lock()  # serializes ops, per spec §3.3
        self._assembler = FrameAssembler()
        self._response_event: asyncio.Event = asyncio.Event()
        self._last_frame: Any = None
        self._unsub_bt_callback = None
        self._heartbeat_task: asyncio.Task | None = None
        self._last_op: float = 0.0  # monotonic ts of last successful round-trip
        self._fail_count: int = 0   # consecutive poll failures (drives backoff)
        self._auth_failing: bool = False  # last failure was CCCD auth (bond lost)
        self._issue_raised: bool = False  # repair issue (bond lost) is showing
        self._closing: bool = False  # set on async_close to stop eager reconnect
        # Cached state pushed to entities (see AB_2B_DECODE.md).
        # outlet1_on / outlet2_on / outlet0_on are booleans driven by the
        # bit-packed outlet_state byte in payload[13]. flow_lpm is what we'll
        # echo back in the next 0xAB write (× 4 over the wire).
        self._state: dict[str, Any] = {
            "available": False,
            "session_ready": False,
            "running": False,
            "outlet0_on": False,
            "outlet1_on": False,
            "outlet2_on": False,
            "target_temp": 38.0,
            "flow_lpm": 12,  # sane default; gets replaced on first poll
            "measured_temp": None,
            "status_code": None,
            "iot_status": None,
            "paused": False,
            "error": False,
            "raw_status0": None,
            "raw_outlet_state": None,
        }

    # ---- Lifecycle ----------------------------------------------------------

    async def async_init(self) -> None:
        """Register for BT availability callbacks. Do not block setup."""
        self._unsub_bt_callback = async_register_callback(
            self.hass,
            self._on_bt_advertisement,
            BluetoothCallbackMatcher(address=self.address),
            BluetoothScanningMode.PASSIVE,
        )
        # Schedule first refresh in the background
        self.hass.async_create_task(self.async_config_entry_first_refresh())
        # Keepalive: hold the link inside the firmware's idle-drop window.
        self._heartbeat_task = self.hass.async_create_background_task(
            self._heartbeat(), name=f"{DOMAIN}_keepalive[{self.address}]"
        )

    async def async_close(self) -> None:
        self._closing = True
        if self._unsub_bt_callback is not None:
            self._unsub_bt_callback()
            self._unsub_bt_callback = None
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        await self._disconnect()

    # ---- BT callbacks -------------------------------------------------------

    @callback
    def _on_bt_advertisement(
        self, service_info: BluetoothServiceInfoBleak, change: BluetoothChange
    ) -> None:
        """Track availability based on advertising presence."""
        was_available = self._state["available"]
        self._state["available"] = True
        if not was_available:
            self.async_set_updated_data(self._state)

    # ---- Connection management ---------------------------------------------

    async def _get_ble_device(self) -> BLEDevice | None:
        dev = async_ble_device_from_address(self.hass, self.address, connectable=True)
        if dev is not None or not self._device_id:
            return dev
        # Stored address is gone — the Activate likely regenerated its random
        # address. Re-resolve the current address by the stable name-id so we
        # follow the rotation without needing a fresh discovery/re-add.
        for info in async_discovered_service_info(self.hass):
            if (
                info.connectable
                and SERVICE_UUID in info.service_uuids
                and device_id_from_name(info.name) == self._device_id
            ):
                if info.address != self.address:
                    _LOGGER.warning(
                        "Activate %s address rotated %s -> %s; following",
                        self._device_id, self.address, info.address,
                    )
                    self.address = info.address
                return async_ble_device_from_address(
                    self.hass, self.address, connectable=True
                )
        return dev

    async def _ensure_connected(self) -> BleakClient:
        if self._client is not None and self._client.is_connected:
            return self._client
        return await self._connect()

    async def _connect(self) -> BleakClient:
        """Establish ONE persistent BLE connection and hold it.

        The official app's HCI capture (CCCD_AUTH_RESOLVED.md) proves the app
        holds a single connection for the whole session, subscribes CCCD once,
        and never re-pairs or tears down on the hot path — it just rides the
        existing LE bond that lives in the proxy/device. So this method:

          1. connects,
          2. pins a tight connection interval (proxy-side) so polls AND user
             commands round-trip in tens of ms instead of waiting up to the
             device's ~4s idle interval,
          3. subscribes the notify CCCD,
          4. hands back the client to be held until the link genuinely drops.

        It deliberately does NOT run pair()/clear-cache recovery. That path
        (the 0.1.6 regression, see CLAUDE.md) destroyed the precious SMP bond
        and stranded the unit: the device rejects a fresh pair() on demand
        (`error 82`), and clearing the proxy cache drops the link, so the
        "recovery" only deepened the auth failure it was meant to fix. A real
        Insufficient-authentication here means the bond is momentarily
        unavailable (e.g. another proxy holds the connection slot, or the
        device hasn't re-exposed the bond yet); the cure is to back off and
        retry the *connection*, never to nuke the bond.
        """
        device = await self._get_ble_device()
        if device is None:
            raise UpdateFailed(f"Device {self.address} not in BT registry")
        _LOGGER.debug("Establishing persistent BLE connection to %s", self.address)
        client = await establish_connection(
            BleakClientWithServiceCache,
            device,
            self.address,
            disconnected_callback=self._on_disconnected,
            max_attempts=3,
        )
        self._assembler.reset()
        self._response_event.clear()

        # Pin a fast connection interval up front (the app's 15ms "active"
        # state). Best-effort: no-ops cleanly on older proxy firmware.
        await self._set_fast_connection_params(client)

        # Activate the cached SMP bond on this fresh connection. Over an ESPHome
        # proxy the stored LTK is NOT auto-applied on connect — without an
        # explicit pair() the link stays unencrypted and the CCCD subscribe
        # below returns Insufficient authentication. When the bond already
        # exists this just (re)establishes encryption from the cached key: no
        # re-bonding, no user action, no pairing mode needed (that's only
        # required to create a bond in the first place, which the config flow's
        # bonding.async_seed_bond handles). Tolerant — if it raises we still try
        # start_notify, which is the real arbiter of whether the link is usable.
        try:
            await client.pair()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("pair() on connect raised (continuing): %s", exc)

        # IMPORTANT: After pair(), we must wait for the encryption to actually 
        # engage on the link before attempting the CCCD write. The BLE stack 
        # may return from pair() immediately while the LL_ENC_REQ is still 
        # in flight or the link is transitioning to encrypted state.
        await asyncio.sleep(0.5)

        try:
            await client.start_notify(NOTIFY_CHAR_UUID, self._on_notify)
        except Exception as exc:  # noqa: BLE001
            # CCCD subscribe failed. If it's an auth failure the bond is not
            # currently usable on this connection — DO NOT pair()/clear-cache.
            # Drop the client so the next poll reconnects (backoff handles the
            # cadence); the bond is left intact for when it becomes available.
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
            msg = str(exc)
            if (
                "Insufficient authentication" in msg
                or "Insufficient authorization" in msg
            ):
                self._auth_failing = True
                raise UpdateFailed(
                    f"CCCD auth not available for {self.address} "
                    "(bond temporarily unusable — will retry, bond left intact)"
                ) from exc
            raise

        self._auth_failing = False
        self._client = client
        self._last_op = monotonic()
        _LOGGER.debug("BLE link to %s up + notify subscribed", self.address)
        return client

    async def _set_fast_connection_params(self, client: BleakClient) -> None:
        """Pin a tight LE connection interval via the ESPHome proxy.

        Mirrors the official app, which drives the link at a ~15ms interval
        while it's talking (HCI capture). Reaches the bleak-esphome backend's
        set_connection_params (proxy firmware ≥ ESPHome 2025.x with the
        CONNECTION_PARAMS_SETTING flag; older firmware logs a warning and
        no-ops). Best-effort — never fatal."""
        backend = getattr(client, "_backend", None)
        setter = getattr(backend, "set_connection_params", None)
        if setter is None:
            return
        try:
            await setter(
                FAST_MIN_INTERVAL, FAST_MAX_INTERVAL, CONN_LATENCY, CONN_TIMEOUT
            )
            _LOGGER.debug(
                "pinned fast conn params for %s (%.1f-%.1fms)",
                self.address,
                FAST_MIN_INTERVAL * 1.25,
                FAST_MAX_INTERVAL * 1.25,
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("set_connection_params best-effort failed: %s", exc)

    def _proxy_api_client(self, client: BleakClient):
        """Reach the underlying aioesphomeapi APIClient via the bleak-esphome
        backend (client._backend._client and friends). None if not found."""
        backend = getattr(client, "_backend", None)
        for attr in ("_client", "client", "api", "_api_client"):
            if backend is not None and getattr(backend, attr, None) is not None:
                return getattr(backend, attr)
        return None

    async def _heartbeat(self) -> None:
        """Hold the BLE link inside the firmware's ~36s idle-drop window.

        Checks FREQUENTLY (every HEARTBEAT_CHECK seconds) and pings the moment
        the link has been idle for KEEPALIVE_INTERVAL. The old version slept a
        fixed KEEPALIVE_INTERVAL between checks, so a check that landed just
        before the idle threshold skipped, and the *next* check fell ~2×
        KEEPALIVE_INTERVAL out — past the 36s firmware drop. The link idle-
        dropped and every command then paid a full (sometimes minute-long)
        reconnect. Frequent checks guarantee the ping lands inside the window.

        The ping is fire-and-forget (wait_response=False): the write alone resets
        the firmware idle timer and it doesn't hold the op lock waiting ~6s for a
        reply, so it never blocks a user command."""
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_CHECK)
                if self._client is None or not self._client.is_connected:
                    continue
                if (monotonic() - self._last_op) < KEEPALIVE_INTERVAL:
                    continue
                try:
                    await self._request(frame_unit_prime(), wait_response=False)
                    _LOGGER.debug("keepalive ping ok for %s", self.address)
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.debug(
                        "keepalive ping failed for %s: %s", self.address, exc
                    )
        except asyncio.CancelledError:
            pass

    async def _clear_cache_via_client(self, client: BleakClient) -> None:
        """Clear the bond cache + unpair on the proxy currently servicing this
        client. We reach the underlying aioesphomeapi APIClient through the
        bleak-esphome backend."""
        addr_int = int(self.address.replace(":", ""), 16)
        api = self._proxy_api_client(client)
        if api is None:
            _LOGGER.warning("could not reach proxy APIClient")
            return
        _LOGGER.warning("reached proxy APIClient (%s)", type(api).__name__)
        for op_name, op_call in (
            ("unpair",      lambda: api.bluetooth_device_unpair(addr_int)),
            ("clear_cache", lambda: api.bluetooth_device_clear_cache(addr_int)),
        ):
            try:
                await asyncio.wait_for(op_call(), timeout=8)
                _LOGGER.warning("proxy %s OK for %s", op_name, self.address)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("proxy %s raised: %s: %s", op_name, type(exc).__name__, exc)

    def _note_failure(self) -> None:
        """Grow the poll interval on consecutive failures (capped)."""
        self._fail_count += 1
        secs = min(
            POLL_INTERVAL.total_seconds() * (2 ** min(self._fail_count, 4)),
            BACKOFF_MAX.total_seconds(),
        )
        self.update_interval = timedelta(seconds=secs)
        if self._fail_count == 1 or self._fail_count % 5 == 0:
            _LOGGER.warning(
                "poll failing (x%d) — backing off to %ds", self._fail_count, int(secs)
            )

    def _note_success(self) -> None:
        """Reset backoff after a good poll."""
        if self._fail_count:
            _LOGGER.info("poll recovered after %d failure(s)", self._fail_count)
        self._fail_count = 0
        self._auth_failing = False
        if self.update_interval != POLL_INTERVAL:
            self.update_interval = POLL_INTERVAL
        if self._issue_raised:
            self._clear_repair_issue()

    def _raise_repair_issue(self) -> None:
        """Surface a one-shot Repair pointing the user at Reconfigure to re-pair.

        Idempotent (HA dedupes by issue_id) and wrapped so it can never break a
        poll. Raised once per coordinator life on a CCCD-auth failure; cleared on
        the first good poll."""
        if self._issue_raised:
            return
        try:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"bond_lost_{self.entry.entry_id}",
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="bond_lost",
                translation_placeholders={"name": self.entry.title or self.address},
            )
            self._issue_raised = True
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("could not raise repair issue: %s", exc)

    def _clear_repair_issue(self) -> None:
        try:
            ir.async_delete_issue(
                self.hass, DOMAIN, f"bond_lost_{self.entry.entry_id}"
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("could not clear repair issue: %s", exc)
        self._issue_raised = False

    def _on_disconnected(self, client: BleakClient) -> None:
        _LOGGER.debug("BLE disconnect from %s", self.address)
        self._client = None
        self._state["available"] = False
        # The device/proxy drops the link periodically (shared-radio flakiness).
        # Don't wait out the poll backoff (up to BACKOFF_MAX) to come back — that
        # leaves state stale and commands slow for tens of seconds. Reconnect
        # eagerly so the link is almost always up and commands ride it live.
        if not self._closing:
            self.hass.async_create_task(self._eager_reconnect())

    async def _eager_reconnect(self) -> None:
        await asyncio.sleep(1.0)  # let the drop settle before re-dialling
        if self._closing or (self._client is not None and self._client.is_connected):
            return
        _LOGGER.debug("eager reconnect after drop for %s", self.address)
        # Reset the backoff so the refresh fires now, not after the grown
        # interval; async_request_refresh reconnects via _ensure_connected.
        self.update_interval = POLL_INTERVAL
        try:
            await self.async_request_refresh()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("eager reconnect refresh raised: %s", exc)

    async def _disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("disconnect raised: %s", exc)
            self._client = None

    def _on_notify(self, _char, data: bytearray) -> None:
        try:
            frame = self._assembler.feed(bytes(data))
        except IncompleteError:
            return
        except InvalidFrameError as e:
            _LOGGER.warning("invalid frame from %s: %s", self.address, e)
            self._assembler.reset()
            return
        self._last_frame = frame
        self._response_event.set()

    # ---- Op primitives ------------------------------------------------------

    async def _request(self, frame: bytes, *, wait_response: bool = True) -> Any:
        """Send a frame; optionally await one response. Serialized via _lock.

        ``wait_response=True`` (the poll path) blocks up to ``OP_TIMEOUT`` for
        the device's reply notification and returns the parsed frame.

        ``wait_response=False`` (the user-command path) is fire-and-return: it
        writes the ATT Write Command and releases immediately. The water reacts
        on the write itself — the reply notification is only an app-level "got
        it" ack — so making a user command wait up to 10s for that ack (and hold
        the lock the whole time) is the bulk of the "shower takes a minute to
        turn on" lag. The sibling ``mira_mode`` integration fires-and-returns
        and is instant; this matches it. State catches up on the next poll.
        """
        async with self._lock:
            client = await self._ensure_connected()
            self._assembler.reset()
            self._response_event.clear()
            self._last_frame = None
            _LOGGER.debug("→ %s", frame.hex())
            # The official app uses ATT Write Command (response=False) — no
            # ATT-layer ACK round-trip; the device's notification on 267f0003
            # is the application-level "I got it" signal.
            await client.write_gatt_char(
                WRITE_CHAR_UUID, frame, response=False
            )
            # Link held — record so the keepalive heartbeat need not ping.
            self._last_op = monotonic()
            if not wait_response:
                return None
            try:
                await asyncio.wait_for(
                    self._response_event.wait(), timeout=OP_TIMEOUT
                )
            except asyncio.TimeoutError as e:
                raise UpdateFailed(
                    f"timeout waiting for response to {frame.hex()}"
                ) from e
            return self._last_frame

    # ---- Coordinator update loop -------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        """Poll the device (one 0x2B Unit Prime read per cycle)."""
        if not async_address_present(self.hass, self.address, connectable=True):
            self._state["available"] = False
            return self._state
        try:
            frame = await self._request(frame_unit_prime())
        except UpdateFailed:
            self._note_failure()
            # A CCCD-auth failure means the SMP bond is gone. We do NOT raise
            # ConfigEntryAuthFailed here: raising it on every poll makes HA
            # reload the entry in a tight loop, which re-grabs the proxy slot
            # every couple of seconds and starves the re-pair flow's bond attempt
            # (it just spins). Instead we stay quiet and let the backoff settle
            # to 120s so the slot is free; the user re-pairs from the device's
            # ⋮ → Reconfigure menu (config_flow → bonding.async_seed_bond), and a
            # Repair issue (raised once below) points them there.
            if self._auth_failing:
                self._raise_repair_issue()
            raise
        except Exception as e:  # noqa: BLE001
            self._note_failure()
            raise UpdateFailed(f"poll failed: {e}") from e
        self._note_success()
        # Parse the 0x2B response per AB_2B_DECODE.md §2.
        if frame is not None:
            payload_hex = frame.payload.hex()
            parsed = parse_unit_prime(frame.payload)
            if parsed:
                self._state.update(parsed)
                # available: prefer parsed session_ready (bit 6 of byte 0);
                # fall back to "we got a valid response at all".
                self._state["available"] = True
                _LOGGER.debug(
                    "0x2B ← %s : target=%.1f°C flow=%dLPM outlet=0x%02x "
                    "running=%s paused=%s error=%s session_ready=%s "
                    "iot=0x%02x measured=%.1f",
                    payload_hex,
                    parsed["target_temp"],
                    parsed["flow_lpm"],
                    parsed["outlet_state"],
                    parsed["running"],
                    parsed["paused"],
                    parsed["error"],
                    parsed["session_ready"],
                    parsed["iot_status"],
                    parsed["measured_temp"],
                )
            else:
                _LOGGER.warning(
                    "0x2B payload too short to parse: %s", payload_hex
                )
                self._state["available"] = True
        else:
            self._state["available"] = False
        return self._state

    # ---- Public API used by entities ---------------------------------------

    async def async_set_outlet1(self, on: bool) -> None:
        """Toggle outlet B (bit 1 / 0x02). Verified rain head."""
        self._state["outlet1_on"] = on
        await self._send_temperature_frame()

    async def async_set_outlet0(self, on: bool) -> None:
        """Toggle outlet A (bit 0 / 0x01). Inferred handheld."""
        self._state["outlet0_on"] = on
        await self._send_temperature_frame()

    async def async_set_flow_rate(self, flow_raw: int) -> None:
        """Set flow rate. `flow_raw` is the wire byte (LPM × 4). Cap 64."""
        self._state["flow_raw"] = max(0, min(int(flow_raw), 64))
        await self._send_temperature_frame()

    async def _run_brute_force_probe(self) -> None:
        """Try a series of candidate frames and log every response.

        Goal: find the opcode that actually starts water flowing on the
        Mira Activate. Each candidate frame is sent, then we wait up to
        2s for a response, log it, and move on. After the sequence, the
        regular poll resumes — if any candidate flipped an outlet bit
        in the device's response payload, we have our answer.
        """
        from datetime import datetime
        ts = datetime.now().strftime("%y%m%d%H%M%S")  # 12 chars
        # Frame builder helpers (raw — bypass the typed builders).
        def _f(opcode: int, payload: bytes) -> bytes:
            frame = bytes([0xAA, 0x55, 0x00, opcode, len(payload)]) + payload
            return frame + bytes([((~sum(frame) + 1) & 0xFF)])
        candidates = [
            # Most likely to flip iot status: 0xF8 Write Commissioning State.
            # Try each value 0..3; if writing 0x02 makes iot in the next poll
            # decode to 2, we've found the unblock.
            ("F8_state0",  _f(0xF8, bytes([0x00]))),
            ("F8_state2",  _f(0xF8, bytes([0x02]))),
            ("F8_state3",  _f(0xF8, bytes([0x03]))),
            ("F8_state1",  _f(0xF8, bytes([0x01]))),
            # 0xC0 reads bathroom name (per APK f2$c.a) — also serves as a
            # session-establishment ping; harmless to re-send.
            ("C0_v9000",  _f(0xC0, ("9000" + ts).encode("ascii").ljust(18, b"\x00"))),
            # 0xAB outlet write (what we've been doing).
            ("AB_o1_on",  _f(0xAB, bytes([0x01, 0x7C, 0x54, 0x04]))),
            # 0xC2 sub-commands — Write SSid / Tenant id / etc., possible iot
            # auth side door.
            ("C2_sub00",  _f(0xC2, bytes([0x00]))),
            ("C2_sub01",  _f(0xC2, bytes([0x01]))),
            # 0xDD / 0x9B — last-ditch GCS commands.
            ("DD_empty",  _f(0xDD, b"")),
            ("9B_zeros",  _f(0x9B, bytes(16))),
            # Final AB write to see if outlet flips after all the state changes.
            ("AB_o1_on_post", _f(0xAB, bytes([0x01, 0x7C, 0x54, 0x04]))),
        ]
        # Re-entry guard: if a probe is already running, ignore subsequent toggles.
        if getattr(self, "_probe_running", False):
            _LOGGER.warning("PROBE: already running — ignoring re-toggle")
            return
        self._probe_running = True
        _LOGGER.warning("PROBE: starting brute-force candidate sweep, %d frames, ts=%s", len(candidates), ts)
        for name, frame in candidates:
            try:
                async with self._lock:
                    client = await self._ensure_connected()
                    self._assembler.reset()
                    self._response_event.clear()
                    self._last_frame = None
                    _LOGGER.warning("PROBE → %s : %s", name, frame.hex())
                    try:
                        await client.write_gatt_char(WRITE_CHAR_UUID, frame, response=True)
                    except Exception as e:  # noqa: BLE001
                        _LOGGER.warning("PROBE × %s write failed: %s", name, e)
                        await asyncio.sleep(0.5)
                        continue
                    try:
                        await asyncio.wait_for(self._response_event.wait(), timeout=2.5)
                        f = self._last_frame
                        if f is not None:
                            _LOGGER.warning(
                                "PROBE ← %s : opcode=0x%02X payload=%s",
                                name, f.opcode, f.payload.hex(),
                            )
                        else:
                            _LOGGER.warning("PROBE ← %s : (no frame parsed)", name)
                    except asyncio.TimeoutError:
                        _LOGGER.warning("PROBE × %s : no response in 2.5s", name)
                await asyncio.sleep(0.7)
            except Exception as e:  # noqa: BLE001
                _LOGGER.warning("PROBE × %s raised: %s: %s", name, type(e).__name__, e)
                await asyncio.sleep(0.7)
        _LOGGER.warning("PROBE: sweep complete")
        self._probe_running = False

    async def async_set_temperature(self, temp_c: float) -> None:
        """Set target temperature (°C)."""
        self._state["target_temp"] = temp_c
        await self._send_temperature_frame()

    async def async_turn_off_all(self) -> None:
        """Emergency "turn off all outlets" — mirrors the app's
        turnOffAllOutlets path: flags=1, temp=0, flow=0, outlet_state=0."""
        self._state["outlet0_on"] = False
        self._state["outlet1_on"] = False
        self._state["outlet2_on"] = False
        frame = frame_set_temperature(
            flags=1, temp_x10=0, flow_x4=0, outlet_state=0
        )
        try:
            await self._request(frame, wait_response=False)
        except UpdateFailed as e:
            _LOGGER.error("turn-off-all failed: %s", e)
            raise
        self.async_set_updated_data(self._state)  # reflect immediately
        self.hass.async_create_task(self.async_request_refresh())

    async def _send_temperature_frame(self) -> None:
        """Build and send a 0xAB frame from the cached desired state.

        Encoding corrected from live HCI snoop 2026-05-30:
          flags           = 0 (the app's `flags` is also always 0 for normal ops)
          temp_x10        = target_temp * 10 (split between byte 5 bit 0 & byte 6)
          byte 7 (flow)   = the device's reported flow_raw (mirror what we got
                            from the last poll — the app does the same)
          byte 8 (outlet) = bit 0=outlet A, bit 1=outlet B, 0x40=pause
        """
        temp_x10 = int(round(self._state["target_temp"] * 10))
        # Echo the last-observed flow_raw back. App defaults to 64 if unknown.
        flow_x4 = int(self._state.get("flow_raw") or 64)
        outlet_state = 0
        if self._state.get("outlet0_on"):
            outlet_state |= OUTLET_BIT_0  # 0x01
        if self._state.get("outlet1_on"):
            outlet_state |= OUTLET_BIT_1  # 0x02
        # outlet2 bit (0x04) is unused on the live device — skip it entirely
        # rather than send an invalid bit the firmware ignores.
        if self._state.get("paused"):
            outlet_state |= OUTLET_PAUSE  # 0x40
        frame = frame_set_temperature(
            flags=0,
            temp_x10=temp_x10,
            flow_x4=flow_x4,
            outlet_state=outlet_state,
        )
        _LOGGER.debug(
            "0xAB → flags=0 temp_x10=%d flow_x4=%d outlet_state=0x%02x "
            "frame=%s",
            temp_x10,
            flow_x4,
            outlet_state,
            frame.hex(),
        )
        try:
            # Fire-and-return so the command lands fast (no 10s ack wait).
            await self._request(frame, wait_response=False)
        except UpdateFailed as e:
            _LOGGER.error("set-temperature failed: %s", e)
            raise
        # Reflect the optimistic state to the entities IMMEDIATELY. _state was
        # already updated to the desired values by the caller; without pushing it
        # here the UI sits on the old value until the device answers the confirm
        # poll, which can take several seconds on this slow device — that was the
        # whole "button press takes seconds to show" lag. The background refresh
        # below then reconciles against device truth (and corrects if it differs).
        self.async_set_updated_data(self._state)
        self.hass.async_create_task(self.async_request_refresh())
