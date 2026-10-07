"""Append-only audit trail for sequence interlock actions.

Every command, state transition, feedback decision, alarm, override and
parameter change lands here as one entry carrying the operator identity and a
trace id. The trace id is minted when a start sequence begins and stays with
the chain until the machine is back to idle or locked out, so an abnormal
run can be replayed end to end and every entry can be listed per operator.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass

from coldstore.store.commands import append_command, load_commands
from coldstore.store.store import Store

from .auth import Actor

TRAIL_KEY = "sequence:trail"


@dataclass(frozen=True)
class TrailEntry:
    """One traceable record: who did what, in which chain, on what basis."""

    id: str
    time: str
    comp_id: str
    trace_id: str
    category: str  # COMMAND / STATE / FEEDBACK / ALARM / OVERRIDE / PARAM / AUTH
    actor_uid: str
    actor_name: str
    actor_level: int
    action: str
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "time": self.time,
            "comp_id": self.comp_id,
            "trace_id": self.trace_id,
            "category": self.category,
            "actor_uid": self.actor_uid,
            "actor_name": self.actor_name,
            "actor_level": self.actor_level,
            "action": self.action,
            "detail": self.detail,
        }


class Trail:
    """Records and queries the interlock audit chain."""

    def __init__(self, store: Store, key: str = TRAIL_KEY) -> None:
        self._store = store
        self._key = key

    def record(
        self,
        *,
        comp_id: str,
        trace_id: str,
        category: str,
        actor: Actor,
        action: str,
        detail: str = "",
        at: float | None = None,
    ) -> TrailEntry:
        stamp = time.time() if at is None else at
        entry = TrailEntry(
            id=uuid.uuid4().hex,
            time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp)),
            comp_id=comp_id,
            trace_id=trace_id,
            category=category,
            actor_uid=actor.uid,
            actor_name=actor.name,
            actor_level=int(actor.level),
            action=action,
            detail=detail,
        )
        append_command(self._store, self._key, json.dumps(entry.as_dict(), ensure_ascii=False))
        return entry

    def entries(self) -> list[TrailEntry]:
        entries: list[TrailEntry] = []
        for item in load_commands(self._store, self._key):
            try:
                payload = json.loads(item)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            entries.append(
                TrailEntry(
                    id=str(payload.get("id", "")),
                    time=str(payload.get("time", "")),
                    comp_id=str(payload.get("comp_id", "")),
                    trace_id=str(payload.get("trace_id", "")),
                    category=str(payload.get("category", "")),
                    actor_uid=str(payload.get("actor_uid", "")),
                    actor_name=str(payload.get("actor_name", "")),
                    actor_level=int(payload.get("actor_level", 0) or 0),
                    action=str(payload.get("action", "")),
                    detail=str(payload.get("detail", "")),
                )
            )
        return entries

    def query(
        self,
        *,
        actor_uid: str = "",
        trace_id: str = "",
        category: str = "",
        comp_id: str = "",
    ) -> list[TrailEntry]:
        """Filter the chain; every empty argument matches everything."""

        return [
            entry
            for entry in self.entries()
            if (not actor_uid or entry.actor_uid == actor_uid)
            and (not trace_id or entry.trace_id == trace_id)
            and (not category or entry.category == category)
            and (not comp_id or entry.comp_id == comp_id)
        ]

    def for_operator(self, actor_uid: str) -> list[TrailEntry]:
        """Everything one operator touched, for the maintenance console."""

        return self.query(actor_uid=actor_uid)

    def for_trace(self, trace_id: str) -> list[TrailEntry]:
        """One full start/stop/fault chain, in recorded order."""

        return self.query(trace_id=trace_id)

    def count(self) -> int:
        return len(self.entries())
