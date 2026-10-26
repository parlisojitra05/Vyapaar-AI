from datetime import date

from domain import inr, invoice_rows, summary


def receivables_summary(conn):
    return summary(invoice_rows(conn))


def overdue_invoices(conn):
    rows = [r for r in invoice_rows(conn) if r["days_overdue"] > 0 and r["outstanding_minor"] > 0]
    return sorted(rows, key=lambda r: r["score"], reverse=True)


def top_debtors(conn):
    owed = {}
    for r in invoice_rows(conn):
        if r["outstanding_minor"] > 0:
            owed[r["customer"]] = owed.get(r["customer"], 0) + r["outstanding_minor"]
    return [{"name": k, "owed": v} for k, v in sorted(owed.items(), key=lambda kv: -kv[1])]


def low_stock(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM product WHERE stock < min_stock ORDER BY (min_stock - stock) DESC")]


def find_products(conn, text):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM product WHERE name LIKE ? OR sku LIKE ?", (f"%{text}%", f"%{text}%"))]


def find_customers(conn, text):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM customer WHERE name LIKE ?", (f"%{text}%",))]


def sales_this_month(conn):
    first = date.today().replace(day=1).isoformat()
    r = conn.execute("SELECT COALESCE(SUM(total_minor),0) t, COUNT(*) n FROM sale WHERE created_at >= ?",
                     (first,)).fetchone()
    return {"total": r["t"], "count": r["n"]}


def pending_count(conn):
    return conn.execute("SELECT COUNT(*) FROM pending_action WHERE status='proposed'").fetchone()[0]


def briefing(conn):
    """Computed on open from records. No background monitoring."""
    items = []
    for r in overdue_invoices(conn):
        items.append({"what": f"{r['customer']} owes {inr(r['outstanding_minor'])} on {r['number']} "
                              f"({r['days_overdue']} days overdue)",
                      "why": "Priority factors: " + "; ".join(r["factors"]), "link": "/money"})
    for p in low_stock(conn):
        items.append({"what": f"{p['name']}: {p['stock']} left, minimum {p['min_stock']}",
                      "why": "Stock is below the minimum you set.", "link": "/inventory"})
    n = pending_count(conn)
    if n:
        items.append({"what": f"{n} action(s) waiting for your approval",
                      "why": "Nothing happens until you approve.", "link": "/approvals"})
    return items