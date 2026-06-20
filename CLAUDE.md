# Mira Activate for Home Assistant

HACS custom integration (domain `mira_activate`) that controls **Mira Activate**
digital showers over BLE, routed through HA's bluetooth integration / ESPHome BT
proxies (no direct host BLE). `iot_class: local_polling`.

Sister project: `mira-ha` (Mira *Mode* — a completely different wire format).

## Layout
- `custom_components/mira_activate/` — the entire integration. No build step; this dir is copied verbatim into `<config>/custom_components/`.
  - `mira_protocol.py` — pure frame builder/parser (no HA deps): `build()`, `chk8()`, `frame_unit_prime()`, `frame_set_temperature()`, `parse_unit_prime()`, `FrameAssembler`, opcode constants. Edit byte-level logic here.
  - `coordinator.py` — `MiraActivateCoordinator`: one persistent BLE connection per entry, serialized ops via `asyncio.Lock`, keepalive heartbeat, bond-recovery logic. Entity-facing API: `async_set_outlet0/1`, `async_set_temperature`, `async_set_flow_rate`, `async_turn_off_all`.
  - `config_flow.py` — bluetooth-discovery-driven; unique_id is the stable **name-id** (`MIRA <hex>`), never the BLE address.
  - `switch.py` / `number.py` / `sensor.py` / `binary_sensor.py` — entities reading `coordinator.data` dict keys.
  - `manifest.json` — bump `version` here for each release.
- `docs/PROTOCOL.md` — reverse-engineered wire spec (the source of truth referenced throughout the code as PROTOCOL_SPEC.md / AB_2B_DECODE.md).
- `hacs.json` — HACS metadata. No CI, no tests, no Makefile.

## Wire protocol essentials
- Frame: `AA 55 00 <opcode> <body_len> <payload> <chk8>`. `chk8` = 8-bit 2's-complement so `sum(frame) & 0xFF == 0`.
- GATT: write to `267f0002-…` (use `response=False`, the app does), notify on `267f0003-…`, service `267f0001-…`.
- Key opcodes: `0x2B` Unit Prime (poll/read, 18-byte response), `0xAB` Set Temperature (outlet + temp + flow control).
- `0xAB` payload: byte0 = flags|temp_hi, byte1 = temp_lo (temp×10), byte2 = flow×4, byte3 = outlet bitmap (bit0/bit1 outlets, 0x40 pause).
- Byte-level claims are validated against a live HCI snoop, NOT the decompiled APK — when they disagree, the snoop wins. Preserve that when editing.

## Gotchas
- The Activate regenerates its random BLE address (e.g. on power-cycle). Identity is the `MIRA <hex>` token (`entry.data["device_id"]`), not the MAC. Key entity unique_ids on `coordinator.unique_base` (= name-id, address fallback), never the raw address — address-keyed uids spawn duplicate entities/devices on rotation. `__init__._migrate_identity` self-heals legacy entries (sets the entry `unique_id` + rewrites `<MAC>_<suffix>` entity uids to `<device_id>_<suffix>`).
- The unit advertises under TWO local-name forms — firmware `"Mira 003F#<serial>"` and the user-set `"Mira 003F en suite"`. `device_id_from_name` MUST collapse both to the same id (it truncates at `#` → `003F`), else each form is discovered as a separate device. Single-unit assumption: distinct Activates with the same model code would collide. `DeviceInfo` carries `connections={("bluetooth", addr)}`, so HA merges any same-MAC duplicate's identifier onto the one device — a leftover `003F#<serial>` identifier on the device is that residue.
- User commands are **fire-and-return** (`wait_response=False`): the valve reacts on the write; the notification is only an app-level ack. Do not make toggles await it — that was the multi-second lag. The command methods set the desired `_state` then call `async_set_updated_data(self._state)` so the entities reflect **optimistically and instantly**; the background `async_request_refresh` reconciles against device truth (the `0x2B` confirm is several seconds out). Without the optimistic push the UI lags seconds behind every press.
- **`pair()` on EVERY connect, before `start_notify`** (`_connect`). Over an ESPHome proxy the cached SMP bond's LTK is NOT auto-applied on a fresh connection — the link stays unencrypted and the CCCD subscribe returns `Insufficient authentication`. `client.pair()` re-arms encryption from the stored key; when the bond exists it's cheap and silent (no re-bond, no pairing mode). Tolerant: if it raises, still try `start_notify` (the subscribe succeeding is the real proof). The official app appears to "ride the bond" only because the phone holds one link with encryption already active; the proxy makes a new connection each time. See `docs/PROTOCOL.md` §3.1.
- **Creating** the bond (first time / after loss) needs the device in **pairing mode** (panel or power-cycle) — an unsolicited `createBond`/`bluetooth_device_pair` is rejected with `error 82`. This is the only step needing user action; it's driven by the config-flow `pair_confirm` step (`config_flow.py` + `bonding.py::async_seed_bond`), reachable via discovery/manual add, **reconfigure**, or an auto-surfaced **re-auth** Repairs issue. NEVER auto-clear/unpair a bond on the hot path (`_clear_cache_via_client` is dormant, manual-only) — that strands the unit (the reverted 0.1.6 regression). The bond is **per-proxy** (lives in the pairing proxy's NVS, not shared) — route Activate connections through that one proxy; don't run two *active* proxies for one unit.
- Firmware idle-drops the BLE link after ~36s. The keepalive checks every `HEARTBEAT_CHECK=5s` and sends a **fire-and-forget** `0x2B` once idle ≥ `KEEPALIVE_INTERVAL=20s` — a fixed-cadence sleep misses the window and the link drops. On an unexpected drop, `_on_disconnected` schedules `_eager_reconnect` (~2s back) instead of waiting out the backoff. `POLL_INTERVAL=25s` (the `0x2B` reply takes ~6s; 10s polls overlapped and cancelled each other → `CancelledError` drops). Connection params: `set_connection_params(24,60,0,2000)` = 30–75ms interval, **20s supervision** — a tight 15ms pin (the app's value) overloads the shared proxy radio and a short supervision drops the link mid-stall.
- Backoff: consecutive poll failures grow `update_interval` (×2 up to `BACKOFF_MAX=30s`) via `_note_failure`/`_note_success`, resetting on the first good poll. Eager reconnect-on-drop is the primary path back; backoff is just the floor for sustained failure.
- **Stuck-connection detection** (`STUCK_DISCONNECT_THRESHOLD=2`): if consecutive polls time out while `is_connected` is still True, the link is application-dead but ATT-alive — the proxy slot is jammed and nobody will drop it. After 2 such timeouts the coordinator force-disconnects (`_disconnect`) so the next poll reconnects fresh. This is the fix for the "every couple of days the connection dies and jams up the proxy" failure mode: without it, the dead-but-connected client blocks `_ensure_connected` from ever reconnecting, and the backoff just spreads the timeouts further apart without freeing the slot.
- **Address rotation in the poll path** (`_maybe_follow_address_rotation`): `_async_update_data` gates on `async_address_present(self.address)` *before* reaching `_connect`. If the Activate has rotated its BLE address, the old address is gone, the gate returns `available=False`, and the re-resolution logic in `_get_ble_device` is never reached — the integration dies permanently. `_maybe_follow_address_rotation` runs the name-id scan *before* the availability check, updates `self.address` in-place, and re-registers the BT callback on the new address.
- **500ms settle after `pair()`**: the BLE stack may return from `pair()` before the `LL_ENC_REQ`/`LL_ENC_RSP` handshake completes. A CCCD subscribe landing mid-handshake returns `Insufficient authentication` — indistinguishable from a missing bond. The 500ms sleep matches the official app's post-`createBond` delay (PROTOCOL.md §3 step 5).
- `_eager_reconnect` has a `_reconnecting` dedup guard so multiple rapid disconnects don't schedule overlapping reconnect tasks.
- `_run_brute_force_probe` is dev-only protocol-exploration scaffolding — not part of normal operation.

## Release flow
Manual: bump `manifest.json` `version`, commit `release: vX.Y.Z — …`, tag/GitHub release. Conventional-commit prefixes used (`fix(coordinator):`, `release:`). No automated pipeline.
