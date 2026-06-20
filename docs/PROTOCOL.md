# Mira Activate BLE protocol

This is the working spec for the Activate's BLE protocol. It's the basis
of the integration in this repo. Every byte-level claim is backed by a
live HCI snoop capture taken while the official `uk.co.mirashowers` v5.6
Android app was actively driving the shower (`mira_postapp_btsnoop.log`,
2026-05-30; 2184 packets, no truncated ACL payloads). The decompiled APK
was used to identify *what* the bytes mean semantically — variable names,
opcode catalogue, session flow — but where the static decode disagrees
with the live capture, the live capture wins.

The product covered here is the Mira Activate digital shower (service
UUID `267f…`). The unrelated Mira Mode shower uses a different wire
format under `bccb…`; see [`mira-ha`](https://github.com/jonwilliams84/mira-ha).

## 1. GATT layout

The Activate exposes three services. Only one carries the application
protocol; the other two are standard Bluetooth boilerplate.

| Service UUID | Purpose |
|---|---|
| `267f0001-eb15-43f5-94c3-67d2221188f7` | Mira application protocol |
| `00001800-0000-1000-8000-00805f9b34fb` | Generic Access |
| `00001801-0000-1000-8000-00805f9b34fb` | Generic Attribute |
| `0000fe59-0000-1000-8000-00805f9b34fb` | Nordic Secure DFU (firmware update, not used at runtime) |

Within the Mira service:

| Characteristic UUID | Properties | Role |
|---|---|---|
| `267f0002-…` | Write, Write-no-response | Commands to the device |
| `267f0003-…` | Notify (+ CCCD `0x2902`) | Responses + asynchronous status |

The device advertises a local name of the form `MIRA <hex> <ROOM>` (e.g.
`MIRA 003F ENSUITE`). It uses a static random address that is stable while
powered, but the device **regenerates it on a power-cycle** (and the unit also
advertises under two name forms — the firmware `Mira <model>#<serial>` and the
user-set room name). Identity must therefore key on the stable model id parsed
from the name (`device_id_from_name` → e.g. `003F`), never the BLE address;
otherwise every power-cycle or name form looks like a brand-new device. The
integration follows the address rotation by re-resolving the current address
for that name id.

## 2. Wire framing

Every command and every response uses the same envelope:

```
+------+------+------+--------+----------+--- payload ---+----------+
| 0xAA | 0x55 | rsvd | opcode | body_len |   body_len B  |   chk8   |
+------+------+------+--------+----------+---------------+----------+
   0      1      2       3         4        5 … 4+L         5+L
```

- Bytes 0–1: fixed sync (`0xAA 0x55`).
- Byte 2 (`rsvd`): zero on requests. On responses the device sets this
  to `0x01` for every command, regardless of the request opcode. It is
  not an opcode echo.
- Byte 3 (`opcode`): the application opcode.
- Byte 4 (`body_len`): length of the body in bytes (0–251).
- Body: `body_len` bytes.
- Last byte (`chk8`): 2's-complement byte sum. The whole frame including
  `chk8` sums to `0 mod 256`. Equivalently, `chk8 = (~sum(prev_bytes) + 1) & 0xFF`.

There is no CRC, no slot byte, no per-request counter, and no chunking.
Frames fit inside a single GATT write or notification (the longest the
app sends is 24 bytes for `0xC0`; the longest response is 24 bytes for
`0x2B`).

## 3. Session establishment

Activate's app-layer "auth" is just an LE bond. There is no key exchange,
identity registration, or `0xEB`-style pair opcode (that's a Mode artefact;
the Activate dispatcher's pair branch is an explicit no-op).

The official app's flow from `connectGatt(...)` to the first useful read:

1. **GATT connect**, transport `TRANSPORT_LE`, no MTU exchange.
2. **Discover services**, locate `267f0001`.
3. **Enable notifications** on `267f0003` by writing `0x0001 0x0000` to its
   CCCD descriptor.
4. **`device.createBond()`** if the device isn't already bonded. Wait for
   the `BOND_STATE_CHANGED → BOND_BONDED` broadcast.
5. Sleep ~500 ms.
6. Begin polling `0x2B Read unit prime data` once per app tick.

Without the bond, the device returns GATT error `5` (Insufficient
authentication) on *any* write — including the CCCD descriptor — and
drops the link.

### 3.1 Bonding over an ESPHome proxy — the part that bit us

The single most important, hard-won finding (it cost a full debugging session
and contradicts what an earlier draft of this doc claimed):

> **On a fresh connection through an ESPHome Bluetooth proxy, the stored SMP
> bond is _not_ applied automatically. The proxy connects unencrypted, and the
> CCCD subscribe then fails with GATT error 5 — even though a valid bond exists
> in the proxy's NVS. You must call `pair()` (→ `aioesphomeapi
> bluetooth_device_pair`) _before_ the CCCD subscribe on _every_ connect to
> re-arm encryption from the stored key.**

This is why the official app appears to "just ride the bond": the phone's own
BLE stack keeps the LE encryption active on its single held link. A proxy makes
a brand-new GATT connection each time, and each one needs encryption
re-triggered. So:

- **Creating the bond** (first ever pair, or after it's lost): the device must be
  in **pairing mode** — put it there at the panel or power-cycle it. A
  central-initiated `createBond` / `bluetooth_device_pair` while it is *not*
  pairable is rejected with **error 82**. This is the only step that needs user
  action, and it's what the integration's config-flow `pair_confirm` step drives.
- **Activating an existing bond** (every reconnect): `pair()` with the cached
  LTK present just starts encryption — it does **not** re-bond, need pairing
  mode, or prompt anything. Cheap and silent. Do it on every connect.

The bond is **per-proxy**: it lives in the NVS of whichever proxy performed the
pairing. ESPHome proxies do not share bonds, so all connections to the Activate
must be routed through that one bonded proxy. (Most peripherals, the Activate
included, also store only one bond, so pairing a second proxy would evict the
first — don't run two *active* proxies for one Activate; scan-only proxies are
fine.)

### 3.2 What the HCI capture actually proves (and doesn't)

The capture analysed for this work was **steady-state** — the connection was
already up and encrypted, and the snoop ring had wrapped past the original
pairing. So it contains **no SMP packets and no Encryption-Change events**: the
Just-Works / no-MITM nature of the bond is **inferred from the APK** (it calls
only `createBond()`, has no passkey/IO-capability code, and the device is
display-less), **not wire-proven**. What the wire *does* prove is the
connection-management shape, which drove the tuning in §7:

- The app holds **one persistent connection** for the whole session; it never
  reconnects per command.
- It modulates the **connection interval**: ~4 s when idle, **15 ms** when
  actively talking (via `requestConnectionPriority(HIGH)`). At 15 ms,
  write→notify round-trips are 24–65 ms. The "multi-second" feel comes only from
  a request landing during the 4 s idle window.
- No MTU exchange; no reads of Generic Access/Attribute characteristics.

## 4. Control: opcode `0xAB` (Set Temperature / Operate)

This is the only command the integration needs to drive the shower. One
opcode handles temperature, flow, outlet on/off, and pause.

Frame: `AA 55 00 AB 04 [flags|tempMS] [tempLS] [flow] [outlets] [chk8]`

| Byte | Field | Meaning |
|---|---|---|
| 5 | `flags \| tempMS` | OR of flags (high bits) and the high byte of `target_temp × 10` (bit 0 only). |
| 6 | `tempLS` | Low byte of `target_temp × 10`. Target temperature in °C × 10 is a 9-bit value split as `((flags|tempMS) & 0x01) << 8 \| tempLS`. |
| 7 | `flow` | Flow rate × 4 (LPM × 4). The device caps at 16 L/min, so `0x40` = 64 is the maximum the official app sends. |
| 8 | `outlets` | Bit-packed outlet state — see below. |

The `flags` value is `0` for normal operation. `flags=1` is the app's
"force stop / clear all outlets" idiom; it pairs with `temp=0`, `flow=0`,
`outlets=0` and turns the shower off cleanly. No other flag bits were
observed in the capture.

The outlet byte is a bitmap:

| Bit | Mask | Meaning |
|---|---|---|
| 0 | `0x01` | Outlet A on |
| 1 | `0x02` | Outlet B on |
| 6 | `0x40` | Pause |
| 7 | `0x80` | Error (device-set, read-only in responses) |

Bits 2–5 are unused. **Sending bit 2 (`0x04`) does not turn on a "third
outlet" — it makes the device silently ignore the entire write** (no GATT
error, no state change in the next poll). On a typical 2-outlet shower
the mapping is: bit 1 (`0x02`) is the head/main outlet, bit 0 (`0x01`)
is the secondary (handheld / bath). The integration exposes them as
"Rain Head" (bit 1) and "Handheld" (bit 0); confirm against your unit
the first time you toggle them.

`0x03` (both bits) turns both outlets on simultaneously. `0x40` alone
pauses whichever outlet was running. Returning to a non-zero outlet
bit while paused resumes.

Example frames seen in the live capture:

| Frame | Decoded |
|---|---|
| `AA 55 00 AB 04 01 7C 40 02 93` | 38.0 °C, 16 L/min, outlet B on |
| `AA 55 00 AB 04 01 86 20 02 A9` | 39.0 °C, 8 L/min, outlet B on |
| `AA 55 00 AB 04 01 7C 40 03 92` | 38.0 °C, 16 L/min, both outlets on |
| `AA 55 00 AB 04 01 7C 40 40 55` | 38.0 °C, 16 L/min, pause |
| `AA 55 00 AB 04 00 00 00 00 06` | Stop everything (clean off) |

The device acknowledges each `0xAB` with a one-byte `01` payload (under
the `rsvd=01` response envelope). The new outlet state appears in the
next `0x2B` poll response — typically within the next polling cycle.

## 5. Status: opcode `0x2B` (Read unit prime data)

This is the only read the integration uses at runtime. Request body is
the single byte `0x02` (hardcoded in the official app); the device
responds with 18 payload bytes.

Request: `AA 55 00 2B 01 02 D3`
Response: `AA 55 00 01 12 [18-byte payload] [chk8]`

The 18-byte payload structure:

| Byte | Field | Notes |
|---|---|---|
| 0 | `status0` | Bit 6 (`0x40`) flips on after the first successful poll of a fresh session — call it `session_ready`. Other bits don't drive the integration. |
| 1 | reserved (`0x20` in every capture) | Possibly a fixed body marker. |
| 2–7 | reserved | Zero in every capture. |
| 8 | `iot_status` | IOT module / cloud connectivity state. `0x90` (only bit 4 set) is the cold/idle value. **Actuation does not depend on this** — the smali method that decodes it to a status code has no callers on the actuation path. The integration parses it for diagnostics and ignores it for decisions. |
| 9 | reserved | Zero. |
| 10–11 | `target_temp × 10` | 9-bit BE: high bit lives in byte 10 (mask `0x01`), low byte in 11. e.g. `01 7C` → 380 → 38.0 °C. |
| 12 | `flow` | Same scale as the write side: LPM × 4. `0x40` → 16 L/min. |
| 13 | `outlets` | Mirror of the write-side bit map (§4). Bit 0 = outlet A, bit 1 = outlet B, bit 6 = pause, bit 7 = error. |
| 14–15 | `measured_temp × 8.5` | BE 16-bit. Divide by 8.5 for the probe reading in °C. `01 0A` → 266 / 8.5 ≈ 31.3 °C. |
| 16 | counter | Slow tick (≈1 increment per minute of idle). Not actionable. |
| 17 | reserved (`0x01`) | Constant in every capture. |

This is the entire device state. There are no extra reads needed for the
basic operate-the-shower use case.

## 6. Other opcodes the app uses

The official app fires a longer pre-flight read sequence after bonding
before its first `0xAB`. None of it is required for actuation — the
integration in this repo skips the lot and goes straight from CCCD
enable to `0x2B` polls plus `0xAB` writes, and the shower responds. They
are listed here in case a future model rejects writes without seeing the
prep run first.

| Opcode | Body | Purpose |
|---|---|---|
| `0x1A` | empty | Read GCS valve config |
| `0x1B` | `00` (outlet index) | Read outlet config |
| `0x32` | `02` | Read warm-up data (extended) |
| `0x3C` | empty | Read warm-up data (normal) |
| `0x40` | `01` | Read firmware version (extended) |
| `0x41` | empty | Read date of manufacture |
| `0x44` | empty | Read interface / unit name |
| `0x5A`/`5B` | index | Error log read |
| `0x5C` | `02 XX` | GCS usage log read |
| `0x5D` | index | Last-usage read |
| `0x5F` | `01 01 02` | GCS preset read |
| `0xB1` | index | Activate memory read (preset slots) |
| `0xC0` | `"9000" + yyMMddHHmmss + "\0\0"` (18 B) | Read bathroom name / outlet serial. Response is the bathroom name string the user set during commissioning, terminated with NULs. |
| `0xC4` | name string (≤16 B) | Write bathroom name |
| `0xF4` | empty | Extended reset valve |

These all use the framing in §2 and get the same `rsvd=01`-tagged
response envelope. Where the body shape isn't obvious, the live capture
has examples.

## 7. Implementation notes

A correct minimal driver only needs the two opcodes above plus the bond
flow. Things that cost time during development and are worth knowing:

- **`pair()` on _every_ connect, before the CCCD subscribe** — see §3.1. The
  cached bond is dormant on a fresh proxy connection; `pair()` re-arms encryption
  and is cheap/silent when the bond already exists. (An earlier version of this
  doc said the opposite; that was the single biggest time-sink.) Make it
  tolerant: if it raises, still attempt the CCCD subscribe — the subscribe
  succeeding is the real proof the bond is live.
- **Set a _long_ supervision timeout, not the proxy default.** The Activate
  stalls (>10 s with no `0x2B` reply) when idle; a short supervision timeout
  (the old `400` = 4 s, and arguably the proxy default) tears the link down
  mid-stall. We pin `min=24, max=60, latency=0, timeout=2000` (30–75 ms interval,
  20 s supervision). Do **not** pin the app's 15 ms interval over a proxy — it
  overloads a radio shared with other devices/scanning and destabilises the link
  for no real gain (a fire-and-return write goes out within one interval anyway).
- **Keep the link alive with a frequent-check keepalive.** The firmware idle-
  drops after ~36 s. Check every ~5 s and send a fire-and-forget `0x2B` once the
  link has been idle ~20 s. A keepalive that *sleeps* a fixed ~20 s between checks
  will miss the window (a check landing just under the threshold pushes the next
  one past 36 s) and the link drops.
- **Reconnect eagerly on an unexpected drop** rather than waiting out the poll
  backoff — the device/proxy will drop the link occasionally no matter what, so
  make the drop invisible (~2 s back) instead of trying to eliminate it.
- **Detect stuck connections and force-disconnect.** The most insidious failure
  mode is a link that is ATT-alive (`is_connected == True`) but application-dead
  (the device stops responding to `0x2B` polls). The keepalive's fire-and-forget
  write succeeds at the ATT layer regardless, and `_ensure_connected` sees
  `is_connected == True` so it never reconnects. The proxy slot is jammed
  indefinitely. After 2 consecutive poll timeouts while `is_connected`, the
  coordinator force-disconnects the client so the next poll reconnects fresh.
  This is the fix for the "every couple of days the connection dies and jams up
  the proxy" symptom.
- **Handle BLE address rotation in the poll path, not just the connection path.**
  The re-resolution logic in `_get_ble_device` follows an address rotation by
  name-id, but `_async_update_data` gates on `async_address_present(self.address)`
  *before* `_connect` is ever called. With the old address, the gate returns
  false and the re-resolution is unreachable — the integration dies permanently
  on a power-cycle. Run the name-id scan *before* the availability check, update
  `self.address` in-place, and re-register the BT callback on the new address.
- **`outlets = 0x04` does nothing**. Bit 2 is not a valid outlet bit and
  sending it makes the device ignore the whole write. Use bits 0/1 only.
- **Echo the device's `flow` byte back on writes** unless the user has
  explicitly changed the slider. The official app sends `flow = byte 12
  from the last 0x2B response` whenever it's just adjusting temperature.
- **Notifications can arrive 4–8 s after the write**. Don't set the per-op
  timeout below 10 s or you'll false-negative successful writes.
- **Poll no faster than the `0x2B` round-trip.** The reply takes ~6 s, so a poll
  interval ≤ the round-trip makes successive polls overlap and cancel each other
  (`CancelledError`, which then drops the link). The integration uses a 25 s poll
  with a 10 s op timeout.
- **Use optimistic state for HA-initiated changes.** The `0x2B` confirm is
  several seconds out, so push the commanded value to the entities the instant
  the write is sent and reconcile on the next poll — otherwise every button press
  appears to lag by seconds. Panel-side changes still reflect within a poll cycle.

## 8. Reproducing the capture

To repeat the capture against a different model or firmware:

1. Enable Bluetooth HCI snoop log in Android Developer Options (full,
   not the truncated `btsnooz` default).
2. Reboot the phone so the snoop log starts clean.
3. Open the Mira app and drive the shower through the operations you
   want documented.
4. `adb bugreport bugreport.zip`, then extract
   `FS/data/misc/bluetooth/logs/btsnoop_hci.log`.
5. Filter ATT writes on the connection handle for the Activate's MAC.
   The protocol is fully visible at that layer.

If the Mira app crashes on launch with `java.lang.NullPointerException`
in `h4.i4.B0`, it's because of an unrelated weather widget that NPEs
when `api.weatherapi.com` returns a null temperature field. Locally mock
the endpoint and DNS-override `api.weatherapi.com` to the mock; the app
will launch normally. (The minimum JSON shape is documented in the build
history at commit `f1d1216`.)
