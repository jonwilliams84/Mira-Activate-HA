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
- **Resilient session** — one persistent BLE connection held alive by a
  keepalive, re-arming encryption from the stored SMP bond on every connect,
  eager reconnect on a dropped link, and per-entity optimistic state so toggles
  feel instant in HA. The SMP bond is never destroyed automatically — if it's
  genuinely lost, HA surfaces a guided re-pair step (see Setup).

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
   (or accept it from the discovery feed). Its name will be `MIRA <hex> <ROOM>`.
2. The setup walks you through a **pairing step**: put the shower into pairing
   mode at the panel (or power-cycle it so it's freshly pairable), then press
   **Submit**. HA establishes the SMP bond and verifies it before finishing.
3. The entities appear under the new device card.

The bond is created once and then reused. If it's ever lost (e.g. the proxy
holding it was reset), HA raises a **re-pair** prompt — open the device → ⋮ →
**Reconfigure**, put the shower in pairing mode, and Submit again. The bond
must be (re)created while the unit is pairable; an unsolicited pair attempt is
rejected by the firmware (`error 82`).

The bond lives in the NVS of the **one proxy** that paired it (ESPHome proxies
don't share bonds), so the shower must connect through that proxy. Keep that
proxy `active: true` and near the bathroom; scan-only proxies elsewhere are
fine. Don't run two *active* proxies for one Activate — it stores a single bond.

## Performance notes

End-to-end latency from HA toggle to water flowing is **~3-5 seconds**, of
which most is the shower's hardware valve actuation — not the integration.

HA-initiated changes (slider, toggles) appear **instantly** in the UI via
optimistic state. Panel-side changes (pressing a button on the shower itself)
reflect on the next successful poll, typically within the 25 s poll cycle. The
integration holds the BLE link open continuously (a keepalive defeats the
firmware's ~36 s idle-drop) and reconnects within ~2 s if it does drop, so
commands ride a live connection. The `0x2B` status read takes several seconds
to answer when the device is idle, which is why feedback is optimistic rather
than waiting on the device.

## How it works under the hood

The Activate uses a fully different wire format from Mira Mode:

- Frame: `[0xAA, 0x55, 0x00, opcode, body_len, payload, chk8]`
- Integrity: 8-bit 2's-complement checksum (no CRC-16)
- Control opcode: `0xAB` Set Temperature, byte 8 = bit-packed outlet bitmap
- Auth: LE bonding (SMP), no application-layer pair handshake. Over a proxy the
  bond's encryption is re-armed with `pair()` on every connect (the stored key
  isn't auto-applied) — see [`docs/PROTOCOL.md`](docs/PROTOCOL.md) §3.1
- Reads via `0x2B` Read Unit Prime, polled every ~25 s

The integration was reverse-engineered from the official `uk.co.mirashowers`
Android app combined with a live HCI snoop capture of the app driving the
shower. Detailed protocol writeup: [`docs/PROTOCOL.md`](docs/PROTOCOL.md).

## Troubleshooting

**A duplicate device keeps being "discovered."**
Fixed in 0.1.6. The Activate advertises under two name forms and older builds
keyed identity on each separately, so an address rotation looked like a new
unit. Update, restart HA, and it stops; any leftover duplicate device can be
deleted from its device page.

**`CCCD ... Insufficient authentication`, or the shower drops to unavailable.**
The Activate needs a bonded (encrypted) link. Two causes:
1. **The bond is genuinely lost** (the proxy holding it was reset/cleared). HA
   raises a re-pair prompt — open the device → ⋮ → **Reconfigure**, put the
   shower in pairing mode at the panel, and Submit. The integration never wipes
   the bond on its own (doing so strands the unit, since the firmware rejects an
   on-demand pair with `error 82`).
2. **The connection is routing through a proxy that doesn't hold the bond.**
   Make the one bonded proxy `active: true` and near the bathroom; set distant
   proxies scan-only so HA always connects via the bonded one.

If it still recurs with the bond intact, it's marginal RF — improve the proxy
placement/signal near the shower.

## Status

Alpha. Verified working on one 2-outlet ensuite Activate. Reports of
behaviour on other models, and PRs, welcome.

## License

MIT
