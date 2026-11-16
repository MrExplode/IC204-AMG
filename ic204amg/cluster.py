"""The procedure: session, unlock, backup reads, flag-byte decision, verified writes."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from . import uds
from .profile import W204_COLOUR, Profile
from .seedkey import ic204_generate_key
from .transport import Transport

log = logging.getLogger(__name__)

LEVEL = 0x09
# Floors; the 50 03 reply can only raise them (the CBF says P2 150 ms, P2* 2000 ms).
P2 = 1.0
P2_STAR = 3.0

# The cluster reboots after 11 01 and does not answer until it is back up.
RESET_SETTLE = 10.0
STOP_WORDS = ("q", "quit", "stop")

Confirm = Callable[[str, str], bool]
Ask = Callable[[str], str]
Sleep = Callable[[float], None]


class Stop(Exception):  # noqa: N818 - a controlled halt, not a failure
    """A safety rule halts the job here. Nothing further is written."""


@dataclass(frozen=True, slots=True)
class Backup:
    """Original bytes of the profile's windows; lamp is None when the profile has no lamp fix."""

    amg: bytes
    lamp: bytes | None = None
    meta: dict[str, str] = field(default_factory=dict)

    def save(self, path: Path, profile: Profile) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = {profile.amg.name: self.amg.hex().upper()}
        if profile.lamp is not None and self.lamp is not None:
            doc[profile.lamp.window.name] = self.lamp.hex().upper()
        path.write_text(json.dumps(doc | self.meta, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path, profile: Profile) -> Backup:
        names = {profile.amg.name}
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            amg = _window_bytes(doc, profile.amg)
            lamp = None
            if profile.lamp is not None and profile.lamp.window.name in doc:
                lamp = _window_bytes(doc, profile.lamp.window)
                names.add(profile.lamp.window.name)
            meta = {k: str(v) for k, v in doc.items() if k not in names}
            return cls(amg, lamp, meta)
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"{path}: not a backup file ({e})") from e


def _window_bytes(doc: dict[str, object], window: uds.Window) -> bytes:
    data = bytes.fromhex(str(doc[window.name]))
    if len(data) != window.length:
        raise ValueError(f"{window.name} must be {window.length} bytes, got {len(data)}")
    return data


class Cluster:
    def __init__(
        self,
        transport: Transport,
        sw: str,
        level: int = LEVEL,
        p2: float = P2,
        p2_star: float = P2_STAR,
        meta: Mapping[str, str] | None = None,
        profile: Profile = W204_COLOUR,
    ):
        self.t = transport
        self.sw, self.level, self.p2, self.p2_star = sw, level, p2, p2_star
        self.meta = dict(meta or {})
        self.profile = profile

    def request(self, label: str, req: bytes) -> bytes:
        self.t.flush()
        log.info("%-22s -> %s", label, uds.hexs(req))
        self.t.send(req)
        deadline = time.monotonic() + self.p2
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise uds.NoResponseError(f"{uds.hexs(req)}: no response")
            resp = self.t.recv(remaining)
            # udsoncan's rx thread queues b"" when PassThruReadMsgs times out.
            if not resp:
                continue
            log.info("%-22s <- %s", "", uds.hexs(resp))
            if not uds.is_for(req, resp):
                log.info("%-22s    ignored, not for this request", "")
                continue
            if uds.is_pending(resp):
                deadline = time.monotonic() + self.p2_star
                continue
            return uds.check(req, resp)

    def extended_session(self) -> None:
        resp = self.request("extended session", uds.extended_session())
        p2, p2_star = uds.session_timing(resp)
        self.p2, self.p2_star = max(self.p2, p2), max(self.p2_star, p2_star)
        log.info("timeouts: P2 %.2f s, P2* %.2f s", self.p2, self.p2_star)

    def unlock(self, level: int | None = None) -> None:
        level = self.level if level is None else level
        resp = self.request("request seed", uds.request_seed(level))
        seed = resp[2:]
        if len(seed) != 8:
            raise uds.UnexpectedResponseError(f"seed reply has {len(seed)} seed bytes, expected 8")
        if seed == bytes(8):
            log.info("seed is all zero: already unlocked")
            return
        key = ic204_generate_key(level, seed, self.sw)
        log.info("seed %s -> key %s (%s level %02X)", uds.hexs(seed), uds.hexs(key), self.sw, level)
        self.request("send key", uds.send_key(level, key))

    def read_window(self, window: uds.Window) -> bytes:
        resp = self.request(f"read {window.name}", uds.read_memory(window))
        return uds.memory_data(window, resp)

    def write_window(self, window: uds.Window, data: bytes) -> None:
        if window not in self.profile.windows:
            allowed = ", ".join(w.name for w in self.profile.windows)
            raise ValueError(f"refusing to write {window.name}: only {allowed} are writable")
        req = uds.write_memory(window, data)
        uds.check_write_echo(req, self.request(f"write {window.name}", req))
        got = self.read_window(window)
        if got != data:
            raise uds.UnexpectedResponseError(
                f"read-back {window.name} MISMATCH: wrote {uds.hexs(data)} got {uds.hexs(got)}; "
                "restore from the backup"
            )
        log.info("VERIFIED %s = %s", window.name, uds.hexs(got))

    def reset(self) -> None:
        self.request("ECU reset", uds.ecu_reset())

    def backup(self) -> Backup:
        amg = self.read_window(self.profile.amg)
        log.info("BACKUP %s = %s", self.profile.amg.name, uds.hexs(amg))
        lamp = None
        if self.profile.lamp is not None:
            lamp = self.read_window(self.profile.lamp.window)
            log.info("BACKUP %s = %s", self.profile.lamp.window.name, uds.hexs(lamp))
        meta = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "sw": self.sw} | self.meta
        return Backup(amg, lamp, meta)


def run_readonly(cluster: Cluster, backup_path: Path) -> Backup:
    cluster.unlock()
    b = cluster.backup()
    b.save(backup_path, cluster.profile)
    log.info("backup saved: %s", backup_path)
    return b


def run_write(
    cluster: Cluster,
    backup_path: Path,
    confirm: Confirm,
    expect: Backup | None = None,
    write_level: int | None = None,
) -> None:
    b = run_readonly(cluster, backup_path)
    window, rule = cluster.profile.amg, cluster.profile.rule
    if expect is not None and b.amg != expect.amg:
        raise Stop(
            f"{window.name} is {uds.hexs(b.amg)} but the earlier backup says "
            f"{uds.hexs(expect.amg)}; find out why before writing"
        )
    cur = b.amg[rule.byte]
    if cur == rule.off:
        new_val = rule.on
    elif cur == rule.on:
        new_val = rule.off
    else:
        raise Stop(
            f"{window.name} byte {rule.byte} is {cur:02X}, "
            f"not {rule.off:02X}/{rule.on:02X}: do not write"
        )
    new = bytearray(b.amg)
    new[rule.byte] = new_val
    new = bytes(new)
    prompt = (
        f"WRITE {window.name} byte {rule.byte}: {cur:02X} -> {new_val:02X}\n"
        f"  current: {uds.hexs(b.amg)}\n  new:     {uds.hexs(new)}"
    )
    if not confirm(prompt, f"{new_val:02X}"):
        raise Stop("write declined; nothing written")
    # Security access lapses while the prompt waits for the operator, faster than the S3
    # session timer TesterPresent refreshes, so re-unlock immediately before every write.
    cluster.unlock(write_level)
    cluster.write_window(window, new)
    cluster.reset()
    log.info(
        "done: check the AMG page, the odometer and the warning lamps; lamp trouble -> lampfix"
    )


def run_lampfix(
    cluster: Cluster, backup_path: Path, confirm: Confirm, write_level: int | None = None
) -> None:
    """Only after a lamp misbehaves: set the profile's lamp byte."""
    lamp = cluster.profile.lamp
    if lamp is None:
        raise Stop("this profile has no lamp window; nothing to fix")
    b = run_readonly(cluster, backup_path)
    assert b.lamp is not None  # the profile has a lamp window, so backup read it
    new = bytearray(b.lamp)
    new[lamp.byte] = lamp.value
    new = bytes(new)
    if b.lamp == new:
        log.info(
            "%s byte %d already %02X: nothing to write", lamp.window.name, lamp.byte, lamp.value
        )
        return
    prompt = (
        f"WRITE {lamp.window.name} byte {lamp.byte}: {b.lamp[lamp.byte]:02X} -> {lamp.value:02X}\n"
        f"  current: {uds.hexs(b.lamp)}\n  new:     {uds.hexs(new)}"
    )
    if not confirm(prompt, f"{lamp.value:02X}"):
        raise Stop("lamp fix declined; nothing written")
    cluster.unlock(write_level)
    cluster.write_window(lamp.window, new)
    cluster.reset()


def _bits(v: int) -> str:
    return f"{v:08b} ({v:02X})"


def _changed(a: int, b: int) -> str:
    return "".join("^" if (a ^ b) >> (7 - k) & 1 else " " for k in range(8)).rstrip()


def _ask_bits(ask: Ask, prompt: str, base: int, enter: int | None = None) -> int | None:
    """A lamp-byte value from the operator, None to stop.

    8 binary digits set the value, one digit 0-7 flips that bit of `base`; a bare Enter
    returns `enter`, or asks again when that is None.
    """
    while True:
        answer = ask(prompt).strip().replace(" ", "").replace("_", "")
        if not answer:
            if enter is None:
                continue
            return enter
        if answer.lower() in STOP_WORDS:
            return None
        if len(answer) == 8 and set(answer) <= {"0", "1"}:
            return int(answer, 2)
        if len(answer) == 1 and answer in "01234567":
            return base ^ 1 << int(answer)
        log.warning("%r: need 8 binary digits (e.g. 01100000) or one bit number 0-7", answer)


def run_lampbits(
    cluster: Cluster,
    backup_path: Path,
    ask: Ask,
    write_level: int | None = None,
    sleep: Sleep = time.sleep,
) -> None:
    """Experiment: write operator-chosen bits to the lamp byte, reset, repeat until stopped."""
    lamp = cluster.profile.lamp
    if lamp is None:
        raise Stop("this profile has no lamp window; nothing to try")
    b = run_readonly(cluster, backup_path)
    assert b.lamp is not None  # the profile has a lamp window, so backup read it
    window, i = lamp.window, lamp.byte % lamp.window.length
    label = f"LAMP 0x{window.offset + i:04X} ({window.name} byte {i})"
    cur_window, start = b.lamp, b.lamp[i]
    tried: list[int] = []
    now: int | None = start  # None from the moment a write is sent until its read-back verifies
    try:
        while True:
            cur = cur_window[i]
            prompt = (
                f"{label}\n  {'bit:':<10}76543210\n  {'at start:':<10}{_bits(start)}\n"
                f"  {'current:':<10}{_bits(cur)}\n"
                "new value (8 bits, or 0-7 to flip one bit), q to stop: "
            )
            val = _ask_bits(ask, prompt, cur)
            while val is not None and val != cur:
                prompt = (
                    f"WRITE {label}\n  {'current:':<10}{_bits(cur)}\n  {'new:':<10}{_bits(val)}\n"
                    f"  {'':<10}{_changed(cur, val)}\n"
                    "Enter to write, or a correction (8 bits, or 0-7 to flip a bit of new), "
                    "q to stop: "
                )
                corrected = _ask_bits(ask, prompt, val, enter=val)
                if corrected == val:
                    break
                val = corrected
            if val is None:
                break
            if val == cur:
                log.info("lamp byte already %s: nothing to write", _bits(cur))
                continue
            new = bytearray(cur_window)
            new[i] = val
            # An ignition cycle while a prompt waited drops the session; 10 03 again is
            # harmless when it did not.
            cluster.extended_session()
            cluster.unlock(write_level)
            tried.append(val)
            now = None
            cluster.write_window(window, bytes(new))
            now = val
            cluster.reset()
            log.info("waiting %.0f s for the cluster to restart", RESET_SETTLE)
            sleep(RESET_SETTLE)
            cluster.extended_session()
            cluster.unlock()
            cur_window = cluster.read_window(window)
            now = cur_window[i]
            if now == val:
                log.info("lamp byte kept %s across the reset", _bits(now))
            else:
                log.warning(
                    "lamp byte reads %s after the reset, not the written %s: the cluster "
                    "did not keep it",
                    _bits(now),
                    _bits(val),
                )
            note = ask("what do the lamps do now? (note for the log, Enter to skip, q to stop): ")
            note = note.strip()
            if note.lower() in STOP_WORDS:
                break
            if note:
                log.info("NOTE %s: %s", _bits(now), note)
    finally:
        if not tried:
            log.info("lampbits stopped; nothing written")
        else:
            hint = "" if now == start else f"; `restore {backup_path} --lamp` puts {start:02X} back"
            log.info(
                "lampbits stopped: at start %s, tried %s, now %s%s",
                _bits(start),
                " ".join(f"{v:02X}" for v in tried),
                "unknown" if now is None else _bits(now),
                hint,
            )


def run_restore(
    cluster: Cluster,
    saved: Backup,
    confirm: Confirm,
    lamp: bool,
    backup_path: Path,
    write_level: int | None = None,
) -> None:
    current = run_readonly(cluster, backup_path)
    windows = [(cluster.profile.amg, current.amg, saved.amg, cluster.profile.rule.byte)]
    if lamp:
        if cluster.profile.lamp is None:
            raise Stop("this profile has no lamp window to restore")
        windows.append(
            (cluster.profile.lamp.window, current.lamp, saved.lamp, cluster.profile.lamp.byte)
        )
    for window, cur, want, token_byte in windows:
        if cur is None or want is None:
            raise Stop(f"{window.name}: backup has no bytes for this window")
        if cur == want:
            log.info("%s already equals the backup, skipping", window.name)
            continue
        prompt = f"RESTORE {window.name}\n  current: {uds.hexs(cur)}\n  backup:  {uds.hexs(want)}"
        if not confirm(prompt, f"{want[token_byte]:02X}"):
            raise Stop("restore declined")
        cluster.unlock(write_level)
        cluster.write_window(window, want)
    cluster.reset()
