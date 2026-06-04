"""Mira Activate BLE frame builder + parser.

Wire framing (see PROTOCOL_SPEC.md §1.2 and §3.2):

    REQ:  AA 55 00 opcode body_len <payload>      chk8
    RSP:  AA 55 <rsvd> opcode body_len <payload>  chk8

`chk8` is the 2's-complement of the byte-sum of every preceding byte, i.e.
`(sum(frame_with_chk) & 0xFF) == 0`.

Only the opcodes we actually use in this scaffold are listed. Add more from
PROTOCOL_SPEC.md §2.3 as needed.
"""

from __future__ import annotations

from dataclasses import dataclass

# Service UUIDs (PROTOCOL_SPEC.md §0)
SERVICE_UUID = "267f0001-eb15-43f5-94c3-67d2221188f7"
WRITE_CHAR_UUID = "267f0002-eb15-43f5-94c3-67d2221188f7"
NOTIFY_CHAR_UUID = "267f0003-eb15-43f5-94c3-67d2221188f7"


def device_id_from_name(name: str | None) -> str | None:
    """Stable per-unit id from the advertised local name.

    The Activate uses a random BLE address that it regenerates (e.g. on a
    power-cycle), so the address can't be a stable identity. We key on the
    first token after "MIRA" instead — but the unit advertises under more than
    one local-name form, and they MUST collapse to the same id or HA discovery
    treats each form as a brand-new device:

      - firmware default:  "Mira 003F#0203090622105802"  (model#serial)
      - user-set name:     "Mira 003F en suite"

    Only the part before the '#' (the model code, "003F") is present in every
    form, so we truncate there. Single-unit assumption: two Activates sharing a
    model code would collide — acceptable for a per-home install, and the only
    token recoverable once a unit is renamed in the Mira app.

    Returns None if the name doesn't match (caller falls back to the address).
    """
    if not name:
        return None
    parts = name.split()
    if len(parts) >= 2 and parts[0].upper() == "MIRA":
        return parts[1].upper().split("#", 1)[0]
    return None

# Frame constants
SYNC = bytes([0xAA, 0x55, 0x00])
SYNC_LEN = 3
HEADER_LEN = 5  # sync (3) + opcode (1) + body_len (1)
TRAILER_LEN = 1  # chk8
OVERHEAD = HEADER_LEN + TRAILER_LEN

# Opcodes (PROTOCOL_SPEC.md §2.3)
OP_UNIT_PRIME = 0x2B  # poll/hello (body=0x02)
OP_READ_IFACE_NAME = 0x44  # body_len=0
OP_READ_DTM = 0x41  # body_len=0
OP_SET_TEMPERATURE = 0xAB  # OperateOutlets analogue, body_len=4
OP_READ_OUTLET_CONFIG = 0x1B  # idx in slot byte
OP_READ_COMMISSIONING = 0x78  # body_len=0
OP_EXT_RESET_VALVE = 0xF4


def chk8(data: bytes) -> int:
    """8-bit 2's-complement checksum (Lf5/d;->e)."""
    return ((~sum(data) + 1) & 0xFF)


def build(opcode: int, payload: bytes = b"") -> bytes:
    """Build a request frame: SYNC + opcode + body_len + payload + chk8."""
    frame = SYNC + bytes([opcode, len(payload)]) + payload
    return frame + bytes([chk8(frame)])


# Specific frames whose payload byte is fixed by the official app.

def frame_unit_prime() -> bytes:
    """The "Read unit prime data" poll opcode that the official app fires every
    500ms after pairing. Body byte `0x02` is hardcoded in Lf5/g;->u."""
    return build(OP_UNIT_PRIME, bytes([0x02]))


def frame_set_temperature(
    flags: int, temp_x10: int, flow_x4: int, outlet_state: int
) -> bytes:
    """0xAB Set Temperature (PROTOCOL_SPEC.md §2.3, AB_2B_DECODE.md §1).

    Args:
        flags: state flags int (per Lf5/e;->w arg v20).
            - 0 = "normal write" — apply outlet_state as the new bitmap.
            - 1 = "force stop / clear all outlets" (used with
              outlet_state=0; matches the app's turnOffAllOutlets path).
            - 0x80 is NOT a valid flag — current "running" guess was wrong.
        temp_x10: target temperature × 10 (e.g. 38.0°C → 380). Lower 9 bits
            occupy byte5 bit 0 and all of byte 6. flags is OR'd into the
            upper bits of byte 5 (collision-safe at bit 7 and above).
        flow_x4: desired flow rate × 4 (e.g. 21 LPM → 84). NOT a boolean.
            The app's `h5/a.d() * 4` encoding.
        outlet_state: bit-packed outlet bitmap:
            bit 0 (0x01) = outlet 0 on
            bit 1 (0x02) = outlet 2 (3rd) on
            bit 2 (0x04) = outlet 1 (head/main) on
            bit 6 (0x40) = pause
    """
    temp_hi = (temp_x10 >> 8) & 0xFF
    temp_lo = temp_x10 & 0xFF
    payload = bytes(
        [
            (flags | temp_hi) & 0xFF,
            temp_lo,
            flow_x4 & 0xFF,
            outlet_state & 0xFF,
        ]
    )
    return build(OP_SET_TEMPERATURE, payload)


# 0x2B response payload bit definitions — CORRECTED from live HCI snoop
# (2026-05-30, the Mira app actually controlling the shower). The earlier
# APK decode was wrong: the app NEVER sends bit 2 (0x04). The real bits:
OUTLET_BIT_A = 0x01  # outlet "A" (one of the two)
OUTLET_BIT_B = 0x02  # outlet "B" (the other)
OUTLET_BIT_BOTH = 0x03  # both on at once
OUTLET_PAUSE = 0x40  # pause flag
OUTLET_ERROR = 0x80  # error flag (device-set, read-only)
# Aliases kept for backward compat with imports in coordinator/switch/number.
OUTLET_BIT_0 = OUTLET_BIT_A
OUTLET_BIT_1 = OUTLET_BIT_B
OUTLET_BIT_2 = 0x04  # unused on 2-outlet models — kept to satisfy imports

# System status byte (payload byte 0) bits:
STATUS_CODE_MASK = 0x07
STATUS_BIT_3 = 0x08
STATUS_BIT_4 = 0x10
STATUS_BIT_5 = 0x20
STATUS_SESSION_READY = 0x40  # bit 6: post-pair "session bonded" flag
STATUS_BIT_7 = 0x80


def parse_unit_prime(payload: bytes) -> dict:
    """Decode the 18-byte 0x2B response payload into a state dict.

    Returns keys: status_code, session_ready, status_bits, iot_status,
    target_temp, flow_lpm, outlet_state, outlet0_on, outlet1_on, outlet2_on,
    running, paused, error, measured_temp, secondary_int, raw_status0.
    """
    if len(payload) < 18:
        return {}
    status0 = payload[0]
    outlet_state = payload[13]
    target_temp = (((payload[10] & 0x01) << 8) | payload[11]) / 10.0
    measured_temp = ((payload[14] << 8) | payload[15]) / 8.5
    return {
        "raw_status0": status0,
        "status_code": status0 & STATUS_CODE_MASK,
        "session_ready": bool(status0 & STATUS_SESSION_READY),
        "iot_status": payload[8],
        "target_temp": target_temp,
        "flow_lpm": payload[12] / 4,
        "flow_raw": payload[12],
        "outlet_state": outlet_state,
        "outlet_a_on": bool(outlet_state & OUTLET_BIT_A),
        "outlet_b_on": bool(outlet_state & OUTLET_BIT_B),
        # backward-compat aliases:
        "outlet0_on": bool(outlet_state & OUTLET_BIT_A),
        "outlet1_on": bool(outlet_state & OUTLET_BIT_B),
        "outlet2_on": False,
        "running": bool(outlet_state & (OUTLET_BIT_A | OUTLET_BIT_B)),
        "paused": bool(outlet_state & OUTLET_PAUSE),
        "error": bool(outlet_state & OUTLET_ERROR),
        "measured_temp": round(measured_temp, 1),
        "secondary_int": payload[17],
    }


# Response parsing


@dataclass
class Frame:
    opcode: int
    payload: bytes
    raw: bytes


class IncompleteError(Exception):
    """The accumulated chunks do not yet form a complete frame."""


class InvalidFrameError(Exception):
    """The frame is structurally invalid (sync mismatch / chk8 fail)."""


class FrameAssembler:
    """Accumulates notification chunks until a complete Activate frame.

    The Activate path in `Lg5/b;->b()` only completes when:
        len(chunk) >= 6
        chunk[5] & 0x80 == 0
        chk8 valid
        expected_response_len == chunk[4]    # declared body_len
        expected_response_len + 6 == chunk.length

    i.e. the device sends single-chunk responses for Activate. We still
    accumulate (defensively) in case a future firmware splits, but in practice
    each notification carries one whole frame.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def reset(self) -> None:
        self._buf.clear()

    def feed(self, chunk: bytes) -> Frame:
        """Feed a notification chunk. Returns a Frame iff complete.

        Raises:
            IncompleteError if more bytes are required.
            InvalidFrameError if the buffer is malformed (sync/chk8/length).
        """
        self._buf.extend(chunk)
        b = bytes(self._buf)
        if len(b) < HEADER_LEN + TRAILER_LEN:
            raise IncompleteError()
        if b[0:2] != b"\xAA\x55":
            raise InvalidFrameError(f"bad sync: {b.hex()}")
        declared = b[4]
        if b[5] & 0x80:
            # First payload byte has high bit ⇒ "more chunks" (Mode-style hint).
            # Activate never sets this in practice; keep the guard for safety.
            raise IncompleteError()
        expected = declared + OVERHEAD
        if len(b) < expected:
            raise IncompleteError()
        if len(b) > expected:
            raise InvalidFrameError(
                f"length overflow: got {len(b)} expected {expected}: {b.hex()}"
            )
        if (sum(b) & 0xFF) != 0:
            raise InvalidFrameError(f"chk8 fail: {b.hex()}")
        opcode = b[3]
        payload = b[5:-1]
        self._buf.clear()
        return Frame(opcode=opcode, payload=bytes(payload), raw=b)
