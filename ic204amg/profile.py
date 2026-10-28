"""Per-cluster AMG recipe: which EEPROM windows may be written and what to write.

Only W204_COLOUR (the default) is verified on a car. Every other IC204 variant's offsets,
flag byte and lamp fix are community-reported and unverified: define a profile in car.toml
([amg] and optional [lamp]) and confirm it against your own backup before writing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .uds import Window


def _in_range(byte: int, length: int) -> bool:
    return -length <= byte < length


@dataclass(frozen=True, slots=True)
class FlagRule:
    """One flag byte of the AMG window that toggles off<->on; any other value is a STOP."""

    byte: int
    off: int
    on: int


@dataclass(frozen=True, slots=True)
class LampFix:
    """Indicator-lamp fix: set one byte of a second window to a fixed value."""

    window: Window
    byte: int
    value: int


@dataclass(frozen=True, slots=True)
class Profile:
    amg: Window
    rule: FlagRule
    lamp: LampFix | None = None

    def __post_init__(self) -> None:
        if not _in_range(self.rule.byte, self.amg.length):
            raise ValueError(
                f"amg byte {self.rule.byte} is outside the {self.amg.length}-byte window"
            )
        if self.rule.off == self.rule.on:
            raise ValueError("amg off and on values must differ")
        if self.lamp is not None and not _in_range(self.lamp.byte, self.lamp.window.length):
            raise ValueError(
                f"lamp byte {self.lamp.byte} is outside the {self.lamp.window.length}-byte window"
            )

    @property
    def windows(self) -> tuple[Window, ...]:
        """Every window this profile may write; the Cluster write allow-list is built from it."""
        return (self.amg, self.lamp.window) if self.lamp else (self.amg,)


# Verified on a 2013 W204 colour cluster (SW 2049022903): byte 0 of 0x01D0 is FC off / FF on.
# The lamp fix (last byte of 0x0260 -> 0x60, for a wrong ESP/CNG/preheat lamp after the write)
# is community-reported only; see the README's confidence table.
W204_COLOUR = Profile(
    amg=Window(0x01D0, 8),
    rule=FlagRule(byte=0, off=0xFC, on=0xFF),
    lamp=LampFix(Window(0x0260, 10), byte=-1, value=0x60),
)


def from_config(amg: Mapping[str, Any] | None, lamp: Mapping[str, Any] | None) -> Profile:
    """Build a Profile from the [amg]/[lamp] config tables; W204_COLOUR when [amg] is absent."""
    if not amg:
        return W204_COLOUR
    try:
        window = Window(int(amg["offset"]), int(amg["length"]))
        rule = FlagRule(int(amg.get("byte", 0)), int(amg["off"]), int(amg["on"]))
        lampfix = None
        if lamp:
            lampfix = LampFix(
                Window(int(lamp["offset"]), int(lamp["length"])),
                int(lamp.get("byte", -1)),
                int(lamp["value"]),
            )
    except KeyError as e:
        raise ValueError(f"profile config missing {e}") from e
    return Profile(window, rule, lampfix)
