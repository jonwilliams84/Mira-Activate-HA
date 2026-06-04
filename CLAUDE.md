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
- User commands are **fire-and-return** (`wait_response=False`): the valve reacts on the write; the notification is only an app-level ack. Do not make toggles await it — that was the multi-second lag. State catches up on the next poll (`async_request_refresh`).
- Firmware idle-drops the BLE link after ~36s; `KEEPALIVE_INTERVAL=22s` holds it. Defaults (`POLL_INTERVAL=10s`, `OP_TIMEOUT=10s`) are tuned — faster polling does not help.
- Bond recovery: stale bonds can pass `start_notify` (CCCD subscribe) yet fail real GATT ops, so `_verify_link` probes with a real write before trusting the link; escalates to `pair()` then proxy bond-cache clear. ESPHome proxies do NOT share bonds — a bond made via proxy A is invisible to proxy B, so `_clear_bonds_all_proxies` unpairs/clears the device on EVERY esphome APIClient (not just the connected one) during recovery. Persistent CCCD `Insufficient authorization` is usually the connection bouncing to a proxy without the bond, and/or marginal RF dropping the link mid-pairing (`error 22`) — the real fix is a single well-placed active-connection proxy near the unit.
- Backoff: consecutive poll failures grow `update_interval` (×2 up to `BACKOFF_MAX=120s`) via `_note_failure`/`_note_success`, so a dead/marginal link stops the 10s reconnect churn from hammering the proxy and flooding the log; resets on the first good poll.
- `_run_brute_force_probe` is dev-only protocol-exploration scaffolding — not part of normal operation.

## Release flow
Manual: bump `manifest.json` `version`, commit `release: vX.Y.Z — …`, tag/GitHub release. Conventional-commit prefixes used (`fix(coordinator):`, `release:`). No automated pipeline.
