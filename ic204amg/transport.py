"""The Openport 2.0 over J2534, through udsoncan's J2534Connection."""

from __future__ import annotations

import ctypes
import logging
import struct
from collections.abc import Callable
from typing import Protocol

from udsoncan.connections import J2534Connection

from . import uds

log = logging.getLogger(__name__)

OP20_DLL = r"C:\Windows\SysWOW64\op20pt32.dll"
# CP_S3_TP_FUNC_TIMER in the IC_204 CBF: the interval Xentry itself uses.
TESTER_PRESENT_MS = 2000
WRITE_TIMEOUT_MS = 1000


class TransportError(Exception):
    pass


class Transport(Protocol):
    def send(self, data: bytes) -> None: ...

    def recv(self, timeout: float) -> bytes | None: ...

    def flush(self) -> None: ...


class J2534Transport:
    """op20pt32.dll is 32-bit with no 64-bit twin, so this only works in a 32-bit Python."""

    def __init__(self, txid: int, rxid: int, baud: int = 500000, dll: str = OP20_DLL):
        self.txid, self.rxid, self.baud, self.dll = txid, rxid, baud, dll
        self._conn: J2534Connection | None = None
        self._start_periodic: Callable[..., int] | None = None
        self._stop_periodic: Callable[..., int] | None = None
        self._periodic_id: int | None = None

    @property
    def conn(self) -> J2534Connection:
        if self._conn is None:
            raise TransportError("Openport is not open")
        return self._conn

    def open(self) -> None:
        if struct.calcsize("P") != 4:
            raise TransportError("op20pt32.dll is 32-bit; run with the bundled 32-bit python.exe")
        try:
            conn = J2534Connection(self.dll, rxid=self.rxid, txid=self.txid, baudrate=self.baud)
        except RuntimeError as e:
            raise TransportError(f"Openport open failed: {e}") from e
        conn.open()
        self._conn = conn
        self._bind_periodic()
        log.info("Openport open: %s", self.versions())

    def _bind_periodic(self) -> None:
        # udsoncan's PassThruStartPeriodicMsg wrapper sends a message with no ProtocolID,
        # TxFlags or CAN-id header, so the raw entry points are bound here (stdcall, like
        # every J2534 export).
        from udsoncan import j2534 as j

        dll = self.conn.interface.hDLL
        c_ulong, ptr = ctypes.c_ulong, ctypes.POINTER
        start = ctypes.WINFUNCTYPE(
            ctypes.c_long, c_ulong, ptr(j.PASSTHRU_MSG), ptr(c_ulong), c_ulong
        )
        stop = ctypes.WINFUNCTYPE(ctypes.c_long, c_ulong, c_ulong)
        self._start_periodic = start(("PassThruStartPeriodicMsg", dll))
        self._stop_periodic = stop(("PassThruStopPeriodicMsg", dll))

    def _last_error(self) -> str:
        return self.conn.interface.PassThruGetLastError()[1]

    def versions(self) -> str:
        c = self.conn
        return (
            f"firmware {c.firmwareVersion.value.decode()} "
            f"dll {c.dllVersion.value.decode()} api {c.apiVersion.value.decode()}"
        )

    def vbatt(self) -> float | None:
        from udsoncan import j2534 as j

        conn = self.conn
        mv = ctypes.c_ulong()
        with conn.interfaceSemaphore:
            res = conn.interface.PassThruIoctl(conn.devID, j.Ioctl_ID.READ_VBATT, None, mv)
        if res != j.Error_ID.ERR_SUCCESS:
            log.warning("READ_VBATT failed: %s (%s)", self._last_error(), res.name)
            return None
        return mv.value / 1000

    def send(self, data: bytes) -> None:
        from udsoncan import j2534 as j

        conn = self.conn
        # udsoncan's own send() raises with interfaceSemaphore still held when the write
        # fails, which hangs its rx thread and close(); drive the interface directly.
        with conn.interfaceSemaphore:
            res = conn.interface.PassThruWriteMsgs(
                conn.channelID, data, conn.protocol.value, Timeout=WRITE_TIMEOUT_MS
            )
        if res != j.Error_ID.ERR_SUCCESS:
            raise TransportError(f"PassThruWriteMsgs: {self._last_error()} ({res.name})")

    def recv(self, timeout: float) -> bytes | None:
        return self.conn.wait_frame(timeout=timeout, exception=False)

    def flush(self) -> None:
        self.conn.empty_rxqueue()

    def start_tester_present(self, interval_ms: int = TESTER_PRESENT_MS) -> None:
        """Firmware-side periodic 3E 80; the cluster's S3 timer would drop the session otherwise."""
        from udsoncan import j2534 as j

        conn = self.conn
        if self._start_periodic is None:
            raise TransportError("periodic messages not bound")
        msg = j.PASSTHRU_MSG()
        msg.ProtocolID = j.Protocol_ID.ISO15765.value
        msg.TxFlags = j.TxFlags.ISO15765_FRAME_PAD.value
        msg.setData(self.txid.to_bytes(4, "big") + uds.tester_present())
        msg_id = ctypes.c_ulong()
        with conn.interfaceSemaphore:
            res = self._start_periodic(
                conn.channelID, ctypes.byref(msg), ctypes.byref(msg_id), ctypes.c_ulong(interval_ms)
            )
        if res != 0:
            raise TransportError(f"PassThruStartPeriodicMsg: {self._last_error()} ({res})")
        self._periodic_id = msg_id.value
        log.info("TesterPresent: firmware periodic every %d ms (id %d)", interval_ms, msg_id.value)

    def stop_tester_present(self) -> None:
        if self._periodic_id is None or self._stop_periodic is None:
            return
        conn = self.conn
        with conn.interfaceSemaphore:
            res = self._stop_periodic(conn.channelID, self._periodic_id)
        self._periodic_id = None
        if res != 0:
            log.warning("PassThruStopPeriodicMsg: %s (%s)", self._last_error(), res)

    def close(self) -> None:
        if self._conn is None:
            return
        try:
            self.stop_tester_present()
        finally:
            self._conn.close()
            self._conn = None
