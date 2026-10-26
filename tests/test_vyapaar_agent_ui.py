import os
import sqlite3
import unittest
from datetime import date, timedelta

from vyapaar_agent_ui.pulse import (
    reduce_state,
    render_pulse,
    pulse_activity,
)
from vyapaar_agent_ui.intro import (
    decide_intro,
    intro_finished,
    build_intro_html,
    INTRO_SECONDS,
)
from vyapaar_agent_ui.actions import (
    new_action,
    transition,
    approve,
    update_payload,
    start_execution,
    Action,
)
from vyapaar_agent_ui.next_move import (
    compute_overdue_move,
    dismiss_move,
    is_move_dismissed,
)
from vyapaar_agent_ui.trail import record_trail_step


class FakeSlot:
    def __init__(self):
        self.history = []

    def markdown(self, content, unsafe_allow_html=False):
        self.history.append(content)


class TestVyapaarAgentUI(unittest.TestCase):

    # 1. reduce_state
    def test_reduce_state_transitions(self):
        self.assertEqual(reduce_state("idle", "message_submitted"), "request")
        self.assertEqual(reduce_state("request", "model_started"), "thinking")
        self.assertEqual(reduce_state("thinking", "tool_started"), "tool")
        self.assertEqual(reduce_state("tool", "tool_finished"), "thinking")
        self.assertEqual(reduce_state("thinking", "model_finished"), "idle")
        self.assertEqual(reduce_state("thinking", "approval_requested"), "approval")
        self.assertEqual(reduce_state("approval", "action_verified"), "done")
        self.assertEqual(reduce_state("thinking", "failure"), "error")

        # With model_status="unavailable", every event except reset/model_status_changed yields unavailable
        for ev in ("message_submitted", "model_started", "tool_started", "tool_finished", "failure"):
            self.assertEqual(reduce_state("idle", ev, model_status="unavailable"), "unavailable")
        self.assertEqual(reduce_state("unavailable", "reset", model_status="unavailable"), "idle")

    # 2. No fake activity
    def test_no_fake_activity(self):
        markup = render_pulse("idle", "Ask Vyapaar")
        self.assertIn('data-state="idle"', markup)
        self.assertNotIn("thinking", markup)

        # Unknown state falls back to idle
        unknown_markup = render_pulse("non_existent_state", "Hello")
        self.assertIn('data-state="idle"', unknown_markup)

        # Unknown channel is dropped
        unknown_ch_markup = render_pulse("tool", "Op", channel="fake_ch")
        self.assertIn('data-channel=""', unknown_ch_markup)

    # 3. pulse_activity
    def test_pulse_activity_success_and_exception(self):
        slot = FakeSlot()
        with pulse_activity(slot, "thinking", "Working on your request…"):
            self.assertEqual(len(slot.history), 1)
            self.assertIn('data-state="thinking"', slot.history[0])
            self.assertIn("Working on your request…", slot.history[0])

        self.assertEqual(len(slot.history), 2)
        self.assertIn('data-state="idle"', slot.history[1])
        self.assertIn("Ask Vyapaar", slot.history[1])

        # Exception case
        slot_err = FakeSlot()
        with self.assertRaises(ZeroDivisionError):
            with pulse_activity(slot_err, "thinking", "Working…"):
                _ = 1 / 0

        self.assertEqual(len(slot_err.history), 2)
        self.assertIn('data-state="error"', slot_err.history[1])

    # 4. decide_intro / intro_finished
    def test_decide_intro_and_intro_finished(self):
        # first launch plays
        self.assertTrue(decide_intro(True, False, False))
        # returning user does not
        self.assertFalse(decide_intro(False, False, True))
        # replay plays
        self.assertTrue(decide_intro(False, True, True))

        # intro_finished
        t0 = 1000.0
        self.assertFalse(intro_finished(t0, False, now=1000.0 + 3.0))
        # 6.4s ends it
        self.assertTrue(intro_finished(t0, False, now=1000.0 + INTRO_SECONDS))
        self.assertTrue(intro_finished(t0, False, now=1000.0 + 7.0))
        # skip ends immediately
        self.assertTrue(intro_finished(t0, True, now=1000.5))

    # 5. build_intro_html
    def test_build_intro_html(self):
        html_ready = build_intro_html("ready")
        self.assertIn('window.VYAPAAR_STATUS="ready"', html_ready)

        # Unknown string falls back to connecting
        html_unknown = build_intro_html("unknown_xyz")
        self.assertIn('window.VYAPAAR_STATUS="connecting"', html_unknown)

        # Malicious string cannot break out of the script tag
        evil_status = '</script><script>alert("xss")</script>'
        html_safe = build_intro_html(evil_status)
        # Should have fallen back to connecting, or escaped </script>
        self.assertNotIn("</script><script>alert", html_safe)
        self.assertIn('window.VYAPAAR_STATUS="connecting"', html_safe)

    # 6. Actions state machine
    def test_actions_state_machine(self):
        payload = {"invoice_id": 1, "amount": 500}
        act = new_action("send_reminder", payload)
        self.assertEqual(act.status, "suggested")

        # Legal transition: suggested -> draft -> awaiting_approval
        draft = transition(act, "draft")
        self.assertEqual(draft.status, "draft")
        awaiting = transition(draft, "awaiting_approval")
        self.assertEqual(awaiting.status, "awaiting_approval")

        # Illegal transition: suggested -> approved
        with self.assertRaises(ValueError):
            transition(act, "approved")

        # Approve fails for different action_id
        with self.assertRaises(PermissionError):
            approve(awaiting, "wrong_id", awaiting.phash)

        # Approve fails for stale hash after edit
        edited = update_payload(awaiting, {"invoice_id": 1, "amount": 600})
        with self.assertRaises(PermissionError):
            approve(edited, edited.action_id, awaiting.phash)

        # Approve fails for status other than awaiting_approval (edited is in 'draft')
        with self.assertRaises(PermissionError):
            approve(edited, edited.action_id, edited.phash)

        # Transition back to awaiting_approval with new hash and approve
        awaiting_new = transition(edited, "awaiting_approval")
        approved = approve(awaiting_new, awaiting_new.action_id, awaiting_new.phash)
        self.assertEqual(approved.status, "approved")

        # Second execution with same key is refused
        seen_keys = set()
        executing = start_execution(approved, "exec-key-1", seen_keys)
        self.assertEqual(executing.status, "executing")
        with self.assertRaises(ValueError):
            start_execution(approved, "exec-key-1", seen_keys)

        # Rejecting changes no DB rows
        mem_db = sqlite3.connect(":memory:")
        mem_db.execute("CREATE TABLE test (val INT)")
        mem_db.execute("INSERT INTO test VALUES (42)")
        rejected = transition(act, "rejected")
        self.assertEqual(rejected.status, "rejected")
        count = mem_db.execute("SELECT COUNT(*) FROM test").fetchone()[0]
        self.assertEqual(count, 1)

    # 7. compute_overdue_move
    def test_compute_overdue_move(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE customer (id INT PRIMARY KEY, name TEXT);
            CREATE TABLE invoice (id INT PRIMARY KEY, number TEXT, customer_id INT, total_minor INT, issue_date TEXT, due_date TEXT);
            CREATE TABLE payment (id INT PRIMARY KEY, invoice_id INT, amount_minor INT, paid_on TEXT, method TEXT, idem_key TEXT);
        """)

        today = date(2026, 10, 26)

        # Empty DB returns None
        self.assertIsNone(compute_overdue_move(conn, today))

        # Add customer and overdue invoice
        conn.execute("INSERT INTO customer VALUES (1, 'Ramesh Traders')")
        conn.execute("INSERT INTO invoice VALUES (1, 'INV-001', 1, 100000, '2026-09-01', '2026-09-20')")  # 36 days overdue
        conn.commit()

        move = compute_overdue_move(conn, today)
        self.assertIsNotNone(move)
        self.assertEqual(move.count, 1)
        self.assertEqual(move.total, 1000.0)
        self.assertEqual(move.oldest_days, 36)
        initial_fp = move.fingerprint

        # Record payment: fingerprint changes
        conn.execute("INSERT INTO payment VALUES (1, 1, 40000, '2026-10-01', 'cash', 'pay-1')")
        conn.commit()
        move2 = compute_overdue_move(conn, today)
        self.assertIsNotNone(move2)
        self.assertEqual(move2.total, 600.0)
        self.assertNotEqual(initial_fp, move2.fingerprint)

        # Dismissal by fingerprint
        session = {}
        self.assertFalse(is_move_dismissed(session, move2.fingerprint))
        dismiss_move(session, move2.fingerprint)
        self.assertTrue(is_move_dismissed(session, move2.fingerprint))

    # 8. Trail updates in place
    def test_action_trail_in_place(self):
        trail = []
        record_trail_step(
            trail,
            entry_id="req-101",
            requested="Check overdue invoices",
            records_checked="12 invoices",
            status="Analyzing",
        )
        self.assertEqual(len(trail), 1)
        self.assertEqual(trail[0]["status"], "Analyzing")

        # Same action_id / request updates the existing entry instead of adding a second
        record_trail_step(
            trail,
            entry_id="req-101",
            finding="3 invoices overdue",
            suggested_action="Review affected records",
            status="Awaiting your decision",
        )
        self.assertEqual(len(trail), 1)
        self.assertEqual(trail[0]["finding"], "3 invoices overdue")
        self.assertEqual(trail[0]["status"], "Awaiting your decision")


if __name__ == "__main__":
    unittest.main()
