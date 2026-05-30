# Corrections to the static APK decode

The protocol writeup in `PROTOCOL.md` and `AB_2B_DECODE.md` was derived from
static analysis of the `uk.co.mirashowers` v5.6 APK. A live HCI snoop capture
on 2026-05-30, taken while the official Android app was actively driving the
shower, contradicted three of the byte-level claims. This document is the
record of what was wrong and what's actually correct.

If you're implementing against this protocol, **trust this document over
the static decode** for the items listed below. The framing, chk8, opcode
table, and bond-flow descriptions are still correct.

## 1. `0xAB` outlet byte (byte 8) bit positions

**Static decode said:** bit 0 = outlet 0, bit 1 = outlet 2 (the 3rd), bit 2 =
outlet 1 (head/main). `r()` setter targeting bit 2 was claimed to be the
"head" outlet.

**Live capture shows:**

| byte 8 value | meaning |
|---|---|
| `0x01` | one outlet on (bit 0) |
| `0x02` | the other outlet on (bit 1) |
| `0x03` | both on |
| `0x40` | pause (bit 6) |
| `0x00` | all off |

**Bit 2 (`0x04`) is never used by the official app.** Sending it makes the
device silently ignore the entire write — no GATT error, but the outlet
state in the next `0x2B` response doesn't change.

For a typical 2-outlet shower, bits 0 and 1 are the only ones you'll see.
On the test unit (Mira Activate ensuite, rain head + handheld),
bit 1 (`0x02`) drove the rain head; bit 0 (`0x01`) is inferred to be the
handheld.

## 2. `0xAB` byte 7 (flow) scaling

**Static decode said:** `flow_lpm × 4`, range 0..100 raw.

**Live capture shows:** `flow_lpm × 4`, but the device cap is **16 L/min =
64 raw**, not 100. The app's UI maxes out at 16 L/min on the test unit
(this may vary by model — check what your shower's panel advertises).

The `0x2B` response byte 12 reports flow on the same scale (LPM × 4), not
LPM × 3 as one earlier draft of `AB_2B_DECODE.md` suggested.

## 3. `iot_status` (payload byte 8 of `0x2B`) is not an actuation gate

**Static decode said:** the device gates actuation on
`Lh5/a;->g() == 2`, i.e. an IOT-module-status decode that requires bit 5
of byte 8 set and bit 4 clear. The implication: writes to a device with
`iot=0x90` (bit 4 only, decoded as 1) would be silently ignored.

**Live capture shows:** the gating method `Lf5/a;->g()` has **zero
xrefs** in the rest of the app — nothing in the actuation path actually
calls it. The test capture ran with `iot=0x90` throughout and the device
happily actuated outlets on every `0xAB` write.

This was the most expensive red herring of the day. If your writes are
syntactically correct but the device doesn't react, the problem is byte 8
(outlet bit positions, item 1 above), not the IOT module status.

## 4. Response opcode echo

**Static decode said (implicitly):** the response frame echoes the request
opcode at byte 3, in line with most BLE protocols.

**Live capture shows:** the device returns ALL command responses with
frame byte 3 set to `0x01`, regardless of the requesting opcode. Writes
get a short 1-byte payload (`0x01` = ack). Polls (`0x2B`) get an 18-byte
status payload, still under outer opcode `0x01`.

This doesn't change wire-correctness — the chk8 still validates — but
parsers that route notifications by opcode echo need to either ignore
byte 3 or distinguish by payload length (1 byte = write ack,
18 bytes = status update).

## 5. Session establishment is shorter than the app does

The app, on first connect, sends a long pre-write sequence: `0x44` read
iface name → `0xC0` read bathroom name → `0xC4` write bathroom name →
`0x32` read warm-up → `0x1A` GCS valve config → `0x40` fw version → `0x1B`
outlet config → `0x5F` GCS preset. Only then does it start `0xAB`
writes.

This integration **skips all of that** and goes straight from CCCD enable
+ LE bond to `0x2B` polls and `0xAB` writes. On the test unit this Just
Works. If another model rejects writes before the prep sequence runs,
fire those reads once on first connect and ignore the responses.

## 6. Performance ceiling over a BT proxy

Polling more aggressively than ~10 s gives diminishing returns: the
Activate firmware only responds reliably to `0x2B` within ~30 s windows
when accessed via an ESPHome BT proxy. Polls outside those windows time
out and trigger a reconnect. The integration settled on 10 s poll, 10 s
op timeout, no `bluetooth_device_set_connection_params` tuning. The latter
in particular: setting `supervision_timeout=400` (4 s) actively breaks
things — the link drops faster than the poll cadence.

The Mira app's apparent 500 ms responsiveness comes from polling at that
rate over the phone's native BT, not the proxy. There's no obvious way to
match that via a proxy without saturating it.
