"""llm.py – LLM integration for Vyapaar.

Backend priority:
  1. Groq  (cloud, fast, free tier)  — used when GROQ_API_KEY env var is set.
  2. Ollama (local)                   — used when Ollama daemon is running locally.
  3. None                             — agent.py falls back to deterministic rules.
"""
import json
import logging
import os
import re
import urllib.request
from typing import Any, Dict, List, Optional

import actions
import tools
from domain import inr

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")

SYSTEM_PROMPT = (
    "You are Vyapaar, an AI business assistant for Indian retail and wholesale business owners. "
    "You have access to tools that query the store's live database. "
    "Always use the appropriate tool to check current records rather than guessing. "
    "Be concise, polite, and practical. Use Indian English (₹/INR, Lakhs/Thousands). "
    "Never send messages or finalise financial records without explicit user approval. "
    "Explain findings clearly with actionable recommendations."
)

# ---------------------------------------------------------------------------
# Groq / OpenAI-compatible JSON tool schemas
# ---------------------------------------------------------------------------
TOOL_SCHEMAS: List[Dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_overdue_invoices",
            "description": "Retrieve all customer invoices that are overdue and have an outstanding balance.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_receivables_summary",
            "description": "Get overall summary of business receivables: total pending balance and overdue amounts.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_top_debtors",
            "description": "List customers who owe the most money to the business, ordered by outstanding amount.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_low_stock",
            "description": "Check inventory items that have fallen below their minimum stock threshold.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_products",
            "description": "Search products in inventory by name or SKU to check stock and price.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Product name or SKU to search for"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_customers",
            "description": "Search customers by name to view their contact info.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Customer name to search for"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_sales_this_month",
            "description": "Get total sales revenue and number of transactions recorded this month.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_business_briefing",
            "description": "Get a prioritised daily briefing of items needing attention: overdue payments, low stock, pending approvals.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "draft_invoice_reminder",
            "description": "Draft a polite payment reminder for an overdue invoice (e.g. INV-0001). Requires user approval before anything is sent.",
            "parameters": {
                "type": "object",
                "properties": {
                    "invoice_number": {"type": "string", "description": "Invoice number like INV-0001"},
                },
                "required": ["invoice_number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_sale",
            "description": "Propose a sale transaction for user approval. Nothing is finalised without approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_name": {"type": "string", "description": "Name of product to sell"},
                    "quantity": {"type": "integer", "description": "Number of units to sell"},
                    "customer_name": {"type": "string", "description": "Customer name (leave empty for walk-in)"},
                },
                "required": ["product_name", "quantity"],
            },
        },
    },
]

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def get_ollama_client():
    try:
        import ollama
        return ollama.Client(host=OLLAMA_HOST)
    except Exception:
        return None


def is_model_available(timeout: float = 1.5) -> bool:
    """Returns True if the configured Ollama model is reachable."""
    try:
        req = urllib.request.Request(
            f"{OLLAMA_HOST.rstrip('/')}/api/tags",
            headers={"User-Agent": "Vyapaar/1.0"},
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


def is_llm_available() -> bool:
    """Returns True if either Groq (cloud) or Ollama (local) is reachable."""
    return bool(GROQ_API_KEY) or is_model_available()


def call_model(prompt: str, system: Optional[str] = None, timeout: float = 10.0) -> Optional[str]:
    """Simple one-shot completion. Uses Groq if available, else Ollama. Returns None if both are offline."""
    if GROQ_API_KEY:
        try:
            from groq import Groq
            client = Groq(api_key=GROQ_API_KEY)
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})
            resp = client.chat.completions.create(model=GROQ_MODEL, messages=messages)
            return resp.choices[0].message.content
        except Exception as e:
            logger.warning("Groq call_model error: %s", e)

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
        logger.warning("Ollama call_model error: %s", e)
        return None


# ---------------------------------------------------------------------------
# Tool implementations (shared — bound to `conn` at call time)
# ---------------------------------------------------------------------------

def _fresh_metadata() -> Dict:
    return {
        "channel": "",
        "pulse_state": "idle",
        "pulse_label": "Ask Vyapaar",
        "based_on": [],
        "proposal": None,
        "records_checked": [],
        "suggested_action": "",
    }


def _build_tool_implementations(conn, metadata: Dict):
    """Return (tool_list_for_ollama, tool_map_by_name).
    Each function mutates `metadata` in-place as side-effects.
    """

    def _link(label, url):
        if (label, url) not in metadata["based_on"]:
            metadata["based_on"].append((label, url))

    def get_overdue_invoices() -> str:
        """Retrieve all customer invoices that are overdue and have an outstanding balance."""
        metadata["channel"] = "ledger"
        _link("Money Owed", "/money")
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
                "factors": r["factors"],
            }
            for r in rows
        ])

    def get_receivables_summary() -> str:
        """Get overall summary of business receivables."""
        metadata["channel"] = "ledger"
        _link("Money Owed", "/money")
        s = tools.receivables_summary(conn)
        debtors = tools.top_debtors(conn)
        metadata["records_checked"].append("Customer balance ledger")
        return json.dumps({
            "total_receivables": inr(s["outstanding"]),
            "total_overdue": inr(s["overdue"]),
            "open_invoice_count": s["count"],
            "top_debtors": [{"customer": d["name"], "owed": inr(d["owed"])} for d in debtors[:5]],
        })

    def get_top_debtors() -> str:
        """List customers who owe the most money to the business."""
        metadata["channel"] = "ledger"
        _link("Money Owed", "/money")
        debtors = tools.top_debtors(conn)
        metadata["records_checked"].append(f"{len(debtors)} customers with balances")
        if not debtors:
            return json.dumps({"message": "No customers have an outstanding balance."})
        return json.dumps([{"customer": d["name"], "owed": inr(d["owed"])} for d in debtors])

    def check_low_stock() -> str:
        """Check inventory items below minimum stock threshold."""
        metadata["channel"] = "stock"
        _link("Inventory", "/inventory")
        low = tools.low_stock(conn)
        metadata["records_checked"].append(f"{len(low)} low-stock items")
        if not low:
            return json.dumps({"message": "All inventory items are above their minimum stock."})
        return json.dumps([
            {"product": p["name"], "sku": p["sku"], "stock": p["stock"],
             "min_stock": p["min_stock"], "price": inr(p["price_minor"])}
            for p in low
        ])

    def search_products(query: str) -> str:
        """Search products in inventory by name or SKU."""
        metadata["channel"] = "stock"
        _link("Inventory", "/inventory")
        prods = tools.find_products(conn, query)
        metadata["records_checked"].append(f"Product search '{query}'")
        if not prods:
            return json.dumps({"message": f"No products found matching '{query}'."})
        return json.dumps([
            {"name": p["name"], "sku": p["sku"], "stock": p["stock"], "price": inr(p["price_minor"])}
            for p in prods[:10]
        ])

    def search_customers(query: str) -> str:
        """Search customers by name."""
        metadata["channel"] = "ledger"
        _link("Money Owed", "/money")
        custs = tools.find_customers(conn, query)
        metadata["records_checked"].append(f"Customer search '{query}'")
        if not custs:
            return json.dumps({"message": f"No customers found matching '{query}'."})
        return json.dumps([{"id": c["id"], "name": c["name"], "phone": c["phone"]} for c in custs[:10]])

    def get_sales_this_month() -> str:
        """Get total sales revenue and number of transactions recorded this month."""
        metadata["channel"] = "sales"
        _link("Record Sale", "/sale")
        s = tools.sales_this_month(conn)
        metadata["records_checked"].append("Monthly sales records")
        return json.dumps({"total_sales": inr(s["total"]), "transaction_count": s["count"]})

    def get_business_briefing() -> str:
        """Get a prioritised daily briefing of items needing attention."""
        metadata["channel"] = "briefing"
        _link("Money Owed", "/money")
        _link("Inventory", "/inventory")
        items = tools.briefing(conn)
        metadata["records_checked"].append(f"{len(items)} briefing items")
        if not items:
            return json.dumps({"message": "Nothing urgent needs attention today."})
        return json.dumps(items)

    def draft_invoice_reminder(invoice_number: str) -> str:
        """Draft a polite payment reminder for an overdue invoice. Requires user approval."""
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
            _link("Approvals", "/approvals")
            metadata["suggested_action"] = f"Review drafted reminder for {inv_no} in Approvals"
            return json.dumps({
                "status": "drafted",
                "action_id": aid,
                "message": f"Reminder drafted for {inv_no}. Waiting in Approvals — nothing is sent until you approve.",
            })
        return json.dumps({"status": "not_drafted", "message": note})

    def propose_sale(product_name: str, quantity: int, customer_name: str = "") -> str:
        """Propose a sale transaction for user approval."""
        metadata["channel"] = "sales"
        prods = tools.find_products(conn, product_name)
        if not prods:
            return json.dumps({"status": "error", "message": f"No product matching '{product_name}'."})
        if len(prods) > 1:
            return json.dumps({"status": "ambiguous", "message": "Multiple products match: " + ", ".join(p["name"] for p in prods)})
        p = prods[0]
        cust = None
        if customer_name:
            custs = tools.find_customers(conn, customer_name)
            if not custs:
                return json.dumps({"status": "error", "message": f"No customer matching '{customer_name}'."})
            if len(custs) > 1:
                return json.dumps({"status": "ambiguous", "message": "Multiple customers: " + ", ".join(c["name"] for c in custs)})
            cust = custs[0]
        if quantity <= 0:
            return json.dumps({"status": "error", "message": "Quantity must be greater than 0."})
        if p["stock"] < quantity:
            return json.dumps({"status": "insufficient_stock", "message": f"Only {p['stock']} units of {p['name']} in stock."})
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
        aid = actions.propose(conn, "record_sale", payload, "Requested via LLM agent", "agent")
        metadata["proposal"] = aid
        metadata["pulse_state"] = "approval"
        metadata["pulse_label"] = "Your approval is needed"
        _link("Approvals", "/approvals")
        metadata["suggested_action"] = f"Approve sale of {quantity} x {p['name']} ({inr(total)})"
        return json.dumps({"status": "proposed", "action_id": aid,
                           "message": f"Sale preview ready: {quantity} x {p['name']} for {inr(total)}. Awaiting your approval."})

    tool_list = [
        get_overdue_invoices, get_receivables_summary, get_top_debtors,
        check_low_stock, search_products, search_customers,
        get_sales_this_month, get_business_briefing,
        draft_invoice_reminder, propose_sale,
    ]
    tool_map = {fn.__name__: fn for fn in tool_list}
    return tool_list, tool_map


def _make_reply(metadata: Dict, text: str) -> Dict:
    records_str = ", ".join(metadata["records_checked"]) if metadata["records_checked"] else "Business records"
    return {
        "text": text,
        "based_on": metadata["based_on"],
        "proposal": metadata["proposal"],
        "pulse_state": metadata["pulse_state"],
        "pulse_channel": metadata["channel"],
        "pulse_label": metadata["pulse_label"],
        "trail_info": {
            "records_checked": records_str,
            "finding": text[:120].strip() + ("…" if len(text) > 120 else ""),
            "suggested_action": metadata["suggested_action"] or "Review the relevant business page",
            "status": "Awaiting your decision" if metadata["proposal"] else "Completed",
        },
    }


# ---------------------------------------------------------------------------
# Groq agent  (cloud — works on Vercel)
# ---------------------------------------------------------------------------

def _run_groq_agent(conn, question: str, on_event, api_key: str) -> Optional[Dict]:
    try:
        from groq import Groq
    except ImportError:
        logger.warning("groq package not installed; run: pip install groq")
        return None

    metadata = _fresh_metadata()
    _, tool_map = _build_tool_implementations(conn, metadata)

    try:
        if on_event:
            on_event("thinking", "Analyzing your request…", "")

        client = Groq(api_key=api_key)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ]

        resp = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            tools=TOOL_SCHEMAS,
            tool_choice="auto",
        )
        msg = resp.choices[0].message

        if msg.tool_calls:
            # Append the assistant turn with tool calls
            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in msg.tool_calls
                ],
            })
            for tc in msg.tool_calls:
                fn_name = tc.function.name
                fn_args = json.loads(tc.function.arguments or "{}")
                if fn_name in tool_map:
                    if on_event:
                        on_event("tool", fn_name.replace("_", " ").title(), metadata.get("channel", ""))
                    output = tool_map[fn_name](**fn_args)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": str(output),
                    })

            if on_event:
                on_event("thinking", "Synthesizing response…", metadata.get("channel", ""))
            final = client.chat.completions.create(model=GROQ_MODEL, messages=messages)
            reply_text = final.choices[0].message.content or ""
        else:
            reply_text = msg.content or ""

        if on_event:
            on_event("approval" if metadata["proposal"] else "idle",
                     metadata["pulse_label"], metadata.get("channel", ""))
        return _make_reply(metadata, reply_text)

    except Exception as e:
        logger.error("Groq agent error: %s", e)
        return None


# ---------------------------------------------------------------------------
# Ollama agent  (local — works in development)
# ---------------------------------------------------------------------------

def _run_ollama_agent(conn, question: str, on_event) -> Optional[Dict]:
    client = get_ollama_client()
    if not client or not is_model_available():
        return None

    metadata = _fresh_metadata()
    tool_list, tool_map = _build_tool_implementations(conn, metadata)

    try:
        if on_event:
            on_event("thinking", "Analyzing your request…", "")

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ]
        resp = client.chat(model=OLLAMA_MODEL, messages=messages, tools=tool_list)

        if resp.message.tool_calls:
            messages.append(resp.message)
            for tc in resp.message.tool_calls:
                fn_name = tc.function.name
                fn_args = tc.function.arguments or {}
                if fn_name in tool_map:
                    if on_event:
                        on_event("tool", fn_name.replace("_", " ").title(), metadata.get("channel", ""))
                    output = tool_map[fn_name](**fn_args)
                    messages.append({"role": "tool", "name": fn_name, "content": str(output)})

            if on_event:
                on_event("thinking", "Synthesizing response…", metadata.get("channel", ""))
            final = client.chat(model=OLLAMA_MODEL, messages=messages)
            reply_text = final.message.content or ""
        else:
            reply_text = resp.message.content or ""

        if on_event:
            on_event("approval" if metadata["proposal"] else "idle",
                     metadata["pulse_label"], metadata.get("channel", ""))
        return _make_reply(metadata, reply_text)

    except Exception as e:
        logger.error("Ollama agent error: %s", e)
        return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_llm_agent(conn, question: str, on_event=None) -> Optional[Dict]:
    """Run the LLM agent.
    Tries Groq first (works on Vercel), then Ollama (local dev). Returns None if both unavailable.
    """
    if GROQ_API_KEY:
        result = _run_groq_agent(conn, question, on_event, GROQ_API_KEY)
        if result:
            return result

    if is_model_available():
        return _run_ollama_agent(conn, question, on_event)

    return None
