"""Behavioural tests for the valve/compressor sequence interlock."""

from __future__ import annotations

import tempfile
import unittest

from coldstore.comp import Bank
from coldstore.interlock import (
    Actor,
    Interlock,
    Level,
    PermissionDenied,
    Phase,
    SequenceCenter,
)
from coldstore.store import Store
from coldstore.valve import SupplyValve


class SequenceCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/state.json")
        self.bank = Bank()
        self.bank.add_compressor("c1", 3)
        self.supply = SupplyValve(self.store)
        self.interlock = Interlock(self.store)
        self.center = SequenceCenter(self.store, self.supply, self.bank, self.interlock)
        self.seq = self.center.controller("c1")
        self.op = Actor("u-op", "operator", Level.OPERATOR)
        self.lead = Actor("u-lead", "shift-lead", Level.SUPERVISOR)
        self.peer = Actor("u-peer", "peer-engineer", Level.SUPERVISOR)
        self.admin = Actor("u-admin", "instrument-admin", Level.ADMIN)
        self.viewer = Actor("u-view", "viewer", Level.VIEWER)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def reach_running(self) -> None:
        """Drive the happy path up to RUNNING, starting at t=1000."""

        self.seq.start(self.op, 1000.0)
        self.seq.set_feedback(1002.0, valve_open=True)
        self.seq.evaluate(1003.5)
        self.assertEqual(self.seq.snapshot(1003.5).phase, "COMP_STARTING")
        self.seq.set_feedback(1004.0, comp_run=True)
        self.seq.evaluate(1004.0)
        self.assertEqual(self.seq.snapshot(1004.0).phase, "RUNNING")

    def test_happy_path_orders_valve_before_compressor(self) -> None:
        self.seq.start(self.op, 1000.0)
        snap = self.seq.snapshot(1000.0)
        self.assertEqual(snap.phase, "VALVE_OPENING")
        self.assertTrue(self.supply.is_open("c1"))
        self.assertFalse(self.bank.compressor("c1").running)

        # Feedback must hold steady through the debounce window first.
        self.seq.set_feedback(1002.0, valve_open=True)
        self.seq.evaluate(1002.5)
        self.assertEqual(self.seq.snapshot(1002.5).phase, "VALVE_OPENING")
        self.assertFalse(self.bank.compressor("c1").running)
        self.seq.evaluate(1003.5)
        self.assertEqual(self.seq.snapshot(1003.5).phase, "COMP_STARTING")
        self.assertTrue(self.bank.compressor("c1").running)

        self.seq.set_feedback(1004.0, comp_run=True)
        self.seq.evaluate(1004.0)
        self.assertEqual(self.seq.snapshot(1004.0).phase, "RUNNING")

        # Normal stop: compressor stops first, valve closes only after.
        self.seq.stop(self.op, 1010.0)
        self.assertEqual(self.seq.snapshot(1010.0).phase, "STOPPING_COMP")
        self.assertTrue(self.supply.is_open("c1"))
        self.seq.set_feedback(1011.0, comp_run=False)
        self.seq.evaluate(1011.0)
        self.assertEqual(self.seq.snapshot(1011.0).phase, "VALVE_CLOSING")
        self.assertFalse(self.supply.is_open("c1"))
        self.seq.set_feedback(1012.0, valve_closed=True)
        self.seq.evaluate(1012.0)
        self.assertEqual(self.seq.snapshot(1012.0).phase, "IDLE")

        # The whole chain shares one trace id.
        trace = self.seq.snapshot(1012.0).trace_id
        entries = self.center.trail.for_trace(trace)
        self.assertTrue(entries)
        self.assertEqual(entries[0].action, "start sequence")
        self.assertEqual(entries[-1].action, "VALVE_CLOSING->IDLE")

    def test_open_feedback_timeout_closes_valve_and_never_starts(self) -> None:
        self.seq.start(self.op, 0.0)
        self.seq.evaluate(21.0)
        snap = self.seq.snapshot(21.0)
        self.assertEqual(snap.phase, "FAULT_CLOSING")
        self.assertFalse(self.bank.compressor("c1").running)
        self.assertFalse(self.supply.is_open("c1"))

        self.seq.set_feedback(22.0, valve_closed=True)
        self.seq.evaluate(22.0)
        self.assertEqual(self.seq.snapshot(22.0).phase, "FAULT_LOCKED")
        self.assertTrue(self.interlock.is_tripped())

        with self.assertRaises(PermissionDenied):
            self.seq.reset(self.op, 23.0)
        self.seq.reset(self.lead, 24.0)
        self.assertEqual(self.seq.snapshot(24.0).phase, "IDLE")

    def test_debounce_ignores_short_feedback_blips(self) -> None:
        self.seq.start(self.op, 0.0)
        self.seq.set_feedback(1.0, valve_open=True)
        self.seq.evaluate(1.9)
        self.assertEqual(self.seq.snapshot(1.9).phase, "VALVE_OPENING")
        # Blip back down before the debounce window closes resets the timer.
        self.seq.set_feedback(1.9, valve_open=False)
        self.seq.set_feedback(2.5, valve_open=True)
        self.seq.evaluate(3.4)
        self.assertEqual(self.seq.snapshot(3.4).phase, "VALVE_OPENING")
        self.seq.evaluate(3.6)
        self.assertEqual(self.seq.snapshot(3.6).phase, "COMP_STARTING")
        self.assertFalse(self.seq.snapshot(3.6).sensor_fault)

    def test_chatter_storm_locks_auto_path_until_manual_release(self) -> None:
        self.seq.start(self.op, 0.0)
        # Seven edges inside the chatter window is a sensor fault.
        for index in range(7):
            self.seq.set_feedback(float(index) * 0.5 + 1.0, valve_open=index % 2 == 0)
        self.assertTrue(self.seq.snapshot(5.0).sensor_fault)

        # A steady signal afterwards still cannot start the machine.
        self.seq.set_feedback(6.0, valve_open=True)
        self.seq.evaluate(8.0)
        self.assertEqual(self.seq.snapshot(8.0).phase, "VALVE_OPENING")

        # Operators cannot release; one supervisor cannot self-approve.
        with self.assertRaises(PermissionDenied):
            self.seq.manual_release(self.op, self.peer, "switch faulty", True, 9.0)
        with self.assertRaises(ValueError):
            self.seq.manual_release(self.lead, self.lead, "switch faulty", True, 9.0)
        with self.assertRaises(ValueError):
            self.seq.manual_release(self.lead, self.peer, "switch faulty", False, 9.0)

        self.seq.manual_release(self.lead, self.peer, "limit switch faulty, confirmed open", True, 9.0)
        snap = self.seq.snapshot(9.0)
        self.assertEqual(snap.phase, "COMP_STARTING")
        self.assertIsNotNone(snap.override)
        overrides = self.center.trail.query(category="OVERRIDE")
        self.assertTrue(any(entry.actor_uid == "u-lead" for entry in overrides))

    def test_manual_release_expiry_falls_back_when_feedback_still_bad(self) -> None:
        self.center.set_param(self.admin, "override_ttl", 60.0, 0.0)
        self.seq.start(self.op, 0.0)
        for index in range(7):
            self.seq.set_feedback(float(index) * 0.5 + 1.0, valve_open=index % 2 == 0)
        self.seq.manual_release(self.lead, self.peer, "switch faulty", True, 5.0)
        self.seq.set_feedback(6.0, comp_run=True)
        self.seq.evaluate(6.0)
        self.assertEqual(self.seq.snapshot(6.0).phase, "RUNNING")

        # Feedback never becomes trustworthy; the release expires at t=65.
        self.seq.set_feedback(7.0, valve_open=False)
        self.seq.evaluate(8.5)
        self.seq.evaluate(66.0)
        self.assertEqual(self.seq.snapshot(66.0).phase, "FAULT_STOPPING")
        self.assertFalse(self.bank.compressor("c1").running)
        self.assertTrue(self.supply.is_open("c1"))

    def test_run_feedback_loss_is_graded_then_falls_back_in_order(self) -> None:
        self.reach_running()

        # Short loss: outputs frozen, machine keeps running.
        self.seq.set_feedback(1100.0, valve_open=False)
        self.seq.evaluate(1101.5)  # debounce confirms the loss at 1101.5
        self.seq.evaluate(1104.0)  # 2.5s missing, below the hold threshold
        self.assertEqual(self.seq.snapshot(1104.0).phase, "RUNNING")
        self.seq.evaluate(1105.0)  # 3.5s missing: hold position and alarm
        self.assertEqual(self.seq.snapshot(1105.0).phase, "FAULT_HOLD")
        self.assertTrue(self.bank.compressor("c1").running)
        self.assertTrue(self.supply.is_open("c1"))

        # Recovery inside the window returns to running.
        self.seq.set_feedback(1106.0, valve_open=True)
        self.seq.evaluate(1107.5)
        self.assertEqual(self.seq.snapshot(1107.5).phase, "RUNNING")

        # A long loss falls back: compressor stops first, valve closes after.
        self.seq.set_feedback(1200.0, valve_open=False)
        self.seq.evaluate(1201.5)
        self.seq.evaluate(1217.0)  # 15.5s missing, past the trip threshold
        snap = self.seq.snapshot(1217.0)
        self.assertEqual(snap.phase, "FAULT_STOPPING")
        self.assertFalse(self.bank.compressor("c1").running)
        self.assertTrue(self.supply.is_open("c1"))  # valve stays open until the stop confirms

        self.seq.set_feedback(1218.0, comp_run=False)
        self.seq.evaluate(1218.0)
        self.assertEqual(self.seq.snapshot(1218.0).phase, "FAULT_CLOSING")
        self.assertFalse(self.supply.is_open("c1"))

        self.seq.set_feedback(1219.0, valve_closed=True)
        self.seq.evaluate(1219.0)
        self.assertEqual(self.seq.snapshot(1219.0).phase, "FAULT_LOCKED")
        self.assertTrue(self.interlock.is_tripped())

        alarms = self.center.trail.query(category="ALARM")
        self.assertTrue(any("feedback missing" in entry.action for entry in alarms))

    def test_permissions_and_param_changes_are_logged(self) -> None:
        with self.assertRaises(PermissionDenied):
            self.seq.start(self.viewer, 0.0)
        with self.assertRaises(PermissionDenied):
            self.center.set_param(self.lead, "run_loss_trip", 30.0, 0.0)
        old, new = self.center.set_param(self.admin, "run_loss_trip", 30.0, 1.0)
        self.assertEqual((old, new), (15.0, 30.0))
        with self.assertRaises(ValueError):
            self.center.set_param(self.admin, "no_such_key", 1.0, 2.0)

        denied = self.center.trail.query(actor_uid="u-view", category="AUTH")
        self.assertTrue(any("DENY" in entry.detail for entry in denied))
        params = self.center.trail.query(category="PARAM")
        self.assertEqual(params[-1].action, "run_loss_trip 15.0->30.0")
        self.assertEqual(params[-1].actor_uid, "u-admin")

    def test_trail_traces_by_operator_and_trace_id(self) -> None:
        self.reach_running()
        trace = self.seq.snapshot(1004.0).trace_id

        mine = self.center.trail.for_operator("u-op")
        self.assertTrue(any(entry.action == "start sequence" for entry in mine))
        self.assertTrue(all(entry.actor_uid == "u-op" for entry in mine))

        chain = self.center.trail.for_trace(trace)
        actions = [entry.action for entry in chain]
        self.assertEqual(actions[0], "start sequence")
        self.assertIn("VALVE_OPENING->COMP_STARTING", actions)
        self.assertIn("COMP_STARTING->RUNNING", actions)

    def test_state_survives_a_controller_reload(self) -> None:
        self.reach_running()
        rebuilt = SequenceCenter(self.store, self.supply, self.bank, self.interlock)
        restored = rebuilt.controller("c1")
        snap = restored.snapshot(1005.0)
        self.assertEqual(snap.phase, "RUNNING")
        self.assertTrue(snap.feedback_open)
        self.assertEqual(snap.trace_id, self.seq.snapshot(1005.0).trace_id)


if __name__ == "__main__":
    unittest.main()
