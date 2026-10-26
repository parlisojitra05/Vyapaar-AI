from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from config import REMINDER_COOLDOWN_DAYS


def to_minor(text):
    """'1500.505' -> paise, half-up. Raises ValueError with a plain message."""
    try:
        v = Decimal(str(text).replace(",", "").strip())
    except Exception:
        raise ValueError("Enter a valid amount, like 1500 or 1500.50")
    if not v.is_finite() or v < 0:
        raise ValueError("Enter a valid amount, like 1500 or 1500.50")
    return int((v * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def inr(minor):
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
    return f"{sign}₹{s}.{paise:02d}"


def priority(days_overdue, outstanding_minor, days_since_reminder,
             cooldown=REMINDER_COOLDOWN_DAYS):
    """Transparent follow-up priority. NOT a prediction of who will pay."""
    if outstanding_minor <= 0:
        return 0, ["Fully paid"]
    score, factors = 0, []
    if days_overdue > 0:
        pts = min(days_overdue, 60)
        score += pts
        factors.append(f"{days_overdue} days overdue (+{pts})")
    else:
        factors.append("Not yet due")
    amt = min(outstanding_minor // 100000, 30)  # +1 per Rs 1,000 pending, max 30
    if amt:
        score += amt
        factors.append(f"{inr(outstanding_minor)} still pending (+{amt})")
    if days_since_reminder is not None and days_since_reminder < cooldown:
        score -= 20
        factors.append(f"Reminder sent {days_since_reminder} day(s) ago (-20)")
    return score, factors


def invoice_rows(conn, today=None):
    """THE single place where outstanding balance is computed."""
    today = today or date.today()
    q = """SELECT i.id, i.number, i.customer_id, c.name AS customer, i.total_minor,
                  i.issue_date, i.due_date,
                  COALESCE((SELECT SUM(amount_minor) FROM payment p
                            WHERE p.invoice_id = i.id), 0) AS paid_minor,
                  (SELECT MAX(created_at) FROM reminder r WHERE r.invoice_id = i.id
                            AND r.status IN ('simulated_sent','sent','delivered')) AS last_reminder
           FROM invoice i JOIN customer c ON c.id = i.customer_id ORDER BY i.due_date"""
    rows = []
    for r in conn.execute(q):
        d = dict(r)
        d["outstanding_minor"] = d["total_minor"] - d["paid_minor"]
        out = d["outstanding_minor"]
        d["days_overdue"] = (max(0, (today - date.fromisoformat(d["due_date"])).days)
                             if out > 0 else 0)
        d["status"] = "paid" if out <= 0 else ("partially_paid" if d["paid_minor"] > 0 else "unpaid")
        since = (today - date.fromisoformat(d["last_reminder"][:10])).days if d["last_reminder"] else None
        d["days_since_reminder"] = since
        d["score"], d["factors"] = priority(d["days_overdue"], out, since)
        rows.append(d)
    return rows


def summary(rows):
    open_rows = [r for r in rows if r["outstanding_minor"] > 0]
    outstanding = sum(r["outstanding_minor"] for r in open_rows)
    overdue = sum(r["outstanding_minor"] for r in open_rows if r["days_overdue"] > 0)
    return {"outstanding": outstanding, "overdue": overdue,
            "current": outstanding - overdue, "open_count": len(open_rows)}