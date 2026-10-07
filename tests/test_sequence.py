"""Behavioural tests for the start/stop sequence interlock."""

from __future__ import annotations

import tempfile
import unittest

from coldstore.audit import Auditor
from coldstore.comp import Bank
from coldstore.sequence import Operator, Role, Sequence, Timing, parse_role
from coldstore.store import Store
from coldstore.valve import SupplyValve

COMP = "c1"


def test_timing() -> Timing:
    return Timing(
        debounce=0.5,
        open_timeout=5.0,
        stabilize=1.0,
        confirm_valid=30.0,
        start_timeout=5.0,
        hold_max=3.0,
        stop_timeout=5.0,
        close_timeout=5.0,
        chatter_edges=6,
        chatter_window=5.0,
        bypass_ttl=60.0,
    )


class SequenceCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store.open(f"{self._tmp.name}/state.json")
        self.addCleanup(self.store.close)
        self.supply = SupplyValve(self.store)
        self.bank = Bank()
        self.bank.add_compressor(COMP, 2)
        self.auditor = Auditor(self.store)
        self.seq = Sequence(self.store, self.supply, self.bank, self.auditor, timing=test_timing())
        self.op = Operator("张三", Role.OPERATOR)
        self.op2 = Operator("赵六", Role.OPERATOR)
        self.eng = Operator("李四", Role.ENGINEER)
        self.viewer = Operator("王五", Role.VIEWER)

    # ---------- helpers ----------

    def boot(self) -> None:
        """Establish the initial feedback: valve closed, compressor stopped."""

        self.seq.feedback(COMP, 0.0, open_limit=False, close_limit=True, running=False)
        self.seq.tick(COMP, 1.0)

    def to_running(self) -> str:
        """Walk the interlocked start up to RUNNING; return the cycle id."""

        self.boot()
        result = self.seq.start(COMP, self.op, 2.0)
        self.assertTrue(result["allowed"])
        self.seq.feedback(COMP, 2.5, open_limit=True, close_limit=False)
        self.seq.tick(COMP, 3.2)  # debounce -> READY
        self.seq.tick(COMP, 4.5)  # stabilize -> STARTING (auto)
        self.seq.feedback(COMP, 4.8, running=True)
        self.seq.tick(COMP, 5.5)  # running confirmed -> RUNNING
        self.assertEqual(self.seq.machine(COMP)["phase"], "RUNNING")
        return result["cycle"]

    def transitions(self, cycle: str) -> list[str]:
        return [
            str(entry["after"])
            for entry in self.seq.trace(cycle=cycle, event="TRANSITION")
        ]

    # ---------- normal sequence ----------

    def test_normal_start_stop_cycle(self) -> None:
        cycle = self.to_running()
        view = self.seq.machine(COMP)
        self.assertEqual(view["valve_command"], "open")
        self.assertEqual(view["compressor_command"], "run")
        self.assertTrue(view["open_confirmed"])
        self.assertTrue(view["running_confirmed"])

        result = self.seq.stop(COMP, self.op, 6.0)
        self.assertTrue(result["allowed"])
        view = self.seq.machine(COMP)
        self.assertEqual(view["phase"], "STOPPING")
        self.assertEqual(view["compressor_command"], "stop")
        self.assertEqual(view["valve_command"], "open")  # valve stays open until stopped

        self.seq.feedback(COMP, 6.5, running=False)
        self.seq.tick(COMP, 7.2)
        view = self.seq.machine(COMP)
        self.assertEqual(view["phase"], "CLOSING")
        self.assertEqual(view["valve_command"], "close")  # closed only after stop confirmed

        self.seq.feedback(COMP, 7.5, open_limit=False, close_limit=True)
        self.seq.tick(COMP, 8.2)
        self.assertEqual(self.seq.machine(COMP)["phase"], "IDLE")

        order = self.transitions(cycle)
        self.assertEqual(
            order,
            ["OPENING", "READY", "STARTING", "RUNNING", "STOPPING", "CLOSING", "IDLE"],
        )

    def test_start_rejected_without_closed_confirmation(self) -> None:
        result = self.seq.start(COMP, self.op, 1.0)
        self.assertFalse(result["allowed"])
        self.assertIn("closed confirmation", str(result["reason"]))
        rejects = self.seq.trace(operator="张三", event="REJECT")
        self.assertEqual(len(rejects), 1)

    def test_start_rejected_for_unknown_compressor(self) -> None:
        self.seq.feedback("ghost", 0.0, close_limit=True, open_limit=False, running=False)
        self.seq.tick("ghost", 1.0)
        result = self.seq.start("ghost", self.op, 2.0)
        self.assertFalse(result["allowed"])
        self.assertIn("not registered", str(result["reason"]))

    # ---------- missing feedback: hold then fallback ----------

    def test_open_feedback_timeout_holds_then_falls_back(self) -> None:
        self.boot()
        result = self.seq.start(COMP, self.op, 2.0)
        self.assertTrue(result["allowed"])

        self.seq.tick(COMP, 7.5)  # open_timeout reached
        view = self.seq.machine(COMP)
        self.assertEqual(view["phase"], "HOLD")
        self.assertEqual(view["valve_command"], "open")  # outputs frozen
        self.assertEqual(view["compressor_command"], "stop")  # never allowed to start

        self.seq.tick(COMP, 11.0)  # hold_max exceeded -> fallback
        view = self.seq.machine(COMP)
        self.assertEqual(view["phase"], "FALLBACK_CLOSE")
        self.assertEqual(view["valve_command"], "close")

        self.seq.tick(COMP, 12.0)  # closed limit already confirmed -> locked
        view = self.seq.machine(COMP)
        self.assertEqual(view["phase"], "FAULT")
        self.assertFalse(self.bank.compressor(COMP).running)

        kinds = [entry.get("kind") for entry in self.seq.trace(cycle=result["cycle"], event="ALARM")]
        self.assertIn("feedback_missing", kinds)
        self.assertIn("fallback", kinds)

    def test_running_feedback_loss_short_recovers(self) -> None:
        self.to_running()
        self.seq.feedback(COMP, 6.0, open_limit=False)
        self.seq.tick(COMP, 6.6)
        view = self.seq.machine(COMP)
        self.assertEqual(view["phase"], "HOLD")
        self.assertEqual(view["valve_command"], "open")
        self.assertEqual(view["compressor_command"], "run")  # held, not tripped

        self.seq.feedback(COMP, 7.0, open_limit=True)
        self.seq.tick(COMP, 7.6)  # recovered inside the hold limit
        self.assertEqual(self.seq.machine(COMP)["phase"], "RUNNING")

    def test_running_feedback_loss_long_fallback_order(self) -> None:
        cycle = self.to_running()
        self.seq.feedback(COMP, 6.0, open_limit=False)
        self.seq.tick(COMP, 6.6)  # HOLD
        self.seq.tick(COMP, 10.0)  # hold limit exceeded
        view = self.seq.machine(COMP)
        self.assertEqual(view["phase"], "FALLBACK_STOP")
        self.assertEqual(view["compressor_command"], "stop")
        self.assertEqual(view["valve_command"], "open")  # never closed against a running machine

        self.seq.feedback(COMP, 10.5, running=False)
        self.seq.tick(COMP, 11.2)
        view = self.seq.machine(COMP)
        self.assertEqual(view["phase"], "FALLBACK_CLOSE")
        self.assertEqual(view["valve_command"], "close")  # closed only after stop confirmed

        self.seq.feedback(COMP, 11.5, close_limit=True)
        self.seq.tick(COMP, 12.2)
        view = self.seq.machine(COMP)
        self.assertEqual(view["phase"], "FAULT")
        self.assertIn("fallback complete", view["fault"])

        events = [entry["event"] for entry in self.seq.trace(cycle=cycle)]
        self.assertEqual(events.count("ALARM"), 2)
        self.assertIn("FAULT", events)

    def test_fallback_stop_feedback_missing_holds_valve_open(self) -> None:
        self.to_running()
        self.seq.feedback(COMP, 6.0, open_limit=False)
        self.seq.tick(COMP, 6.6)
        self.seq.tick(COMP, 10.0)  # FALLBACK_STOP
        self.seq.tick(COMP, 16.0)  # stop feedback never arrives
        view = self.seq.machine(COMP)
        self.assertEqual(view["phase"], "FAULT")
        self.assertEqual(view["valve_command"], "open")  # held open: no dry-run risk
        self.assertIn("stop feedback missing", view["fault"])

    # ---------- debounce and chatter ----------

    def test_debounce_delays_confirmation_until_steady(self) -> None:
        self.boot()
        self.seq.start(COMP, self.op, 2.0)
        self.seq.feedback(COMP, 2.2, open_limit=True)
        self.seq.feedback(COMP, 2.4, open_limit=False)
        self.seq.feedback(COMP, 2.6, open_limit=True)
        self.seq.tick(COMP, 3.0)  # steady for 0.4 < debounce 0.5
        self.assertEqual(self.seq.machine(COMP)["phase"], "OPENING")
        self.seq.tick(COMP, 3.2)  # steady for 0.6 >= debounce
        self.assertEqual(self.seq.machine(COMP)["phase"], "READY")

    def test_chatter_locks_out_and_needs_engineer_reset(self) -> None:
        self.boot()
        result = self.seq.start(COMP, self.op, 2.0)
        cycle = str(result["cycle"])
        at = 2.1
        for _ in range(4):  # 8 edges inside the window, limit is 6
            self.seq.feedback(COMP, at, open_limit=True)
            at += 0.1
            self.seq.feedback(COMP, at, open_limit=False)
            at += 0.1
        view = self.seq.machine(COMP)
        self.assertEqual(view["phase"], "FAULT")

        faults = self.seq.trace(cycle=cycle, event="FAULT")
        self.assertTrue(any(entry.get("kind") == "chatter" for entry in faults))

        denied = self.seq.reset(COMP, self.op, 5.0)
        self.assertFalse(denied["allowed"])
        allowed = self.seq.reset(COMP, self.eng, 5.0)
        self.assertTrue(allowed["reset"])
        self.assertEqual(self.seq.machine(COMP)["phase"], "IDLE")

    # ---------- manual release ----------

    def test_manual_release_is_engineer_signed_and_traced(self) -> None:
        self.boot()
        self.seq.start(COMP, self.op, 2.0)
        self.seq.tick(COMP, 7.5)  # open feedback timeout -> HOLD

        denied = self.seq.grant_bypass(COMP, self.op, grantee="张三", reason="x", now=8.0)
        self.assertFalse(denied["allowed"])  # L1 cannot sign a release

        granted = self.seq.grant_bypass(
            COMP, self.eng, grantee="张三", reason="开到位开关故障，现场确认已开", now=8.0
        )
        self.assertIn("permit", granted)

        wrong_hand = self.seq.release(COMP, self.op2, 8.5)
        self.assertFalse(wrong_hand["allowed"])  # permit belongs to 张三

        released = self.seq.release(COMP, self.op, 8.5)
        self.assertTrue(released["released"])
        self.assertEqual(self.seq.machine(COMP)["phase"], "STARTING")

        self.seq.feedback(COMP, 9.0, running=True)
        self.seq.tick(COMP, 9.6)
        self.assertEqual(self.seq.machine(COMP)["phase"], "RUNNING")

        self.seq.tick(COMP, 40.0)  # open feedback stays missing; bypass holds the run
        self.assertEqual(self.seq.machine(COMP)["phase"], "RUNNING")

        auth = self.seq.trace(operator="李四", event="AUTH")
        used = self.seq.trace(operator="张三", event="BYPASS")
        self.assertEqual(len(auth), 1)
        self.assertEqual(len(used), 1)
        self.assertEqual(auth[0]["permit"], used[0]["permit"])

    def test_release_rejected_without_or_with_expired_permit(self) -> None:
        self.boot()
        self.seq.start(COMP, self.op, 2.0)
        early = self.seq.release(COMP, self.op, 3.0)
        self.assertFalse(early["allowed"])

        self.seq.tick(COMP, 7.5)  # HOLD
        self.seq.grant_bypass(COMP, self.eng, grantee="张三", reason="x", now=8.0)
        expired = self.seq.release(COMP, self.op, 100.0)  # past bypass_ttl
        self.assertFalse(expired["allowed"])

    # ---------- permissions ----------

    def test_viewer_cannot_operate(self) -> None:
        self.boot()
        self.assertFalse(self.seq.start(COMP, self.viewer, 2.0)["allowed"])
        self.assertFalse(self.seq.stop(COMP, self.viewer, 2.0)["allowed"])
        rejects = self.seq.trace(operator="王五", event="REJECT")
        self.assertEqual(len(rejects), 2)

    def test_role_parsing(self) -> None:
        self.assertEqual(parse_role("engineer"), Role.ENGINEER)
        self.assertEqual(parse_role(" Operator "), Role.OPERATOR)
        with self.assertRaises(ValueError):
            parse_role("root")

    # ---------- reset ----------

    def test_reset_requires_confirmed_safe_state(self) -> None:
        self.to_running()
        self.seq.feedback(COMP, 6.0, open_limit=False)
        self.seq.tick(COMP, 6.6)
        self.seq.tick(COMP, 10.0)  # FALLBACK_STOP
        self.seq.feedback(COMP, 10.5, running=False)
        self.seq.tick(COMP, 11.2)  # FALLBACK_CLOSE
        self.seq.tick(COMP, 17.0)  # close feedback never arrives -> FAULT
        self.assertEqual(self.seq.machine(COMP)["phase"], "FAULT")

        denied = self.seq.reset(COMP, self.eng, 18.0)
        self.assertFalse(denied["allowed"])  # closed confirmation still missing

        self.seq.feedback(COMP, 18.5, close_limit=True)
        self.seq.tick(COMP, 19.2)
        allowed = self.seq.reset(COMP, self.eng, 19.5)
        self.assertTrue(allowed["reset"])
        self.assertEqual(self.seq.machine(COMP)["phase"], "IDLE")

    # ---------- traceability ----------

    def test_trace_by_operator_and_cycle(self) -> None:
        cycle = self.to_running()
        self.seq.feedback(COMP, 6.0, open_limit=False)
        self.seq.tick(COMP, 6.6)
        self.seq.tick(COMP, 10.0)

        mine = self.seq.trace(operator="张三")
        self.assertTrue(mine)
        self.assertTrue(all(entry["operator"] == "张三" for entry in mine))

        chain = self.seq.trace(cycle=cycle)
        self.assertTrue(all(entry["cycle"] == cycle for entry in chain))
        kinds = [entry["event"] for entry in chain]
        self.assertIn("ALARM", kinds)
        self.assertLess(kinds.index("ALARM"), kinds.index("TRANSITION", kinds.index("ALARM")))

    # ---------- timing ----------

    def test_timing_change_is_engineer_only_and_persists(self) -> None:
        denied = self.seq.set_timing(self.op, 1.0, {"hold_max": 9.0})
        self.assertFalse(denied["updated"])

        done = self.seq.set_timing(self.eng, 1.0, {"hold_max": 9.0})
        self.assertTrue(done["updated"])

        reloaded = Sequence(self.store, self.supply, self.bank, self.auditor)
        self.assertEqual(reloaded.timing().hold_max, 9.0)

        with self.assertRaises(ValueError):
            self.seq.set_timing(self.eng, 2.0, {"bogus": 1.0})
        with self.assertRaises(ValueError):
            self.seq.set_timing(self.eng, 2.0, {"hold_max": -1.0})

    # ---------- persistence ----------

    def test_machine_state_survives_reload(self) -> None:
        self.boot()
        self.seq.start(COMP, self.op, 2.0)
        reloaded = Sequence(
            Store.open(f"{self._tmp.name}/state.json"), self.supply, self.bank, self.auditor,
            timing=test_timing(),
        )
        view = reloaded.machine(COMP)
        self.assertEqual(view["phase"], "OPENING")
        self.assertEqual(view["valve_command"], "open")


if __name__ == "__main__":
    unittest.main()
