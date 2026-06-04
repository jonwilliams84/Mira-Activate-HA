# Changelog

All notable changes to this project are documented here. The format loosely
follows [Keep a Changelog](https://keepachangelog.com/); versions match
`custom_components/mira_activate/manifest.json` and the git tags.

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
