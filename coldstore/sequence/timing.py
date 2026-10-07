"""Timing constraints for the start/stop sequence interlock."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Timing:
    """Tunable delays and timeouts, in seconds.

    debounce:       a feedback signal must hold steady this long before trusted
    open_timeout:   wait for the open limit switch before holding the sequence
    stabilize:      minimum dwell after open confirmation before start is allowed
    confirm_valid:  open confirmation expires after this and must be re-acquired
    start_timeout:  wait for the running feedback after the start command
    hold_max:       feedback may stay missing this long before the fallback runs
    stop_timeout:   wait for the stopped confirmation while stopping
    close_timeout:  wait for the closed limit switch while closing
    chatter_edges:  raw signal edges inside the window that mean a faulty loop
    chatter_window: sliding window used for edge counting
    bypass_ttl:     a manual release permit expires after this
    """

    debounce: float = 1.0
    open_timeout: float = 30.0
    stabilize: float = 3.0
    confirm_valid: float = 120.0
    start_timeout: float = 15.0
    hold_max: float = 5.0
    stop_timeout: float = 15.0
    close_timeout: float = 30.0
    chatter_edges: int = 6
    chatter_window: float = 10.0
    bypass_ttl: float = 600.0

    def as_dict(self) -> dict[str, float]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}
