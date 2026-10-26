import sqlite3
import unittest
from datetime import date

import actions
import agent
import db
import domain
import services
import tools


def create_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(db.SCHEMA)
    db.seed(conn)
    return conn


class TestCore(unittest.TestCase):

    def setUp(self):
        self.conn = create_test_db()

    def tearDown(self):
        self.conn.close()

    def test_inr_formatting(self):
        self.assertEqual(domain.inr(0), "₹0.00")
        self.assertEqual(domain.inr(150050), "₹1,500.50")
        self.assertEqual(domain.inr(1200000), "₹12,000.00")
        self.assertEqual(domain.inr(-5000), "-₹50.00")

    def test_to_minor(self):
        self.assertEqual(domain.to_minor("1500"), 150000)
        self.assertEqual(domain.to_minor("1500.50"), 150050)
        self.assertEqual(domain.to_minor("1,500.50"), 150050)
        with self.assertRaises(ValueError):
            domain.to_minor("-10")
        with self.assertRaises(ValueError):
            domain.to_minor("abc")

    def test_invoice_rows_and_summary(self):
        rows = domain.invoice_rows(self.conn)
        self.assertEqual(len(rows), 4)
        s = domain.summary(rows)
        self.assertGreater(s["outstanding"], 0)
        self.assertGreater(s["overdue"], 0)

    def test_services_record_payment(self):
        # INV-0001 has 1,200,000 paise outstanding
        replay = services.record_payment(self.conn, 1, 50000, "test-pay-1", "upi")
        self.assertFalse(replay)

        # Repeating with same idempotency key returns replay=True
        replay2 = services.record_payment(self.conn, 1, 50000, "test-pay-1", "upi")
        self.assertTrue(replay2)

        # Overpaying raises ValueError
        with self.assertRaises(ValueError):
            services.record_payment(self.conn, 1, 999999999, "test-pay-overpay", "cash")

    def test_services_record_sale(self):
        # Sell 2 units of Basmati Rice (id 1, stock 40)
        res, replay = services.record_sale(self.conn, "test-sale-1", None, 1, 2, 90000)
        self.assertFalse(replay)
        self.assertEqual(res["total_minor"], 90000)

        # Duplicate key returns replay
        res2, replay2 = services.record_sale(self.conn, "test-sale-1", None, 1, 2, 90000)
        self.assertTrue(replay2)

    def test_actions_draft_and_decide(self):
        # Draft a reminder for INV-0001
        aid, note = actions.draft_reminder(self.conn, "INV-0001", "agent")
        self.assertIsNotNone(aid)
        self.assertIn("Reminder drafted", note)

        # Decide approve
        ok, msg = actions.decide(self.conn, aid, approve=True)
        self.assertTrue(ok)
        self.assertIn("SIMULATED", msg)

        # Cannot decide twice
        ok2, msg2 = actions.decide(self.conn, aid, approve=True)
        self.assertFalse(ok2)

    def test_tools(self):
        overdue = tools.overdue_invoices(self.conn)
        self.assertGreater(len(overdue), 0)

        low = tools.low_stock(self.conn)
        self.assertGreater(len(low), 0)

        briefing = tools.briefing(self.conn)
        self.assertGreater(len(briefing), 0)

    def test_agent_answers(self):
        rep = agent.answer(self.conn, "overdue invoices")
        self.assertIn("Overdue invoices:", rep["text"])

        rep_stock = agent.answer(self.conn, "what needs restocking")
        self.assertIn("Needs restocking:", rep_stock["text"])

        rep_empty = agent.answer(self.conn, "   ")
        self.assertEqual(rep_empty["text"], "Please type a question.")

    def test_agent_deterministic_fallback(self):
        from unittest.mock import patch
        with patch("llm.is_model_available", return_value=False):
            rep = agent.answer(self.conn, "who owes me money?")
            self.assertIn("Total pending:", rep["text"])
            self.assertIn("owes the most", rep["text"])
            self.assertEqual(rep["based_on"], [("Money Owed", "/money")])


if __name__ == "__main__":
    unittest.main()
