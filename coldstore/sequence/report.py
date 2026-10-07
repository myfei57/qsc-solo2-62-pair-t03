"""Sequence interlock projections."""

from __future__ import annotations

import enum
from dataclasses import dataclass


class Phase(enum.Enum):
    """Where one valve/compressor pair sits in the sequence."""

    IDLE = "IDLE"  # valve confirmed closed, compressor stopped
    OPENING = "OPENING"  # open command issued, waiting for the open limit switch
    READY = "READY"  # open confirmed, inside the settle/validity window
    STARTING = "STARTING"  # start command issued, waiting for running feedback
    RUNNING = "RUNNING"
    HOLD = "HOLD"  # feedback missing, outputs frozen while it may recover
    STOPPING = "STOPPING"  # normal stop: compressor first
    CLOSING = "CLOSING"  # stop confirmed, closing the valve
    FALLBACK_STOP = "FALLBACK_STOP"  # abnormal fallback: stopping the compressor
    FALLBACK_CLOSE = "FALLBACK_CLOSE"  # abnormal fallback: closing the valve
    FAULT = "FAULT"  # locked, waits for an engineer reset


@dataclass(frozen=True)
class MachineView:
    """One valve/compressor pair as the console shows it."""

    id: str
    phase: str
    cycle: str
    valve_command: str
    compressor_command: str
    open_confirmed: bool
    closed_confirmed: bool
    running_confirmed: bool
    confirmed_at: float | None
    missing_since: float | None
    hold_origin: str
    fault: str
    bypass: dict[str, object] | None
    bypass_cycle: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "phase": self.phase,
            "cycle": self.cycle,
            "valve_command": self.valve_command,
            "compressor_command": self.compressor_command,
            "open_confirmed": self.open_confirmed,
            "closed_confirmed": self.closed_confirmed,
            "running_confirmed": self.running_confirmed,
            "confirmed_at": self.confirmed_at,
            "missing_since": self.missing_since,
            "hold_origin": self.hold_origin,
            "fault": self.fault,
            "bypass": self.bypass,
            "bypass_cycle": self.bypass_cycle,
        }


@dataclass(frozen=True)
class SequenceState:
    """Every machine plus the timing currently in force."""

    machines: list[dict[str, object]]
    timing: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return {"machines": self.machines, "timing": self.timing}
