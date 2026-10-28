"""IC204 seed-key algorithm (NEC V850E1 cluster firmware, reverse engineered).

Python implementation of the same algorithm shipped by jglim/UnlockECU (MIT licensed;
Security/IC204.cs). The salt tables (ic204_salts.json) are the IC204 entries of UnlockECU's
db.json, reverse-engineered firmware constants; the test vectors are from
https://github.com/jglim/UnlockECU/issues/10. The reverse engineering is also documented at
https://github.com/Mattwmaster58/ic204.

The core is bit-identical across all firmware versions; only the per-level 8-byte salt tables
differ. Firmware functions: FUN_00180b56 rotate_right_array, FUN_00180bd2 salt_transform,
FUN_00180f14 ic204_generate_key.

Algorithm:
  1. Permute the 8-byte seed into workspace ws[0x10..0x17]
  2. LFSR iteration count from a seed byte selected by the security level
  3. 8-byte LFSR state from the salt (salt_transform, level-dependent)
  4. Run LFSR: feedback XOR of specific bit taps, then rotate-right the 8-byte state
  5. Copy permuted seed to ws[0x00..0x07]
  6. Feistel-like loop (2 rounds, each with 2 half-rounds): XOR upper half with lower half,
     rotate upper half by a data-dependent count, add (32-bit LE) LFSR state, swap halves
  7. Reverse-permute the result into the 8-byte key
"""

from __future__ import annotations

import functools
import json
from importlib import resources

# (sw, uds_level, seed_hex, key_hex)
VECTORS = (
    ("2129026108", 0x01, "0000000000000000", "D632404B132FE12B"),
    ("2129026108", 0x03, "0000000000000000", "6552C46251393265"),
    ("2129026108", 0x09, "0000000000000000", "B83B5E73528197D3"),
    ("2129026108", 0x0D, "0000000000000000", "162FA93147161D00"),
    ("2129026108", 0x09, "212A2F38F98A8BD7", "775588C8850CF244"),
    ("2129026108", 0x09, "05E8242E8011F8FF", "B73353CD123F6837"),
    ("2129026108", 0x0D, "537C818A537C818A", "7FD7DCB083DCFB46"),
    ("2129026108", 0x0D, "28C1050F7B52C7CE", "AC12805D446FDF54"),
    ("2049022903", 0x09, "5C97A0A552FB0205", "D8F169D68D5D17B6"),
    ("2049022903", 0x0D, "C1EBF4F94CA0A7A6", "49D4BE45A0B6DFF3"),
)

# UDS SecurityAccess sub-function -> internal level (selects the seed byte the LFSR count and
# the rotations key on), verified by the vectors; the ECU uses no other levels.
UDS_TO_INTERNAL = {0x01: 1, 0x03: 3, 0x09: 5, 0x0D: 7}

# {SW part number: {UDS level: 8-byte salt}}, one row per level the ECU uses.
SALTS_FILE = "ic204_salts.json"


@functools.cache
def _salt_tables() -> dict[str, dict[int, bytes]]:
    raw = json.loads(resources.files(__package__).joinpath(SALTS_FILE).read_text("utf-8"))
    return {
        sw: {int(level): bytes.fromhex(salt) for level, salt in levels.items()}
        for sw, levels in raw.items()
        if sw.isdigit()
    }


def supported_sw() -> list[str]:
    """Every SW part number with a salt table."""
    return sorted(_salt_tables())


def get_salt_table(sw_version: str) -> dict[int, bytes]:
    """UDS level -> salt for a SW part number; ValueError if unsupported."""
    try:
        return _salt_tables()[sw_version]
    except KeyError:
        raise ValueError(
            f"no seed-key table for SW {sw_version}; supported: {', '.join(supported_sw())}"
        ) from None


def rotate_right_array(buf: bytearray, length: int) -> None:
    """FUN_00180b56: rotate buf[:length] right by 1 bit as one contiguous bitfield."""
    carries = [buf[i] & 1 for i in range(length)]
    for i in range(length):
        shifted = (buf[i] >> 1) & 0x7F
        if carries[length - 1 if i == 0 else i - 1]:
            shifted |= 0x80
        buf[i] = shifted


def _rotate_right(buf: bytearray, count: int) -> bytearray:
    out = bytearray(buf)
    for _ in range(count):
        rotate_right_array(out, len(out))
    return out


def salt_transform(salt: bytes) -> bytearray:
    """FUN_00180bd2: 8-byte LFSR initial state from the salt alone (never reads the seed).

    Phases follow the firmware's stack frame local[0x00..0x13], with the salt at
    local[0x0C..0x13].
    """
    local = bytearray(0x14)
    local[0x0C:0x14] = salt

    # salt[4..7] -> local[0..3], salt[0..3] -> local[4..7], local[0..3] -> local[8..B]
    local[0x00:0x04] = local[0x10:0x14]
    local[0x04:0x08] = local[0x0C:0x10]
    local[0x08:0x0C] = local[0x00:0x04]

    # transpose local[8..B] -> local[0..3], rotate both by a nibble-selected count, XOR
    t = local[0x08:0x0C]
    local[0x02], local[0x00], local[0x01], local[0x03] = t[0], t[1], t[3], t[2]
    local[0x00:0x04] = _rotate_right(local[0x00:0x04], (t[2] & 0x0F) + 1)
    local[0x08:0x0C] = _rotate_right(local[0x08:0x0C], (local[0x0B] & 0x0F) + 1)
    for i in range(4):
        local[i] ^= local[0x08 + i]

    # same again for the other half
    local[0x08:0x0C] = local[0x04:0x08]
    t = local[0x08:0x0C]
    local[0x06], local[0x04], local[0x05], local[0x07] = t[0], t[1], t[3], t[2]
    local[0x04:0x08] = _rotate_right(local[0x04:0x08], (local[0x0A] & 0x0F) + 1)
    local[0x08:0x0C] = _rotate_right(local[0x08:0x0C], (local[0x0B] & 0x0F) + 1)
    for i in range(4):
        local[0x04 + i] ^= local[0x08 + i]

    # output is local[0x0C..0x13] = local[4..7] then local[0..3]
    return local[0x04:0x08] + local[0x00:0x04]


def compute_lfsr_feedback(ws: bytearray) -> None:
    """LFSR feedback at LAB_00180f82: XOR of 8 bit taps into bit 7 of ws[0x0F]."""
    fb = (ws[0x08] >> 3) & 1
    fb ^= ws[0x09] & 1
    fb ^= (ws[0x0A] >> 1) & 1
    fb ^= (ws[0x0B] >> 7) & 1
    fb ^= (ws[0x0C] >> 5) & 1
    fb ^= (ws[0x0D] >> 2) & 1
    fb ^= (ws[0x0E] >> 6) & 1
    fb ^= (ws[0x0F] >> 4) & 1
    ws[0x0F] = (ws[0x0F] & 0x7F) | (fb << 7)


def _half_round(ws: bytearray, level: int, lfsr_offset: int) -> None:
    for i in range(4):
        ws[0x10 + i] ^= ws[0x14 + i]
    ws[0x10:0x14] = _rotate_right(ws[0x10:0x14], (ws[level] & 0x03) + 1)
    total = int.from_bytes(ws[0x10:0x14], "little") + int.from_bytes(
        ws[lfsr_offset : lfsr_offset + 4], "little"
    )
    ws[0x10:0x14] = (total & 0xFFFFFFFF).to_bytes(4, "little")
    ws[0x14:0x18] = ws[0x00:0x04]
    ws[0x00:0x08] = ws[0x10:0x18]


def ic204_generate_key(uds_level: int, seed: bytes, sw_version: str) -> bytes:
    """FUN_00180f14: the 8-byte key for an 8-byte seed at a UDS SecurityAccess level."""
    if len(seed) != 8:
        raise ValueError(f"seed must be 8 bytes, got {len(seed)}")
    if uds_level not in UDS_TO_INTERNAL:
        raise ValueError(f"unsupported SecurityAccess level {uds_level:02X}")
    level = UDS_TO_INTERNAL[uds_level]
    salt = get_salt_table(sw_version)[uds_level]

    # ws[0x00..0x07] Feistel state, ws[0x08..0x0F] LFSR state, ws[0x10..0x17] permuted seed
    ws = bytearray(0x20)
    ws[0x10:0x18] = bytes(seed[i] for i in (7, 4, 3, 6, 5, 1, 0, 2))
    lfsr_count = (ws[0x10 + level] & 0x07) + 2
    ws[0x08:0x10] = salt_transform(salt)
    for _ in range(lfsr_count):
        compute_lfsr_feedback(ws)
        ws[0x08:0x10] = _rotate_right(ws[0x08:0x10], 1)
    ws[0x00:0x08] = ws[0x10:0x18]
    for _ in range(2):
        _half_round(ws, level, 0x08)
        _half_round(ws, level, 0x0C)
    return bytes(ws[i] for i in (3, 5, 6, 1, 0, 7, 4, 2))
