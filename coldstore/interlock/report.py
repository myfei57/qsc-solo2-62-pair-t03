"""Interlock projections."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class InterlockState:
    """Latch state together with the recorded trip log."""

    tripped: bool
    reason: str
    trips: list[dict[str, str]]
    count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "tripped": self.tripped,
            "reason": self.reason,
            "trips": self.trips,
            "count": self.count,
        }


@dataclass(frozen=True)
class SequenceState:
    """Live view of one valve/compressor sequence interlock."""

    comp_id: str
    phase: str
    phase_elapsed: float
    trace_id: str
    valve_command: str
    comp_commanded: bool
    feedback_open: bool
    feedback_open_raw: bool
    feedback_closed: bool
    comp_run: bool
    sensor_fault: bool
    feedback_lost_for: float | None
    override: dict[str, object] | None

    def as_dict(self) -> dict[str, object]:
        return {
            "comp_id": self.comp_id,
            "phase": self.phase,
            "phase_elapsed": self.phase_elapsed,
            "trace_id": self.trace_id,
            "valve_command": self.valve_command,
            "comp_commanded": self.comp_commanded,
            "feedback_open": self.feedback_open,
            "feedback_open_raw": self.feedback_open_raw,
            "feedback_closed": self.feedback_closed,
            "comp_run": self.comp_run,
            "sensor_fault": self.sensor_fault,
            "feedback_lost_for": self.feedback_lost_for,
            "override": self.override,
        }
