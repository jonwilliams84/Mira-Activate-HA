# Mira Activate — `0xAB` and `0x2B` byte semantics

Decoded from `uk.co.mirashowers` v153 smali. All claims cite the exact method
+ instruction index in the smali dump (`scripts/dump_method.py`).

> Frame layout context (already in `PROTOCOL_SPEC.md` §1.2 / §2.3):
> Request `AA 55 00 AB 04 [flags|tempMS] [tempLS] [outlet1] [outlet2] chk8`.
> Response `AA 55 00 2B [len=0x12] [18B payload] chk8`. The parser
> indexes the **full frame**, not just the payload — so payload byte N is
> `a[N+5]` in the source.

---

## 1. `0xAB Set Temperature` — flags / outlet1 / outlet2

### 1.1 What's actually called

`Lf5/e;->w(P, c, cb, ZZZZ, IIIIIIIII)V` is the dispatcher. The Activate branch
(family.b()!=1) calls `Lf5/g;->l0(P, c, cb, I, I, I, I)V` passing only the
**6th, 7th, 8th, 9th int args** (registers v27..v30; see `Lf5/e;->w` insns
0036–0040). `l0` then calls `k0(...)` which builds the frame.

`Lf5/g;->k0` (insns 0019–0023, 0038–0076):

```
v20 = flags                  (arg 1, int)
v21 = temp_scaled            (arg 2, int)  → bytes[2,3] BE = tempMS, tempLS
v22 = outlet1                (arg 3, int)
v23 = outlet2                (arg 4, int)

byte5 (payload[0]) = (flags | tempMS) & 0xFF       (or-int v6, v20, v7)
byte6 (payload[1]) = tempLS                        (low byte)
byte7 (payload[2]) = outlet1
byte8 (payload[3]) = outlet2
```

### 1.2 What the call sites actually pass

Five reachable call sites traced; in each, the **6th..9th ints** to `Lf5/e;->w`
become `(flags, temp_x10, outlet1, outlet2)`:

| Caller | flags | temp_x10 | outlet1 | outlet2 | Intent |
|---|---|---|---|---|---|
| `LY4/B2;->k0` op 0179 | **0** | `h5/a.j()*10` | `h5/a.d()*4` | `Lf5/a;->i(curr, action, prodType)` | normal user "set outlet/temp/flow" |
| `Lcom/kohler/.../ProductControlActivity;->N9` op 0107 | **1** | 0 | `h5/a.d()*4`\|0 | **0** | turnOffAllOutlets (emergency) |
| `LY4/l2;->O0` op 0066 | **1** | `i5/c.i()*10`\|0 | 0 | 0 | turnOffAllOutlets (Mode-VM variant) |
| `LY4/l2;->r0(Lh5/a;)` op 0011/0015 | 0 if state else **1** | temp\|0 | flow\|0 | **0** | "clear outlets" / re-init |
| `LY4/D2;->y0(I)` op 0069 | **0** | `h5/a.j()*10` | `h5/a.d()*4` | `Lf5/a;->i(curr, action, prodType)` | set temp + outlet action |
| `Lcom/kohler/.../PresetControlActivity;->k2` op 0095 | **1** | temp_x10 | flow | **0** | launch preset (clears, then engages) |

### 1.3 Bit semantics

#### `flags` byte (OR'd with tempMS into byte 5)

- **`flags = 0`** = "normal write, apply outlet2 as the new outletState bitmap".
- **`flags = 1`** = "force-stop / clear all outlets" — always paired with
  `outlet2 = 0`. Used for turnOffAllOutlets and the "no current state" branches.
- **`flags = 0x80`** is **never set by any call site.** The current
  `coordinator.py` value `flags = 0x80 if running else 0x00` is wrong — there
  is no "running" flag in this opcode at all; running state is conveyed
  entirely through `outlet2` bits.

Conflict note: `flags | tempMS` collides on bit 0 at temps where the high
byte of `temp_x10` is 0x01 (256–511, i.e. **25.6–51.1 °C**). The app does
this OR blindly. The device must therefore treat the bit-0 conflict as
"either flag or temp top-bit = 1" — i.e. **the device decodes the temp from
the lower 9 bits of bytes 5–6 specifically**, leaving bit 7 of byte 5 free
for a flag. Practically `flags=0` is the only safe value to write from a
client that wants to set a specific temp without unintended side effects.

#### `outlet1` byte (byte 7)

- This is **flow rate, NOT a flag/boolean**.
- Encoded as `desired_flow_LPM × 4` per `LY4/B2;->k0` op 0128–0129
  (`h5/a.d() * 4`).
- Range: 0..100 (=0..25 LPM at the ×4 scale).
- `0xFF` / `0x00` (current code) is wrong: 0xFF=255 is out of range,
  0x00 means "zero flow" which on a thermostatic mixer means "no water".

#### `outlet2` byte (byte 8)

- This is the **bit-packed outletState bitmap**, NOT a second flow value.
- Built by `Lf5/a;->i(current_state, action, productType)Z`. Bit map:

| Bit | Mask | Tester (`Lf5/a;`) | Setter | Meaning |
|---|---|---|---|---|
| 0 | `0x01` | `g(B)` insn 0000 | `p(B,Z)` | **outlet 0 on** |
| 1 | `0x02` | `k(B)` insn 0000 | `s(B,Z)` | **outlet 2 (3rd) on** |
| 2 | `0x04` | `j(B)` insn 0000 | `r(B,Z)` | **outlet 1 (head/main) on** |
| 6 | `0x40` | `h(B)` insn 0000 | `q(B,Z)` | **pause** |
| 7 | `0x80` | — | — | **error** (read-only flag, set by device) |

Confirmed via `Lh5/a;->e(B)Ljava/lang/String;` op 0007–0017:
`if (g||h||i) → "ON"; else byte==0x40 → "PAUSE"; byte==0x80 → "ERROR"; else "OFF"`.

So for **outlet 1 (head) on** at 38°C 21 LPM, the write becomes
`flags=0, tempMS|temp=0x01, tempLS=0x7C, outlet1=84 (0x54), outlet2=0x04`,
giving wire bytes `AA 55 00 AB 04 01 7C 54 04 [chk8]`.

### 1.4 Device-side guard

`Lh5/a;->g()I` returns `v` field = `Lf5/a;->n(byte13)` = the IOT module
status code. The dispatcher `Lf5/i$c;->a` op 0187–0204 only treats the
device as "ready for state-change side effects" when `g() == 2`. So if the
shower's IOT status (`payload[8]` decoded via `f5/a.n`) is not 2, writes
may be accepted but ignored. This is **not** something the app guards
against client-side, but it matches the symptom "frames accepted, no
water flows". Recovery: re-pair via the app, observe `payload[0]` bit-6
(see §2 below) flip to 1, then retry.

---

## 2. `0x2B` poll response — 18-byte payload

Parser: `Lh5/a;-><init>([B)V`. Reads the **full frame array** so indices
shown below are payload-relative `i` → `a[i+5]` in the smali.

### 2.1 Byte table

| Payload byte | Field in `Lh5/a;` | Decoder | Meaning |
|---|---|---|---|
| 0 | `p`, `r`, `s`, `t`, `u` | `Lf5/a;->{e,t,u,m,o}` (see §2.2) | system status byte: status code + bit flags |
| 1 | — | — | `Unknown` (always `0x20` in capture; possibly fixed body marker) |
| 2 | — | — | `Unknown` (zero in capture) |
| 3 | — | — | `Unknown` (zero) |
| 4 | — | — | `Unknown` (zero) |
| 5 | — | — | `Unknown` (zero) |
| 6 | `b` | `Lf5/a;->f(B)` = bit 7 (`0x80`) | misc bit flag (zero in capture) |
| 7 | — | — | `Unknown` |
| 8 | `v` | `Lf5/a;->n(B)` | IOT module status; insns dump `*****IOT MODULE STATUS:***** {n}` and `Unsigned:{raw}`. `g()` returns this; dispatcher treats `==2` as "session ready for command-driven state change" |
| 9 | — | — | `Unknown` (zero in capture) |
| 10 | `n` (high bit) | `(a[15] & 0x01) << 8` | temp high bit |
| 11 | `n` (low byte) | `(\| Lf5/d;->c(B)) / 10` | **target_temp ×10 (lower 9 bits combined with byte 10)** — `0x01 0x7c` = 380 → 38.0 °C |
| 12 | `o` | `Lf5/d;->i(B) / 3` | **flow rate scaled** (device units → divide by 3 to get LPM; `o.d()` returns this). Capture `0x40`=64 → 21 LPM |
| 13 | `c`, plus `f,g,h,i` booleans | `Lf5/a;->{g,k,j,h}(B)` | **outletState bitmap** (same bit map as `0xAB` outlet2; see §1.3). Capture `0x00` → all outlets OFF |
| 14 | `l` (high byte) | `Lf5/a;->c(B,B) / 8.5` then `Math.round` | **measured/probe temperature × 8.5** (rounded to int LPM-ish unit). Capture `0x01 0x1a` = 282 / 8.5 ≈ 33.18 — plausible ambient/inlet temp |
| 15 | `l` (low byte) | (see byte 14) | |
| 16 | `m` | `Lf5/d;->i(B) / 3` | secondary flow / `Unknown` (zero in capture) |
| 17 | `j` | `Lf5/d;->i(B)` | secondary status int (capture `0x01`); `c()` getter returns this |

### 2.2 Byte 0 — system status sub-decode

`payload[0]` is bit-decoded into several fields:

| Bits | Decoder | Field | Meaning |
|---|---|---|---|
| 0–2 (`0x07`) | `Lf5/a;->e(B)` op 0000–0014 | `p` (int) | status code; 0→0, 1→1, 3→3, 4→4, else→2 (so `2` is the "other" bucket) |
| bit 3 (`0x08`) | `Lf5/a;->o(B)` | `u` (bool) | unknown sub-flag |
| bit 4 (`0x10`) | `Lf5/a;->m(B)` | `t` (bool) | unknown sub-flag |
| bit 5 (`0x20`) | `Lf5/a;->t(B)` | `r` (bool) | unknown sub-flag |
| **bit 6 (`0x40`)** | `Lf5/a;->u(B)` | `s` (bool) | **session-bonded / paired-with-this-session ready** — exactly matches the `0x31→0x71` flip observed after re-pair |
| bit 7 (`0x80`) | — | — | `Unknown` (zero in both captures) |

The bit-6 flip from `0x31` (pre-pair) to `0x71` (post-pair) is **decoded as
field `s` = `Lf5/a;->u(payload[0])`** — the cleanest "available"/"healthy
session" indicator. Use this for the integration's availability flag.

### 2.3 Confirmed against capture

`payload = 31 20 00 00 00 00 00 00 90 00 01 7c 40 00 01 1a 00 01`

- `payload[0] = 0x31` = status_code 1, bit4/bit5 set, **bit6=0** (not bonded
  yet — pre-pair capture)
- `payload[8] = 0x90` = IOT status raw; bit 4 set; passed to `f5/a.n`
- `payload[10..11] = 0x01 0x7c` → 380 → **target_temp = 38.0 °C** ✓
- `payload[12] = 0x40` → 64 → flow_scaled / 3 = **21 LPM**
- `payload[13] = 0x00` → no outlets active → **OFF**
- `payload[14..15] = 0x01 0x1a` → 282/8.5 ≈ **33.2** (probe temp ~33°C)
- `payload[17] = 0x01` → status int (j field)

After re-pair the payload becomes `71 20 …` — same except bit 6 of byte 0
flips → "session ready". This is consistent with our model.

### 2.4 What's still `Unknown`

- payload bytes 1–7 (apart from byte 6 bit 7) are not read by the parser
  and may be reserved / firmware-version fields. Safe to ignore.
- The exact meaning of system-status code values 0/1/2/3/4 — the strings
  table dump shows status labels but no direct mapping; needs further
  trace if needed.
- The `/3` vs `*4` flow scaling asymmetry: read = `raw / 3`, write =
  `LPM * 4`. Likely two different device-side scales for two different
  fields; tests will tell us if we should send `raw_read_byte` back
  unchanged, or convert via `LPM`. Empirically `*4` is what the app uses.

---

## 3. Concrete patch directives for `coordinator.py`

### `_send_temperature_frame`

```python
# was: flags = 0x80 if running else 0x00, outlet1 = 255/0
# correct:
flags    = 0x00                                              # normal "set" op
temp_x10 = int(round(self._state["target_temp"] * 10))
outlet1  = int(self._state.get("flow_lpm", 12)) * 4 & 0xFF   # flow encoded ×4
outlet2  = 0x04 if self._state["outlet1_on"] else 0x00       # bit 2 = head outlet
# (set bit 1 = 0x02 / bit 0 = 0x01 for the other outlets if added later)
```

For a clean "all off" (matching turnOffAllOutlets):
```python
flags=1, temp_x10=0, outlet1=0, outlet2=0
```

### `_async_update_data` (parse 0x2B response)

```python
p = frame.payload   # 18 bytes
status0 = p[0]
session_ready = bool(status0 & 0x40)         # bit 6
status_code   = status0 & 0x07               # bits 0-2
target_temp   = (((p[10] & 0x01) << 8) | p[11]) / 10.0
flow_lpm      = p[12] // 3
outlet_state  = p[13]
outlet1_on    = bool(outlet_state & 0x04)    # head
outlet2_on    = bool(outlet_state & 0x02)
outlet0_on    = bool(outlet_state & 0x01)
running       = bool(outlet_state & 0x07)    # any outlet on
paused        = (outlet_state == 0x40)
error         = (outlet_state == 0x80)
measured_temp = ((p[14] << 8) | p[15]) / 8.5 # probe temperature
iot_status    = p[8]                         # raw; ready when decoded ==2
```

Set `available = session_ready` (or `True` once we've seen at least one
valid response). The current "available based on advertising presence"
should stay as a fallback, but the parsed bit 6 is the authoritative
session-healthy indicator.
