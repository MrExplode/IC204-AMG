"""UDS byte-level helpers for the IC204 AMG-menu job (ISO 14229 over ISO-TP)."""

from __future__ import annotations

from dataclasses import dataclass

SID_SESSION = 0x10
SID_RESET = 0x11
SID_READ_MEMORY = 0x23
SID_SECURITY = 0x27
SID_WRITE_MEMORY = 0x3D
SID_TESTER_PRESENT = 0x3E
POSITIVE_OFFSET = 0x40
NEGATIVE = 0x7F
NRC_RESPONSE_PENDING = 0x78

# The cluster maps its EEPROM at 0x10000000; ALFID 0x14 = 4-byte address, 1-byte length.
EEPROM_BASE = 0x1000_0000
ALFID = 0x14


@dataclass(frozen=True, slots=True)
class Window:
    offset: int
    length: int

    @property
    def name(self) -> str:
        return f"0x{self.offset:04X}"


AMG = Window(0x01D0, 8)
LAMP = Window(0x0260, 10)
# The only EEPROM bytes this tool may ever write; write_memory() refuses anything else.
WRITABLE = (AMG, LAMP)
LAMP_FIX_BYTE = 0x60

NRC = {
    0x10: "general reject",
    0x11: "service not supported",
    0x12: "sub-function not supported",
    0x13: "incorrect message length or invalid format",
    0x22: "conditions not correct",
    0x24: "request sequence error (key sent without a fresh seed)",
    0x31: "request out of range (address or length rejected)",
    0x33: "security access denied (not unlocked or session dropped)",
    0x35: "invalid key (wrong definition or level) - STOP, do not retry",
    0x36: "exceeded number of attempts - STOP, ignition off and wait",
    0x37: "required time delay not expired - wait, do not hammer",
    0x72: "general programming failure",
    0x78: "response pending",
    0x7E: "sub-function not supported in active session",
    0x7F: "service not supported in active session (send 10 03 first)",
}


def hexs(data: bytes | None) -> str:
    if data is None:
        return "(none)"
    if not data:
        return "(empty)"
    return " ".join(f"{b:02X}" for b in data)


def extended_session() -> bytes:
    return bytes([SID_SESSION, 0x03])


def request_seed(level: int) -> bytes:
    return bytes([SID_SECURITY, level])


def send_key(level: int, key: bytes) -> bytes:
    if len(key) != 8:
        raise ValueError("IC204 key must be 8 bytes")
    return bytes([SID_SECURITY, level + 1]) + key


def _address(window: Window) -> bytes:
    return (
        bytes([ALFID]) + (EEPROM_BASE + window.offset).to_bytes(4, "big") + bytes([window.length])
    )


def read_memory(window: Window) -> bytes:
    return bytes([SID_READ_MEMORY]) + _address(window)


def write_memory(window: Window, data: bytes) -> bytes:
    if window not in WRITABLE:
        allowed = ", ".join(w.name for w in WRITABLE)
        raise ValueError(f"refusing to write {window.name}: only {allowed} are writable")
    if len(data) != window.length:
        raise ValueError(f"{window.name} takes {window.length} bytes, got {len(data)}")
    return bytes([SID_WRITE_MEMORY]) + _address(window) + data


def ecu_reset() -> bytes:
    return bytes([SID_RESET, 0x01])


def tester_present() -> bytes:
    return bytes([SID_TESTER_PRESENT, 0x80])


class UdsError(Exception):
    pass


class NoResponseError(UdsError):
    pass


class UnexpectedResponseError(UdsError):
    pass


class NegativeResponseError(UdsError):
    def __init__(self, req: bytes, resp: bytes):
        self.req, self.resp = req, resp
        self.nrc = resp[2] if len(resp) > 2 else None
        meaning = NRC.get(self.nrc, "unknown NRC") if self.nrc is not None else "malformed"
        super().__init__(f"{hexs(req)} -> {hexs(resp)}: {meaning}")


def is_for(req: bytes, resp: bytes) -> bool:
    """True if resp answers req: its positive response, or a negative naming req's service."""
    if resp[0] == NEGATIVE:
        return len(resp) >= 2 and resp[1] == req[0]
    return resp[0] == req[0] + POSITIVE_OFFSET


def is_pending(resp: bytes) -> bool:
    return len(resp) >= 3 and resp[0] == NEGATIVE and resp[2] == NRC_RESPONSE_PENDING


def check(req: bytes, resp: bytes | None) -> bytes:
    """Return resp if it is the positive response to req, else raise."""
    if not resp:
        raise NoResponseError(f"{hexs(req)}: no response")
    if resp[0] == NEGATIVE:
        raise NegativeResponseError(req, resp)
    if resp[0] != req[0] + POSITIVE_OFFSET:
        raise UnexpectedResponseError(
            f"{hexs(req)} -> {hexs(resp)}: not a response to this service"
        )
    return resp


def session_timing(resp: bytes) -> tuple[float, float]:
    """P2 and P2* in seconds from a 50 xx reply (P2 in ms, P2* in 10 ms units); 0 if absent."""
    if len(resp) < 6:
        return 0.0, 0.0
    return int.from_bytes(resp[2:4], "big") / 1000, int.from_bytes(resp[4:6], "big") / 100


def memory_data(window: Window, resp: bytes) -> bytes:
    """Data bytes of a 0x63 reply; the length must match the window exactly."""
    if len(resp) != 1 + window.length:
        raise UnexpectedResponseError(
            f"read {window.name} -> {hexs(resp)}: expected {window.length} data bytes"
        )
    return resp[1:]


def check_write_echo(req: bytes, resp: bytes) -> None:
    """A 0x7D reply must echo ALFID + address + length (e.g. 7D 14 10 00 01 D0 08)."""
    if resp != bytes([SID_WRITE_MEMORY + POSITIVE_OFFSET]) + req[1:7]:
        raise UnexpectedResponseError(f"{hexs(req)} -> {hexs(resp)}: write echo mismatch")
