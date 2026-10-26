import contextlib
import html
import json
import os
import pathlib
import urllib.request
from typing import Optional

STATES = {"idle", "request", "thinking", "tool", "approval", "done", "error", "unavailable"}
_SVG = pathlib.Path(__file__).with_name("pulse-svg.html").read_text(encoding="utf-8")
_CSS = pathlib.Path(__file__).with_name("pulse.css").read_text(encoding="utf-8")

TOOL_CHANNEL = {
    # Project tools
    "get_overdue_invoices": ("ledger", "Looking up invoices"),
    "overdue_invoices": ("ledger", "Looking up invoices"),
    "get_customer_balance": ("ledger", "Looking up customer balance"),
    "top_debtors": ("ledger", "Looking up top debtors"),
    "receivables_summary": ("ledger", "Summarizing receivables"),
    "check_stock": ("stock", "Checking inventory"),
    "low_stock": ("stock", "Checking low stock"),
    "find_products": ("stock", "Finding products"),
    "find_customers": ("ledger", "Finding customers"),
    "record_sale": ("sales", "Recording a sale"),
    "sales_this_month": ("sales", "Calculating monthly sales"),
    "daily_briefing": ("briefing", "Building your daily briefing"),
    "briefing": ("briefing", "Building your daily briefing"),
}

DEFAULT_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")
DEFAULT_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")


def reduce_state(current: str, event: str, *, model_status: str = "ready") -> str:
    """Pure function. Events: message_submitted, model_started, model_finished, tool_started, tool_finished,
    approval_requested, approval_resolved, action_verified, failure, model_status_changed, reset."""
    if model_status == "unavailable" and event not in ("model_status_changed", "reset"):
        return "unavailable"
    table = {
        "message_submitted": "request",
        "model_started": "thinking",
        "model_finished": "idle",
        "tool_started": "tool",
        "tool_finished": "thinking",
        "approval_requested": "approval",
        "approval_resolved": "idle",
        "action_verified": "done",
        "failure": "error",
        "reset": "idle",
    }
    if event == "model_status_changed":
        return "unavailable" if model_status == "unavailable" else "idle"
    return table.get(event, current)


def render_pulse(state: str, label: str = "Ask Vyapaar", channel: str = "", size: int = 56) -> str:
    state = state if state in STATES else "idle"
    channel = channel if channel in ("ledger", "stock", "sales", "briefing") else ""
    return (
        _SVG.replace('data-state="idle"', f'data-state="{state}"')
        .replace('data-channel=""', f'data-channel="{channel}"')
        .replace("--vy-size:56px", f"--vy-size:{int(size)}px")
        .replace("<b>Ask Vyapaar</b>", f"<b>{html.escape(label)}</b>")
    )


def get_pulse_css() -> str:
    return _CSS


@contextlib.contextmanager
def pulse_activity(slot, kind, label, channel=""):
    """Wrap a REAL operation. The animation exists exactly as long as the `with` block runs."""
    slot.markdown(render_pulse(kind, label, channel), unsafe_allow_html=True)
    try:
        yield
    except Exception:
        slot.markdown(render_pulse("error", "Something went wrong. Please try again."), unsafe_allow_html=True)
        raise
    else:
        slot.markdown(render_pulse("idle", "Ask Vyapaar"), unsafe_allow_html=True)


def get_status(db_conn=None, ollama_host: Optional[str] = None, model_name: Optional[str] = None) -> str:
    """The Truth Contract (Section 3).
    - ready: DB opens AND GET {OLLAMA_HOST}/api/tags returned 200 within 1.5s AND model is listed.
    - connecting: check in progress / loading.
    - unavailable: check failed or timed out, model not pulled, or DB cannot be opened.
    """
    host = ollama_host or DEFAULT_HOST
    target_model = model_name or DEFAULT_MODEL

    # Check DB
    if db_conn is not None:
        try:
            if hasattr(db_conn, "execute"):
                c = db_conn
            elif callable(db_conn):
                c = db_conn()
            else:
                c = db_conn
            c.execute("SELECT 1").fetchone()
        except Exception:
            return "unavailable"

    # Check Ollama
    req_url = f"{host.rstrip('/')}/api/tags"
    try:
        req = urllib.request.Request(req_url, headers={"User-Agent": "VyapaarPulse/1.0"})
        with urllib.request.urlopen(req, timeout=1.5) as response:
            if response.status != 200:
                return "unavailable"
            data = json.loads(response.read().decode("utf-8"))
            models = [m.get("name") or m.get("model") for m in data.get("models", [])]
            # Match exact or with :latest
            matched = any(
                m == target_model or m.split(":")[0] == target_model.split(":")[0]
                for m in models
                if m
            )
            if matched:
                return "ready"
            return "unavailable"
    except Exception:
        return "unavailable"
