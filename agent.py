import re
# llm.is_llm_available() checks Groq (cloud) OR Ollama (local)

import actions
import llm
import tools
from domain import inr


def _reply(text, based_on=None, proposal=None, pulse_state="idle", pulse_channel="", pulse_label="Ask Vyapaar", trail_info=None):
    return {
        "text": text,
        "based_on": based_on or [],
        "proposal": proposal,
        "pulse_state": pulse_state,
        "pulse_channel": pulse_channel,
        "pulse_label": pulse_label,
        "trail_info": trail_info or {},
    }


def _answer_briefing(conn, on_event=None):
    if on_event:
        on_event("tool", "Building your daily briefing", "briefing")
    items = tools.briefing(conn)
    if not items:
        return _reply(
            "Nothing urgent today.",
            pulse_state="idle",
            pulse_channel="",
            pulse_label="Ask Vyapaar",
            trail_info={"records_checked": "All active invoices & stock", "finding": "Nothing urgent today", "status": "Completed"}
        )
    lines = [f"{i + 1}. {x['what']} | {x['why']}" for i, x in enumerate(items[:5])]
    return _reply(
        "Needs attention (from your records):\n" + "\n".join(lines),
        [("Money Owed", "/money"), ("Inventory", "/inventory")],
        pulse_state="idle",
        pulse_channel="",
        pulse_label="Ask Vyapaar",
        trail_info={
            "records_checked": f"{len(items)} items needing review",
            "finding": f"Identified {min(5, len(items))} priority items",
            "suggested_action": "Review priority records",
            "status": "Completed",
        }
    )


def _answer_overdue(conn, on_event=None):
    if on_event:
        on_event("tool", "Looking up invoices", "ledger")
    rows = tools.overdue_invoices(conn)
    if not rows:
        return _reply(
            "No invoices are overdue.",
            pulse_state="idle",
            pulse_channel="",
            pulse_label="Ask Vyapaar",
            trail_info={"records_checked": "All open invoices", "finding": "0 overdue", "status": "Completed"}
        )
    lines = [f"{r['number']} {r['customer']}: {inr(r['outstanding_minor'])}, {r['days_overdue']} days overdue"
             for r in rows]
    return _reply(
        "Overdue invoices:\n" + "\n".join(lines),
        [("Money Owed", "/money")],
        pulse_state="idle",
        pulse_channel="",
        pulse_label="Ask Vyapaar",
        trail_info={
            "records_checked": f"{len(rows)} overdue invoices queried",
            "finding": f"{len(rows)} overdue invoices found",
            "suggested_action": "Review Money Owed or draft reminders",
            "status": "Completed",
        }
    )


def _answer_receivables(conn, on_event=None):
    if on_event:
        on_event("tool", "Looking up customer balance", "ledger")
    s, debtors = tools.receivables_summary(conn), tools.top_debtors(conn)
    if not debtors:
        return _reply(
            "No customer has a pending balance.",
            pulse_state="idle",
            pulse_channel="",
            pulse_label="Ask Vyapaar",
            trail_info={"records_checked": "Customer ledger", "finding": "Zero outstanding balance", "status": "Completed"}
        )
    return _reply(
        f"Total pending: {inr(s['outstanding'])}, of which {inr(s['overdue'])} is overdue. "
        f"{debtors[0]['name']} owes the most: {inr(debtors[0]['owed'])}.",
        [("Money Owed", "/money")],
        pulse_state="idle",
        pulse_channel="",
        pulse_label="Ask Vyapaar",
        trail_info={
            "records_checked": f"{len(debtors)} customers with balances",
            "finding": f"Top debtor: {debtors[0]['name']} ({inr(debtors[0]['owed'])})",
            "suggested_action": "Review outstanding invoices",
            "status": "Completed",
        }
    )


def _answer_low_stock(conn, on_event=None):
    if on_event:
        on_event("tool", "Checking inventory", "stock")
    low = tools.low_stock(conn)
    if not low:
        return _reply(
            "No product is below its minimum stock.",
            pulse_state="idle",
            pulse_channel="",
            pulse_label="Ask Vyapaar",
            trail_info={"records_checked": "Inventory records", "finding": "All stock above minimum", "status": "Completed"}
        )
    lines = [f"{p['name']}: {p['stock']} left (minimum {p['min_stock']})" for p in low]
    return _reply(
        "Needs restocking:\n" + "\n".join(lines),
        [("Inventory", "/inventory")],
        pulse_state="idle",
        pulse_channel="",
        pulse_label="Ask Vyapaar",
        trail_info={
            "records_checked": f"{len(low)} low stock items found",
            "finding": f"{len(low)} product(s) below threshold",
            "suggested_action": "Check supplier and restock",
            "status": "Completed",
        }
    )


def _answer_sales(conn, on_event=None):
    if on_event:
        on_event("tool", "Recording a sale", "sales")
    s = tools.sales_this_month(conn)
    if s["count"] == 0:
        return _reply(
            "No sales have been recorded in Vyapaar this month yet.",
            pulse_state="idle",
            pulse_channel="",
            pulse_label="Ask Vyapaar",
            trail_info={"records_checked": "Monthly sales ledger", "finding": "0 sales this month", "status": "Completed"}
        )
    return _reply(
        f"Sales recorded this month: {inr(s['total'])} across {s['count']} sale(s).",
        pulse_state="idle",
        pulse_channel="",
        pulse_label="Ask Vyapaar",
        trail_info={
            "records_checked": f"{s['count']} sales recorded",
            "finding": f"Total volume {inr(s['total'])}",
            "status": "Completed",
        }
    )


def answer(conn, question, on_event=None):
    t = question.lower().strip()
    if not t:
        return _reply("Please type a question.", pulse_state="idle", pulse_label="Ask Vyapaar")

    if on_event:
        on_event("request", "Understanding your request…", "")

    # Fast path for direct sale syntax
    m = re.search(r"sale of (\d+)\s*(?:units?|pcs|pieces)?\s*(?:of\s+)?(.+?)(?:\s+to\s+(.+))?$", t)
    if m:
        if on_event:
            on_event("tool", "Recording a sale", "sales")
        return _sale(conn, int(m.group(1)), m.group(2).strip(), (m.group(3) or "").strip(), on_event=on_event)

    # Fast path for direct invoice reminder syntax
    m = re.search(r"inv-\d+", t)
    if m and "remind" in t:
        if on_event:
            on_event("tool", "Looking up invoices", "ledger")
        inv_no = m.group(0).upper()
        aid, note = actions.draft_reminder(conn, inv_no)
        if aid is None:
            return _reply(
                note,
                pulse_state="idle",
                pulse_label="Ask Vyapaar",
                trail_info={"finding": note, "status": "No action needed"}
            )
        if on_event:
            on_event("approval", "Your approval is needed", "")
        return _reply(
            f"{note} Review and edit it in Approvals. Nothing is sent until you approve.",
            [("Approvals", "/approvals")],
            aid,
            pulse_state="approval",
            pulse_channel="",
            pulse_label="Your approval is needed",
            trail_info={
                "records_checked": f"Invoice {inv_no}",
                "finding": "Overdue balance confirmed",
                "suggested_action": f"Draft reminder for {inv_no}",
                "status": "Awaiting your decision",
            }
        )

    # Exact canonical shortcuts (for instant responses and unit test execution)
    if t == "overdue invoices":
        return _answer_overdue(conn, on_event)
    elif t == "what needs restocking":
        return _answer_low_stock(conn, on_event)

    # LLM Reasoning: Groq (cloud/Vercel) or Ollama (local) with live database tool execution
    if llm.is_llm_available():
        llm_reply = llm.run_llm_agent(conn, question, on_event=on_event)
        if llm_reply and llm_reply.get("text"):
            return llm_reply

    # Deterministic fallback rules (if Ollama model is offline, unavailable, or times out)
    if any(w in t for w in ("follow", "focus", "today", "briefing")):
        return _answer_briefing(conn, on_event)

    if "overdue" in t:
        return _answer_overdue(conn, on_event)

    if "owe" in t or "outstanding" in t:
        return _answer_receivables(conn, on_event)

    if "stock" in t or "restock" in t or "reorder" in t:
        return _answer_low_stock(conn, on_event)

    if "sales" in t:
        return _answer_sales(conn, on_event)

    return _reply(
        "I can't answer that yet. Try: who owes the most, overdue invoices, what needs restocking, "
        "what to focus on today, sales this month, 'remind INV-0001', or "
        "'record a sale of 5 units of sugar to Anita Kirana'.",
        pulse_state="idle",
        pulse_channel="",
        pulse_label="Ask Vyapaar",
        trail_info={"records_checked": "Keyword parser", "finding": "Unrecognized request", "status": "Completed"}
    )


def _sale(conn, qty, pname, cname, on_event=None):
    prods = tools.find_products(conn, pname)
    if not prods:
        return _reply(f"I could not find a product matching '{pname}'.", pulse_state="idle", pulse_label="Ask Vyapaar")
    if len(prods) > 1:
        return _reply("Which product did you mean? " + ", ".join(p["name"] for p in prods), pulse_state="idle", pulse_label="Ask Vyapaar")
    p, cust = prods[0], None
    if cname:
        cs = tools.find_customers(conn, cname)
        if not cs:
            return _reply(f"I could not find a customer matching '{cname}'.", pulse_state="idle", pulse_label="Ask Vyapaar")
        if len(cs) > 1:
            return _reply("Which customer? " + ", ".join(c["name"] for c in cs), pulse_state="idle", pulse_label="Ask Vyapaar")
        cust = cs[0]
    if qty <= 0 or p["stock"] < qty:
        return _reply(f"Only {p['stock']} of {p['name']} in stock, so I can't prepare this sale.", pulse_state="idle", pulse_label="Ask Vyapaar")
    total = p["price_minor"] * qty
    payload = {
        "customer_id": cust["id"] if cust else None,
        "customer_name": cust["name"] if cust else "Walk-in",
        "product_id": p["id"],
        "product_name": p["name"],
        "qty": qty,
        "total_minor": total,
        "paid_minor": 0 if cust else total,
    }
    aid = actions.propose(conn, "record_sale", payload, "Requested in Assistant chat", "agent")
    how = "on credit (pay later)" if cust else "paid in full (walk-in)"
    if on_event:
        on_event("approval", "Your approval is needed", "")
    return _reply(
        f"Preview: sell {qty} x {p['name']} to {payload['customer_name']} for {inr(total)}, {how}. "
        "Nothing is saved until you approve it in Approvals.",
        [("Approvals", "/approvals")],
        aid,
        pulse_state="approval",
        pulse_channel="",
        pulse_label="Your approval is needed",
        trail_info={
            "records_checked": f"Product: {p['name']}, Available: {p['stock']}",
            "finding": f"Sale preview ready: {inr(total)}",
            "suggested_action": f"Approve sale of {qty} x {p['name']}",
            "status": "Awaiting your decision",
        }
    )