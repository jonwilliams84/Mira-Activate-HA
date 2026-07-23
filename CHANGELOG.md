# Changelog

All notable changes to this project are documented here. The format loosely
follows [Keep a Changelog](https://keepachangelog.com/); versions match
`custom_components/mira_activate/manifest.json` and the git tags.

## [0.1.19] - 2026-07-23

### Added
- **`proxy_wedged` repair issue — wedged-proxy vs lost-bond discrimination.**
  RCA of the 16–23 Jul 2026 outage: the shared ESPHome proxy crashed
  (`exception/panic`), rebooted, and came back with a wedged BLE stack —
  connects succeeded but `pair()` silently failed to bring encryption up, so
  every CCCD subscribe returned Insufficient authentication for 7 days. The
  existing `bond_lost` repair told the user to re-pair, which was wrong (and
  risky) — the bonds were intact the whole time; restarting the proxy fixed
  both units in minutes with zero re-pairing.
  The coordinator now tracks how long the CCCD-auth failure streak has run
  (`_auth_fail_since`). If it exceeds `PROXY_WEDGE_AFTER` (1 h) **while the
  device is still advertising and connectable**, it raises a distinct
  `proxy_wedged` repair ("restart the BLE proxy — do NOT re-pair") and
  withdraws `bond_lost`. Short-lived auth failures still raise `bond_lost`
  as before. Both issues clear on the first good poll.

## [0.1.18] - 2026-06-20

Fixes the "every couple of days the connection dies and jams up the proxy"
failure mode. Two root causes found and fixed, plus a hardening pass on the
reconnect logic.

### Fixed
- **Stuck-connection jamming the proxy slot.** The BLE link could go
  ATT-alive (`is_connected == True`) but application-dead (the Mira firmware
  stops responding to `0x2B` polls). The keepalive's fire-and-forget write
  succeeds at the ATT layer regardless, so nobody detected the stall. The
  proxy slot was held hostage by a dead connection indefinitely. After 2
  consecutive poll timeouts while `is_connected`, the coordinator now
  force-disconnects so the next poll reconnects fresh.
- **Address rotation kills the poll path permanently.** The re-resolution
  logic in `_get_ble_device` was correct but unreachable: `_async_update_data`
  gated on `async_address_present(self.address)` *before* `_connect` was
  called, so when the Activate rotated its BLE address (power-cycle), the old
  address was gone, the gate returned False, and the integration died
  permanently. A new `_maybe_follow_address_rotation` method now runs the
  name-id scan *before* the availability check, updates `self.address`
  in-place, and re-registers the BT callback on the new address.
- **500ms settle after `pair()` before CCCD subscribe.** The BLE stack can
  return from `pair()` before the `LL_ENC_REQ`/`LL_ENC_RSP` handshake
  completes. A subscribe landing mid-handshake returns `Insufficient
  authentication` — indistinguishable from a missing bond. Matches the
  official app's post-`createBond` delay.
- **Eager-reconnect dedup.** Multiple rapid disconnects could schedule
  overlapping `_eager_reconnect` tasks. A `_reconnecting` guard now prevents
  this.

## [0.1.17] - 2026-06-05

A ground-up fix of the Activate's BLE auth, connection stability, and command
latency, after a fresh reverse-engineering pass over the APK and the HCI capture.
The headline discovery corrects a long-standing wrong assumption in our own docs:
**over an ESPHome Bluetooth proxy the cached SMP bond's key is _not_ applied
automatically on a new connection — you must `pair()` on every connect to
re-arm encryption, or the CCCD subscribe fails with Insufficient authentication.**

### Fixed
- **CCCD `Insufficient authentication` even with a valid bond.** The coordinator
  now calls `client.pair()` immediately before subscribing the notify CCCD on
  every connect. Over a proxy the stored LTK is dormant until pairing is
  (re)triggered; when the bond already exists this just starts encryption — no
  re-bond, no pairing mode, no user action. This is the opposite of the old
  "skip pair() on the happy path" advice, which was wrong for the proxy path.
- **Self-inflicted bond destruction.** Removed the automatic `pair()`/bond-cache-
  clear "recovery" from the hot path entirely. A CCCD auth failure now disconnects
  and retries the *connection* — it never touches the SMP bond. (The destructive
  loop is the same class of bug as the reverted 0.1.6 regression, but it had crept
  back into the auth-failure path and was wiping the bond on a loop.)
- **The link dropped every ~35 s, making commands take up to a minute.** The
  keepalive slept a fixed interval between checks, so it kept missing the firmware's
  ~36 s idle-drop window. It now checks every 5 s and pings (fire-and-forget) the
  moment the link has been idle ~20 s, so the link is held continuously.
- **Periodic mid-poll link drops.** Supervision timeout raised to 20 s
  (`CONN_TIMEOUT=2000`); the Activate stalls (>10 s with no `0x2B` reply) when
  idle and a short supervision tore the link down during the stall.
- **Slow recovery after a drop.** Added eager reconnect: on an unexpected
  disconnect the coordinator reconnects within ~2 s instead of waiting out the
  poll backoff (which could be tens of seconds).
- **"Button press takes several seconds to show."** User commands now push
  optimistic state to the entities the instant the write is sent, then reconcile
  against device truth on the next poll — the UI no longer waits on the device's
  slow `0x2B` confirm.
- **Polls overlapping and cancelling each other** (`CancelledError` drops). Poll
  interval raised 10 s → 25 s; the `0x2B` reply takes ~6 s, so a 10 s cadence made
  the next poll cancel the in-flight one.

### Added
- **Bonding lives in the config flow.** A new guided `pair_confirm` step
  ("put the shower into pairing mode, then Submit") seeds/repairs the SMP bond
  from inside HA's bluetooth stack — discovery, manual add, **re-auth** (auto-
  surfaced when the bond is lost, via a Repairs issue) and **reconfigure** all
  funnel through it. New `bonding.py::async_seed_bond` does the connect → `pair()`
  → CCCD-subscribe verification, unloading the entry first so the coordinator
  releases the proxy's connection slot.
- **`translations/en.json`** — HA renders config-flow text from here, not
  `strings.json`; without it the pairing step rendered as a blank form.

### Changed
- Connection interval loosened from a pinned 15 ms to 30–75 ms
  (`FAST_MIN/MAX_INTERVAL`). The app's 15 ms (from the HCI capture) overloads a
  proxy radio shared with the Mira Mode unit and presence scanning; a
  fire-and-return command's write goes out within one interval regardless, so
  30–75 ms is imperceptible and far more stable. Stability beats raw interval.
- Poll backoff cap lowered 120 s → 30 s; eager reconnect is now the primary path
  back after a drop.
- Bond verification accepts a successful CCCD subscribe as proof of the bond
  (plus a best-effort `0x2B` round-trip), rather than requiring an exact `0x2B`
  opcode echo — the device legitimately answers with an unsolicited status frame
  first, which a strict check wrongly rejected.

## [0.1.7] - 2026-06-04

### Fixed
- **Reverted the 0.1.6 "clear bond across all proxies" recovery step — it
  stranded the unit.** The SMP bond is precious and the Activate rejects a
  fresh `pair()` on demand (`error 82`), so wiping the working bond on every
  proxy left the device unable to re-bond and stuck on CCCD `Insufficient
  authorization`. Recovery again clears the bond cache on the *connected*
  proxy only. The genuinely useful 0.1.6 changes (duplicate-discovery fix,
  identity self-heal, entity-uid stabilisation, poll backoff) are unaffected.

## [0.1.6] - 2026-06-04

### Fixed
- **Duplicate device repeatedly "discovered."** The Activate advertises under
  two local-name forms (firmware `Mira <model>#<serial>` and a user-set name);
  `device_id_from_name` now collapses both to the stable model id, so HA dedupes
  on the name-id instead of offering a brand-new device on every BLE-address
  rotation.
- **Legacy entries self-heal on startup.** Entries created before identity was
  keyed on the name-id get their config-entry `unique_id` set, and their
  `<MAC>_<suffix>` entity unique_ids migrated to `<device_id>_<suffix>` — no
  re-add, no orphaned entities.
- **`CCCD ... Insufficient authorization` recovery churn.** ESPHome proxies do
  not share bonds, so a connection routed through a proxy without the bond
  looped forever. Recovery now clears the device bond on *every* proxy, not just
  the connected one, so it self-heals.

### Changed
- Entity unique_ids are keyed on the stable name-id (`coordinator.unique_base`)
  instead of the rotating BLE address.
- Added poll backoff: the interval doubles on consecutive failures up to 120 s
  and resets on the first good poll, so a marginal RF link no longer drives a
  10 s reconnect storm or floods the log.

## [0.1.5] - 2026-06-03
### Changed
- User commands are fire-and-return — no longer block on the app-level ack — so
  outlet/temperature changes feel instant (#7).

## [0.1.4] - 2026-06-03
### Fixed
- Follow the Activate's BLE address rotation; quieter routine logging.

## [0.1.3] - 2026-06-02
### Fixed
- Keepalive holds the BLE link inside the firmware idle-drop window; recovery
  link-verify now actually probes the link (#4).

## [0.1.2] - 2026-06-02
### Fixed
- Hardened the discovery name filter (#5).

## [0.1.1] - 2026-05-30
- Release.

## [0.1.0] - 2026-05-30
- Initial release.
