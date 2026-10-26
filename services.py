"""services.py - Vyapaar business services.

Every function takes a Ctx (business_id/user_id/role come from the session,
never from the client). Money is integer paise. All writes run in one
transaction and write an audit event in the same transaction.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

import db

# --------------------------------------------------------------------------
# Errors, context, permissions
# --------------------------------------------------------------------------


class ServiceError(Exception):
    def __init__(self, message: str, code: str = "bad_request", status: int = 400):
        super().__init__(message)
        self.message, self.code, self.status = message, code, status


class PermissionDenied(ServiceError):
    def __init__(self, message="You don't have permission to do that."):
        super().__init__(message, "forbidden", 403)


class NotFound(ServiceError):
    def __init__(self, message="Record not found."):
        super().__init__(message, "not_found", 404)


@dataclass(frozen=True)
class Ctx:
    business_id: int
    user_id: int
    role: str  # owner | manager | staff


PERMISSIONS = {
    "owner": {"*"},
    "manager": {
        "read", "sale.create", "payment.record", "stock.adjust", "reminder.draft",
        "reminder.send", "promise.manage", "po.propose", "approve.basic",
    },
    "staff": {"read", "sale.create", "reminder.draft", "promise.manage"},
}


def has_perm(ctx: Ctx, perm: str) -> bool:
    allowed = PERMISSIONS.get(ctx.role, set())
    return "*" in allowed or perm in allowed or perm == "read"


def require(ctx: Ctx, perm: str) -> None:
    if not has_perm(ctx, perm):
        raise PermissionDenied()


# --------------------------------------------------------------------------
# Small helpers: money, dates, transactions, audit
# --------------------------------------------------------------------------


def to_minor(rupees) -> int:
    """'1234.50' -> 123450 (half-up)."""
    return int((Decimal(str(rupees)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def fmt_inr(minor: int) -> str:
    sign = "-" if minor < 0 else ""
    rupees, paise = divmod(abs(int(minor)), 100)
    s = str(rupees)
    if len(s) > 3:
        head, tail, parts = s[:-3], s[-3:], []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    return f"{sign}\u20b9{s}.{paise:02d}"


def _tax(amount_minor: int, tax_bp: int) -> int:
    """Tax in basis points, rounded half-up on paise (the one rounding rule)."""
    return int((Decimal(amount_minor) * tax_bp / 10000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_date(value: str, label: str = "Date") -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ServiceError(f"{label} must look like 2026-10-26.", "validation")


def _today(settings: dict) -> date:
    tz = ZoneInfo(settings.get("timezone") or "Asia/Kolkata")
    return datetime.now(tz).date()


@contextmanager
def tx():
    """Write transaction. BEGIN IMMEDIATE serialises writers, so the
    idempotency/duplicate checks inside are race-safe on SQLite."""
    conn = db.get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def read():
    conn = db.get_conn()
    try:
        yield conn
    finally:
        conn.close()


def _settings(conn, business_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM business_settings WHERE business_id=?", (business_id,)
    ).fetchone()
    if row is None:
        raise NotFound("Business settings not found.")
    return dict(row)


_OWNED_TABLES = {"customer", "product", "invoice", "payment", "supplier"}


def _get_owned(conn, table: str, record_id, business_id: int) -> dict:
    """Fetch a row ONLY if it belongs to this business (tenant isolation)."""
    if table not in _OWNED_TABLES:
        raise ValueError("bad table")
    row = conn.execute(
        f"SELECT * FROM {table} WHERE id=? AND business_id=?", (record_id, business_id)
    ).fetchone()
    if row is None:
        raise NotFound(f"{table.capitalize()} not found.")
    return dict(row)


def audit(conn, ctx: Ctx, action: str, entity: str, entity_id, before=None, after=None) -> None:
    conn.execute(
        "INSERT INTO audit_event(business_id, actor_user_id, action, entity, entity_id,"
        " before_json, after_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (ctx.business_id, ctx.user_id, action, entity, str(entity_id),
         json.dumps(before, default=str) if before is not None else None,
         json.dumps(after, default=str) if after is not None else None, _now()),
    )


def _idem_get(conn, ctx: Ctx, scope: str, key: str):
    r = conn.execute(
        "SELECT result_json FROM idempotency_key WHERE business_id=? AND scope=? AND key=?",
        (ctx.business_id, scope, key),
    ).fetchone()
    return json.loads(r["result_json"]) if r else None


def _idem_put(conn, ctx: Ctx, scope: str, key: str, result: dict) -> None:
    conn.execute(
        "INSERT INTO idempotency_key(business_id, scope, key, result_json) VALUES (?,?,?,?)",
        (ctx.business_id, scope, key, json.dumps(result, default=str)),
    )


# --------------------------------------------------------------------------
# Invoice balance - ONE definition used everywhere
# --------------------------------------------------------------------------

_INVOICE_BASE = """
SELECT i.id, i.invoice_no, i.customer_id, c.name AS customer_name, c.phone AS customer_phone,
       i.total_minor, i.issue_date, i.due_date, i.status, i.disputed,
       i.total_minor - COALESCE((SELECT SUM(a.amount_minor) FROM payment_allocation a
                                 WHERE a.invoice_id = i.id), 0) AS outstanding_minor,
       (SELECT MAX(r.sent_at) FROM payment_reminder r
         WHERE r.invoice_id = i.id AND r.status IN ('simulated_sent','sent','delivered')) AS last_reminder_at
FROM invoice i LEFT JOIN customer c ON c.id = i.customer_id
WHERE i.business_id = ? AND i.status != 'cancelled'
"""


def _invoice_status(total: int, outstanding: int) -> str:
    if outstanding <= 0:
        return "paid"
    return "partially_paid" if outstanding < total else "unpaid"


def _invoice(conn, business_id: int, invoice_id: int) -> dict:
    row = conn.execute(_INVOICE_BASE + " AND i.id = ?", (business_id, invoice_id)).fetchone()
    if row is None:
        raise NotFound("Invoice not found.")
    return dict(row)


def _open_invoices(conn, business_id: int, customer_id=None) -> list[dict]:
    sql = _INVOICE_BASE
    args: list = [business_id]
    if customer_id is not None:
        sql += " AND i.customer_id = ?"
        args.append(customer_id)
    rows = [dict(r) for r in conn.execute(sql + " ORDER BY i.due_date, i.id", args)]
    return [r for r in rows if r["outstanding_minor"] > 0]


# --------------------------------------------------------------------------
# Promises (status computed in code, never by the model)
# --------------------------------------------------------------------------


def compute_promise_status(stored_status: str, promised_minor: int, received_minor: int,
                           promised_date: str, today: date) -> str:
    if stored_status == "cancelled":
        return "cancelled"
    if received_minor >= promised_minor:
        return "fulfilled"
    if received_minor > 0:
        return "partially_fulfilled"
    return "missed" if today > date.fromisoformat(promised_date) else "upcoming"


def _promise_received(conn, p: dict) -> int:
    start, end = p["created_at"][:10], p["promised_date"]
    if p.get("invoice_id"):
        r = conn.execute(
            "SELECT COALESCE(SUM(a.amount_minor),0) FROM payment_allocation a"
            " JOIN payment py ON py.id = a.payment_id"
            " WHERE a.invoice_id=? AND a.business_id=? AND py.paid_on BETWEEN ? AND ?",
            (p["invoice_id"], p["business_id"], start, end)).fetchone()
    else:
        r = conn.execute(
            "SELECT COALESCE(SUM(amount_minor),0) FROM payment"
            " WHERE customer_id=? AND business_id=? AND paid_on BETWEEN ? AND ?",
            (p["customer_id"], p["business_id"], start, end)).fetchone()
    return int(r[0])


def _promises_with_status(conn, business_id: int, today: date, customer_id=None) -> list[dict]:
    sql = "SELECT * FROM payment_promise WHERE business_id=?"
    args: list = [business_id]
    if customer_id is not None:
        sql += " AND customer_id=?"
        args.append(customer_id)
    out = []
    for row in conn.execute(sql + " ORDER BY promised_date", args):
        p = dict(row)
        received = _promise_received(conn, p)
        p["received_minor"] = received
        p["live_status"] = compute_promise_status(
            p["status"], p["promised_amount_minor"], received, p["promised_date"], today)
        out.append(p)
    return out


def _persist_promise_statuses(conn, business_id: int, today: date, customer_id=None) -> None:
    for p in _promises_with_status(conn, business_id, today, customer_id):
        if p["live_status"] != p["status"]:
            conn.execute("UPDATE payment_promise SET status=? WHERE id=? AND business_id=?",
                         (p["live_status"], p["id"], business_id))


def create_promise(ctx: Ctx, *, customer_id: int, promised_amount_minor: int, promised_date: str,
                   invoice_id=None, follow_up_date=None, notes: str = "") -> dict:
    require(ctx, "promise.manage")
    if promised_amount_minor <= 0:
        raise ServiceError("Promised amount must be more than zero.", "validation")
    _parse_date(promised_date, "Promised date")
    if follow_up_date:
        _parse_date(follow_up_date, "Follow-up date")
    with tx() as conn:
        _get_owned(conn, "customer", customer_id, ctx.business_id)
        if invoice_id is not None:
            inv = _get_owned(conn, "invoice", invoice_id, ctx.business_id)
            if inv["customer_id"] != customer_id:
                raise ServiceError("That invoice belongs to a different customer.", "validation")
        cur = conn.execute(
            "INSERT INTO payment_promise(business_id, customer_id, invoice_id, promised_amount_minor,"
            " promised_date, follow_up_date, status, notes, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (ctx.business_id, customer_id, invoice_id, promised_amount_minor, promised_date,
             follow_up_date or promised_date, "upcoming", notes[:500], _now()))
        audit(conn, ctx, "promise.created", "payment_promise", cur.lastrowid,
              after={"customer_id": customer_id, "amount": promised_amount_minor, "date": promised_date})
        return {"id": cur.lastrowid, "status": "upcoming"}


def cancel_promise(ctx: Ctx, promise_id: int) -> dict:
    require(ctx, "promise.manage")
    with tx() as conn:
        row = conn.execute("SELECT * FROM payment_promise WHERE id=? AND business_id=?",
                           (promise_id, ctx.business_id)).fetchone()
        if row is None:
            raise NotFound("Promise not found.")
        conn.execute("UPDATE payment_promise SET status='cancelled' WHERE id=? AND business_id=?",
                     (promise_id, ctx.business_id))
        audit(conn, ctx, "promise.cancelled", "payment_promise", promise_id,
              before={"status": row["status"]}, after={"status": "cancelled"})
        return {"id": promise_id, "status": "cancelled"}


def list_promises(ctx: Ctx, customer_id=None) -> list[dict]:
    require(ctx, "read")
    with read() as conn:
        st = _settings(conn, ctx.business_id)
        return _promises_with_status(conn, ctx.business_id, _today(st), customer_id)


# --------------------------------------------------------------------------
# Follow-up priority (transparent rules, NOT a prediction of behaviour)
# --------------------------------------------------------------------------


def priority_score(*, days_overdue: int, outstanding_minor: int, promise_status, promise_date,
                   days_since_reminder, cooldown_days: int, on_time_paid_count: int):
    score, reasons = 0, []
    d = round(min(max(days_overdue, 0), 90) / 90 * 40)
    score += d
    reasons.append(f"{days_overdue} days overdue (+{d})")
    amt = (30 if outstanding_minor >= 5_000_000 else 20 if outstanding_minor >= 1_000_000
           else 10 if outstanding_minor >= 200_000 else 5)
    score += amt
    reasons.append(f"{fmt_inr(outstanding_minor)} still owed (+{amt})")
    if promise_status == "missed":
        score += 20
        reasons.append(f"Missed promise to pay on {promise_date} (+20)")
    elif promise_status in ("upcoming", "partially_fulfilled"):
        score -= 30
        reasons.append(f"Customer promised to pay by {promise_date} (-30)")
    if days_since_reminder is None:
        score += 5
        reasons.append("No reminder sent yet (+5)")
    elif days_since_reminder < cooldown_days:
        score -= 15
        reasons.append(f"Reminded {days_since_reminder} day(s) ago (-15)")
    if on_time_paid_count >= 3:
        score -= 5
        reasons.append(f"{on_time_paid_count} earlier invoices were paid on time (-5)")
    score = max(0, min(100, score))
    label = "High" if score >= 60 else "Medium" if score >= 35 else "Low"
    return score, label, reasons


def _on_time_count(conn, business_id: int, customer_id) -> int:
    if customer_id is None:
        return 0
    r = conn.execute(
        "SELECT COUNT(*) FROM invoice i WHERE i.business_id=? AND i.customer_id=? AND i.status='paid'"
        " AND (SELECT MAX(py.paid_on) FROM payment_allocation a JOIN payment py ON py.id=a.payment_id"
        "      WHERE a.invoice_id=i.id) <= i.due_date", (business_id, customer_id)).fetchone()
    return int(r[0])


# --------------------------------------------------------------------------
# Read services (receivables, ledger, stock, sales)
# --------------------------------------------------------------------------


def list_overdue_invoices(ctx: Ctx) -> list[dict]:
    require(ctx, "read")
    with read() as conn:
        st = _settings(conn, ctx.business_id)
        today = _today(st)
        cooldown = int(st.get("reminder_cooldown_days") or 3)
        promises: dict = {}
        for p in _promises_with_status(conn, ctx.business_id, today):
            if p["live_status"] != "cancelled":
                promises.setdefault(p["customer_id"], []).append(p)
        out = []
        for inv in _open_invoices(conn, ctx.business_id):
            days_over = (today - date.fromisoformat(inv["due_date"])).days
            if days_over <= 0:
                continue
            cust_promises = promises.get(inv["customer_id"], [])
            pr = next((p for p in cust_promises if p["live_status"] == "missed"), None) or \
                 next((p for p in cust_promises if p["live_status"] in ("upcoming", "partially_fulfilled")), None)
            since = None
            if inv["last_reminder_at"]:
                since = (today - date.fromisoformat(inv["last_reminder_at"][:10])).days
            score, label, reasons = priority_score(
                days_overdue=days_over, outstanding_minor=inv["outstanding_minor"],
                promise_status=pr["live_status"] if pr else None,
                promise_date=pr["promised_date"] if pr else None,
                days_since_reminder=since, cooldown_days=cooldown,
                on_time_paid_count=_on_time_count(conn, ctx.business_id, inv["customer_id"]))
            if inv["disputed"]:
                reasons.append("Invoice is marked as disputed - review before reminding")
            inv.update(days_overdue=days_over, priority_score=score, priority=label,
                       why=reasons, promise_status=pr["live_status"] if pr else None)
            out.append(inv)
        return sorted(out, key=lambda r: r["priority_score"], reverse=True)


def get_receivables_summary(ctx: Ctx) -> dict:
    require(ctx, "read")
    with read() as conn:
        today = _today(_settings(conn, ctx.business_id))
        total = overdue = 0
        n_open = n_over = 0
        for inv in _open_invoices(conn, ctx.business_id):
            total += inv["outstanding_minor"]
            n_open += 1
            if date.fromisoformat(inv["due_date"]) < today:
                overdue += inv["outstanding_minor"]
                n_over += 1
        return {"total_outstanding_minor": total, "overdue_minor": overdue,
                "current_minor": total - overdue, "open_invoices": n_open, "overdue_invoices": n_over}


def get_customer_ledger(ctx: Ctx, customer_id: int) -> dict:
    require(ctx, "read")
    with read() as conn:
        cust = _get_owned(conn, "customer", customer_id, ctx.business_id)
        today = _today(_settings(conn, ctx.business_id))
        invoices = [dict(r) for r in conn.execute(
            _INVOICE_BASE + " AND i.customer_id=? ORDER BY i.issue_date DESC", (ctx.business_id, customer_id))]
        for i in invoices:
            i["paid_minor"] = i["total_minor"] - i["outstanding_minor"]
        payments = [dict(r) for r in conn.execute(
            "SELECT id, amount_minor, unallocated_minor, method, paid_on, reference, source FROM payment"
            " WHERE business_id=? AND customer_id=? ORDER BY paid_on DESC, id DESC",
            (ctx.business_id, customer_id))]
        reminders = [dict(r) for r in conn.execute(
            "SELECT r.id, r.invoice_id, r.channel, r.status, r.sent_at, r.created_at FROM payment_reminder r"
            " JOIN invoice i ON i.id=r.invoice_id WHERE r.business_id=? AND i.customer_id=?"
            " ORDER BY r.id DESC", (ctx.business_id, customer_id))]
        owed = sum(i["outstanding_minor"] for i in invoices)
        late = sum(1 for i in invoices if i["status"] == "paid" and False)  # factual counts live in D3
        return {"customer": cust, "outstanding_minor": owed,
                "credit_minor": sum(p["unallocated_minor"] or 0 for p in payments),
                "invoices": invoices, "payments": payments, "reminders": reminders,
                "promises": _promises_with_status(conn, ctx.business_id, today, customer_id)}


def list_customers(ctx: Ctx, q: str = "") -> list[dict]:
    require(ctx, "read")
    with read() as conn:
        rows = conn.execute(
            "SELECT id, name, phone FROM customer WHERE business_id=? AND name LIKE ? ORDER BY name LIMIT 100",
            (ctx.business_id, f"%{q.strip()}%"))
        return [dict(r) for r in rows]


def list_products(ctx: Ctx, q: str = "") -> list[dict]:
    require(ctx, "read")
    with read() as conn:
        like = f"%{q.strip()}%"
        rows = conn.execute(
            "SELECT id, name, sku, price_minor, cost_minor, min_stock, stock_on_hand FROM product"
            " WHERE business_id=? AND (name LIKE ? OR sku LIKE ?) ORDER BY name LIMIT 200",
            (ctx.business_id, like, like))
        return [dict(r) for r in rows]


def list_low_stock_products(ctx: Ctx) -> list[dict]:
    require(ctx, "read")
    with read() as conn:
        rows = conn.execute(
            "SELECT p.id, p.name, p.sku, p.stock_on_hand, p.min_stock, p.lead_time_days, s.name AS supplier_name"
            " FROM product p LEFT JOIN supplier s ON s.id = p.supplier_id"
            " WHERE p.business_id=? AND p.stock_on_hand < p.min_stock ORDER BY (p.min_stock - p.stock_on_hand) DESC",
            (ctx.business_id,))
        out = []
        for r in rows:
            d = dict(r)
            d["why"] = f"Available {d['stock_on_hand']} is below the minimum {d['min_stock']}"
            out.append(d)
        return out


def get_sales_totals(ctx: Ctx, start: str, end: str) -> dict:
    require(ctx, "read")
    _parse_date(start, "Start date")
    _parse_date(end, "End date")
    with read() as conn:
        r = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(total_minor),0) AS total FROM sale"
            " WHERE business_id=? AND sale_date BETWEEN ? AND ?", (ctx.business_id, start, end)).fetchone()
        return {"start": start, "end": end, "sales_count": r["n"], "billed_minor": r["total"]}


# --------------------------------------------------------------------------
# Quick sale (B4): atomic + idempotent
# --------------------------------------------------------------------------


def _record_sale_direct(conn, idem_key: str, customer_id, product_id: int, qty: int, paid_minor: int = 0) -> tuple:
    if not idem_key:
        raise ValueError("Missing idempotency key.")
    if qty <= 0:
        raise ValueError("Quantity must be at least 1.")
    if paid_minor < 0:
        raise ValueError("Paid amount cannot be negative.")
    prior = conn.execute("SELECT * FROM sale WHERE idem_key=?", (idem_key,)).fetchone()
    if prior:
        return dict(prior), True
    prod = conn.execute("SELECT * FROM product WHERE id=?", (product_id,)).fetchone()
    if not prod:
        raise ValueError(f"Product {product_id} not found.")
    if prod["stock"] < qty:
        raise ValueError(f"Only {prod['stock']} units of {prod['name']} in stock, requested {qty}.")
    total_minor = prod["price_minor"] * qty
    if customer_id is None and paid_minor < total_minor:
        raise ValueError("Walk-in customers must pay in full.")
    with conn:
        new_stock = prod["stock"] - qty
        conn.execute("UPDATE product SET stock=? WHERE id=?", (new_stock, product_id))
        conn.execute("INSERT INTO inventory_movement(product_id, delta, reason, ref, created_at) VALUES (?, ?, 'sale', ?, ?)",
                     (product_id, -qty, idem_key, db.now_iso()))
        inv_id = None
        if customer_id is not None and paid_minor < total_minor:
            count = conn.execute("SELECT COUNT(*) FROM invoice").fetchone()[0] + 1
            inv_no = f"INV-{count:04d}"
            today = date.today()
            due = (today + timedelta(days=15)).isoformat()
            cur = conn.execute("INSERT INTO invoice(number, customer_id, total_minor, issue_date, due_date) VALUES (?, ?, ?, ?, ?)",
                               (inv_no, customer_id, total_minor, today.isoformat(), due))
            inv_id = cur.lastrowid
            if paid_minor > 0:
                conn.execute("INSERT INTO payment(invoice_id, amount_minor, paid_on, method, idem_key) VALUES (?, ?, ?, 'cash', ?)",
                             (inv_id, paid_minor, today.isoformat(), f"{idem_key}-pay"))
        cur_sale = conn.execute("INSERT INTO sale(idem_key, customer_id, product_id, qty, total_minor, invoice_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                (idem_key, customer_id, product_id, qty, total_minor, inv_id, db.now_iso()))
        sale_id = cur_sale.lastrowid
        db.audit(conn, "sale_recorded", f"sale:{sale_id}", f"product:{prod['name']}, qty:{qty}, total:{total_minor}")
    return {"id": sale_id, "idem_key": idem_key, "customer_id": customer_id, "product_id": product_id, "qty": qty, "total_minor": total_minor, "invoice_id": inv_id}, False


def record_sale(*args, **kwargs):
    if args and not isinstance(args[0], Ctx):
        conn = args[0]
        idem_key = args[1] if len(args) > 1 else kwargs.get("idem_key")
        customer_id = args[2] if len(args) > 2 else kwargs.get("customer_id")
        product_id = args[3] if len(args) > 3 else kwargs.get("product_id")
        qty = args[4] if len(args) > 4 else kwargs.get("qty")
        paid_minor = args[5] if len(args) > 5 else kwargs.get("paid_minor", 0)
        return _record_sale_direct(conn, idem_key, customer_id, product_id, qty, paid_minor)
    return _record_sale_ctx(*args, **kwargs)


def _record_sale_ctx(ctx: Ctx, *, items: list[dict], idempotency_key: str, customer_id=None,
                    paid_minor: int = 0, method: str = "cash", due_date=None, notes: str = "") -> dict:
    require(ctx, "sale.create")
    if not idempotency_key:
        raise ServiceError("Missing idempotency key.", "validation")
    if not items:
        raise ServiceError("Add at least one product.", "validation")
    if paid_minor < 0:
        raise ServiceError("Amount received cannot be negative.", "validation")

    with tx() as conn:
        prior = _idem_get(conn, ctx, "sale", idempotency_key)
        if prior:
            prior["replayed"] = True
            return prior

        st = _settings(conn, ctx.business_id)
        today = _today(st)
        if customer_id is not None:
            _get_owned(conn, "customer", customer_id, ctx.business_id)

        wanted: dict[int, int] = {}
        for it in items:
            try:
                pid, qty = int(it["product_id"]), int(it["qty"])
            except (KeyError, TypeError, ValueError):
                raise ServiceError("Each line needs a product and a whole-number quantity.", "validation")
            if qty <= 0:
                raise ServiceError("Quantity must be at least 1.", "validation")
            wanted[pid] = wanted.get(pid, 0) + qty

        allow_neg = bool(st.get("allow_negative_stock")) and has_perm(ctx, "stock.negative")
        tax_bp = int(st.get("tax_bp") or 0)
        lines, subtotal, tax_total = [], 0, 0
        for pid, qty in wanted.items():
            p = _get_owned(conn, "product", pid, ctx.business_id)
            if p["stock_on_hand"] - qty < 0 and not allow_neg:
                raise ServiceError(
                    f"Not enough stock for {p['name']}: you have {p['stock_on_hand']}, need {qty}.",
                    "insufficient_stock", 409)
            line = p["price_minor"] * qty
            tax = _tax(line, tax_bp)
            subtotal += line
            tax_total += tax
            lines.append((p, qty, line, tax))

        total = subtotal + tax_total
        if paid_minor > total:
            raise ServiceError("Amount received is more than the bill. Record extra money as a payment.",
                               "overpayment")
        credit = total - paid_minor
        if credit > 0:
            if customer_id is None:
                raise ServiceError("Pick a customer for credit or part-payment sales.", "validation")
            if not due_date:
                raise ServiceError("Add a due date for the amount still owed.", "validation")
        due = _parse_date(due_date, "Due date").isoformat() if due_date else today.isoformat()

        sale_id = conn.execute(
            "INSERT INTO sale(business_id, customer_id, total_minor, tax_minor, sale_date, created_by, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (ctx.business_id, customer_id, total, tax_total, today.isoformat(), ctx.user_id, _now())).lastrowid

        for p, qty, line, tax in lines:
            conn.execute(
                "INSERT INTO sale_item(business_id, sale_id, product_id, qty, unit_price_minor, line_total_minor, tax_minor)"
                " VALUES (?,?,?,?,?,?,?)", (ctx.business_id, sale_id, p["id"], qty, p["price_minor"], line, tax))
            conn.execute(
                "INSERT INTO inventory_movement(business_id, product_id, delta, reason, ref_type, ref_id,"
                " actor_user_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (ctx.business_id, p["id"], -qty, "sale", "sale", sale_id, ctx.user_id, _now()))
            conn.execute("UPDATE product SET stock_on_hand = stock_on_hand - ? WHERE id=? AND business_id=?",
                         (qty, p["id"], ctx.business_id))

        n = int(st.get("next_invoice_no") or 1)
        invoice_no = f"INV-{n:04d}"
        conn.execute("UPDATE business_settings SET next_invoice_no=? WHERE business_id=?", (n + 1, ctx.business_id))
        invoice_id = conn.execute(
            "INSERT INTO invoice(business_id, invoice_no, sale_id, customer_id, total_minor, issue_date, due_date,"
            " status, disputed) VALUES (?,?,?,?,?,?,?,?,0)",
            (ctx.business_id, invoice_no, sale_id, customer_id, total, today.isoformat(), due, "unpaid")).lastrowid

        if paid_minor > 0:
            pay_id = conn.execute(
                "INSERT INTO payment(business_id, customer_id, amount_minor, unallocated_minor, method, paid_on,"
                " reference, source, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ctx.business_id, customer_id, paid_minor, 0, method, today.isoformat(),
                 f"sale:{sale_id}", "manual", ctx.user_id, _now())).lastrowid
            conn.execute("INSERT INTO payment_allocation(business_id, payment_id, invoice_id, amount_minor)"
                         " VALUES (?,?,?,?)", (ctx.business_id, pay_id, invoice_id, paid_minor))

        status = _invoice_status(total, credit)
        conn.execute("UPDATE invoice SET status=? WHERE id=? AND business_id=?", (status, invoice_id, ctx.business_id))

        # Verify post-state before committing; a mismatch rolls everything back.
        check = _invoice(conn, ctx.business_id, invoice_id)
        if check["outstanding_minor"] != credit:
            raise ServiceError("Sale verification failed; nothing was saved.", "verify_failed", 500)
        for p, qty, _, _ in lines:
            now_stock = conn.execute("SELECT stock_on_hand FROM product WHERE id=?", (p["id"],)).fetchone()[0]
            if now_stock != p["stock_on_hand"] - qty:
                raise ServiceError("Stock verification failed; nothing was saved.", "verify_failed", 500)

        result = {"sale_id": sale_id, "invoice_id": invoice_id, "invoice_no": invoice_no,
                  "total_minor": total, "paid_minor": paid_minor, "outstanding_minor": credit,
                  "status": status, "replayed": False}
        audit(conn, ctx, "sale.recorded", "sale", sale_id, after=result)
        _idem_put(conn, ctx, "sale", idempotency_key, result)
        return result


# --------------------------------------------------------------------------
# Payments (A4): allocation, credit, duplicate detection
# --------------------------------------------------------------------------


def _record_payment_direct(conn, invoice_id: int, amount_minor: int, idem_key: str, method: str = "cash") -> bool:
    if amount_minor <= 0:
        raise ValueError("Payment amount must be greater than zero.")
    if not idem_key:
        raise ValueError("Missing idempotency key.")
    prior = conn.execute("SELECT id FROM payment WHERE idem_key=?", (idem_key,)).fetchone()
    if prior:
        return True  # replay
    inv = conn.execute("SELECT * FROM invoice WHERE id=?", (invoice_id,)).fetchone()
    if not inv:
        raise ValueError(f"Invoice {invoice_id} not found.")
    paid = conn.execute("SELECT COALESCE(SUM(amount_minor), 0) FROM payment WHERE invoice_id=?", (invoice_id,)).fetchone()[0]
    outstanding = inv["total_minor"] - paid
    if amount_minor > outstanding:
        raise ValueError(f"Payment amount exceeds remaining balance.")
    with conn:
        conn.execute("INSERT INTO payment(invoice_id, amount_minor, paid_on, method, idem_key) VALUES (?, ?, ?, ?, ?)",
                     (invoice_id, amount_minor, date.today().isoformat(), method, idem_key))
        db.audit(conn, "payment_recorded", f"invoice:{inv['number']}", f"amount: {amount_minor}, method: {method}")
    return False


def record_payment(*args, **kwargs):
    if args and not isinstance(args[0], Ctx):
        conn = args[0]
        invoice_id = args[1] if len(args) > 1 else kwargs.get("invoice_id")
        amount_minor = args[2] if len(args) > 2 else kwargs.get("amount_minor")
        idem_key = args[3] if len(args) > 3 else kwargs.get("idem_key")
        method = args[4] if len(args) > 4 else kwargs.get("method", "cash")
        return _record_payment_direct(conn, invoice_id, amount_minor, idem_key, method)
    return _record_payment_ctx(*args, **kwargs)


def _record_payment_ctx(ctx: Ctx, *, customer_id: int, amount_minor: int, idempotency_key: str,
                       method: str = "cash", paid_on=None, reference: str = "", allocations=None,
                       confirm_duplicate: bool = False) -> dict:
    require(ctx, "payment.record")
    if not idempotency_key:
        raise ServiceError("Missing idempotency key.", "validation")
    if amount_minor <= 0:
        raise ServiceError("Payment amount must be more than zero.", "validation")

    with tx() as conn:
        prior = _idem_get(conn, ctx, "payment", idempotency_key)
        if prior:
            prior["replayed"] = True
            return prior

        st = _settings(conn, ctx.business_id)
        today = _today(st)
        paid_date = _parse_date(paid_on, "Payment date") if paid_on else today
        _get_owned(conn, "customer", customer_id, ctx.business_id)
        reference = (reference or "").strip()[:100]

        dup = conn.execute(
            "SELECT id FROM payment WHERE business_id=? AND customer_id=? AND amount_minor=? AND paid_on=?"
            " AND COALESCE(reference,'')=?",
            (ctx.business_id, customer_id, amount_minor, paid_date.isoformat(), reference)).fetchone()
        if dup and not confirm_duplicate:
            raise ServiceError("A payment with the same customer, amount, date and reference already exists. "
                               "Confirm if this is really a second payment.", "possible_duplicate", 409)

        open_inv = {i["id"]: i for i in _open_invoices(conn, ctx.business_id, customer_id)}
        plan: list[tuple[int, int]] = []
        if allocations is None:  # oldest-first suggestion
            left = amount_minor
            for inv in open_inv.values():
                if left <= 0:
                    break
                take = min(left, inv["outstanding_minor"])
                plan.append((inv["id"], take))
                left -= take
        else:
            for a in allocations:
                inv = open_inv.get(int(a["invoice_id"]))
                amt = int(a["amount_minor"])
                if inv is None:
                    raise ServiceError("One of the invoices is not open for this customer.", "validation")
                if amt <= 0 or amt > inv["outstanding_minor"]:
                    raise ServiceError(f"Invoice {inv['invoice_no']} only has "
                                       f"{fmt_inr(inv['outstanding_minor'])} left to pay.", "validation")
                plan.append((inv["id"], amt))
        allocated = sum(a for _, a in plan)
        if allocated > amount_minor:
            raise ServiceError("Allocated more than the payment amount.", "validation")
        unallocated = amount_minor - allocated  # kept as explicit customer credit

        pay_id = conn.execute(
            "INSERT INTO payment(business_id, customer_id, amount_minor, unallocated_minor, method, paid_on,"
            " reference, source, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ctx.business_id, customer_id, amount_minor, unallocated, method, paid_date.isoformat(),
             reference, "manual", ctx.user_id, _now())).lastrowid

        touched = []
        for inv_id, amt in plan:
            conn.execute("INSERT INTO payment_allocation(business_id, payment_id, invoice_id, amount_minor)"
                         " VALUES (?,?,?,?)", (ctx.business_id, pay_id, inv_id, amt))
            inv = _invoice(conn, ctx.business_id, inv_id)
            new_status = _invoice_status(inv["total_minor"], inv["outstanding_minor"])
            conn.execute("UPDATE invoice SET status=? WHERE id=? AND business_id=?",
                         (new_status, inv_id, ctx.business_id))
            touched.append({"invoice_id": inv_id, "invoice_no": inv["invoice_no"],
                            "outstanding_minor": inv["outstanding_minor"], "status": new_status})

        _persist_promise_statuses(conn, ctx.business_id, today, customer_id)
        result = {"payment_id": pay_id, "amount_minor": amount_minor, "allocated_minor": allocated,
                  "credit_minor": unallocated, "invoices": touched, "source": "Manually recorded",
                  "replayed": False}
        audit(conn, ctx, "payment.recorded", "payment", pay_id, after=result)
        _idem_put(conn, ctx, "payment", idempotency_key, result)
        return result


# --------------------------------------------------------------------------
# Reminders (A1): drafts only here. Sending is gated by the Approval Center.
# --------------------------------------------------------------------------

_BANNED_WORDS = ("legal action", "police", "court", "shame", "defaulter", "fraud", "threat")


def build_reminder_text(business_name: str, inv: dict, instructions: str) -> str:
    how = instructions.strip() or "Please reply to this message and we will share payment details."
    return (
        f"Hello {inv['customer_name'] or 'there'}, this is a friendly reminder from {business_name}. "
        f"Invoice {inv['invoice_no']} for {fmt_inr(inv['total_minor'])} was due on {inv['due_date']}. "
        f"The balance still to pay is {fmt_inr(inv['outstanding_minor'])}. {how} "
        f"If you have already paid, please ignore this message or let us know. Thank you!"
    )


def check_reminder_fidelity(message: str, inv: dict) -> None:
    """Reject text that drops or contradicts the real records."""
    if inv["invoice_no"] not in message:
        raise ServiceError(f"The message must mention invoice {inv['invoice_no']}.", "fidelity")
    if fmt_inr(inv["outstanding_minor"]) not in message:
        raise ServiceError(f"The message must state the balance {fmt_inr(inv['outstanding_minor'])}.", "fidelity")
    low = message.lower()
    for w in _BANNED_WORDS:
        if w in low:
            raise ServiceError("Please keep the reminder polite and factual (no threats or pressure).", "tone")


def _reminder_warnings(conn, ctx: Ctx, inv: dict, st: dict, today: date) -> list[str]:
    warns = []
    if inv["disputed"]:
        warns.append("This invoice is marked as disputed.")
    for p in _promises_with_status(conn, ctx.business_id, today, inv["customer_id"]):
        if p["live_status"] in ("upcoming", "partially_fulfilled") and \
                date.fromisoformat(p["follow_up_date"] or p["promised_date"]) > today:
            warns.append(f"Customer promised to pay by {p['promised_date']}; follow-up is set for "
                         f"{p['follow_up_date'] or p['promised_date']}.")
    cooldown = int(st.get("reminder_cooldown_days") or 3)
    if inv["last_reminder_at"]:
        since = (today - date.fromisoformat(inv["last_reminder_at"][:10])).days
        if since < cooldown:
            warns.append(f"A reminder was already sent {since} day(s) ago (cooldown is {cooldown} days).")
    return warns


def draft_payment_reminder(ctx: Ctx, invoice_id: int, channel: str = "whatsapp") -> dict:
    require(ctx, "reminder.draft")
    with tx() as conn:
        st = _settings(conn, ctx.business_id)
        inv = _invoice(conn, ctx.business_id, invoice_id)
        if inv["outstanding_minor"] <= 0:
            raise ServiceError("This invoice is already fully paid.", "validation")
        biz = conn.execute("SELECT name FROM business WHERE id=?", (ctx.business_id,)).fetchone()
        message = build_reminder_text(biz["name"], inv, st.get("payment_instructions") or "")
        check_reminder_fidelity(message, inv)
        rid = conn.execute(
            "INSERT INTO payment_reminder(business_id, invoice_id, channel, message, status, provider,"
            " created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (ctx.business_id, invoice_id, channel, message, "draft", None, ctx.user_id, _now())).lastrowid
        warnings = _reminder_warnings(conn, ctx, inv, st, _today(st))
        audit(conn, ctx, "reminder.drafted", "payment_reminder", rid, after={"invoice_id": invoice_id})
        return {"reminder_id": rid, "invoice_no": inv["invoice_no"], "customer": inv["customer_name"],
                "phone": inv["customer_phone"], "channel": channel, "message": message,
                "status": "draft", "warnings": warnings}


def update_reminder_message(ctx: Ctx, reminder_id: int, message: str) -> dict:
    require(ctx, "reminder.draft")
    message = (message or "").strip()
    if not message or len(message) > 1000:
        raise ServiceError("Message must be 1 to 1000 characters.", "validation")
    with tx() as conn:
        r = conn.execute("SELECT * FROM payment_reminder WHERE id=? AND business_id=?",
                         (reminder_id, ctx.business_id)).fetchone()
        if r is None:
            raise NotFound("Reminder not found.")
        if r["status"] not in ("draft", "pending_approval"):
            raise ServiceError("Only drafts can be edited.", "bad_state", 409)
        inv = _invoice(conn, ctx.business_id, r["invoice_id"])
        check_reminder_fidelity(message, inv)
        conn.execute("UPDATE payment_reminder SET message=? WHERE id=? AND business_id=?",
                     (message, reminder_id, ctx.business_id))
        audit(conn, ctx, "reminder.edited", "payment_reminder", reminder_id)
        return {"reminder_id": reminder_id, "message": message}


def mark_reminder_simulated_sent(ctx: Ctx, reminder_id: int) -> dict:
    """Called by actions.py AFTER human approval. With no messaging provider this is the
    only 'sent' state allowed, and it is labelled SIMULATED. Never sets sent/delivered."""
    require(ctx, "reminder.send")
    with tx() as conn:
        r = conn.execute("SELECT * FROM payment_reminder WHERE id=? AND business_id=?",
                         (reminder_id, ctx.business_id)).fetchone()
        if r is None:
            raise NotFound("Reminder not found.")
        if r["status"] not in ("draft", "pending_approval", "approved"):
            raise ServiceError("This reminder was already processed.", "bad_state", 409)
        conn.execute("UPDATE payment_reminder SET status='simulated_sent', provider='simulated', sent_at=?"
                     " WHERE id=? AND business_id=?", (_now(), reminder_id, ctx.business_id))
        audit(conn, ctx, "reminder.simulated_sent", "payment_reminder", reminder_id,
              before={"status": r["status"]}, after={"status": "simulated_sent"})
        return {"reminder_id": reminder_id, "status": "simulated_sent",
                "label": "SIMULATED - not actually delivered"}


# --------------------------------------------------------------------------
# Stock adjustment (B1)
# --------------------------------------------------------------------------


def adjust_stock(ctx: Ctx, product_id: int, delta: int, reason: str, approved: bool = False) -> dict:
    """Large adjustments need approval: actions.py calls this with approved=True after approval."""
    require(ctx, "stock.adjust")
    if not delta:
        raise ServiceError("Adjustment cannot be zero.", "validation")
    if not (reason or "").strip():
        raise ServiceError("Please give a reason for the adjustment.", "validation")
    with tx() as conn:
        st = _settings(conn, ctx.business_id)
        p = _get_owned(conn, "product", product_id, ctx.business_id)
        threshold = int(st.get("adjustment_approval_threshold") or 20)
        if abs(delta) > threshold and not approved:
            raise ServiceError(f"Adjustments above {threshold} units need approval in the Approval Center.",
                               "approval_required", 409)
        new_stock = p["stock_on_hand"] + delta
        if new_stock < 0 and not (st.get("allow_negative_stock") and has_perm(ctx, "stock.negative")):
            raise ServiceError(f"That would take {p['name']} below zero (now {p['stock_on_hand']}).",
                               "insufficient_stock", 409)
        conn.execute(
            "INSERT INTO inventory_movement(business_id, product_id, delta, reason, ref_type, ref_id,"
            " actor_user_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (ctx.business_id, product_id, delta, "adjustment", "adjustment", None, ctx.user_id, _now()))
        conn.execute("UPDATE product SET stock_on_hand=? WHERE id=? AND business_id=?",
                     (new_stock, product_id, ctx.business_id))
        audit(conn, ctx, "stock.adjusted", "product", product_id,
              before={"stock": p["stock_on_hand"]}, after={"stock": new_stock, "reason": reason[:200]})
        return {"product_id": product_id, "name": p["name"], "stock_on_hand": new_stock}


# --------------------------------------------------------------------------
# Daily briefing (C3): items and numbers from code only, computed when opened
# --------------------------------------------------------------------------


def get_briefing(ctx: Ctx) -> dict:
    require(ctx, "read")
    overdue = list_overdue_invoices(ctx)
    low = list_low_stock_products(ctx)
    items = []
    for inv in overdue[:5]:
        items.append({"kind": "overdue", "title": f"{inv['customer_name']} owes {fmt_inr(inv['outstanding_minor'])}",
                      "why": f"Invoice {inv['invoice_no']} is {inv['days_overdue']} days overdue. Priority: {inv['priority']}.",
                      "reasons": inv["why"], "link": f"/money#invoice-{inv['id']}", "action": "Draft reminder"})
    with read() as conn:
        st = _settings(conn, ctx.business_id)
        today = _today(st)
        for p in _promises_with_status(conn, ctx.business_id, today):
            if p["live_status"] == "missed":
                items.append({"kind": "missed_promise", "title": "A promise to pay was missed",
                              "why": f"Promised {fmt_inr(p['promised_amount_minor'])} by {p['promised_date']}; "
                                     f"received {fmt_inr(p['received_minor'])}.",
                              "link": f"/money#customer-{p['customer_id']}", "action": "Follow up"})
        soon = (today + timedelta(days=7)).isoformat()
        for b in conn.execute(
                "SELECT amount_minor - paid_minor AS due_minor, due_date FROM supplier_bill"
                " WHERE business_id=? AND status!='paid' AND due_date <= ? ORDER BY due_date",
                (ctx.business_id, soon)):
            items.append({"kind": "supplier_bill", "title": f"Supplier bill of {fmt_inr(b['due_minor'])} due {b['due_date']}",
                          "why": "Unpaid supplier bill due within 7 days.", "link": "/money", "action": "Review"})
        pend = conn.execute("SELECT COUNT(*) FROM pending_action WHERE business_id=? AND status='proposed'",
                            (ctx.business_id,)).fetchone()[0]
    for p in low[:5]:
        items.append({"kind": "low_stock", "title": f"{p['name']} is running low", "why": p["why"],
                      "link": "/inventory", "action": "Review stock"})
    if pend:
        items.append({"kind": "approvals", "title": f"{pend} action(s) waiting for your approval",
                      "why": "Nothing happens until you approve.", "link": "/approvals", "action": "Open approvals"})
    return {"generated_at": _now(), "items": items,
            "empty_message": "Nothing urgent today." if not items else None,
            "note": "Computed when you opened this page from your current records."}