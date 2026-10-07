"""Operator roles and the manual release permit."""

from __future__ import annotations

import enum
from dataclasses import dataclass


class Role(enum.IntEnum):
    """Permission grades, lowest to highest."""

    VIEWER = 0  # L0 watch only
    OPERATOR = 1  # L1 normal start/stop
    ENGINEER = 2  # L2 manual release, fault reset, timing changes
    ADMIN = 3  # L3 reserved for authorization management


ROLE_NAMES = {
    "viewer": Role.VIEWER,
    "operator": Role.OPERATOR,
    "engineer": Role.ENGINEER,
    "admin": Role.ADMIN,
}

ACTION_MIN_ROLE = {
    "start": Role.OPERATOR,
    "stop": Role.OPERATOR,
    "release": Role.OPERATOR,
    "bypass": Role.ENGINEER,
    "reset": Role.ENGINEER,
    "timing": Role.ENGINEER,
}


def parse_role(value: str) -> Role:
    """Resolve a role name, rejecting anything unknown."""

    role = ROLE_NAMES.get(value.strip().lower())
    if role is None:
        raise ValueError(f"role must be one of {', '.join(sorted(ROLE_NAMES))}")
    return role


@dataclass(frozen=True)
class Operator:
    """Who is asking the interlock to do something."""

    name: str
    role: Role

    def allows(self, action: str) -> bool:
        return self.role >= ACTION_MIN_ROLE[action]


@dataclass
class BypassPermit:
    """A manual release: engineer-signed, single use, time boxed, audited.

    It exists so a confirmed instrument fault can be worked around under
    human judgement; it never becomes the default path for a noisy signal.
    """

    permit_id: str
    granted_by: str
    granted_to: str
    reason: str
    expires_at: float
    used: bool = False

    def active(self, now: float) -> bool:
        return not self.used and now < self.expires_at

    def as_dict(self) -> dict[str, object]:
        return {
            "permit_id": self.permit_id,
            "granted_by": self.granted_by,
            "granted_to": self.granted_to,
            "reason": self.reason,
            "expires_at": self.expires_at,
            "used": self.used,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "BypassPermit":
        return cls(
            permit_id=str(payload.get("permit_id", "")),
            granted_by=str(payload.get("granted_by", "")),
            granted_to=str(payload.get("granted_to", "")),
            reason=str(payload.get("reason", "")),
            expires_at=float(payload.get("expires_at", 0.0)),
            used=bool(payload.get("used", False)),
        )
