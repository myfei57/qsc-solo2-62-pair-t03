"""Start/stop sequence interlock binding the supply valve to its compressor.

The valve and the compressor never move on their own. Starting walks a fixed
order — command the valve open, wait for the open limit switch, settle, then
start the compressor and wait for the running feedback. Stopping runs the
opposite order. A feedback signal is only trusted after a debounce delay; one
that goes missing freezes the outputs (hold), and one that stays missing past
the hold limit runs the fallback — stop the compressor, confirm stopped,
close the valve, confirm closed — and locks the machine until an engineer
resets it. The valve is never closed against a compressor that may still be
running. Every decision lands in the audit trail under one cycle id, so an
abnormal chain can be replayed from the first missing feedback to the locked
end state, operator by operator.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, replace

from coldstore.audit import Auditor
from coldstore.comp import Bank
from coldstore.store.store import Store
from coldstore.valve import SupplyValve

from .auth import BypassPermit, Operator
from .debounce import Debouncer
from .report import MachineView, Phase, SequenceState
from .timing import Timing

MACHINE_KEY_PREFIX = "sequence:machine:"
TIMING_KEY = "sequence:timing"
AUDIT_KIND = "sequence"
CHANNELS = ("open_limit", "close_limit", "running")


@dataclass
class _Machine:
    """Persisted state of one valve/compressor pair."""

    comp_id: str
    phase: Phase = Phase.IDLE
    phase_at: float = 0.0
    cycle: str = ""
    valve_command: str = "close"
    compressor_command: str = "stop"
    confirmed_at: float | None = None
    missing_since: float | None = None
    hold_origin: str = ""
    fault: str = ""
    bypass: BypassPermit | None = None
    bypass_cycle: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "phase": self.phase.value,
            "phase_at": self.phase_at,
            "cycle": self.cycle,
            "valve_command": self.valve_command,
            "compressor_command": self.compressor_command,
            "confirmed_at": self.confirmed_at,
            "missing_since": self.missing_since,
            "hold_origin": self.hold_origin,
            "fault": self.fault,
            "bypass": None if self.bypass is None else self.bypass.as_dict(),
            "bypass_cycle": self.bypass_cycle,
        }

    @classmethod
    def from_dict(cls, comp_id: str, payload: dict[str, object]) -> "_Machine":
        bypass = payload.get("bypass")
        return cls(
            comp_id=comp_id,
            phase=Phase(str(payload.get("phase", "IDLE"))),
            phase_at=float(payload.get("phase_at", 0.0)),
            cycle=str(payload.get("cycle", "")),
            valve_command=str(payload.get("valve_command", "close")),
            compressor_command=str(payload.get("compressor_command", "stop")),
            confirmed_at=payload.get("confirmed_at"),  # type: ignore[arg-type]
            missing_since=payload.get("missing_since"),  # type: ignore[arg-type]
            hold_origin=str(payload.get("hold_origin", "")),
            fault=str(payload.get("fault", "")),
            bypass=None if not isinstance(bypass, dict) else BypassPermit.from_dict(bypass),
            bypass_cycle=bool(payload.get("bypass_cycle", False)),
        )


class Sequence:
    """Runs the interlocked start/stop order for every registered pair."""

    def __init__(
        self,
        store: Store,
        supply: SupplyValve,
        bank: Bank,
        auditor: Auditor,
        timing: Timing | None = None,
    ) -> None:
        self._store = store
        self._supply = supply
        self._bank = bank
        self._auditor = auditor
        self._timing = timing if timing is not None else self._load_timing()
        self._field: dict[str, dict[str, Debouncer]] = {}

    # ---------- timing ----------

    def _load_timing(self) -> Timing:
        raw, present = self._store.get(TIMING_KEY)
        if present:
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                known = {name: value for name, value in payload.items() if name in Timing.__dataclass_fields__}
                try:
                    return Timing(**known)
                except TypeError:
                    pass
        return Timing()

    def timing(self) -> Timing:
        return self._timing

    def set_timing(self, operator: Operator, now: float, changes: dict[str, object]) -> dict[str, object]:
        """Tune timing parameters; an engineer-only, audited action."""

        if not operator.allows("timing"):
            self._record("REJECT", now, operator=operator.name, action="timing", reason="role too low")
            return {"updated": False, "reason": "requires engineer role"}
        valid: dict[str, float] = {}
        for key, value in changes.items():
            if key not in Timing.__dataclass_fields__:
                raise ValueError(f"unknown timing field {key}")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"timing {key} must be a positive number")
            valid[key] = float(value) if not isinstance(value, int) else value
        self._timing = replace(self._timing, **valid)
        self._store.put(TIMING_KEY, json.dumps(self._timing.as_dict(), ensure_ascii=False))
        for channels in self._field.values():
            for debouncer in channels.values():
                debouncer.delay = self._timing.debounce
                debouncer.chatter_edges = self._timing.chatter_edges
                debouncer.chatter_window = self._timing.chatter_window
        self._record("TIMING", now, operator=operator.name, changes=valid)
        return {"updated": True, "timing": self._timing.as_dict()}

    # ---------- machine state ----------

    def _machine(self, comp_id: str) -> _Machine:
        raw, present = self._store.get(f"{MACHINE_KEY_PREFIX}{comp_id}")
        if present:
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                try:
                    return _Machine.from_dict(comp_id, payload)
                except (ValueError, TypeError, KeyError):
                    pass
        return _Machine(comp_id=comp_id)

    def _save(self, machine: _Machine) -> None:
        self._store.put(
            f"{MACHINE_KEY_PREFIX}{machine.comp_id}",
            json.dumps(machine.as_dict(), ensure_ascii=False),
        )

    def _machine_ids(self) -> list[str]:
        prefix = MACHINE_KEY_PREFIX
        return [key[len(prefix) :] for key in self._store.keys() if key.startswith(prefix)]

    def _debouncers(self, comp_id: str) -> dict[str, Debouncer]:
        if comp_id not in self._field:
            timing = self._timing
            self._field[comp_id] = {
                channel: Debouncer(timing.debounce, timing.chatter_edges, timing.chatter_window)
                for channel in CHANNELS
            }
        return self._field[comp_id]

    # ---------- outputs ----------

    def _command_valve(self, machine: _Machine, command: str) -> None:
        machine.valve_command = command
        if command == "open":
            self._supply.open_valve(machine.comp_id)
        else:
            self._supply.close_valve(machine.comp_id)

    def _command_compressor(self, machine: _Machine, command: str) -> None:
        machine.compressor_command = command
        if command == "run":
            self._bank.start(machine.comp_id)
        else:
            self._bank.stop(machine.comp_id)

    # ---------- audit ----------

    def _record(
        self,
        event: str,
        now: float,
        machine: _Machine | None = None,
        operator: str | None = None,
        **detail: object,
    ) -> None:
        payload: dict[str, object] = {
            "machine": "" if machine is None else machine.comp_id,
            "cycle": "" if machine is None else machine.cycle,
            "event": event,
            "phase": "" if machine is None else machine.phase.value,
            "at": round(now, 3),
        }
        if operator:
            payload["operator"] = operator
        payload.update(detail)
        self._auditor.record(AUDIT_KIND, json.dumps(payload, ensure_ascii=False))

    def _goto(self, machine: _Machine, phase: Phase, now: float, reason: str, **detail: object) -> None:
        before = machine.phase
        machine.phase = phase
        machine.phase_at = now
        self._record(
            "TRANSITION", now, machine, before=before.value, after=phase.value, reason=reason, **detail
        )

    def _reject(
        self, machine: _Machine, now: float, operator: Operator, action: str, reason: str
    ) -> dict[str, object]:
        self._record("REJECT", now, machine, operator=operator.name, action=action, reason=reason)
        self._save(machine)
        return {"allowed": False, "action": action, "reason": reason}

    # ---------- operator commands ----------

    def start(self, comp_id: str, operator: Operator, now: float) -> dict[str, object]:
        """Begin the interlocked start: valve first, compressor only behind confirmation."""

        machine = self._machine(comp_id)
        if not operator.allows("start"):
            return self._reject(machine, now, operator, "start", "role too low")
        if machine.phase != Phase.IDLE:
            return self._reject(machine, now, operator, "start", f"phase {machine.phase.value} not startable")
        if self._bank.compressor(comp_id) is None:
            return self._reject(machine, now, operator, "start", "compressor not registered")
        debouncers = self._debouncers(comp_id)
        if debouncers["close_limit"].stable is not True:
            return self._reject(machine, now, operator, "start", "valve closed confirmation missing")
        machine.cycle = uuid.uuid4().hex[:12]
        machine.confirmed_at = None
        machine.missing_since = None
        machine.hold_origin = ""
        machine.fault = ""
        machine.bypass_cycle = False
        self._record("COMMAND", now, machine, operator=operator.name, action="start")
        self._command_valve(machine, "open")
        self._goto(machine, Phase.OPENING, now, "open command issued")
        self._evaluate(machine, debouncers, now)
        self._save(machine)
        return {"allowed": True, "cycle": machine.cycle, "phase": machine.phase.value}

    def stop(self, comp_id: str, operator: Operator, now: float) -> dict[str, object]:
        """Begin the interlocked stop: compressor first, valve only behind confirmation."""

        machine = self._machine(comp_id)
        if not operator.allows("stop"):
            return self._reject(machine, now, operator, "stop", "role too low")
        stoppable = (Phase.OPENING, Phase.READY, Phase.STARTING, Phase.RUNNING, Phase.HOLD)
        if machine.phase not in stoppable:
            return self._reject(machine, now, operator, "stop", f"phase {machine.phase.value} has nothing to stop")
        debouncers = self._debouncers(comp_id)
        self._record("COMMAND", now, machine, operator=operator.name, action="stop")
        possibly_running = (
            machine.compressor_command == "run" or debouncers["running"].stable is True
        )
        self._command_compressor(machine, "stop")
        if possibly_running:
            self._goto(machine, Phase.STOPPING, now, "stop command: compressor first, valve held open")
        else:
            self._command_valve(machine, "close")
            self._goto(machine, Phase.CLOSING, now, "stop command: closing valve")
        self._evaluate(machine, debouncers, now)
        self._save(machine)
        return {"allowed": True, "phase": machine.phase.value}

    def grant_bypass(
        self, comp_id: str, operator: Operator, grantee: str, reason: str, now: float
    ) -> dict[str, object]:
        """Issue a manual release permit; engineer-only, single use, time boxed."""

        machine = self._machine(comp_id)
        if not operator.allows("bypass"):
            return self._reject(machine, now, operator, "bypass", "role too low")
        if not grantee:
            raise ValueError("grantee is required")
        permit = BypassPermit(
            permit_id=uuid.uuid4().hex[:8],
            granted_by=operator.name,
            granted_to=grantee,
            reason=reason or "manual release",
            expires_at=now + self._timing.bypass_ttl,
        )
        machine.bypass = permit
        self._record(
            "AUTH",
            now,
            machine,
            operator=operator.name,
            grantee=grantee,
            permit=permit.permit_id,
            reason=permit.reason,
            expires_at=round(permit.expires_at, 3),
        )
        self._save(machine)
        return {"permit": permit.as_dict()}

    def release(self, comp_id: str, operator: Operator, now: float) -> dict[str, object]:
        """Spend a manual release permit to start without the open confirmation."""

        machine = self._machine(comp_id)
        if not operator.allows("release"):
            return self._reject(machine, now, operator, "release", "role too low")
        if machine.phase not in (Phase.OPENING, Phase.READY, Phase.HOLD):
            return self._reject(machine, now, operator, "release", f"phase {machine.phase.value} not releasable")
        permit = machine.bypass
        if permit is None or not permit.active(now):
            return self._reject(machine, now, operator, "release", "no active permit")
        if permit.granted_to != operator.name:
            return self._reject(machine, now, operator, "release", "permit granted to someone else")
        permit.used = True
        machine.bypass_cycle = True
        self._record(
            "BYPASS",
            now,
            machine,
            operator=operator.name,
            permit=permit.permit_id,
            granted_by=permit.granted_by,
            reason=permit.reason,
        )
        self._start_compressor(machine, now, via="manual-release")
        self._save(machine)
        return {"released": True, "phase": machine.phase.value, "permit": permit.permit_id}

    def reset(self, comp_id: str, operator: Operator, now: float) -> dict[str, object]:
        """Clear a faulted machine; engineer-only and only into a confirmed safe state."""

        machine = self._machine(comp_id)
        if not operator.allows("reset"):
            return self._reject(machine, now, operator, "reset", "role too low")
        if machine.phase != Phase.FAULT:
            return self._reject(machine, now, operator, "reset", "not faulted")
        debouncers = self._debouncers(comp_id)
        if debouncers["close_limit"].stable is not True or debouncers["running"].stable is not False:
            return self._reject(
                machine, now, operator, "reset", "valve closed and compressor stopped must be confirmed first"
            )
        self._record("RESET", now, machine, operator=operator.name)
        machine.fault = ""
        machine.cycle = ""
        machine.bypass = None
        machine.bypass_cycle = False
        machine.confirmed_at = None
        machine.missing_since = None
        machine.hold_origin = ""
        self._goto(machine, Phase.IDLE, now, "fault reset")
        self._save(machine)
        return {"reset": True, "phase": machine.phase.value}

    # ---------- field side ----------

    def feedback(
        self,
        comp_id: str,
        now: float,
        open_limit: bool | None = None,
        close_limit: bool | None = None,
        running: bool | None = None,
    ) -> dict[str, object]:
        """Fold raw field signals into the debounced confirmations."""

        machine = self._machine(comp_id)
        debouncers = self._debouncers(comp_id)
        samples = {"open_limit": open_limit, "close_limit": close_limit, "running": running}
        for channel, raw in samples.items():
            if raw is None:
                continue
            debouncer = debouncers[channel]
            before = debouncer.stable
            chatter = debouncer.update(bool(raw), now)
            if debouncer.stable != before and debouncer.stable is not None:
                self._record("FEEDBACK", now, machine, channel=channel, value=debouncer.stable)
            if chatter:
                self._on_chatter(machine, debouncers, channel, now)
        self._evaluate(machine, debouncers, now)
        self._save(machine)
        return self.view(machine)

    def tick(self, comp_id: str, now: float) -> dict[str, object]:
        """One scan cycle: settle debouncers against their raw inputs, then evaluate."""

        machine = self._machine(comp_id)
        debouncers = self._debouncers(comp_id)
        for channel, debouncer in debouncers.items():
            before = debouncer.stable
            debouncer.refresh(now)
            if debouncer.stable != before and debouncer.stable is not None:
                self._record("FEEDBACK", now, machine, channel=channel, value=debouncer.stable)
        self._evaluate(machine, debouncers, now)
        self._save(machine)
        return self.view(machine)

    # ---------- projections ----------

    def view(self, machine: _Machine) -> dict[str, object]:
        debouncers = self._debouncers(machine.comp_id)
        return MachineView(
            id=machine.comp_id,
            phase=machine.phase.value,
            cycle=machine.cycle,
            valve_command=machine.valve_command,
            compressor_command=machine.compressor_command,
            open_confirmed=debouncers["open_limit"].stable is True,
            closed_confirmed=debouncers["close_limit"].stable is True,
            running_confirmed=debouncers["running"].stable is True,
            confirmed_at=machine.confirmed_at,
            missing_since=machine.missing_since,
            hold_origin=machine.hold_origin,
            fault=machine.fault,
            bypass=None if machine.bypass is None else machine.bypass.as_dict(),
            bypass_cycle=machine.bypass_cycle,
        ).as_dict()

    def machine(self, comp_id: str) -> dict[str, object]:
        return self.view(self._machine(comp_id))

    def machines(self) -> list[dict[str, object]]:
        return [self.machine(comp_id) for comp_id in sorted(self._machine_ids())]

    def state(self) -> SequenceState:
        return SequenceState(machines=self.machines(), timing=self._timing.as_dict())

    def trace(
        self, machine: str = "", operator: str = "", cycle: str = "", event: str = ""
    ) -> list[dict[str, object]]:
        """Replay interlock decisions, filtered by machine, operator, cycle or event."""

        hits: list[dict[str, object]] = []
        for entry in self._auditor.entries():
            if entry.kind != AUDIT_KIND:
                continue
            try:
                payload = json.loads(entry.detail)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            if machine and payload.get("machine") != machine:
                continue
            if operator and payload.get("operator") != operator:
                continue
            if cycle and payload.get("cycle") != cycle:
                continue
            if event and payload.get("event") != event:
                continue
            hits.append({"id": entry.id, "time": entry.time, **payload})
        return hits

    def describe(self) -> str:
        views = self.machines()
        faults = sum(1 for view in views if view["phase"] == Phase.FAULT.value)
        running = sum(1 for view in views if view["phase"] == Phase.RUNNING.value)
        return f"sequence machines={len(views)} running={running} faults={faults}"

    # ---------- state machine ----------

    def _evaluate(self, machine: _Machine, debouncers: dict[str, Debouncer], now: float) -> None:
        timing = self._timing
        phase = machine.phase
        if phase == Phase.OPENING:
            if debouncers["open_limit"].stable is True:
                machine.confirmed_at = now
                self._goto(machine, Phase.READY, now, "open limit confirmed")
            elif now - machine.phase_at >= timing.open_timeout:
                self._enter_hold(machine, now, "open feedback timeout")
        elif phase == Phase.READY:
            if not machine.bypass_cycle and debouncers["open_limit"].stable is False:
                self._goto(machine, Phase.OPENING, now, "open confirmation lost, re-confirming")
            elif machine.confirmed_at is not None:
                elapsed = now - machine.confirmed_at
                if elapsed > timing.confirm_valid:
                    self._goto(machine, Phase.OPENING, now, "open confirmation expired, re-confirming")
                elif elapsed >= timing.stabilize:
                    self._start_compressor(machine, now, via="auto")
        elif phase == Phase.STARTING:
            if debouncers["running"].stable is True:
                self._goto(machine, Phase.RUNNING, now, "running feedback confirmed")
            elif not machine.bypass_cycle and debouncers["open_limit"].stable is False:
                self._enter_hold(machine, now, "open feedback lost while starting")
            elif now - machine.phase_at >= timing.start_timeout:
                self._record("ALARM", now, machine, kind="start_timeout")
                self._begin_fallback(machine, debouncers, now, "running feedback timeout")
        elif phase == Phase.RUNNING:
            if not machine.bypass_cycle and debouncers["open_limit"].stable is not True:
                self._enter_hold(machine, now, "open feedback missing while running")
        elif phase == Phase.HOLD:
            if debouncers["open_limit"].stable is True:
                self._recover(machine, now)
            elif machine.missing_since is not None and now - machine.missing_since >= timing.hold_max:
                self._begin_fallback(
                    machine, debouncers, now, f"feedback missing beyond hold limit {timing.hold_max:.1f}s"
                )
        elif phase == Phase.STOPPING:
            if debouncers["running"].stable is False:
                self._command_valve(machine, "close")
                self._goto(machine, Phase.CLOSING, now, "stopped confirmed, closing valve")
            elif now - machine.phase_at >= timing.stop_timeout:
                self._fault(machine, now, "stop feedback timeout; valve held open", hold_valve=True)
        elif phase == Phase.CLOSING:
            if debouncers["close_limit"].stable is True:
                self._goto(machine, Phase.IDLE, now, "closed limit confirmed, cycle complete")
                machine.cycle = ""
                machine.bypass_cycle = False
            elif now - machine.phase_at >= timing.close_timeout:
                self._fault(machine, now, "close feedback timeout")
        elif phase == Phase.FALLBACK_STOP:
            if debouncers["running"].stable is False:
                self._command_valve(machine, "close")
                self._goto(machine, Phase.FALLBACK_CLOSE, now, "fallback: stopped confirmed, closing valve")
            elif now - machine.phase_at >= timing.stop_timeout:
                # Never close the valve against a compressor that may still run.
                self._fault(
                    machine,
                    now,
                    "fallback failed: stop feedback missing; valve held open, escalate",
                    hold_valve=True,
                )
        elif phase == Phase.FALLBACK_CLOSE:
            if debouncers["close_limit"].stable is True:
                self._fault(machine, now, "fallback complete: valve closed, locked for reset")
            elif now - machine.phase_at >= timing.close_timeout:
                self._fault(machine, now, "fallback failed: close feedback missing")

    def _start_compressor(self, machine: _Machine, now: float, via: str) -> None:
        self._command_compressor(machine, "run")
        self._goto(machine, Phase.STARTING, now, f"compressor start allowed ({via})", via=via)

    def _enter_hold(self, machine: _Machine, now: float, reason: str) -> None:
        machine.hold_origin = machine.phase.value
        machine.missing_since = now
        self._record("ALARM", now, machine, kind="feedback_missing", reason=reason, action="hold")
        self._goto(machine, Phase.HOLD, now, reason)

    def _recover(self, machine: _Machine, now: float) -> None:
        origin = machine.hold_origin
        missing = now - (machine.missing_since if machine.missing_since is not None else now)
        machine.missing_since = None
        self._record("TRANSITION", now, machine, note=f"feedback recovered after {missing:.2f}s")
        if origin == Phase.OPENING.value:
            machine.confirmed_at = now
            self._goto(machine, Phase.READY, now, "open limit confirmed during hold")
        elif origin == Phase.STARTING.value:
            self._goto(machine, Phase.STARTING, now, "feedback recovered, resuming start")
        else:
            self._goto(machine, Phase.RUNNING, now, "feedback recovered, resuming run")

    def _begin_fallback(
        self, machine: _Machine, debouncers: dict[str, Debouncer], now: float, reason: str
    ) -> None:
        if machine.phase in (Phase.FALLBACK_STOP, Phase.FALLBACK_CLOSE, Phase.FAULT):
            return
        self._record("ALARM", now, machine, kind="fallback", reason=reason)
        machine.missing_since = None
        possibly_running = (
            machine.compressor_command == "run" or debouncers["running"].stable is True
        )
        self._command_compressor(machine, "stop")
        if possibly_running:
            self._goto(machine, Phase.FALLBACK_STOP, now, reason)
        else:
            self._command_valve(machine, "close")
            self._goto(machine, Phase.FALLBACK_CLOSE, now, reason)

    def _fault(self, machine: _Machine, now: float, reason: str, hold_valve: bool = False) -> None:
        if hold_valve:
            # Hold the valve open: closing it now could starve a running machine.
            self._command_valve(machine, "open")
        self._command_compressor(machine, "stop")
        machine.fault = reason
        self._record("FAULT", now, machine, reason=reason)
        self._goto(machine, Phase.FAULT, now, reason)

    def _on_chatter(
        self, machine: _Machine, debouncers: dict[str, Debouncer], channel: str, now: float
    ) -> None:
        if machine.phase in (Phase.FALLBACK_STOP, Phase.FALLBACK_CLOSE, Phase.FAULT):
            return
        edges = debouncers[channel].edges
        if machine.bypass_cycle and channel == "open_limit":
            self._record(
                "ALARM", now, machine, kind="chatter", channel=channel, edges=edges, note="bypass active, alarm only"
            )
            return
        self._record("FAULT", now, machine, kind="chatter", channel=channel, edges=edges)
        self._begin_fallback(machine, debouncers, now, f"feedback chatter on {channel} ({edges} edges)")
