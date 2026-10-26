import json
import logging
import os
import re
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

import actions
import tools
from domain import inr

logger = logging.getLogger(__name__)

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")

SYSTEM_PROMPT = """You are Vyapaar, an AI business assistant for Indian retail and wholesale business owners.
You have access to tools that query the store's real database.
Always use the tools to check current records rather than guessing.
Be concise, polite, and practical. Use Indian English (₹/INR, Lakhs/Thousands, units).
Never confirm or finalize consequential actions (like sending messages or updating permanent financial records) without user approval.
Explain findings clearly with actionable recommendations.
"""


def get_ollama_client():
    try:
        import ollama
        return ollama.Client(host=OLLAMA_HOST)
    except Exception:
        return None


def is_model_available(timeout: float = 1.5) -> bool:
    try:
        req = urllib.request.Request(
            f"{OLLAMA_HOST.rstrip('/')}/api/tags",
            headers={"User-Agent": "Vyapaar/1.0"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as res:
            if res.status != 200:
                return False
            data = json.loads(res.read().decode())
            models = [m.get("name") or m.get("model") for m in data.get("models", [])]
            return any(
                m == OLLAMA_MODEL or m.split(":")[0] == OLLAMA_MODEL.split(":")[0]
                for m in models
                if m
            )
    except Exception:
        return False


def call_model(prompt: str, system: Optional[str] = None, timeout: float = 10.0) -> Optional[str]:
    """Calls Ollama model if available. Returns None if unreachable or timed out."""
    client = get_ollama_client()
    if not client or not is_model_available():
        return None
    try:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        resp = client.chat(model=OLLAMA_MODEL, messages=messages)
        return resp["message"]["content"]
    except Exception as e:
        logger.warning("call_model error: %s", e)
        return None


def run_llm_agent(conn, question: str, on_event: Optional[Callable] = None) -> Optional[Dict[str, Any]]:
    """Runs the Ollama LLM with tool calling against the current database connection.
    Returns structured reply dict or None if LLM is unavailable or fails.
    """
    client = get_ollama_client()
    if not client or not is_model_available():
        return None

    # Metadata accumulators
    metadata = {
        "channel": "",
        "pulse_state": "idle",
        "pulse_label": "Ask Vyapaar",
        "based_on": [],
        "proposal": None,
        "records_checked": [],
        "finding": "",
        "suggested_action": "",
    }

    # 1. Define tools bound to conn
    def get_overdue_invoices() -> str:
        """Retrieve all customer invoices that are overdue and have an outstanding balance."""
        metadata["channel"] = "ledger"
        if ("Money Owed", "/money") not in metadata["based_on"]:
            metadata["based_on"].append(("Money Owed", "/money"))
        rows = tools.overdue_invoices(conn)
        metadata["records_checked"].append(f"{len(rows)} overdue invoices")
        if not rows:
            return json.dumps({"count": 0, "message": "No invoices are currently overdue."})
        return json.dumps([
            {
                "invoice": r["number"],
                "customer": r["customer"],
                "outstanding": inr(r["outstanding_minor"]),
                "days_overdue": r["days_overdue"],
                "factors": r["factors"]
            }
            for r in rows
        ])

    def get_receivables_summary() -> str:
        """Get an overall summary of business receivables, total pending balance, and overdue amounts."""
        metadata["channel"] = "ledger"
        if ("Money Owed", "/money") not in metadata["based_on"]:
            metadata["based_on"].append(("Money Owed", "/money"))
        s = tools.receivables_summary(conn)
        debtors = tools.top_debtors(conn)
        metadata["records_checked"].append("Customer balance ledger")
        return json.dumps({
            "total_receivables": inr(s["outstanding"]),
            "total_overdue": inr(s["overdue"]),
            "open_invoice_count": s["count"],
            "top_debtors": [{"customer": d["name"], "owed": inr(d["owed"])} for d in debtors[:5]]
        })

    def get_top_debtors() -> str:
        """List customers who owe the most money to the business, ordered by outstanding amount."""
        metadata["channel"] = "ledger"
        if ("Money Owed", "/money") not in metadata["based_on"]:
            metadata["based_on"].append(("Money Owed", "/money"))
        debtors = tools.top_debtors(conn)
        metadata["records_checked"].append(f"{len(debtors)} customers with balances")
        if not debtors:
            return json.dumps({"message": "No customers have an outstanding balance."})
        return json.dumps([
            {"customer": d["name"], "owed": inr(d["owed"])}
            for d in debtors
        ])

    def check_low_stock() -> str:
        """Check inventory items that have fallen below their minimum stock threshold."""
        metadata["channel"] = "stock"
        if ("Inventory", "/inventory") not in metadata["based_on"]:
            metadata["based_on"].append(("Inventory", "/inventory"))
        low = tools.low_stock(conn)
        metadata["records_checked"].append(f"{len(low)} low-stock items")
        if not low:
            return json.dumps({"message": "All inventory items are above their minimum stock."})
        return json.dumps([
            {
                "product": p["name"],
                "sku": p["sku"],
                "stock": p["stock"],
                "min_stock": p["min_stock"],
                "price": inr(p["price_minor"])
            }
            for p in low
        ])

    def search_products(query: str) -> str:
        """Search products in inventory by name or SKU to check stock and price."""
        metadata["channel"] = "stock"
        if ("Inventory", "/inventory") not in metadata["based_on"]:
            metadata["based_on"].append(("Inventory", "/inventory"))
        prods = tools.find_products(conn, query)
        metadata["records_checked"].append(f"Product search '{query}'")
        if not prods:
            return json.dumps({"message": f"No products found matching '{query}'."})
        return json.dumps([
            {"name": p["name"], "sku": p["sku"], "stock": p["stock"], "price": inr(p["price_minor"])}
            for p in prods[:10]
        ])

    def search_customers(query: str) -> str:
        """Search customers by name to view contact info and details."""
        metadata["channel"] = "ledger"
        if ("Money Owed", "/money") not in metadata["based_on"]:
            metadata["based_on"].append(("Money Owed", "/money"))
        custs = tools.find_customers(conn, query)
        metadata["records_checked"].append(f"Customer search '{query}'")
        if not custs:
            return json.dumps({"message": f"No customers found matching '{query}'."})
        return json.dumps([
            {"id": c["id"], "name": c["name"], "phone": c["phone"]}
            for c in custs[:10]
        ])

    def get_sales_this_month() -> str:
        """Get total sales revenue and number of sales transactions recorded this month."""
        metadata["channel"] = "sales"
        if ("Record Sale", "/sale") not in metadata["based_on"]:
            metadata["based_on"].append(("Record Sale", "/sale"))
        s = tools.sales_this_month(conn)
        metadata["records_checked"].append("Monthly sales records")
        return json.dumps({
            "total_sales": inr(s["total"]),
            "transaction_count": s["count"]
        })

    def get_business_briefing() -> str:
        """Get an automated daily briefing of items needing attention (overdue payments, low stock, pending approvals)."""
        metadata["channel"] = "briefing"
        if ("Money Owed", "/money") not in metadata["based_on"]:
            metadata["based_on"].append(("Money Owed", "/money"))
        if ("Inventory", "/inventory") not in metadata["based_on"]:
            metadata["based_on"].append(("Inventory", "/inventory"))
        items = tools.briefing(conn)
        metadata["records_checked"].append(f"{len(items)} briefing items")
        if not items:
            return json.dumps({"message": "Nothing urgent needs attention today."})
        return json.dumps(items)

    def draft_invoice_reminder(invoice_number: str) -> str:
        """Draft a polite payment reminder for an overdue invoice (e.g. INV-0001). Requires user approval before sending."""
        metadata["channel"] = "ledger"
        inv_no = invoice_number.upper().strip()
        if not inv_no.startswith("INV-") and re.match(r"^\d+$", inv_no):
            inv_no = f"INV-{int(inv_no):04d}"
        aid, note = actions.draft_reminder(conn, inv_no)
        metadata["records_checked"].append(f"Invoice {inv_no}")
        if aid is not None:
            metadata["proposal"] = aid
            metadata["pulse_state"] = "approval"
            metadata["pulse_label"] = "Your approval is needed"
            if ("Approvals", "/approvals") not in metadata["based_on"]:
                metadata["based_on"].insert(0, ("Approvals", "/approvals"))
            metadata["suggested_action"] = f"Review drafted reminder for {inv_no} in Approvals"
            return json.dumps({
                "status": "drafted",
                "action_id": aid,
                "message": f"Reminder drafted for {inv_no}. It is waiting in Approvals; nothing is sent until approved."
            })
        else:
            return json.dumps({"status": "not_drafted", "message": note})

    def propose_sale(product_name: str, quantity: int, customer_name: str = "") -> str:
        """Propose a sale transaction for approval in Approvals panel. Nothing is finalized without user approval."""
        metadata["channel"] = "sales"
        prods = tools.find_products(conn, product_name)
        if not prods:
            return json.dumps({"status": "error", "message": f"Could not find product matching '{product_name}'."})
        if len(prods) > 1:
            names = ", ".join(p["name"] for p in prods)
            return json.dumps({"status": "ambiguous", "message": f"Multiple products match: {names}. Please specify."})
        p = prods[0]
        cust = None
        if customer_name:
            custs = tools.find_customers(conn, customer_name)
            if not custs:
                return json.dumps({"status": "error", "message": f"Could not find customer matching '{customer_name}'."})
            if len(custs) > 1:
                names = ", ".join(c["name"] for c in custs)
                return json.dumps({"status": "ambiguous", "message": f"Multiple customers match: {names}."})
            cust = custs[0]

        if quantity <= 0:
            return json.dumps({"status": "error", "message": "Quantity must be greater than 0."})
        if p["stock"] < quantity:
            return json.dumps({
                "status": "insufficient_stock",
                "message": f"Only {p['stock']} units of {p['name']} in stock. Cannot propose sale of {quantity} units."
            })

        total = p["price_minor"] * quantity
        payload = {
            "customer_id": cust["id"] if cust else None,
            "customer_name": cust["name"] if cust else "Walk-in",
            "product_id": p["id"],
            "product_name": p["name"],
            "qty": quantity,
            "total_minor": total,
            "paid_minor": 0 if cust else total,
        }
        aid = actions.propose(conn, "record_sale", payload, "Requested in Assistant chat via Ollama", "agent")
        metadata["proposal"] = aid
        metadata["pulse_state"] = "approval"
        metadata["pulse_label"] = "Your approval is needed"
        if ("Approvals", "/approvals") not in metadata["based_on"]:
            metadata["based_on"].insert(0, ("Approvals", "/approvals"))
        metadata["suggested_action"] = f"Approve sale of {quantity} x {p['name']} ({inr(total)})"
        return json.dumps({
            "status": "proposed",
            "action_id": aid,
            "message": f"Sale preview ready: {quantity} x {p['name']} for {inr(total)}. Pending your decision in Approvals."
        })

    tool_list = [
        get_overdue_invoices,
        get_receivables_summary,
        get_top_debtors,
        check_low_stock,
        search_products,
        search_customers,
        get_sales_this_month,
        get_business_briefing,
        draft_invoice_reminder,
        propose_sale,
    ]
    tool_map = {fn.__name__: fn for fn in tool_list}

    try:
        if on_event:
            on_event("thinking", "Analyzing your request…", "")

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": question}
        ]

        # Initial chat to trigger tool selection
        resp = client.chat(model=OLLAMA_MODEL, messages=messages, tools=tool_list)

        # Handle tool calls
        if resp.message.tool_calls:
            messages.append(resp.message)
            for tc in resp.message.tool_calls:
                fn_name = tc.function.name
                fn_args = tc.function.arguments or {}
                if fn_name in tool_map:
                    if on_event:
                        on_event("tool", f"Consulting {fn_name.replace('_', ' ')}", metadata.get("channel", ""))
                    tool_output = tool_map[fn_name](**fn_args)
                    messages.append({
                        "role": "tool",
                        "name": fn_name,
                        "content": str(tool_output)
                    })

            if on_event:
                on_event("thinking", "Synthesizing business response…", metadata.get("channel", ""))

            # Final synthesis chat
            final_resp = client.chat(model=OLLAMA_MODEL, messages=messages)
            reply_text = final_resp.message.content or ""
        else:
            reply_text = resp.message.content or ""

        # Prepare trail info
        records_str = ", ".join(metadata["records_checked"]) if metadata["records_checked"] else "Business records"
        status_str = "Awaiting your decision" if metadata["proposal"] else "Completed"
        trail_info = {
            "records_checked": records_str,
            "finding": reply_text[:120].strip() + ("…" if len(reply_text) > 120 else ""),
            "suggested_action": metadata["suggested_action"] or "Review relevant business page",
            "status": status_str,
        }

        if on_event:
            if metadata["proposal"]:
                on_event("approval", "Your approval is needed", metadata.get("channel", ""))
            else:
                on_event("idle", "Ask Vyapaar", "")

        return {
            "text": reply_text,
            "based_on": metadata["based_on"],
            "proposal": metadata["proposal"],
            "pulse_state": metadata["pulse_state"],
            "pulse_channel": metadata["channel"],
            "pulse_label": metadata["pulse_label"],
            "trail_info": trail_info,
        }
    except Exception as e:
        logger.error("Error in run_llm_agent: %s", e)
        return None
