import hashlib
import json

import services
import tools
from config import REMINDER_COOLDOWN_DAYS
from db import audit, now_iso
from domain import inr, invoice_rows

BANNED = ("police", "legal action", "court", "shame", "threat", "fraud")


class Stale(Exception):
    pass


def _hash(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def propose(conn, type_, payload, reason, created_by="agent"):
    with conn:
        cur = conn.execute(
            "INSERT INTO pending_action(type,payload,payload_hash,reason,created_by,status,created_at) "
            "VALUES(?,?,?,?,?,'proposed',?)",
            (type_, json.dumps(payload), _hash(payload), reason, created_by, now_iso()))
        audit(conn, "action_proposed", f"pending_action:{cur.lastrowid}", f"{type_} by {created_by}")
    return cur.lastrowid


def check_message(payload):
    m = payload["message"]
    if payload["invoice_number"] not in m:
        return f"The message must mention invoice {payload['invoice_number']}."
    if inr(payload["outstanding_minor"]) not in m:
        return f"The message must show the pending amount {inr(payload['outstanding_minor'])}."
    if any(w in m.lower() for w in BANNED):
        return "Please keep the message polite: no threats or pressure words."
    return None


def draft_reminder(conn, invoice_number, created_by="agent"):
    """Returns (action_id or None, note)."""
    row = next((r for r in invoice_rows(conn) if r["number"] == invoice_number), None)
    if row is None:
        return None, f"I could not find invoice {invoice_number}."
    if row["outstanding_minor"] <= 0:
        return None, f"{invoice_number} is already fully paid, so no reminder is needed."
    if row["days_since_reminder"] is not None and row["days_since_reminder"] < REMINDER_COOLDOWN_DAYS:
        return None, (f"A reminder for {invoice_number} was sent {row['days_since_reminder']} day(s) ago "
                      f"(cooldown is {REMINDER_COOLDOWN_DAYS} days).")
    msg = (f"Namaste {row['customer']}, a gentle reminder that invoice {row['number']} for "
           f"{inr(row['total_minor'])} was due on {row['due_date']}. Balance remaining: "
           f"{inr(row['outstanding_minor'])}. Please pay by UPI or bank transfer when convenient, "
           f"or reply if you need any help. Thank you!")
    payload = {"invoice_id": row["id"], "invoice_number": row["number"],
               "customer": row["customer"], "outstanding_minor": row["outstanding_minor"], "message": msg}
    reason = f"{row['number']} is {row['days_overdue']} days overdue. " + "; ".join(row["factors"])
    return propose(conn, "send_reminder", payload, reason, created_by), "Reminder drafted."


def decide(conn, action_id, approve, message=None):
    """Human decision. Returns (ok, text)."""
    row = conn.execute("SELECT * FROM pending_action WHERE id=?", (action_id,)).fetchone()
    if row is None or row["status"] != "proposed":
        return False, "This action is not waiting for a decision."
    if not approve:
        with conn:
            conn.execute("UPDATE pending_action SET status='rejected',decided_at=? WHERE id=? AND status='proposed'",
                         (now_iso(), action_id))
            audit(conn, "action_rejected", f"pending_action:{action_id}", row["type"])
        return True, "Rejected. Nothing was changed."
    payload = json.loads(row["payload"])
    if message is not None and row["type"] == "send_reminder":
        payload["message"] = message.strip()
        err = check_message(payload)
        if err:
            return False, err
    with conn:
        n = conn.execute(
            "UPDATE pending_action SET status='approved',payload=?,payload_hash=?,decided_at=? "
            "WHERE id=? AND status='proposed'",
            (json.dumps(payload), _hash(payload), now_iso(), action_id)).rowcount
        if n == 0:
            return False, "Already decided."
        audit(conn, "action_approved", f"pending_action:{action_id}", row["type"])
    return execute(conn, action_id)


def _finish(conn, action_id, status, result):
    with conn:
        conn.execute("UPDATE pending_action SET status=?,result=?,executed_at=? WHERE id=?",
                     (status, result, now_iso(), action_id))
        audit(conn, f"action_{status}", f"pending_action:{action_id}", result)
    return status == "executed", result


def execute(conn, action_id):
    with conn:  # compare-and-swap: only one caller can move approved -> executing
        n = conn.execute("UPDATE pending_action SET status='executing' WHERE id=? AND status='approved'",
                         (action_id,)).rowcount
    if n == 0:
        return False, "Already executed or not approved."
    row = conn.execute("SELECT * FROM pending_action WHERE id=?", (action_id,)).fetchone()
    payload = json.loads(row["payload"])
    if _hash(payload) != row["payload_hash"]:
        return _finish(conn, action_id, "stale", "Payload changed after approval; please re-propose.")
    try:
        if row["type"] == "send_reminder":
            text = _exec_reminder(conn, payload)
        elif row["type"] == "record_sale":
            text = _exec_sale(conn, action_id, payload)
        else:
            raise ValueError(f"Unsupported action type {row['type']}")
    except Stale as e:
        return _finish(conn, action_id, "stale", str(e))
    except ValueError as e:
        return _finish(conn, action_id, "failed", str(e))
    return _finish(conn, action_id, "executed", text)


def _exec_reminder(conn, p):
    inv = next((r for r in invoice_rows(conn) if r["id"] == p["invoice_id"]), None)
    if inv is None or inv["outstanding_minor"] != p["outstanding_minor"]:
        raise Stale("The balance changed since this reminder was drafted. Please draft a new one.")
    err = check_message(p)
    if err:
        raise ValueError(err)
    with conn:
        rid = conn.execute("INSERT INTO reminder(invoice_id,message,status,created_at) VALUES(?,?,?,?)",
                           (p["invoice_id"], p["message"], "simulated_sent", now_iso())).lastrowid
        audit(conn, "reminder_simulated_sent", f"invoice:{p['invoice_number']}", "SIMULATED, not delivered")
    check = conn.execute("SELECT status FROM reminder WHERE id=?", (rid,)).fetchone()  # verify
    if check is None or check["status"] != "simulated_sent":
        raise ValueError("Verification failed after saving the reminder.")
    return (f"SIMULATED: reminder for {p['invoice_number']} marked as sent. "
            "No message was actually delivered.")


def _exec_sale(conn, action_id, p):
    prod = conn.execute("SELECT * FROM product WHERE id=?", (p["product_id"],)).fetchone()
    if prod is None or prod["price_minor"] * p["qty"] != p["total_minor"]:
        raise Stale("The product price changed since this sale was proposed.")
    sale, replay = services.record_sale(conn, f"action-{action_id}", p["customer_id"],
                                        p["product_id"], p["qty"], p["paid_minor"])
    after = conn.execute("SELECT stock FROM product WHERE id=?", (p["product_id"],)).fetchone()
    return f"Sale saved. {prod['name']} stock is now {after['stock']}."