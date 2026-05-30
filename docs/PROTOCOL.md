# Mira/Kohler BLE Protocol — Mode (Legacy) and Activate

Reverse-engineered from `uk.co.mirashowers` v5.6 (versionCode 153). All offsets,
methods, opcodes confirmed against the smali of `Lf5/e;` (dispatcher),
`Lf5/g;` (Activate inner builders), `Lf5/k$a;` (Mode/Legacy inner builders),
`Lf5/d;` (CRC/checksum/byte helpers), `Lf5/c;` (BLE I/O queue + GATT callbacks),
`Lg5/a;` (request/operation), `Lg5/b;` (response chunk).

> **Naming convention used here.** `Mode` = Mira Mode / Legacy product
> (service `bccb…`, `ProductFamily.b() == 1`, this is what mira-ha already
> supports). `Activate` = new Mira Activate product (service `267f…`,
> `ProductFamily.b() != 1`).
>
> **Class-to-product mapping confirmed via `Lf5/e;` dispatch:**
> `family.b() == 1` (Mode) → `Lf5/k$a;` builders;
> `family.b() != 1` (Activate) → `Lf5/g;` builders. The original task brief
> had this inverted — the new class `Lf5/g;` is **Activate**, not Mode.

---

## 0. UUIDs

Set in `Lf5/e;-><clinit>` and exposed via accessor methods on the singleton
`Lf5/e;->a`:

| Accessor | Field | UUID | Purpose |
|---|---|---|---|
| `f5.e.f()` | b | `bccb0001-ca66-11e5-88a4-0002a5d5c51b` | Mode service |
| `f5.e.e()` | d | `bccb0002-ca66-11e5-88a4-0002a5d5c51b` | Mode write char |
| `f5.e.d()` | c | `bccb0003-ca66-11e5-88a4-0002a5d5c51b` | Mode notify char |
| `f5.e.c()` | e | `267f0001-eb15-43f5-94c3-67d2221188f7` | **Activate service** |
| `f5.e.b()` | g | `267f0002-eb15-43f5-94c3-67d2221188f7` | **Activate write char** |
| `f5.e.a()` | f | `267f0003-eb15-43f5-94c3-67d2221188f7` | **Activate notify char** |

CCCD descriptor (0x2902) on the notify char is written to enable notifications
during connect.

---

## 1. Frame Format

The two products use **different framings**. The dispatcher `Lf5/e;` only
selects which builder runs; each builder bakes its own framing.

### 1.1 Mode (Legacy, `bccb…`)

Frame on the wire (Mode write characteristic, `bccb0002`):

```
+----------+----------+----------+----- ... -----+----------+----------+
| slot     | opcode   | body_len | payload bytes | crc_hi   | crc_lo   |
| 1 byte   | 1 byte   | 1 byte   | body_len      | 1 byte   | 1 byte   |
+----------+----------+----------+----- ... -----+----------+----------+
                                                  CRC-16 (big-endian)
```

- **`slot`** — the per-device clientSlot index assigned during pair (the
  `Product.slotIndex` / `Product.key` fields). In pre-pair frames it's `0x00`.
- **`opcode`** — see §2.
- **`body_len`** — number of payload bytes (excludes header, slot, opcode,
  body_len byte itself, and the trailing CRC).
- **`crc_hi/lo`** — CRC-16/CCITT-FALSE, big-endian. **Computed over**
  `[slot, opcode, body_len, payload, client_id_4BE]` where `client_id_4BE` is
  the 4-byte big-endian session counter; the counter is **NOT transmitted**.
  See §1.3.

The pair command (`0xEB`) is the exception — see §4.

Confirmed in `Lf5/k$a;->e([B I)[B` which delegates to
`Lf5/k$a;->f([B [B)[B`:

```java
// f5.k$a
private byte[] e(byte[] frame_without_crc, int counter) {
    return this.f(frame_without_crc, f5.d.a.t(counter));   // t = int->4B BE
}
private byte[] f(byte[] a, byte[] b) {                     // b = appended for CRC only
    byte[] tmp = f5.d.a.u(a, b);                            // concat a||b
    byte[] crc = f5.d.a.P(tmp);                             // CRC16 → 2B BE
    return f5.d.a.u(a, crc);                                // a || crc  (b discarded)
}
```

### 1.2 Activate (`267f…`)

Frame on the wire (Activate write characteristic, `267f0002`):

```
+------+------+--------+----------+----------+----- ... -----+----------+
| 0xAA | 0x55 | rsvd=0 | opcode   | body_len | payload bytes | chk8     |
|  1   |  1   |   1    |    1     |    1     | body_len      |    1     |
+------+------+--------+----------+----------+----- ... -----+----------+
```

- **`0xAA 0x55`** — fixed sync bytes (constants `170, 85`).
- **`rsvd`** — always `0x00` in observed builders.
- **`opcode`** — see §2.
- **`body_len`** — number of bytes from the byte after `body_len` up to (but
  excluding) `chk8`. Confirmed by `0x00→[0,0]`, `0x01→[0x01,X]`, `0x05→[5
  bytes]`, `0x1B→[27 bytes]` across A/E/G/Q/U.
- **`chk8`** — single-byte 2's-complement checksum:
  `chk = ((~sum(all_prev_bytes) + 1) & 0xFF)`. So
  `(sum(all_bytes_including_chk) & 0xFF) == 0`. Confirmed in
  `Lf5/d;->e([B)B`.

Append helper: `Lf5/g;->i([B)[B`:

```java
private byte[] i(byte[] frame_without_chk) {
    byte chk = f5.d.a.e(frame_without_chk);   // 2's-complement of sum
    return f5.d.a.a(frame_without_chk, chk);  // append byte
}
```

### 1.3 CRC-16 (Mode only)

From `Lf5/d;->k([B)I` (bytecode confirmed):

| Parameter | Value |
|---|---|
| Width | 16 |
| Polynomial | `0x1021` |
| Init | `0xFFFF` |
| RefIn | false (MSB first) |
| RefOut | false |
| XorOut | `0x0000` |

Equivalent Python: `crcmod.predefined.mkCrcFun('crc-ccitt-false')` or
`crcmod.mkCrcFun(0x11021, 0xFFFF, False, 0x0000)`. This is **CRC-16/CCITT-FALSE**
(also known as CRC-16/AUTOSAR).

`Lf5/d;->P([B)[B` returns the lower 2 bytes of a 4-byte big-endian
`ByteBuffer.putInt(crc).array()[2..4]` — i.e. CRC emitted **big-endian**.

### 1.4 Counter / client_id (Mode only)

A monotonically-increasing 4-byte big-endian integer threaded through every
non-pair Mode command. **Not transmitted** — folded into the CRC input only.
Value source:

- **During pair**: `java.util.Random(System.currentTimeMillis()).nextInt(MAX_INT)`
  (`Le5/o;->i0`).
- **Post-pair, per-request**: kept on the `Product` object (`Product.key` field,
  incremented per command — exact bump location is `Lf5/c;` / `Lf5/i;`; mira-ha
  already handles this correctly).

Activate has no counter — `Lf5/g;` builders never call `Lf5/d;->t(I)` or
`Lf5/d;->P([B)`.

### 1.5 MTU / Fragmentation

`Lf5/c;->n(Lg5/a;)V` decides write transport per family
(`family.b() == 1` is Mode):

- **Mode**: frame is split into 20-byte chunks. `chunks = ceil(len/20.0)`,
  pushed into `Lf5/c;->g` ArrayDeque, written one per `onCharacteristicWrite`
  callback. The 20-byte ceiling implies default ATT MTU (23, minus 3-byte
  ATT header). The app does **not** request a larger MTU.
- **Activate**: no chunking observed in the builders — frames go in a single
  `BluetoothGatt.writeCharacteristic` call. All observed Activate request
  frames are ≤ 32 bytes (e.g. the 32-byte 0xDF GCS UI Config), comfortably
  under the 23-byte minus header default. Larger writes would presumably
  require MTU negotiation, which the APK does not currently do.

For responses both families accumulate notification chunks until the
completeness check (§3) passes.

---

## 2. Opcode Tables

Builders in `Lf5/e;` are dispatcher pairs. Every public `Lf5/e;` method
(g..x, plus the dispatchers that bypass `f5/e` entirely) ultimately calls one
inner builder per product. The inner builder bakes the opcode byte literal.

### 2.1 Dispatcher → inner builder map (`Lf5/e;`)

The two-letter cells below give the inner method (single letter, capital case
where Mode↔Activate differ in subtle ways).

| `f5/e` | Mode (`Lf5/k$a;`) | Activate (`Lf5/g;`) | Notes |
|---|---|---|---|
| `g(P,gatt,str)` | `d()` (notify-start helper) | `f()` (notify-start helper) | enable CCCD on notify char |
| `h(P,gatt,cb,str,I,I)` | `y()` → `I()` = **0xEB Pair** | `u()` → see below | "h" is the Pair body |
| `i(P,c,cb,I,I)` | `h()` → `g()` = **0x41 Read DTM** | `l()` → `f()` = NOTIFY_START helper variant | |
| `j(P,c,cb,I,I)` | `o()` → `n()` = **0x44 Read valve/iface name** | `B()` → `A()` = **0x44** | |
| `k(P,c,cb,I,I,I)` | `t()` → `r()` = **0x0F Read Outlet 1 cfg** / **0x10 Outlet 2** | `D()` → `C()` = **0x1B Read outlet config data** | |
| `l(P,c,cb,I,I)` | `q()` → `p()` = **0x3E Read OPP Cfg** | `r()` → uses 0x95 | |
| `m(P,c,cb,I)` | `q()` (alt) | `x()` → 0x5C | |
| `n(P,c,cb,I,I)` | `C()` → `B()` = **0x32 Read warm-up extended** | `F()` → `E()` = **0x32** | |
| `o(P,c,cb,I,I)` | `E()` → `D()` = **0x40 Read fw extended** | `H()` → `G()` = **0x40** | |
| `p(P,I,c,cb,I,I)` | `q()` alt | `J()` → `I()` = **0x3C Read warm-up normal** | |
| `q(P,c,cb,I,I,I)` | `G()` → `F()` = **0xF4 Extended Reset Valve** | `L()` → `K()` = **0xF4** | |
| `r(P,c,cb,I,str,str)` | `H()` → `I()` = **0xEB Pair** (the registration body) | (Activate Pair routed elsewhere — see §4) | |
| `s(P,c,cb,str,I,I)` | `J()` → `Z()` = **0xC4 Write Valve Location** | `N()` → `m0()` = **0xC4** | |
| `t(P,c,cb,I,h5/d)` | (no dispatch) | `Z()` → 0xDD write GCS pre-set | Activate-only |
| `u(P,c,cb,I,i5/b,I,I)` | `T()` → `Q()`/`S()` → **0xBE Write OPP Config** | (no dispatch) | Mode-only |
| `v(P,c,cb,I,I,I,I,I,B)` | `N()` → `M()` = **0xBE Write OPP Config** | `R()` → `Q()` = **0xDF Write GCS UI Cfg** | |
| `w(P,c,cb,4Z,9I)` | `Y()` → `X()` = **0x87 OperateOutlets** | `l0()` → `k0()` = **0xAB Set Temperature** | |
| `x(P,I,c,cb,4I)` | `N()` alt | `o0()` → uses 0x95 | |

(The cells with simple opcode hex are the literal byte the device sees on the
wire. Where I write `(alt)` the dispatcher takes a Product field — slot or
clientSlot — into the same inner builder.)

### 2.2 Mode (`Lf5/k$a;`) opcode catalogue

| Opcode (hex / dec) | Inner method | Label / log string | Wire layout (post-slot, pre-CRC) |
|---|---|---|---|
| `0x07` / 7 | `y` (uses normal slot) | "Read unit prime data" (DeviceState) | `len=0` |
| `0x07` / 7 | `z` (uses **PAIR_MAGIC**) | pre-pair DeviceState | `len=0` + magic (4) |
| `0x0F` / 15 | `r` | "Read GCS Last Usage Log" (outlet 1) | `len=0`, idx in slot |
| `0x10` / 16 | `s` | (read outlet config, idx) | `len=0` |
| `0x1B` / 27 | (not in Mode k$a — Activate only) | — | — |
| `0x30` / 48 | `v`/`w` | "Read preset map" / "Read preset memory" | `len=0` + `0x80` flag byte / preset index |
| `0x32` / 50 | `B` | "Read warm-up extended" | `len=1` payload=`0x01` (sub-cmd) |
| `0x3E` / 62 | `p` | "Read OPP Configuration" | `len=0` |
| `0x40` / 64 | `D` | "Read fw extended" | `len=1` payload=`0x01` |
| `0x41` / 65 | `g` | "Read Date and Time of Manufacture" | `len=0` |
| `0x44` / 68 | `n` | "Read valve/interface name" | `len=0` |
| `0x6B` / 107 | `i`,`j`,`k`,`l` | "Read key information / count" (auth read) | `len=1` payload=`{0,1}` (fixed-vs-device-key flag) + **uses PAIR_MAGIC**, see §4 |
| `0x87` / 135 | `X` | **OperateOutlets** (running, outlet flags, temperature, flow1, flow2) | `len=5`: `[flags, tempHi, tempLo, flow1, flow2]` |
| `0x8F` / 143 | `P` | (write — 11-byte payload incl. flags+10 bytes) | `len=11` |
| `0x90` / 144 | `R` | (write — same layout, alt) | `len=11` |
| `0x94` / 148 | `V` | "Bathfill mode with memory index and name" (lite) | `len=3` |
| `0xB0` / 176 | `U` | "Write bathfill preset" (8-byte body + 16-char name) | `len=24` |
| `0xB1` / 177 | `K` | (memory-index variant) | `len=3` |
| `0xBE` / 190 | `M` | "Write OPP Configuration" | `len=5` |
| `0xC4` / 196 | `Z` | "Write Valve Location" | `len=0..16` + name string |
| `0xEB` / 235 | `I` | **Pair** (with PAIR_MAGIC) | `len=24` = counter(4) + name(20), see §4 |
| `0xEB` / 235 | `b` | Pair-related variant | (with PAIR_MAGIC) |
| `0xEB` / 235 | `c` | "delete key with fixed key" | `len=1` + PAIR_MAGIC |
| `0xF4` / 244 | `F` | "Extended Reset Valve" | `len=1` |

Strings the app emits as log labels (verified):
`Read valve/interface name`, `Read valve/interface firmware extended`,
`Read valve/interface serial number extended`, `Read OPP Configuration`,
`Read Date and Time of Manufacture (0x41)`, `Read unit prime data`,
`Write Unit Prime Data`, `Write OPP Configuration`, `Write Valve Location`,
`Write bathfill preset`, `Bathfill mode with memory index and name`,
`Extended Reset Valve`, `Read preset map`, `Read preset memory`,
`Read key information string`, `Read key count with fixed key`,
`Read key count with device key`, `delete key with fixed key`,
`delete key with device key`.

### 2.3 Activate (`Lf5/g;`) opcode catalogue

These are the literals from `const/16` instructions in each builder method.
Header layout is always `[0xAA, 0x55, 0x00, opcode, body_len, payload…, chk8]`.

| Opcode | Inner method | Wire `body_len` | Label / origin |
|---|---|---|---|
| `0x07` / 7 | (no DeviceState helper — see notify) | — | — |
| `0x1A` / 26 | `y` | 3 | "Read GCS Valve Config" |
| `0x1B` / 27 | `C` | 1 | (read outlet config data, idx in slot byte) |
| `0x2B` / 43 | `u` | 1 | (Pair-related — body data) |
| `0x32` / 50 | `E` | 1 | "Read warm-up extended" (payload `0x02`) |
| `0x3C` / 60 | `I` | 1 | "Read warm-up normal" |
| `0x3F`–`0x40` | various | 0–1 | misc reads |
| `0x40` / 64 | `G` | 1 | "Read fw extended" (payload `0x01`) |
| `0x41` / 65 | `k` | 0 | "Read DTM" |
| `0x42` / 66 | `m` | 1 | (warm-up variant) |
| `0x44` / 68 | `A` | 0 | "Read valve/interface name" |
| `0x5A` / 90 | `o` | 1 | "Read GCS Error Log Entry" |
| `0x5B` / 91 | `v` (`I` arg=offset) | 2 | "Read GCS Error Trace" |
| `0x5C` / 92 | `d`/`g` | 1–2 | "Read GCS Usage Log" |
| `0x5D` / 93 | `s` | 0 | (last-usage read) |
| `0x5F` / 95 | `q`/`w` | 3 | "Read GCS Preset" |
| `0x78` / 120 | `j` | 0 | "Read Commissioning state" |
| `0x9B` / 155 | `X` | 16 | (write — 16-byte body) |
| `0xAB` / 171 | `k0` | 4 | **Write Temperature** (`171 Updated Temperature - tempMS - tempLS`) |
| `0xBC` / 188 | `n0` | 0 | (write) |
| `0xC0` / 192 | `h0` | 0 | (notifyOperation related) |
| `0xC2` / 194 | `a0`/`c0`/`e0`/`i0`/`m0` | 0–14 | "Write SSid / Tenant id / Scope ID / password / Valve Location" — all use the same opcode with different sub-commands inside |
| `0xC4` / 196 | `m0` | 0 | "Write Valve Location" |
| `0xDD` / 221 | `S`/`U` | 0–25 | "Write GCS pre-set" (memory: 6 / 32 bytes) |
| `0xDF` / 223 | `Q` (5-byte body) / `U` (27-byte) | 5 / 27 | "Write GCS UI Configuration" |
| `0xF4` / 244 | `K` | 0 | "Extended Reset Valve" |
| `0xF8` / 248 | `O` | 1 | "Write Commissioning state" |

The Activate temperature command (`0xAB`) is the analogue of Mode's `0x87`
OperateOutlets. Log line: `"171 Updated Temperature - X tempMS - Y tempLS Z"`.
Frame on wire (10 bytes): `[0xAA, 0x55, 0x00, 0xAB, 0x04, (flags|tempMS),
tempLS, outlet1, outlet2, chk8]`.
- `flags|tempMS` (byte 5) = bitwise-or of the running/state-flags int param
  (Lf5/e;->w arg `v20`) and the high byte of the temperature value
  (from `Lf5/a;->l(temp_scaled).getBytes()[2]`).
- `tempLS` (byte 6) = low byte of the scaled temperature integer.
- `outlet1`, `outlet2` (bytes 7,8) = the two trailing int params from
  `Lf5/e;->w` (v22, v23).

### 2.4 Other opcodes mentioned in strings, builder unknown

`0x5A` Read GCS Error Log Entry, `0x5B` Read GCS Error Trace,
`0xB1` Activate Memory, `0xDD` Write GCS pre-set,
`0xDF` Write GCS UI Configuration, `0xF8` Write Commissioning state —
all present in the strings table and reachable from the catalogues above.

---

## 3. Notification (response) format

The notify char (Mode `bccb0003` or Activate `267f0003`) receives chunks via
`onCharacteristicChanged`. Each chunk is wrapped in `Lg5/b;` together with the
running total of received bytes. Completion is decided by `Lg5/b;->b()Z`.

### 3.1 Mode response

```
+----------+----------+----------+--- payload (per-cmd) ---+
| slot|0x40| opcode   | body_len | …                       |
+----------+----------+----------+-------------------------+
```

- **First-chunk indicator**: `byte[3] & 0x80 != 0` ⇒ COMPLETE (i.e. the
  device sets the high bit of the FIRST payload byte to mark "single-chunk
  response"). For chunked responses the high bit is clear on intermediate
  chunks; completion is then tracked by total bytes received vs declared length.
- The slot byte echoes `clientSlot | 0x40` (the `0x40` is "this is a response,
  not a command"). mira-ha already strips this.
- No CRC verification on responses in `Lg5/b;` for Mode — mira-ha doesn't
  validate response CRC either, so this is consistent.

### 3.2 Activate response

```
+------+------+--------+----------+----------+--- payload ---+----------+
| 0xAA | 0x55 | rsvd   | echo cmd | body_len | bytes…        | chk8     |
+------+------+--------+----------+----------+---------------+----------+
```

`Lg5/b;->b()` (Activate branch):

```python
def is_complete(chunk, running_total):
    if len(chunk) < 6: return False
    if chunk[5] & 0x80 != 0: return False    # high bit on first payload byte = "more"
    if not chk8_valid(chunk):     return False
    if running_total       != chunk[4]: return False  # declared body_len
    if running_total + 6   != len(chunk): return False  # +6 = 5B header + 1B chk
    return True
```

- **Sync bytes echo back**: `chunk[0..1] == [0xAA, 0x55]`.
- **`chunk[4]` echoes declared body_len** (must equal accumulated bytes
  received).
- **`chunk[5] & 0x80`** = "more chunks coming" indicator on the first payload
  byte. Clear it → last chunk.
- **`chk8`** is verified on every assembled response.
- **Multi-chunk handling**: the `onCharacteristicChanged` "Completion check"
  log line accumulates `chunk.length` per notification; the comparison is
  `received == declared_len + 6` (the 6-byte overhead = 0xAA,0x55,rsvd,opcode,
  body_len + chk8).

### 3.3 Request → response correlation

Notifications are matched to the **single in-flight request** held in
`Lf5/c;->d Lg5/a;`. There is **no per-frame sequence/correlation id** in
either protocol — the queue is strictly serialized:

- `Lf5/c;->b ArrayDeque<Lg5/a;>` holds pending operations.
- `Lf5/c;->d Lg5/a;` is the current in-flight operation.
- A new operation does not start until the previous one's `onCharacteristic
  Changed` accumulates enough bytes to satisfy `Lg5/b;->b()` AND
  `Lg5/a$b;->a(...)` (response callback) returns.
- Timeout: 10000 ms (`i()` method, log line "TIMEOUT cmd=X after Yms").

Implication: implementations MUST serialize requests and rely on opcode echo in
the response for routing.

---

## 4. Pair / authentication

### 4.1 Pair handshake — Mode

1. **Connect** to the device (advertised local name starts with `BLE-`, etc.).
   The peripheral exposes service `bccb0001-…`.
2. **Subscribe** to notify char `bccb0003-…` (CCCD descriptor write,
   `0x0001 0x0000` enable-notify) — `Lf5/e;->g(...)`.
3. **Send Pair command** — `Lf5/k$a;->I(...)`:
    ```
    Frame (29 bytes on wire):
    [0x00, 0xEB, 0x18, c0,c1,c2,c3, n0..n19, crc_hi, crc_lo]

    where:
      0x00          = pre-pair slot
      0xEB          = Pair opcode
      0x18 = 24     = body_len (= 4 counter + 20 name)
      c0..c3        = client_id (4-byte BE) — also folded into CRC
      n0..n19       = identity name padded/truncated to 20 ASCII bytes
                      (default = "{manufacturer} {model}".upper(), see Lf5/d;->p())
      crc           = CRC16(bytes [0..27] || PAIR_MAGIC_ID), big-endian.

    PAIR_MAGIC_ID = 0x54 0xD2 0xEE 0x63   (Lf5/k;->c [B)
    ```
    Confirmed in `Lf5/k$a;->I`:
    `header(3) || t(client_id) || N(name, 20)` then
    `f(combined, PAIR_MAGIC) → combined || CRC16(combined || PAIR_MAGIC)`.
4. **Device responds** on the notify char with the assigned `slotIndex`
   (mira-ha reads this and persists it).
5. After pair, all subsequent commands use the assigned slot byte and the
   monotonic client_id counter (no magic; counter is folded into CRC only,
   per §1.4).

### 4.2 Pre-pair commands (Mode, use PAIR_MAGIC)

These builders ALSO inject the magic into the CRC instead of the client_id:

- `Lf5/k$a;->z` (0x07 DeviceState pre-pair) — slot=0
- `Lf5/k$a;->c` (0xEB delete-key-with-fixed-key)
- `Lf5/k$a;->j`, `Lf5/k$a;->l` (0x6B read-key-info-with-fixed-key)
- `Lf5/k$a;->b` (0xEB variant — probably "unpair")

After pair these morph into the device-key variants (`Lf5/k$a;->i`, `->k`,
`->y`, `->I` second call) which use the real client_id.

### 4.3 Pair handshake — Activate

`Lf5/e;->r` dispatches the Pair body, but **only the Mode branch is
implemented in the dispatcher**:

```java
// f5/e r(...)
if (family.b() == 1) {
    f5.k$a.a.H(p, c, cb, name, clientId, label);   // → Lf5/k$a;->I (0xEB Pair)
}
// else: NO-OP — no Activate branch
```

In `Lf5/e;->h` (the "h" Pair routine, which `Le5/o;->i0` calls during
"Legacy Pairing attempt"), the family-!=1 path routes to `Lf5/g;->u(...)`:

| `Lf5/g;->u` | Activate opcode | body_len | Notes |
|---|---|---|---|
| const `0x2B` (43) | `0x2B` | 1 | non-magic, no counter |

So **Activate does NOT use a `0xEB`-style identity-key pair**. The opcode-0x2B
write is the closest analogue. The String parameter to `Lf5/e;->h` is still
passed through (presumably written into the frame body or stored client-side).

`Le5/o;->z0` reveals that Activate skips the legacy-pair branch entirely:

```java
if ((family.b() != 1) || product.f() != 0 || product.r() != 0) {
    // … goes to C0() or A0() (non-legacy pair flows) …
} else {
    // legacy pair, defer until CCCD descriptor write completes
    this.m = p6; this.n = p5;
}
```

**Conclusion on Activate auth**: there is no equivalent of the Mode `0xEB`
identity-key exchange in the BLE protocol. Activate either relies on
**OS-level BLE bonding** (LE Secure Connections — pairing handled by Android
itself, no app-level keys) or on the `0x2B` write as a lightweight session
hello. The next step here would be an HCI capture during a real Activate
pairing to see whether SMP pairing fires.

> **Unknown — would need an HCI capture during a real Activate pairing to
> confirm whether SMP bonding is required and whether the `0x2B` write is the
> entire app-layer auth.** Without that, the safe assumption is that Activate
> requires BLE bonding (`createBond()`) and otherwise has no per-frame auth.

---

## 5. Mode vs Activate divergence

Walking `Lf5/e;` g..x:

| Dimension | Mode | Activate |
|---|---|---|
| Service / write / notify | `bccb0001/2/3` | `267f0001/2/3` |
| Frame sync | none (slot byte starts frame) | `0xAA 0x55` |
| Length field | `body_len` at offset 2 | `body_len` at offset 4 |
| Integrity | CRC-16/CCITT-FALSE (big-endian) | 8-bit 2's-complement checksum |
| Counter / sequence | 4-byte BE client_id folded into CRC (not on wire) | none |
| MTU handling | chunked into 20-byte writes | single write per frame |
| Opcode space | partial overlap (0x07, 0x32, 0x40, 0x41, 0x44, 0x5A, 0x5B, 0x5C, 0xF4 carry the same meaning) | mostly its own opcodes for control (0xAB temp instead of 0x87, 0xDD/0xDF GCS writes) |
| Control opcode | `0x87` OperateOutlets | `0xAB` Set Temperature |
| Pair | `0xEB` + 20-byte identity name + magic-CRC | none in BLE protocol (likely OS bonding + `0x2B`) |
| Response sync | first byte = `slot | 0x40` | first 2 bytes = `0xAA 0x55` |
| Response "more" flag | `byte[3] & 0x80` (high bit on first payload byte) | `byte[5] & 0x80` (high bit on first payload byte after header) |
| Response chk | not verified app-side | `chk8` verified on every assembled frame |

**Hypothesis check** — "Activate is byte-compatible with Mode, only UUIDs
differ": **REFUTED**. The framings are structurally different (sync header,
checksum algorithm, no counter, different control opcode). They share many
*read* opcodes but the write/control side is incompatible.

---

## 6. Implementation guidance for `mira-ha`

### 6.1 Recommendation

**Option (b): share helpers, split a `mira_activate` integration / module.**

The frame layouts, integrity algorithms and pairing flows are too different to
hide behind a `protocol: Mode | Activate` enum without duplicating most code
paths. A clean split looks like:

```
custom_components/
  mira_mode/                  # existing, unchanged
    mira_protocol.py
    …
  mira_activate/              # NEW
    mira_protocol.py          # frame builder for 267f, chk8 only
    coordinator.py            # mirrors mira_mode/coordinator
    …
  mira_shared/                # optional shared package
    crc16_ccitt.py            # extract from mira_mode
    chk8.py
```

If you'd rather keep one integration, the protocol module factor-out can be:

```python
class MiraProtocol:
    def build(self, slot, opcode, payload, counter): ...
    def parse(self, chunk): ...
    @property
    def write_uuid(self): ...

class ModeProtocol(MiraProtocol): ...
class ActivateProtocol(MiraProtocol): ...
```

…and the coordinator picks the subclass based on which service UUID is
discovered on connect.

### 6.2 Smallest concrete change list (fork-based, Activate as a sibling)

1. Add `mira_activate/mira_protocol.py` containing:
   - UUID constants (§0 Activate row).
   - `SYNC = b"\xAA\x55\x00"` and `RSVD = 0x00`.
   - `def chk8(data: bytes) -> int: return ((~sum(data) + 1) & 0xFF)`.
   - `def build(opcode: int, payload: bytes = b"") -> bytes:`
       `frame = SYNC + bytes([opcode, len(payload)]) + payload; return frame + bytes([chk8(frame)])`.
   - `def parse(chunk: bytes) -> tuple[int, bytes] | None:` verify sync,
     verify chk8, strip header, return `(opcode, payload)`.
   - Opcode constants from §2.3 (start with `OP_SET_TEMPERATURE = 0xAB`,
     `OP_READ_DEVICE_NAME = 0x44`, `OP_READ_DTM = 0x41`).
2. Add `mira_activate/coordinator.py` and entity files by copying the
   `mira_mode` equivalents and swapping the protocol module + UUIDs.
3. Auth: until HCI capture confirms otherwise, assume `device.create_bond()`
   is required before service discovery. Add a config-flow step that calls it
   and waits for `BluetoothDevice.getBondState() == BOND_BONDED`.
4. Implement `OperateOutlets`-equivalent as `OP_SET_TEMPERATURE (0xAB)`:
   payload = `[temp_high, temp_low, outlet1_flow, outlet2_flow]` (4 bytes,
   per `Lf5/g;->k0`). Confirm exact byte order with on-device sniff.
5. No fragmentation needed for normal commands (all ≤ 32 bytes); on
   response, accumulate notifications until `chk8` validates AND `byte[5] &
   0x80 == 0` AND received bytes == `byte[4] + 6`.

If you'd rather extend `mira_mode` rather than fork:

1. Promote the UUID constants to a `ProtocolVariant` enum: `MODE` / `ACTIVATE`.
2. Wrap `build_frame` and `parse_frame` in a dispatch:
   - `MODE`: existing CRC-16 / counter / 20-byte chunk path.
   - `ACTIVATE`: new chk8 / no-counter / single-write path.
3. Swap `WRITE_CHAR_UUID` / `NOTIFY_CHAR_UUID` based on detected service.
4. In `config_flow.py`, detect the service UUID during scan; route to the
   appropriate path.

The CRC-16/CCITT helper from `mira_mode` is unchanged and remains needed for
Mode; Activate doesn't use it.

---

## Appendix A. Quick reference — helper methods in `Lf5/d;`

| Method | Purpose |
|---|---|
| `e([B)B` | 2's-complement 8-bit checksum (Activate) |
| `a([B B)[B` | append byte to array |
| `t(I)[B` | int → 4-byte BE array (client_id / counter) |
| `k([B)I` | CRC-16/CCITT-FALSE compute (Mode) |
| `P([B)[B` | CRC-16 → 2-byte BE array |
| `u([B [B)[B` | concat two arrays |
| `v([B [B [B)[B` | concat three arrays |
| `N(Ljava/lang/String; I)[B` | ASCII string → fixed-length byte array (zero-pad / truncate) |
| `p()Ljava/lang/String;` | default Pair identity name = `"${MANUFACTURER} ${MODEL}".upper()`, truncated to 20 chars |

## Appendix B. PAIR_MAGIC_ID

Hardcoded constant in `Lf5/k;->c [B`:

```
0x54 0xD2 0xEE 0x63
```

Used in Mode-only CRC injection for: Pair (0xEB), pre-pair DeviceState (0x07),
pre-pair key-info reads (0x6B), and the "delete key with fixed key" command.
Activate has no analogous constant.
