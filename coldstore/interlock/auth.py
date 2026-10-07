"""Operator identities and permission levels for the sequence interlock.

Every guarded action names an actor. The level ladder keeps routine start and
stop with the operators, fault resets and manual releases with supervisors,
and interlock retuning with administrators, so a slipped feedback switch can
never be bypassed by the same hands that run the shift.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class Level(IntEnum):
    """Permission levels, ordered by what they may do."""

    VIEWER = 0  # read only
    OPERATOR = 1  # normal sequenced start/stop
    SUPERVISOR = 2  # manual release, fault reset
    ADMIN = 3  # interlock timing and threshold tuning


class PermissionDenied(Exception):
    """Raised when an actor attempts an action above their level."""


@dataclass(frozen=True)
class Actor:
    """The person (or station) behind one control action."""

    uid: str
    name: str
    level: Level

    def as_dict(self) -> dict[str, object]:
        return {"uid": self.uid, "name": self.name, "level": int(self.level)}


SYSTEM = Actor(uid="system", name="interlock", level=Level.ADMIN)


def parse_level(value: object) -> Level:
    """Resolve an integer into a known permission level."""

    try:
        return Level(int(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError(f"unknown permission level: {value!r}") from None
