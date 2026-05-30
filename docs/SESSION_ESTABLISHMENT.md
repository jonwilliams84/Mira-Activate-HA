# Mira Activate — BLE session establishment

Reverse-engineered from `uk.co.mirashowers` v5.6. This document covers everything
the official app does between `BluetoothDevice.connectGatt(...)` and the first
opcode write that would actually drive the shower (`0x2B` "Read unit prime data"
poll). It is the missing piece on top of `PROTOCOL_SPEC.md`.

Source-of-truth bytecode:

| Where | Class | Role |
|---|---|---|
| `Lf5/c;` | BleIOQueue | GATT callbacks + serialized op queue |
| `Lf5/c$b;` | callback iface | impl is `Le5/o;` (see `a/b/c/d`) |
| `Le5/o;` | BleIOManager | connection lifecycle + pair orchestration |
| `Lf5/i;` / `Lf5/i$d;` | poller | Timer that fires polling reads after pair |
| `Lf5/g;->u` | builder | the literal `0x2B` frame |
| `Le5/o;->O` | bond gate | branches on `bondState`, calls `createBond()` |
| `Le5/o;->X` | broadcast rx | listens for `BOND_STATE_CHANGED` |

---

## 1. The exact sequence

What follows is the order of GATT calls and Android-level operations as
implemented in the v5.6 APK for an Activate (`267f...`) device that is **not yet
bonded** and **not yet in the app's local DB** (i.e. our `MIRA 003F ENSUITE`
case).

```
phone                                                 device
─────                                                 ──────
connectGatt(ctx, autoConnect=false,                   ──connect req──▶
            callback=Lf5/c;,
            transport=TRANSPORT_LE=2)                ◀──connect cfm──
                                                            (no MTU exchange;
                                                             no conn-update req;
                                                             no SMP yet)

Lf5/c;->onConnectionStateChange(STATE_CONNECTED, status=0)
   → bluetoothGatt.discoverServices()                 ──discoverServices──▶
                                                     ◀──services found────

Lf5/c;->onServicesDiscovered(status=0)
   → Le5/o;->d(...)
   → Le5/o;->z0(gatt, product, queue):
       (a) queue.j( Lf5/e;->g(product, gatt, "Enabling notify") )
           → enqueues a NOTIFY_START op which:
             * Lf5/c;->n() does:
                 gatt.setCharacteristicNotification(notifyChar=267f0003, true)
                 cccd.setValue(ENABLE_NOTIFICATION_VALUE = 0x01 0x00)
                 gatt.writeDescriptor(cccd)           ──CCCD write 01 00──▶
                                                     ◀──desc write ok────

           * Lf5/c;->onDescriptorWrite(status=0)
                → Le5/o;->b(queue, gatt, status=0, product)
                → if Activate: nothing happens here yet, the queue idles
                  (the Mode branch fires B0 here; Activate goes via z0's
                  "else" branch after notify is queued, not from b())

       (b) z0 also looks up the product in the local SQLite DB
           via Lm5/b;->c(productMac).
             * if product found AND Mode (family.b()==1) AND not paired
                 → defer legacy pair until CCCD write completes
                   (sets o.m=queue, o.n=product; b() reads them)
             * else if product NOT found
                 → Le5/o;->A0(queue, product):
                     handler.postDelayed(N(queue, product), 1500ms)
                     where N returns Le5/b which calls Le5/o;->O(...)
             * else (Activate-already-known)
                 → Le5/o;->C0(queue, product, gatt):
                     handler.postDelayed(Le5/c.run, 500ms)
                     where Le5/c.run() → Le5/o;->i() → D0()

       (For our unbonded device: case (b)·middle — A0 path.)

[+1500ms] Le5/o;->O(product, this, queue) runs:
   * log: "Starting GCS pairing task for {mac}"
   * device = adapter.getRemoteDevice(mac)
   * switch on device.getBondState():
       case BOND_BONDED (12):
           → r0(productName, mac, 0, 0, productKey)   # INSERT into DB
           → Lf5/i;->x(product, btManager, queue)     # START POLLING
       case BOND_BONDING (11):
           → set o.g = true (bonding-in-progress)
             post delayed P(device) at 1000ms  # force-call createBond again
             post delayed Z(o, device, queue) at 35000ms  # 35s timeout
           → wait for BOND_BONDED broadcast
       case BOND_NONE (10):                   # OUR CASE — fresh device
           → b0(device, queue):
                 device.createBond()          # initiates SMP pairing
                                              # Android shows pair UI (or just-works)
           → wait for BOND_BONDED broadcast

Meanwhile, BroadcastReceiver Le5/o$d (registered via s0()) listens for
"android.bluetooth.device.action.BOND_STATE_CHANGED" → Le5/o;->X(device, new, prev).

X() on BOND_NONE → BOND_BONDING:
   * log "Device bonding in progress"
   * set o.g = true, o.h = device

X() on BOND_BONDING → BOND_BONDED:
   * log "✅ Device bonded successfully"
   * o.g = false, o.h = null
   * handler.postDelayed(Le5/g(this), 500ms)
       → Le5/g.run() → Le5/o;->r(this) → Le5/o;->Y(this):
           → if product != null AND family != Mode:
                r0(productName, mac, 0, 0, productKey)    # INSERT into DB
                ── then poller starts as part of r0 ──
                → Lf5/i;->x(product, btManager, queue)
```

After `Lf5/i;->x` is reached, polling begins:

```
Lf5/i;->x:
   * cancel any prior Timer at Lf5/i;->g
   * new Timer.scheduleAtFixedRate(Lf5/i$d, delay=1500ms, period=500ms)

Lf5/i$d.run() (each 500ms tick):
   * check connection still up (BluetoothManager.getConnectionState == STATE_CONNECTED)
   * if Lf5/c;->h() (queueSize) is 0 and currentOp is null:
       op = Lf5/i;->s(product, queue):
           Lf5/e;->h(product, gatt, callback, "Read unit prime data",
                    product.r(),  # = 0 for unpaired
                    product.f())  # = 0 for unpaired
       → for Activate: this returns from Lf5/g;->u(product, 1, gatt, cb, str):
           bytes [0xAA, 0x55, 0x00, 0x2B, 0x01, 0x02] + chk8 → 0xD3
                            ▲opcode  ▲body_len=1  ▲body=2
           expectedResponseLen = (1*8 + 10) = 18 bytes
       queue.j(op) → eventually Lf5/c;->n(op) → GCS WRITE single 7-byte frame.
   * Lf5/c;->onCharacteristicWrite(... status=0) — write accepted, wait for notify.
   * Lf5/c;->onCharacteristicChanged accumulates response chunks until Lg5/b;->b()
     reports complete (≥18 bytes payload, header chk8 ok).
```

In parallel:
```
F0(queue) (started right after start polling):
   handler.postDelayed(Le5/o$e, 200ms) — first HIGH-priority request
   Le5/o$e.run(): gatt.requestConnectionPriority(CONNECTION_PRIORITY_HIGH=1)
                  re-posts self every 3000ms while o.d (isConnected) is true.

Additionally, Lf5/c;->j(op) (the queue add-and-trigger path) calls
gatt.requestConnectionPriority(HIGH) immediately for any **non-polling** op
that gets queued (log line: "Immediate HIGH priority request for non-polling cmd=").
```

---

## 2. Answers to the brief's specific questions

**Does the app request a specific MTU?** No. `Lf5/c;` and `Le5/o;` contain **no
call to `BluetoothGatt.requestMtu()`** anywhere. The post-connect MTU stays at
the GATT default (23 → 20 bytes app payload). All Activate write frames in
`Lf5/g;` are ≤ 32 bytes; the largest single write observed in the catalogue is
the 32-byte 0xDF GCS UI Config which would not fit in one ATT MTU-23 PDU. The
app sends it as a single `writeCharacteristic` call anyway and presumably
relies on the BLE stack to split into multiple LL PDUs.

> Take-away for our client: **do not** request an MTU on the proxy. Match the
> app's behaviour exactly. (BlueZ-side on the ESPHome proxy will negotiate the
> default ATT MTU.)

**Does it read any standard characteristics (Generic Access 0x2A00, 0x2A01)?**
No. There are zero calls to `readCharacteristic` against any Generic Access /
Generic Attribute UUIDs. The only reads in the codebase are app-level reads
through the `Lf5/c$c;` switch on `Lg5/a$d.READ_CHARACTERISTICS`, and those
target the Activate service UUIDs.

**Precise order of CCCD enable + 0x2B hello + first command?**
1. `discoverServices` (driven by `onConnectionStateChange`).
2. `setCharacteristicNotification(267f0003, true)` + `writeDescriptor(CCCD,
   ENABLE_NOTIFICATION_VALUE = [0x01, 0x00])`.
3. **Wait for `BOND_BONDED`.** The CCCD write completes, then the queue idles
   until either:
   - the product was already known and bonded (`C0` path, +500ms timer) or
   - bonding finishes via the system broadcast (`X` → `Y` → `r0`).
4. Only then does `Lf5/i;->x` start the polling Timer and queue the first
   `0x2B` write at +1500ms (first tick of the Timer).

**There is NO standalone `0x2B` "hello".** What we previously thought was a hello
is the **first tick of the read-poll Timer**, which is supposed to fire only
*after* the LE bond is established.

**What does the `0x2B` payload `0x02` mean? Is `0x01` a different mode?**

Both the `1` body_len and the `2` body byte are **hardcoded** in `Lf5/g;->u`
(see `const/4 v0, 2` and `const/4 v1, 1`). The `0x02` is not derived from any
caller parameter. The single int param `v12` to `u` is used **only** to compute
`expectedResponseLen = v12 * 8 + 10`. The only caller is `Lf5/e;->h` which
hardcodes that param to `1` (→ `expectedResponseLen = 18`).

So `0x02` is a fixed sub-command value baked into this code path. No `0x01`
variant is reachable from any builder in `Lf5/g;`. The opcode value `0x2B` is
also used as the body-len byte position 3 — there is some constructor reuse
here but the wire frame is definitively `AA 55 00 2B 01 02 D3`.

**What is the response to `0x2B`? Does the app wait?**

Yes — `Lf5/i$d.run()` calls `Lf5/c;->h()` (queueSize) and `Lf5/c;->f()`
(currentOp) and *skips* the tick entirely if either is non-zero. So while the
0x2B response (or its timeout — 10s per `Lf5/c;->i`) is outstanding, the next
poll tick is dropped. Response framing per §3.2 of `PROTOCOL_SPEC.md`. The
opcode echo will be `0x2B`. `body_len` should be 12 (so total 18 bytes:
5 header + 12 payload + 1 chk).

**Connection parameter update requests?** No explicit `BluetoothGatt`-level
connection-parameter request is issued. Instead the app uses
`requestConnectionPriority(CONNECTION_PRIORITY_HIGH=1)` (Android's high-level
wrapper that maps to a 7.5–15ms interval) **after** the bond is in place. The
priority keeper `Le5/o$e` re-requests HIGH every 3000ms.

---

## 3. The thing we (probably) missed: bonding

**Our nRF Connect tests showed connect succeeded *unbonded* and GATT discovery
worked.** That part matches the official app. The drop-on-write happens because
the device gates app-layer writes on the GATT characteristic behind LE bonding.
Inspecting `Lf5/c;->n` (the write path) shows nothing special — `setValue` +
`writeCharacteristic`, no per-write auth. The bonding requirement is enforced
by the **peripheral**, returning `INSUFFICIENT_AUTHENTICATION` on the write
which the central handles by tearing down the link (status 19 ≡
`REMOTE_USER_TERMINATED_CONNECTION` is the user-visible disconnect, but the
underlying cause is the unauth write).

In other words: the official app **always bonds before its first write**. We've
been writing without bonding, and the device terminates us. This is consistent
with the comment in §4.3 of `PROTOCOL_SPEC.md`.

### What we still don't know for sure

- **Which security level** the device requires. The simplest is LE Just-Works
  (no I/O capabilities, no MITM). The strictest in the wild would be LE Secure
  Connections with Numeric Comparison. The app uses `createBond()` only — no
  call to `setIoCapabilities` or `setPin`, and there are no PIN/passkey UI flows
  in `com.kohler.miraapp.*` that I can find. Best-guess: **Just-Works** (the
  device has no display, no buttons that would let it show a passkey).
- **Whether bonding can be initiated from the central (us) or only from the
  peripheral.** Android's `createBond()` triggers a central-initiated SMP
  pairing request, which Just-Works peripherals always accept. So this should
  work even if the device's BLE stack treats us as the initiator.

### Implications for the POC

The bathroom ESPHome proxy (`esp32-bluetooth-proxy-3492a8`) **does support BLE
bonding** as of ESPHome ≥ 2025.1 (release notes: "ESP32 Bluetooth Proxy: pass
through pairing/bonding"). This is exposed via `aioesphomeapi` as
`bluetooth_device_pair` / `bluetooth_device_unpair`. So we can:

1. Connect (random address).
2. Discover GATT.
3. Enable CCCD on `267f0003`.
4. **Trigger bonding** via `client.bluetooth_device_pair(address)`.
   (If proxy/firmware doesn't expose it, fall back to writing one byte and
   triggering implicit bonding — but the explicit path is cleaner.)
5. Wait for pair complete callback.
6. Send `0x2B 01 02 D3`.
7. Read response.

If `bluetooth_device_pair` is not available on the installed proxy firmware,
the bond will (per ESPHome's implementation) be triggered implicitly when the
device responds to the first auth-required write with the INSUFFICIENT_AUTH
error — the proxy's ESP-IDF stack handles the resulting SMP pairing
automatically. In that case our code should:

1. Issue the write.
2. *Tolerate* the first connection drop, retry connect, the bond will now be
   cached in the proxy's NVS.
3. Retry the write — it should now succeed.

We'll start with the explicit path. If `bluetooth_device_pair` is unavailable
on this firmware version, we will see an error from `aioesphomeapi` and fall
back to the implicit path.

### What this is not

It is NOT a per-device or per-account secret derived from the serial — there
is no key derivation function or stored credential anywhere in the bytecode
that gets injected into Activate writes. Mode has its `PAIR_MAGIC` constant;
Activate doesn't.

---

## 4. TL;DR / sequence-as-a-list

For an Activate device the **first time** a phone connects to it:

1. `connectGatt(TRANSPORT_LE)`
2. (on connect) `discoverServices`
3. CCCD enable on `267f0003` (write `01 00`)
4. **`createBond()`** — this is the missing step in our probes
5. Wait `BOND_BONDED`
6. After +500ms, `Lf5/i;->x` starts a Timer.
7. First tick @ +1500ms: send `AA 55 00 2B 01 02 D3`.
8. Receive notification frame echoing opcode `0x2B` with 12 payload bytes
   (total 18 bytes including the 5-byte header and the 1-byte chk8).

For a **reconnect** (device already in app DB and already bonded), steps 4–5
are skipped; the flow is just connect → discover → CCCD → +500ms wait → poll.

The acceptance criterion for stage 2 (`activate_client.py`) is to do steps 1–7
without the device tearing down the link.
