"""ic204amg: selftest | probe | readonly | write | lampfix | restore. See README for the order."""

from __future__ import annotations

import argparse
import logging
import struct
import sys
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import cluster as cl
from . import profile as pf
from . import seedkey, uds
from .transport import OP20_DLL, J2534Transport, TransportError

log = logging.getLogger(__name__)

# A charger holding ~13.5 V is expected before anything is written; this is only the floor.
MIN_WRITE_VBATT = 12.5


class UsageError(Exception):
    """Bad arguments, config or backup file: reported without a traceback, exit 1."""


@dataclass(frozen=True, slots=True)
class Settings:
    sw: str | None
    dll: str
    baud: int
    profile: pf.Profile
    txid: int | None = None
    rxid: int | None = None

    def can_ids(self) -> tuple[int, int]:
        if self.txid is None or self.rxid is None:
            raise UsageError("txid/rxid not set: fill car.toml [cluster] or pass --txid/--rxid")
        return self.txid, self.rxid

    def require_sw(self) -> str:
        if self.sw is None:
            raise UsageError(
                "sw not set: put the cluster's SW part number in car.toml [cluster] or pass "
                "--sw (selftest lists the supported ones)"
            )
        return self.sw


def parse_int(s: str) -> int:
    return int(s, 0)


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def setup_logging(log_dir: Path) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / time.strftime("ic204amg_%Y%m%d_%H%M%S.log")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(
        logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)s %(name)s: %(message)s", "%H:%M:%S")
    )
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(fh)
    root.addHandler(sh)
    return path


def confirm_cli(prompt: str, token: str) -> bool:
    print("\n" + prompt)
    try:
        answer = input(f"Type {token} to proceed, anything else to abort: ")
    except EOFError:
        return False
    return answer.strip().upper() == token.upper()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ic204amg", description=__doc__)
    p.add_argument("--config", type=Path, default=Path("car.toml"))
    p.add_argument("--txid", type=parse_int, help="request CAN id (IC_204: 0x60A)")
    p.add_argument("--rxid", type=parse_int, help="response CAN id (IC_204: 0x481)")
    p.add_argument("--baud", type=int)
    p.add_argument("--dll", help="J2534 DLL path")
    p.add_argument("--sw", help="cluster SW part number (seed-key table); selftest lists them")
    p.add_argument("--log-dir", type=Path, default=Path("logs"))
    p.add_argument("--backup-dir", type=Path, default=Path("backups"))
    writing = argparse.ArgumentParser(add_help=False)
    writing.add_argument(
        "--ignore-battery", action="store_true", help=f"write even below {MIN_WRITE_VBATT} V"
    )
    writing.add_argument(
        "--write-level",
        choices=["0x01", "0x03", "0x09", "0x0D"],
        default="0x0D",
        help="SecurityAccess level for the write unlock; drop to 0x09 if 0x0D is rejected",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest", help="seed-key vectors, udsoncan import, DLL load; no cable")
    sub.add_parser("probe", help="open the Openport, show versions and battery; no bus traffic")
    sub.add_parser("readonly", help="session, unlock, read the profile's windows, save backup")
    w = sub.add_parser(
        "write", parents=[writing], help="readonly, then toggle the AMG flag byte, confirmed"
    )
    w.add_argument("--expect", type=Path, help="earlier backup; stop if the AMG window differs")
    sub.add_parser(
        "lampfix", parents=[writing], help="only after a lamp misbehaves: write the lamp-fix byte"
    )
    r = sub.add_parser("restore", parents=[writing], help="write a saved backup back (rollback)")
    r.add_argument("backup", type=Path)
    r.add_argument("--lamp", action="store_true", help="also restore the lamp window")
    return p


def settings(args: argparse.Namespace) -> Settings:
    cfg = load_config(args.config)
    c, o = cfg.get("cluster", {}), cfg.get("openport", {})
    sw = args.sw or c.get("sw") or None  # "" in car.toml is the unfilled placeholder
    if sw is not None:
        sw = str(sw)
        try:
            seedkey.get_salt_table(sw)
        except ValueError as e:
            raise UsageError(str(e)) from e
    txid = args.txid if args.txid is not None else c.get("txid")
    rxid = args.rxid if args.rxid is not None else c.get("rxid")
    try:
        profile = pf.from_config(cfg.get("amg"), cfg.get("lamp"))
    except ValueError as e:
        raise UsageError(f"car.toml profile: {e}") from e
    return Settings(
        sw=sw,
        dll=str(args.dll or o.get("dll", OP20_DLL)),
        baud=int(args.baud or c.get("baud", 500000)),
        profile=profile,
        txid=None if txid is None else int(txid),
        rxid=None if rxid is None else int(rxid),
    )


def load_backup(path: Path, profile: pf.Profile) -> cl.Backup:
    try:
        return cl.Backup.load(path, profile)
    except (OSError, ValueError) as e:
        raise UsageError(str(e)) from e


def cmd_selftest(s: Settings) -> int:
    ok = True
    for sw, level, seed, want in seedkey.VECTORS:
        got = seedkey.ic204_generate_key(level, bytes.fromhex(seed), sw).hex().upper()
        good = got == want
        ok = ok and good
        print(f"seed-key {sw} level {level:02X} {seed} -> {got} {'OK' if good else 'FAIL'}")
    print("seed-key tables:", " ".join(seedkey.supported_sw()))
    if s.sw:
        print(f"seed-key table for sw {s.sw}: OK")
    else:
        print("sw not set in car.toml [cluster]: FAIL, pick one of the above")
        ok = False
    p = s.profile
    lamp = f"{p.lamp.window.name}[{p.lamp.byte}]={p.lamp.value:02X}" if p.lamp else "none"
    amg = f"{p.amg.name} byte {p.rule.byte} {p.rule.off:02X}/{p.rule.on:02X}"
    print(f"profile: amg {amg}, lamp {lamp}")
    print(f"python {sys.version.split()[0]} {struct.calcsize('P') * 8}-bit")
    try:
        import udsoncan

        print(f"udsoncan {udsoncan.__version__}")
    except ImportError as e:
        print(f"udsoncan import FAILED: {e}")
        ok = False
    if sys.platform == "win32":
        import ctypes

        try:
            from udsoncan import j2534  # noqa: F401

            ctypes.WinDLL(s.dll)
            print(f"DLL loads: {s.dll}")
        except (ImportError, OSError) as e:
            print(f"DLL load FAILED ({s.dll}): {e}")
            ok = False
    else:
        print("not Windows: DLL load skipped")
    print("SELFTEST", "OK" if ok else "FAILED")
    return 0 if ok else 1


def check_battery(v: float | None, required: bool) -> None:
    if v is None:
        problem = "battery voltage not readable"
    elif v < MIN_WRITE_VBATT:
        problem = f"battery {v:.2f} V, below {MIN_WRITE_VBATT} V"
    else:
        log.info("battery %.2f V", v)
        return
    if required:
        raise cl.Stop(f"{problem}: connect the charger, or pass --ignore-battery")
    log.warning(problem)


def run_job(
    s: Settings,
    job: Callable[[cl.Cluster], object] | None,
    *,
    writes: bool = False,
    ignore_battery: bool = False,
) -> int:
    txid, rxid = s.can_ids()
    sw = None if job is None else s.require_sw()
    log.info("cluster txid 0x%03X rxid 0x%03X baud %d sw %s", txid, rxid, s.baud, sw or "-")
    t = J2534Transport(txid, rxid, s.baud, s.dll)
    try:
        t.open()
        check_battery(t.vbatt(), required=writes and not ignore_battery)
        if job is None or sw is None:
            return 0
        c = cl.Cluster(t, sw, meta={"txid": hex(txid), "rxid": hex(rxid)}, profile=s.profile)
        c.extended_session()
        t.start_tester_present()
        job(c)
        return 0
    except cl.Stop as e:
        log.error("STOP: %s", e)
        return 2
    except (uds.UdsError, TransportError) as e:
        log.error("%s", e)
        return 3
    finally:
        t.close()


def dispatch(args: argparse.Namespace, s: Settings) -> int:
    backup_path = args.backup_dir / time.strftime("ic204_%Y%m%d_%H%M%S.json")
    match args.cmd:
        case "probe":
            return run_job(s, None)
        case "readonly":
            return run_job(s, lambda c: cl.run_readonly(c, backup_path))
        case "write":
            expect = load_backup(args.expect, s.profile) if args.expect else None
            level = int(args.write_level, 16)
            return run_job(
                s,
                lambda c: cl.run_write(c, backup_path, confirm_cli, expect, level),
                writes=True,
                ignore_battery=args.ignore_battery,
            )
        case "lampfix":
            level = int(args.write_level, 16)
            return run_job(
                s,
                lambda c: cl.run_lampfix(c, backup_path, confirm_cli, level),
                writes=True,
                ignore_battery=args.ignore_battery,
            )
        case "restore":
            saved = load_backup(args.backup, s.profile)
            level = int(args.write_level, 16)
            return run_job(
                s,
                lambda c: cl.run_restore(c, saved, confirm_cli, args.lamp, backup_path, level),
                writes=True,
                ignore_battery=args.ignore_battery,
            )
    raise AssertionError(args.cmd)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        s = settings(args)
        if args.cmd == "selftest":
            return cmd_selftest(s)
        log.info("log file %s", setup_logging(args.log_dir))
        return dispatch(args, s)
    except UsageError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        log.error("interrupted")
        return 130
