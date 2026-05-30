# Mira Activate for Home Assistant

A custom Home Assistant integration for controlling **Mira Activate** digital
showers via Bluetooth Low Energy (BLE).

Works through ESPHome Bluetooth proxies — no USB dongle or direct BLE from
the HA host required.

> Sister integration to [`mira-ha`](https://github.com/jonwilliams84/mira-ha)
> (Mira Mode). The two products are different at the byte level so each has
> its own integration. See [`docs/`](docs/) if you want the protocol
> reverse-engineering writeup.

## Features

- **BLE control via ESPHome proxies** — uses HA's Bluetooth integration to
  route through your existing ESPHome BLE proxy mesh
- **Auto-discovery** — devices appear in HA's discovery feed once they
  advertise the Activate service UUID
- **Outlet switches** — Rain Head and Handheld, independent on/off
- **Target temperature** — slider 20–48 °C, 0.5 °C step
- **Flow rate** — slider 0–16 L/min, 0.25 L/min step (device cap)
- **State sensors** — measured water temp, target temp, current flow, raw
  outlet/status bytes for automation use
- **Binary sensors** — Running, Paused, Error, Session Ready
- **Resilient session** — automatic LE re-bond on auth failure, proxy bond
  cache cleared when stale, per-entity optimistic state so toggles feel
  instant in HA

## Supported Devices

- Mira Activate digital shower (verified on a 2-outlet ensuite unit
  advertising as `MIRA <hex> <ROOM>`)

Any device advertising BLE service UUID `267f0001-eb15-43f5-94c3-67d2221188f7`
should work.

## Requirements

- Home Assistant 2024.1 or later
- Bluetooth reachable to HA — typically an ESPHome BT proxy within range of
  the shower
- ESPHome ≥ 2024.4 on the proxy (older firmware may not support the
  `bluetooth_device_pair` API used for the SMP bond)

## Installation

### HACS (Custom Repository)

1. HACS → ⋮ → Custom repositories
2. Add `https://github.com/jonwilliams84/Mira-Activate-HA` as type
   *Integration*
3. Install **Mira Activate Shower**
4. Restart Home Assistant

### Manual

1. Copy `custom_components/mira_activate/` into `<config>/custom_components/`
2. Restart Home Assistant

## Setup

1. Settings → Devices & Services → Add Integration → **Mira Activate Shower**
2. The device should appear in the discovered list (its name will be
   `MIRA <hex> <ROOM>`). Pick it.
3. Home Assistant will trigger an LE bond. Accept any system prompts.
4. The entities appear under the new device card.

If the device is not discovered automatically, make sure the shower is
advertising (touch the panel or put it briefly into pair mode) and that
your BT proxy is in range.

## Performance notes

End-to-end latency from HA toggle to water flowing is **~3-5 seconds**, of
which most is the shower's hardware valve actuation — not the integration.

Panel-side state (when you press a button on the shower itself) reflects
in HA on the next successful poll, typically within 10-30 seconds. The
Activate's firmware drops the BLE link after ~36 s of idle and responds
to polls only on a roughly 30 s cadence over a proxy, so faster polling
doesn't help — failed polls just time out without changing perceived
responsiveness. The defaults (10 s poll, 10 s op timeout) are the sweet
spot.

## How it works under the hood

The Activate uses a fully different wire format from Mira Mode:

- Frame: `[0xAA, 0x55, 0x00, opcode, body_len, payload, chk8]`
- Integrity: 8-bit 2's-complement checksum (no CRC-16)
- Control opcode: `0xAB` Set Temperature, byte 8 = bit-packed outlet bitmap
- Auth: LE bonding (SMP), no application-layer pair handshake
- Reads via `0x2B` Read Unit Prime, polled every ~10 s

The integration was reverse-engineered from the official `uk.co.mirashowers`
Android app combined with a live HCI snoop capture of the app driving the
shower. Detailed protocol writeup: [`docs/PROTOCOL.md`](docs/PROTOCOL.md),
[`docs/SESSION_ESTABLISHMENT.md`](docs/SESSION_ESTABLISHMENT.md),
[`docs/CORRECTIONS.md`](docs/CORRECTIONS.md) (what the static decode got
wrong vs. what the live capture proved).

## Status

Alpha. Verified working on one 2-outlet ensuite Activate. Reports of
behaviour on other models, and PRs, welcome.

## License

MIT
