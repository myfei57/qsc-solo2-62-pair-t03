"""Start/stop sequence interlock between the supply valve and the compressor.

The compressor may only start after the valve-open feedback has been
confirmed through a debounce window. A missing feedback during the run is
handled in graded steps: hold position for a short loss, and for a prolonged
loss stop the compressor first, confirm the stop, then close the valve -
never close the valve with the machine running, that is the dry-run this
interlock exists to prevent. A chattering feedback is a sensor fault: the
automatic path locks out and only a time-boxed, dual-signed manual release
can run the machine. Every command, transition and authorisation is written
to the audit trail with the operator identity and a trace id.
"""

from __future__ import annotations

import enum
import json
import time
import uuid

from coldstore.comp import Bank
from coldstore.interlock.interlock import Interlock
from coldstore.store.store import Store
from coldstore.valve import CLOSED, OPEN, SupplyValve

from .auth import SYSTEM, Actor, Level, PermissionDenied
from .report import SequenceState
from .trail import Trail

STATE_KEY_PREFIX = "sequence:state:"
CONFIG_KEY = "sequence:config"


class Phase(str, enum.Enum):
    """Sequence phases; string values keep the persisted state readable."""

    IDLE = "IDLE"
    VALVE_OPENING = "VALVE_OPENING"
    COMP_STARTING = "COMP_STARTING"
    RUNNING = "RUNNING"
    STOPPING_COMP = "STOPPING_COMP"
    VALVE_CLOSING = "VALVE_CLOSING"
    FAULT_HOLD = "FAULT_HOLD"
    FAULT_STOPPING = "FAULT_STOPPING"
    FAULT_CLOSING = "FAULT_CLOSING"
    FAULT_LOCKED = "FAULT_LOCKED"


# Phases that hold the valve commanded open and the compressor commanded run.
_OPEN_PHASES = frozenset(
    {
        Phase.VALVE_OPENING,
        Phase.COMP_STARTING,
        Phase.RUNNING,
        Phase.STOPPING_COMP,
        Phase.FAULT_HOLD,
        Phase.FAULT_STOPPING,
    }
)
_RUN_PHASES = frozenset({Phase.COMP_STARTING, Phase.RUNNING, Phase.FAULT_HOLD})


class SequenceConfig:
    """Tunable interlock timings, persisted and adjustable by admins only."""

    DEFAULTS: dict[str, float] = {
        "valve_open_timeout": 20.0,  # wait for open feedback after the open command
        "valve_close_timeout": 20.0,  # wait for closed feedback after the close command
        "comp_start_timeout": 15.0,  # wait for the run feedback after the start command
        "comp_stop_timeout": 30.0,  # wait for the stopped feedback after the stop command
        "feedback_debounce": 1.0,  # feedback must hold steady this long to be believed
        "chatter_window": 10.0,  # window the chatter edges are counted over
        "chatter_max_edges": 6.0,  # edges inside the window that mean a sensor fault
        "run_loss_hold": 3.0,  # feedback loss below this is a logged transient
        "run_loss_trip": 15.0,  # feedback loss beyond this triggers the fallback
        "override_ttl": 1800.0,  # manual releases expire after this many seconds
    }

    def __init__(self, store: Store) -> None:
        self._store = store

    def values(self) -> dict[str, float]:
        raw, present = self._store.get(CONFIG_KEY)
        merged = dict(self.DEFAULTS)
        if present:
            try:
                loaded = json.loads(raw)
            except ValueError:
                loaded = {}
            if isinstance(loaded, dict):
                for key in self.DEFAULTS:
                    try:
                        value = float(loaded[key])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if value > 0:
                        merged[key] = value
        return merged

    def get(self, key: str) -> float:
        return self.values()[key]

    def set(self, key: str, value: float) -> tuple[float, float]:
        """Store one timing; returns the (old, new) pair for the audit log."""

        if key not in self.DEFAULTS:
            raise ValueError(f"unknown sequence parameter: {key}")
        value = float(value)
        if value <= 0:
            raise ValueError("sequence timings must be positive")
        values = self.values()
        old = values[key]
        values[key] = value
        self._store.put(CONFIG_KEY, json.dumps(values, ensure_ascii=False))
        return old, value


class SequenceInterlock:
    """Sequence interlock for one compressor and its supply valve."""

    def __init__(
        self,
        comp_id: str,
        store: Store,
        supply: SupplyValve,
        bank: Bank,
        interlock: Interlock,
        trail: Trail,
        config: SequenceConfig,
    ) -> None:
        self._comp_id = comp_id
        self._store = store
        self._supply = supply
        self._bank = bank
        self._interlock = interlock
        self._trail = trail
        self._config = config
        self._phase = Phase.IDLE
        self._phase_since = 0.0
        self._trace_id = ""
        self._sensor_fault = False
        self._override: dict[str, object] | None = None
        self._fb_lost_since: float | None = None
        self._open_stable = False
        self._pending_since: float | None = None
        self._open_edges: list[float] = []
        self._raw_open = False
        self._raw_closed = False
        self._raw_run = False
        self._load()

    # -- inputs ---------------------------------------------------------

    def set_feedback(
        self,
        now: float | None = None,
        *,
        valve_open: bool | None = None,
        valve_closed: bool | None = None,
        comp_run: bool | None = None,
    ) -> None:
        """Take raw field contacts; the open contact is chatter watched."""

        now = time.time() if now is None else now
        if valve_open is not None:
            valve_open = bool(valve_open)
            if valve_open != self._raw_open:
                self._raw_open = valve_open
                # The debounce timer starts at the raw change itself.
                self._pending_since = None if valve_open == self._open_stable else now
                window = self._config.get("chatter_window")
                self._open_edges.append(now)
                self._open_edges = [edge for edge in self._open_edges if now - edge <= window]
                if (
                    len(self._open_edges) > self._config.get("chatter_max_edges")
                    and not self._sensor_fault
                ):
                    self._sensor_fault = True
                    self._alarm(
                        now,
                        "open feedback chattering, sensor fault; "
                        "automatic path locked, manual release required",
                        {"edges": len(self._open_edges), "window": window},
                    )
        if valve_closed is not None:
            self._raw_closed = bool(valve_closed)
        if comp_run is not None:
            self._raw_run = bool(comp_run)
        self._save()

    # -- commands ---------------------------------------------------------

    def start(self, actor: Actor, now: float | None = None) -> Phase:
        """Begin the start sequence: open the valve, wait for confirmation."""

        now = time.time() if now is None else now
        self._require(actor, Level.OPERATOR, "START", now)
        if self._phase is not Phase.IDLE:
            raise ValueError(f"cannot start from {self._phase.value}")
        if self._bank.compressor(self._comp_id) is None:
            raise ValueError(f"compressor {self._comp_id} not found")
        self._trace_id = uuid.uuid4().hex
        self._fb_lost_since = None
        self._log(now, "COMMAND", "start sequence", actor)
        self._transition(now, Phase.VALVE_OPENING, "open command issued, awaiting open feedback", actor)
        self.evaluate(now)
        return self._phase

    def stop(self, actor: Actor, now: float | None = None) -> Phase:
        """Normal stop: compressor first, valve closed only after it stops."""

        now = time.time() if now is None else now
        self._require(actor, Level.OPERATOR, "STOP", now)
        if self._phase in (Phase.RUNNING, Phase.FAULT_HOLD):
            self._log(now, "COMMAND", "stop sequence", actor)
            self._transition(now, Phase.STOPPING_COMP, "normal stop: stop compressor first", actor)
        elif self._phase in (Phase.VALVE_OPENING, Phase.COMP_STARTING):
            self._log(now, "COMMAND", "abort start", actor)
            self._transition(now, Phase.VALVE_CLOSING, "start aborted by operator", actor)
        else:
            raise ValueError(f"cannot stop from {self._phase.value}")
        self.evaluate(now)
        return self._phase

    def manual_release(
        self,
        actor: Actor,
        approver: Actor,
        reason: str,
        field_confirmed: bool,
        now: float | None = None,
    ) -> Phase:
        """Time-boxed manual release around a feedback fault.

        Two supervisors must sign, the valve position must be confirmed in
        the field, and the release expires on its own; nothing here is
        silent or permanent.
        """

        now = time.time() if now is None else now
        self._require(actor, Level.SUPERVISOR, "MANUAL_RELEASE", now)
        self._require(approver, Level.SUPERVISOR, "MANUAL_RELEASE_APPROVE", now)
        if actor.uid == approver.uid:
            raise ValueError("release and approval must come from two different people")
        if not field_confirmed:
            raise ValueError("field confirmation of the valve position is required")
        if not reason.strip():
            raise ValueError("a reason is required for a manual release")
        if not (
            self._sensor_fault
            or self._phase in (Phase.VALVE_OPENING, Phase.FAULT_HOLD, Phase.FAULT_LOCKED)
        ):
            raise ValueError("no feedback fault to release")
        ttl = self._config.get("override_ttl")
        self._override = {
            "granted_by": actor.uid,
            "granted_name": actor.name,
            "approved_by": approver.uid,
            "approved_name": approver.name,
            "reason": reason,
            "expires_at": now + ttl,
        }
        self._log(
            now,
            "OVERRIDE",
            "manual release granted",
            actor,
            {"approver": approver.uid, "reason": reason, "ttl": ttl},
        )
        if self._phase is Phase.FAULT_LOCKED:
            self._transition(
                now, Phase.VALVE_OPENING, "manual release: retry open under supervision", actor
            )
        self.evaluate(now)
        return self._phase

    def reset(self, actor: Actor, now: float | None = None) -> Phase:
        """Clear a locked fault after the field check; supervisors only."""

        now = time.time() if now is None else now
        self._require(actor, Level.SUPERVISOR, "RESET", now)
        if self._phase is not Phase.FAULT_LOCKED:
            raise ValueError("only a locked fault can be reset")
        self._sensor_fault = False
        self._fb_lost_since = None
        self._override = None
        self._open_edges = []
        self._transition(now, Phase.IDLE, "fault reset after manual check", actor)
        self.evaluate(now)
        return self._phase

    # -- evaluation ---------------------------------------------------------

    def evaluate(self, now: float | None = None) -> Phase:
        """Advance timers and transitions once; safe to call every scan."""

        now = time.time() if now is None else now
        self._update_debounce(now)
        self._expire_override(now)
        cfg = self._config.values()
        if self._phase is Phase.VALVE_OPENING:
            self._eval_valve_opening(now, cfg)
        elif self._phase is Phase.COMP_STARTING:
            self._eval_comp_starting(now, cfg)
        elif self._phase is Phase.RUNNING:
            self._eval_running(now, cfg)
        elif self._phase is Phase.FAULT_HOLD:
            self._eval_fault_hold(now, cfg)
        elif self._phase is Phase.STOPPING_COMP:
            self._eval_stopping_comp(now, cfg)
        elif self._phase is Phase.VALVE_CLOSING:
            self._eval_valve_closing(now, cfg)
        elif self._phase is Phase.FAULT_STOPPING:
            self._eval_fault_stopping(now, cfg)
        elif self._phase is Phase.FAULT_CLOSING:
            self._eval_fault_closing(now, cfg)
        self._apply_outputs()
        self._save()
        return self._phase

    def _eval_valve_opening(self, now: float, cfg: dict[str, float]) -> None:
        if self._open_stable and not self._sensor_fault:
            self._transition(now, Phase.COMP_STARTING, "open feedback confirmed, start permitted")
        elif self._override is not None:
            self._log(now, "OVERRIDE", "start without confirmed feedback under release")
            self._transition(now, Phase.COMP_STARTING, "manual release: start without feedback")
        elif now - self._phase_since >= cfg["valve_open_timeout"]:
            self._alarm(now, "open feedback timeout", {"timeout": cfg["valve_open_timeout"]})
            self._transition(
                now,
                Phase.FAULT_CLOSING,
                "open feedback timeout, fallback: close valve (compressor never started)",
            )

    def _eval_comp_starting(self, now: float, cfg: dict[str, float]) -> None:
        if self._raw_run:
            self._transition(now, Phase.RUNNING, "compressor run feedback confirmed")
        elif now - self._phase_since >= cfg["comp_start_timeout"]:
            self._alarm(now, "compressor start feedback timeout", {"timeout": cfg["comp_start_timeout"]})
            self._transition(now, Phase.FAULT_STOPPING, "start feedback timeout, fallback: stop then close")

    def _eval_running(self, now: float, cfg: dict[str, float]) -> None:
        if self._override is not None or self._open_stable or self._fb_lost_since is None:
            return
        loss = now - self._fb_lost_since
        if loss >= cfg["run_loss_trip"]:
            self._begin_fallback(now, f"feedback missing {loss:.1f}s past trip {cfg['run_loss_trip']:.0f}s")
        elif loss >= cfg["run_loss_hold"]:
            self._transition(now, Phase.FAULT_HOLD, f"feedback missing {loss:.1f}s, holding outputs")

    def _eval_fault_hold(self, now: float, cfg: dict[str, float]) -> None:
        if self._open_stable:
            self._transition(now, Phase.RUNNING, "feedback recovered, hold released")
        elif self._override is not None:
            self._transition(now, Phase.RUNNING, "manual release: supervised run")
        elif self._fb_lost_since is not None and now - self._fb_lost_since >= cfg["run_loss_trip"]:
            self._begin_fallback(now, f"feedback still missing past trip {cfg['run_loss_trip']:.0f}s")

    def _eval_stopping_comp(self, now: float, cfg: dict[str, float]) -> None:
        if not self._raw_run:
            self._transition(now, Phase.VALVE_CLOSING, "compressor stop confirmed, closing valve")
        elif now - self._phase_since >= cfg["comp_stop_timeout"]:
            self._alarm(now, "compressor stop feedback timeout, timed close follows")
            self._transition(now, Phase.VALVE_CLOSING, "stop feedback timeout, timed close")

    def _eval_valve_closing(self, now: float, cfg: dict[str, float]) -> None:
        if self._raw_closed:
            self._transition(now, Phase.IDLE, "valve closed, sequence complete")
        elif now - self._phase_since >= cfg["valve_close_timeout"]:
            self._alarm(now, "close feedback missing after normal stop")
            self._lock(now, "close feedback missing after normal stop, locked")

    def _eval_fault_stopping(self, now: float, cfg: dict[str, float]) -> None:
        if not self._raw_run:
            self._transition(now, Phase.FAULT_CLOSING, "compressor stop confirmed, closing valve")
        elif now - self._phase_since >= cfg["comp_stop_timeout"]:
            self._alarm(now, "compressor stop feedback timeout during fallback, timed close follows")
            self._transition(now, Phase.FAULT_CLOSING, "stop feedback timeout, timed close")

    def _eval_fault_closing(self, now: float, cfg: dict[str, float]) -> None:
        if self._raw_closed:
            self._lock(now, "valve closed feedback confirmed, interlock locked")
        elif now - self._phase_since >= cfg["valve_close_timeout"]:
            self._alarm(now, "close feedback missing during fallback")
            self._lock(now, "close feedback timeout, locked pending field check")

    # -- internals ---------------------------------------------------------

    def _update_debounce(self, now: float) -> None:
        """Believe the open contact only after it holds steady."""

        if self._raw_open == self._open_stable:
            self._pending_since = None
            return
        if self._pending_since is None:
            self._pending_since = now
            return
        if now - self._pending_since < self._config.get("feedback_debounce"):
            return
        previous = self._open_stable
        self._open_stable = self._raw_open
        self._pending_since = None
        self._log(
            now,
            "FEEDBACK",
            "open feedback accepted" if self._open_stable else "open feedback lost (debounced)",
            detail={"raw": self._raw_open},
        )
        if previous and not self._open_stable:
            self._fb_lost_since = now
        elif self._open_stable:
            self._fb_lost_since = None

    def _expire_override(self, now: float) -> None:
        if self._override is None or now < float(self._override["expires_at"]):
            return
        self._log(now, "OVERRIDE", "manual release expired", detail=dict(self._override))
        self._override = None
        if self._phase in (Phase.RUNNING, Phase.FAULT_HOLD) and not self._open_stable:
            self._begin_fallback(now, "manual release expired with feedback still invalid")

    def _begin_fallback(self, now: float, cause: str) -> None:
        self._alarm(now, cause)
        self._transition(now, Phase.FAULT_STOPPING, f"fallback: {cause}; stop compressor first")

    def _lock(self, now: float, cause: str) -> None:
        self._transition(now, Phase.FAULT_LOCKED, cause)
        self._interlock.trip(f"sequence:{self._comp_id} {cause}")

    def _apply_outputs(self) -> None:
        if self._bank.compressor(self._comp_id) is None:
            return
        if self._phase in _OPEN_PHASES:
            self._supply.open_valve(self._comp_id)
        else:
            self._supply.close_valve(self._comp_id)
        if self._phase in _RUN_PHASES:
            self._bank.start(self._comp_id)
        else:
            self._bank.stop(self._comp_id)

    def _require(self, actor: Actor, level: Level, action: str, now: float) -> None:
        allowed = actor.level >= level
        self._log(
            now,
            "AUTH",
            action,
            actor,
            {"required": int(level), "result": "PERMIT" if allowed else "DENY"},
        )
        if not allowed:
            raise PermissionDenied(
                f"{action} requires level {int(level)}, {actor.name} has {int(actor.level)}"
            )

    def _transition(self, now: float, phase: Phase, cause: str, actor: Actor = SYSTEM) -> None:
        previous = self._phase
        self._phase = phase
        self._phase_since = now
        self._log(now, "STATE", f"{previous.value}->{phase.value}", actor, {"cause": cause})

    def _alarm(self, now: float, message: str, detail: dict[str, object] | None = None) -> None:
        self._log(now, "ALARM", message, detail=detail)

    def _log(
        self,
        now: float,
        category: str,
        action: str,
        actor: Actor = SYSTEM,
        detail: dict[str, object] | None = None,
    ) -> None:
        self._trail.record(
            comp_id=self._comp_id,
            trace_id=self._trace_id,
            category=category,
            actor=actor,
            action=action,
            detail=json.dumps(detail, ensure_ascii=False) if detail else "",
            at=now,
        )

    # -- persistence and views -------------------------------------------------

    def _load(self) -> None:
        raw, present = self._store.get(f"{STATE_KEY_PREFIX}{self._comp_id}")
        if not present:
            return
        try:
            saved = json.loads(raw)
        except ValueError:
            return
        if not isinstance(saved, dict):
            return
        try:
            self._phase = Phase(str(saved.get("phase", Phase.IDLE.value)))
        except ValueError:
            self._phase = Phase.IDLE
        self._phase_since = float(saved.get("phase_since", 0.0) or 0.0)
        self._trace_id = str(saved.get("trace_id", ""))
        self._sensor_fault = bool(saved.get("sensor_fault", False))
        override = saved.get("override")
        self._override = dict(override) if isinstance(override, dict) else None
        lost = saved.get("fb_lost_since")
        self._fb_lost_since = float(lost) if isinstance(lost, (int, float)) else None
        self._open_stable = bool(saved.get("open_stable", False))
        pending = saved.get("pending_since")
        self._pending_since = float(pending) if isinstance(pending, (int, float)) else None
        edges = saved.get("open_edges", [])
        self._open_edges = [float(edge) for edge in edges] if isinstance(edges, list) else []
        self._raw_open = bool(saved.get("raw_open", False))
        self._raw_closed = bool(saved.get("raw_closed", False))
        self._raw_run = bool(saved.get("raw_run", False))

    def _save(self) -> None:
        self._store.put(
            f"{STATE_KEY_PREFIX}{self._comp_id}",
            json.dumps(
                {
                    "phase": self._phase.value,
                    "phase_since": self._phase_since,
                    "trace_id": self._trace_id,
                    "sensor_fault": self._sensor_fault,
                    "override": self._override,
                    "fb_lost_since": self._fb_lost_since,
                    "open_stable": self._open_stable,
                    "pending_since": self._pending_since,
                    "open_edges": self._open_edges,
                    "raw_open": self._raw_open,
                    "raw_closed": self._raw_closed,
                    "raw_run": self._raw_run,
                },
                ensure_ascii=False,
            ),
        )

    def snapshot(self, now: float | None = None) -> SequenceState:
        now = time.time() if now is None else now
        return SequenceState(
            comp_id=self._comp_id,
            phase=self._phase.value,
            phase_elapsed=round(max(0.0, now - self._phase_since), 3),
            trace_id=self._trace_id,
            valve_command=OPEN if self._phase in _OPEN_PHASES else CLOSED,
            comp_commanded=self._phase in _RUN_PHASES,
            feedback_open=self._open_stable,
            feedback_open_raw=self._raw_open,
            feedback_closed=self._raw_closed,
            comp_run=self._raw_run,
            sensor_fault=self._sensor_fault,
            feedback_lost_for=(
                round(now - self._fb_lost_since, 3) if self._fb_lost_since is not None else None
            ),
            override=dict(self._override) if self._override else None,
        )

    def describe(self) -> str:
        return f"sequence {self._comp_id} phase={self._phase.value}"


class SequenceCenter:
    """Owns one sequence interlock per compressor plus shared config and trail."""

    def __init__(self, store: Store, supply: SupplyValve, bank: Bank, interlock: Interlock) -> None:
        self._store = store
        self._supply = supply
        self._bank = bank
        self._interlock = interlock
        self.trail = Trail(store)
        self.config = SequenceConfig(store)
        self._controllers: dict[str, SequenceInterlock] = {}

    def controller(self, comp_id: str) -> SequenceInterlock:
        controller = self._controllers.get(comp_id)
        if controller is None:
            controller = SequenceInterlock(
                comp_id,
                self._store,
                self._supply,
                self._bank,
                self._interlock,
                self.trail,
                self.config,
            )
            self._controllers[comp_id] = controller
        return controller

    def evaluate_all(self, now: float | None = None) -> dict[str, str]:
        now = time.time() if now is None else now
        return {
            comp_id: self.controller(comp_id).evaluate(now).value for comp_id in self._known_ids()
        }

    def snapshots(self) -> list[dict[str, object]]:
        return [self.controller(comp_id).snapshot().as_dict() for comp_id in self._known_ids()]

    def set_param(self, actor: Actor, key: str, value: float, now: float | None = None) -> tuple[float, float]:
        now = time.time() if now is None else now
        allowed = actor.level >= Level.ADMIN
        self.trail.record(
            comp_id="*",
            trace_id="",
            category="AUTH",
            actor=actor,
            action="SET_PARAM",
            detail=json.dumps(
                {"key": key, "required": int(Level.ADMIN), "result": "PERMIT" if allowed else "DENY"},
                ensure_ascii=False,
            ),
            at=now,
        )
        if not allowed:
            raise PermissionDenied(
                f"SET_PARAM requires level {int(Level.ADMIN)}, {actor.name} has {int(actor.level)}"
            )
        old, new = self.config.set(key, value)
        self.trail.record(
            comp_id="*",
            trace_id="",
            category="PARAM",
            actor=actor,
            action=f"{key} {old}->{new}",
            at=now,
        )
        return old, new

    def _known_ids(self) -> list[str]:
        ids = set(self._controllers)
        for key in self._store.keys():
            if key.startswith(STATE_KEY_PREFIX):
                ids.add(key[len(STATE_KEY_PREFIX) :])
        return sorted(ids)
