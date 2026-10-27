"""Scripted stand-ins: a Transport answering from a handler, and enough of an IC204."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from ic204amg import uds
from ic204amg.seedkey import ic204_generate_key

SW = "2049022903"
SEED = bytes.fromhex("5C97A0A552FB0205")
KEY = ic204_generate_key(0x09, SEED, SW)
# P2 50 ms, P2* 5000 ms
SESSION_TIMING = bytes.fromhex("003201F4")


class FakeEcu:
    """Transport whose ECU is handler(request) -> frames, handed out one per recv()."""

    def __init__(self, handler: Callable[[bytes], list[bytes]]):
        self.handler = handler
        self.sent: list[bytes] = []
        self.timeouts: list[float] = []
        self._pending: list[bytes] = []

    def send(self, data: bytes) -> None:
        self.sent.append(data)
        self._pending.extend(self.handler(data))

    def recv(self, timeout: float) -> bytes | None:
        self.timeouts.append(timeout)
        return self._pending.pop(0) if self._pending else None

    def flush(self) -> None:
        self._pending.clear()


class FakeIc204:
    """Session gate, seed/key, two EEPROM windows, reset counter."""

    def __init__(
        self,
        amg: str = "FC00112233445566",
        lamp: str = "00112233445566778899",
        *,
        seed: bytes = SEED,
        accept_key: bool = True,
        pending_reads: bool = False,
        unlocked: bool = False,
        noise: Sequence[bytes] = (),
        drop_writes: bool = False,
    ):
        self.mem = {uds.AMG: bytearray.fromhex(amg), uds.LAMP: bytearray.fromhex(lamp)}
        self.seed, self.accept_key, self.pending_reads = seed, accept_key, pending_reads
        self.noise, self.drop_writes = list(noise), drop_writes
        self.unlocked, self.session, self.resets = unlocked, 1, 0

    def __call__(self, req: bytes) -> list[bytes]:
        return self.noise + self._reply(req)

    def _reply(self, req: bytes) -> list[bytes]:
        sid = req[0]
        if sid == uds.SID_SESSION:
            self.session = req[1]
            return [bytes([0x50, req[1]]) + SESSION_TIMING]
        if sid == uds.SID_TESTER_PRESENT:
            return []
        if self.session != 3:
            return [bytes([uds.NEGATIVE, sid, 0x7F])]
        if sid == uds.SID_SECURITY:
            sub = req[1]
            if sub % 2:  # odd sub-function = request seed, even = send key for sub - 1
                return [bytes([0x67, sub]) + self.seed]
            expected = ic204_generate_key(sub - 1, self.seed, SW)
            if self.accept_key and req[2:] == expected:
                self.unlocked = True
                return [bytes([0x67, sub])]
            return [bytes([uds.NEGATIVE, sid, 0x35])]
        if sid in (uds.SID_READ_MEMORY, uds.SID_WRITE_MEMORY):
            if not self.unlocked:
                return [bytes([uds.NEGATIVE, sid, 0x33])]
            window = uds.Window(int.from_bytes(req[2:6], "big") - uds.EEPROM_BASE, req[6])
            if window not in self.mem:
                return [bytes([uds.NEGATIVE, sid, 0x31])]
            if sid == uds.SID_READ_MEMORY:
                out = [bytes([0x63]) + bytes(self.mem[window])]
                pending = [bytes([uds.NEGATIVE, sid, 0x78])] if self.pending_reads else []
                return pending + out
            if not self.drop_writes:
                self.mem[window][:] = req[7 : 7 + window.length]
            return [bytes([0x7D]) + req[1:7]]
        if sid == uds.SID_RESET:
            self.resets += 1
            self.unlocked, self.session = False, 1
            return [bytes([0x51, 0x01])]
        return [bytes([uds.NEGATIVE, sid, 0x11])]
