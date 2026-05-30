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
`MIRA 003F ENSUITE`). It uses a non-resolvable random address that does
not rotate.

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
drops the link. With the bond in place, writes are accepted by the device
without any further app-level handshake.

The official app fires `requestConnectionPriority(HIGH)` immediately
after bonding. Over a 2.4 GHz noisy ESPHome BT proxy link, mirroring
that with `bluetooth_device_set_connection_params(min=6, max=10,
latency=0, timeout=400)` breaks things — `timeout=400` is a 4 s
supervision timeout, shorter than the device's natural ~30 s response
cadence, so the link drops on every poll. Leave conn params at the
proxy's defaults.

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

- **Don't request aggressive `bluetooth_device_set_connection_params`**.
  Anything with `supervision_timeout < 5 s` makes the link drop on every
  poll cycle. The proxy's default conn params work; leave them alone.
- **Skip `client.pair()` on the happy path**. Mira's bond persists on the
  proxy side; calling `pair()` every reconnect costs 1–2 s. Try the
  CCCD descriptor write first; only run a full pair flow if it returns
  GATT error 5 (Insufficient authentication).
- **`outlets = 0x04` does nothing**. Bit 2 is not a valid outlet bit and
  sending it makes the device ignore the whole write. Use bits 0/1 only.
- **Echo the device's `flow` byte back on writes** unless the user has
  explicitly changed the slider. The official app sends `flow = byte 12
  from the last 0x2B response` whenever it's just adjusting temperature.
- **Notifications can arrive 4–8 s after the write**. Don't set the per-op
  timeout below 10 s or you'll false-negative successful writes.
- **Polling cadence has a hard ceiling around 10 s over a BT proxy.** The
  device only responds to `0x2B` reliably in ~30 s windows, and faster
  polls just time out in the gaps without changing perceived
  responsiveness. The integration uses 10 s poll, 10 s op timeout. Panel-
  side state changes reflect within 10–30 s; HA-initiated changes use
  per-entity optimistic state and feel instantaneous in the UI even
  though the BLE write is still a few seconds out.

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
